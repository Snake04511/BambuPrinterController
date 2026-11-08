"""Panel API v1 for the ESP32 panel (/api/v1/..., X-API-Key).

The panel was written against an older REST API; these routes answer in the shapes it reads,
from this app's own state and cache (SD files, plates, cover, pick image, objects, inventory,
print log, maintenance, Obico, notifications). Commands go through Engine.command, so the
same rules apply as in the web app.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from . import settings

log = logging.getLogger("compat")
router = APIRouter()
_engine = None


def init(engine) -> None:
    global _engine
    _engine = engine


def _auth(key: str | None) -> None:
    if not settings.api_key_ok(key):
        raise HTTPException(401, "Invalid API key")


def _sd_name(path: str) -> str:
    return (path or "").lstrip("/")


def _sd_file(name: str) -> dict | None:
    return next((f for f in _engine.sd.files if f["name"] == name), None)


# ---------------------------------------------------------------- SD card (from this app's cache)


@router.get("/api/v1/printers/{pid}/files")
async def files(pid: int, path: str = "/", x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    if path not in ("/", ""):
        raise HTTPException(404, "Only the SD card's root folder is listed")
    sd = _engine.sd
    if not sd.listed:
        raise HTTPException(503, sd.error or "SD card not read yet")
    out = []
    for f in sd.files:
        d = (f.get("date") or "").replace(" ", "T")
        out.append({"name": f["name"], "is_directory": False, "size": f["size"], "path": "/" + f["name"],
                    "mtime": d + (":00" if len(d) == 16 else "")})
    return {"path": "/", "files": out, "warnings": []}


@router.delete("/api/v1/printers/{pid}/files")
async def delete_file(pid: int, path: str, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    r = await _engine.sd.delete(_sd_name(path))
    if not r.get("ok"):
        raise HTTPException(502, r.get("message") or "Delete failed")
    return {"ok": True}


# The panel reads whole 3MF files for its previews: not here. A 404 makes it use the
# plates routes below (served from this app's cache, no FTP).
@router.post("/api/v1/printers/{pid}/files/download-job")
@router.get("/api/v1/printers/{pid}/files/download")
async def no_download(pid: int, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    raise HTTPException(404, "Not available here: use files/plates")


@router.get("/api/v1/printers/{pid}/files/plates")
async def plates(pid: int, path: str, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    name = _sd_name(path)
    f = _sd_file(name)
    if not f:
        raise HTTPException(404, "File not on the SD card")
    res = _engine.sd.detail(name, f["size"])
    if res["state"] != "ready":
        _engine.sd.request(name, f["size"])  # read it now; the panel asks again later
        raise HTTPException(503, res.get("error") or "Details are being read, try again shortly")
    out = []
    for p in res["detail"]["plates"]:
        out.append({
            "index": p["index"],
            "name": p.get("name") or "",
            "print_time_seconds": p.get("print_sec", -1) if p.get("print_sec", -1) >= 0 else None,
            "filament_used_grams": p.get("grams") if p.get("grams", -1) >= 0 else None,
            "has_thumbnail": bool(p.get("has_thumb")),
            "filaments": [{"slot_id": x["slot"], "type": x["type"], "color": x["color"], "used_grams": x["grams"],
                           "used_meters": x.get("meters", 0)} for x in p.get("filaments") or []],
        })
    return {"plates": out}


@router.get("/api/v1/printers/{pid}/files/plate-thumbnail/{plate}")
async def plate_thumb(pid: int, plate: int, path: str, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    name = _sd_name(path)
    f = _sd_file(name)
    p = _engine.sd.thumb(name, f["size"], plate) if f else None
    if not p:
        raise HTTPException(404, "No preview")
    return Response(p.read_bytes(), media_type="image/png")


# ---------------------------------------------------------------- running print


@router.get("/api/v1/printers/{pid}/cover")
async def cover(pid: int, view: str = "", x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    if view == "pick":
        png = _pick_png()
        if not png:
            raise HTTPException(404, "No pick image")
        return Response(png, media_type="image/png")
    if not _engine.cover:
        raise HTTPException(404, "No cover available")
    return Response(_engine.cover, media_type="image/png")


def _pick_png() -> bytes | None:
    """The slicer's pick image of the running plate, from this app's SD copy of the file."""
    job = (_engine.status or {}).get("job", "")
    jf = _engine.sd.job_file(job) if job else None
    if not jf or not jf[2]:
        return None
    _, key, detail = jf
    plate = _engine.sd.job_plate(detail, [o["id"] for o in _engine.job.objects["items"]])
    return _engine.sd.png(key, f"pick{plate['index']}") if plate and plate.get("has_pick") else None


@router.get("/api/v1/printers/{pid}/print/objects")
async def objects(pid: int, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    v = _engine.job.objects_view()
    if not v["loaded"]:
        _engine.job.fetch_objects_bg()
        raise HTTPException(503, v["error"] or "Objects are being read, try again shortly")
    return {"objects": v["items"], "total": len(v["items"]), "skipped_count": sum(1 for o in v["items"] if o["skipped"]),
            "is_printing": True, "bbox_all": v["bbox"]}


# ---------------------------------------------------------------- printer, status, commands
# The fields the panel firmware reads.


def _hex(c: str, alpha: str = "FF") -> str:
    c = (c or "").lstrip("#").upper()
    return c[:6] + alpha if len(c) >= 6 else ""


def _tray_raw(t: dict | None, tid: int) -> dict:
    if not t:
        return {"id": tid, "tray_type": "", "tray_color": "", "remain": -1}
    return {
        "id": t.get("id", tid), "tray_type": t.get("type", ""), "tray_sub_brands": t.get("sub_brands", ""),
        "tray_info_idx": t.get("info_idx", ""), "tray_color": _hex(t.get("color", "")), "remain": t.get("remain", -1),
        "state": t.get("state", -1), "nozzle_temp_min": t.get("temp_min", 0), "nozzle_temp_max": t.get("temp_max", 0),
        "k": t.get("k", 0.0), "tag_uid": "1" * 16 if t.get("rfid") else "0" * 16,
    }


def _dbm(v) -> int:
    """'-22' / '-22dBm' -> -22 (the panel reads a number; 0 = unknown)."""
    import re as _re

    m = _re.search(r"-?\d+", str(v or ""))
    return int(m.group()) if m else 0


def _status_raw(st: dict) -> dict:
    temps = {"nozzle": st.get("nozzle"), "nozzle_target": st.get("nozzle_target"), "bed": st.get("bed"),
             "bed_target": st.get("bed_target")}
    if st.get("has_chamber"):
        temps.update(chamber=st.get("chamber"), chamber_target=st.get("chamber_target"))
    return {
        "connected": st.get("connected", False), "state": st.get("state", ""), "subtask_name": st.get("job", ""),
        "current_print": st.get("job", ""), "progress": st.get("progress", 0), "remaining_time": st.get("remaining_min", -1),
        "layer_num": st.get("layer", -1), "total_layers": st.get("total_layers", -1), "temperatures": temps,
        "supports_chamber_heater": st.get("chamber_heater", False), "chamber_light": st.get("light", False),
        "timelapse": st.get("timelapse", False), "speed_level": st.get("speed_level", 2),
        "cooling_fan_speed": st.get("fan_part", -1), "big_fan1_speed": st.get("fan_aux", -1),
        "big_fan2_speed": st.get("fan_chamber", -1), "wifi_signal": _dbm(st.get("wifi_signal")),
        "door_open": st.get("door_open", False), "tray_now": st.get("tray_now", 255),
        # a multi-plate run waits for the next plate: the panel's PLATE CLEAR button starts it
        "awaiting_plate_clear": _plate_waiting(),
        "ams": [{"id": u["id"], "humidity": u.get("humidity", -1), "temp": u.get("temp"), "is_ams_ht": u.get("is_ht", False),
                 "tray": [_tray_raw(t, i) for i, t in enumerate(u.get("trays") or []) if t is not None]}
                for u in st.get("ams") or []],
        "vt_tray": [_tray_raw(t, 254 + i) for i, t in enumerate(st.get("ext") or [])],
        "hms_errors": [{"severity": h.get("severity", 3), "description": h.get("text", ""), "full_code": ""} for h in st.get("hms") or []],
    }


def _plate_waiting() -> bool:
    q = _engine.plate_seq_view()
    return bool(q and q["state"] in ("waiting", "paused") and q["next"])


def _printer_raw() -> dict:
    p = _engine.printer or {}
    cfg = settings.get()
    return {"id": p.get("id", 1), "name": p.get("name") or cfg.get("printer_name") or "P1S", "model": p.get("model") or "P1S",
            "serial_number": p.get("serial") or cfg.get("printer_serial", ""), "ip_address": p.get("ip") or cfg.get("printer_ip", "")}


@router.get("/api/v1/auth/me")
async def auth_me(request: Request, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return {"username": "printerdash", "is_admin": True, "permissions": []}


@router.get("/api/v1/printers/")
async def printers(x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return [_printer_raw()]


@router.get("/api/v1/printers/{pid}")
async def printer(pid: int, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return _printer_raw()


@router.get("/api/v1/printers/{pid}/status")
async def status(pid: int, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    st = _engine.status
    if not st:
        raise HTTPException(503, "No printer status yet")
    return _status_raw(st)


async def _cmd(name: str, args: dict) -> JSONResponse:
    r = await _engine.command(name, args)
    if r.get("ok"):
        return JSONResponse({"success": True, "message": r.get("message", "")})
    return JSONResponse({"detail": r.get("message") or "Failed"}, status_code=400)


def _q(request: Request, key: str, default=None):
    return request.query_params.get(key, default)


@router.post("/api/v1/printers/{pid}/temperature/{which}")
async def c_temp(pid: int, which: str, request: Request, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return await _cmd("set_temp", {"which": which, "target": int(float(_q(request, "target", 0)))})


@router.post("/api/v1/printers/{pid}/chamber-light")
async def c_light(pid: int, request: Request, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return await _cmd("light", {"on": str(_q(request, "on", "false")).lower() == "true"})


@router.post("/api/v1/printers/{pid}/print/{action}")
async def c_print(pid: int, action: str, request: Request, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    if action == "skip-objects":
        try:
            body = await request.json()
        except ValueError as e:
            raise HTTPException(400, "Bad body") from e
        ids = body if isinstance(body, list) else body.get("object_ids") or body.get("objects") or [] if isinstance(body, dict) else []
        r = await _engine.job.skip([int(i) for i in ids])
        return JSONResponse({"success": r["ok"], "message": r["message"]}, status_code=200 if r["ok"] else 400)
    if action not in ("pause", "resume", "stop"):
        raise HTTPException(404, "Unknown print action")
    return await _cmd(action, {})


@router.post("/api/v1/printers/{pid}/print-speed")
async def c_speed(pid: int, request: Request, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return await _cmd("speed", {"mode": int(_q(request, "mode", 2))})


@router.post("/api/v1/printers/{pid}/fan-speed")
async def c_fan(pid: int, request: Request, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return await _cmd("fan", {"fan": _q(request, "fan", "part"), "speed": int(_q(request, "speed", 0))})


@router.post("/api/v1/printers/{pid}/home-axes")
async def c_home(pid: int, request: Request, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return await _cmd("home", {"axes": _q(request, "axes", "all")})


@router.post("/api/v1/printers/{pid}/xy-jog")
async def c_jog_xy(pid: int, request: Request, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return await _cmd("jog_xy", {"x": float(_q(request, "x", 0)), "y": float(_q(request, "y", 0))})


@router.post("/api/v1/printers/{pid}/bed-jog")
async def c_jog_z(pid: int, request: Request, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return await _cmd("jog_z", {"distance": float(_q(request, "distance", 0))})


@router.post("/api/v1/printers/{pid}/extruder-jog")
async def c_extrude(pid: int, request: Request, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return await _cmd("extrude", {"distance": float(_q(request, "distance", 0))})


@router.post("/api/v1/printers/{pid}/hms/clear")
async def c_hms(pid: int, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return await _cmd("clear_hms", {})


@router.post("/api/v1/printers/{pid}/refresh-status")
async def c_refresh(pid: int, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return await _cmd("refresh", {})


@router.post("/api/v1/printers/{pid}/clear-plate")
async def c_clear_plate(pid: int, x_api_key: str | None = Header(default=None)):
    """Plate cleared: in a multi-plate run this starts the next plate (the user's button)."""
    _auth(x_api_key)
    if not _plate_waiting():
        return {"success": True, "message": "Nothing waits for the plate"}
    r = await _engine.plate_seq_command("next")
    if r.get("ok"):
        return {"success": True, "message": r.get("message", "")}
    return JSONResponse({"detail": r.get("message") or "Could not start the next plate"}, status_code=400)


@router.post("/api/v1/printers/{pid}/ams/load")
async def c_load(pid: int, request: Request, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return await _cmd("ams_load", {"tray": int(_q(request, "tray_id", -1))})


@router.post("/api/v1/printers/{pid}/ams/unload")
async def c_unload(pid: int, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return await _cmd("ams_unload", {})


@router.post("/api/v1/printers/{pid}/slots/{ams}/{tray}/configure")
async def c_slot(pid: int, ams: int, tray: int, request: Request, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    q = request.query_params
    return await _cmd("slot_configure", {
        "ams_id": ams, "tray_id": tray, "info_idx": q.get("tray_info_idx", "GFL99"), "type": q.get("tray_type", "PLA"),
        "sub_brands": q.get("tray_sub_brands", ""), "color": "#" + (q.get("tray_color", "FFFFFFFF")[:6]),
        "temp_min": int(q.get("nozzle_temp_min", 190)), "temp_max": int(q.get("nozzle_temp_max", 230)),
    })


@router.post("/api/v1/printers/{pid}/ams/{ams}/tray/{tray}/reset")
async def c_slot_reset(pid: int, ams: int, tray: int, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return await _cmd("slot_reset", {"ams_id": ams, "tray_id": tray})


@router.post("/api/v1/printers/{pid}/ams/{ams}/slot/{tray}/refresh")
async def c_slot_rfid(pid: int, ams: int, tray: int, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return await _cmd("slot_rfid", {"ams_id": ams, "tray_id": tray})


# ---------------------------------------------------------------- inventory (own inventory)


def _spool_raw(sp: dict) -> dict:
    # weight_used includes what the running print used so far: the panel shows live grams
    live = _engine.live_used().get(sp["id"], 0.0)
    return {"id": sp["id"], "material": sp["material"], "subtype": sp["subtype"], "brand": sp["brand"],
            "color_name": sp["color_name"], "rgba": _hex(sp["color"]), "label_weight": sp["label_weight"],
            "weight_used": round(sp["weight_used"] + live, 2), "nozzle_temp_min": sp.get("temp_min") or None,
            "nozzle_temp_max": sp.get("temp_max") or None}


@router.get("/api/v1/inventory/spools")
async def inv_spools(request: Request, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    from . import inventory

    return [_spool_raw(s) for s in inventory.list_spools()]


@router.get("/api/v1/inventory/assignments")
async def inv_assignments(request: Request, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    from . import inventory

    pid = _printer_raw()["id"]
    return [{"printer_id": pid, "ams_id": a["ams_id"], "tray_id": a["tray_id"], "spool_id": a["spool_id"],
             "spool": _spool_raw(a["spool"])} for a in inventory.assignments()]


@router.post("/api/v1/inventory/assignments")
async def inv_assign(request: Request, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    body = await request.json()
    return await _cmd("spool_assign", {"spool_id": body.get("spool_id"), "ams_id": body.get("ams_id"), "tray_id": body.get("tray_id")})


@router.delete("/api/v1/inventory/assignments/{pid}/{ams}/{tray}")
async def inv_unassign(pid: int, ams: int, tray: int, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return await _cmd("spool_unassign", {"ams_id": ams, "tray_id": tray})


@router.patch("/api/v1/inventory/spools/{sid}")
async def inv_patch(sid: int, request: Request, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    body = await request.json()
    args = {k: v for k, v in body.items() if k in ("material", "subtype", "brand", "color_name", "label_weight", "weight_used")}
    if body.get("rgba"):
        args["color"] = "#" + str(body["rgba"])[:6]
    return await _cmd("spool_patch", {"id": sid, **args})


# ---------------------------------------------------------------- maintenance, Obico, notifications


@router.get("/api/v1/maintenance/printers/{pid}")
async def maint(pid: int, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    from . import maintenance

    v, lst = maintenance.view(), maintenance.listing()
    state = {i["id"]: i for i in v["items"]}
    items = []
    for it in lst["items"]:
        s = state.get(it["id"], {})
        items.append({"id": it["id"], "maintenance_type_name": it["name"], "enabled": it.get("enabled", True),
                      "interval_type": it["type"], "is_due": bool(s.get("due")), "is_warning": bool(s.get("warning")),
                      "hours_until_due": it["until"] if it["type"] == "hours" else None,
                      "days_until_due": it["until"] if it["type"] == "days" else None})
    return {"due_count": v["due_count"], "warning_count": v["warning_count"], "maintenance_items": items,
            "total_print_hours": lst["hours"]}


@router.post("/api/v1/maintenance/items/{item_id}/perform")
async def maint_done(item_id: int, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return await _cmd("maintenance_done", {"id": item_id})


@router.get("/api/v1/obico/status")
async def obico(x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    o = _engine.obico or {}
    pid = str(_printer_raw()["id"])
    out = {"enabled": o.get("enabled", False), "is_running": o.get("running", False),
           "thresholds": {"low": o.get("low", 0), "high": o.get("high", 0)}, "sensitivity": o.get("sensitivity", ""),
           "action": o.get("action", ""), "last_error": o.get("error") or None, "per_printer": {}}
    if o.get("has_printer"):
        out["per_printer"][pid] = {"class": o.get("cls", ""), "score": o.get("score", 0), "frame_count": o.get("frames", 0),
                                   "error": o.get("error") or None}
    return out


@router.get("/api/v1/notifications/logs")
async def notif(x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return [{"id": n["id"], "title": n["title"], "message": n["message"], "created_at": n["time"], "success": n["success"]}
            for n in _engine.push.listing()["items"]]


@router.get("/api/v1/queue/")
async def queue(x_api_key: str | None = Header(default=None)):
    """The plates still to come of a multi-plate run (the panel counts them)."""
    _auth(x_api_key)
    q = _engine.plate_seq_view()
    if not q or not _plate_waiting():
        return []
    from . import db

    rest = (db.kv_get("plate_seq") or {}).get("plates") or []
    return [{"id": p, "plate": p, "file": q["file"], "status": "pending"} for p in rest]


# ---------------------------------------------------------------- archive (own print log), AMS history


@router.get("/api/v1/archives/")
async def archives(request: Request, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    from . import printlog

    return printlog.page(int(_q(request, "offset", 0)), min(100, int(_q(request, "limit", 20))))


@router.get("/api/v1/archives/stats")
async def archive_stats(x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    from . import printlog

    return printlog.statistics()


@router.get("/api/v1/archives/slim")
async def archive_slim(request: Request, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    from . import printlog

    return printlog.since(str(_q(request, "date_from", "1970-01-01")), min(2000, int(_q(request, "limit", 500))))


@router.get("/api/v1/archives/{aid}/filament-requirements")
async def archive_fil(aid: int, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    from . import printlog

    row = printlog.get(aid) or {}
    return {"filaments": [{"slot_id": f.get("slot", 0), "type": f.get("type", ""), "color": _hex(f.get("color", "")),
                           "used_grams": f.get("grams", 0), "used_meters": f.get("meters", 0)} for f in row.get("filaments") or []]}


@router.get("/api/v1/archives/{aid}/thumbnail")
async def archive_thumb(aid: int, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    png = await _engine.stats.thumb(aid)
    if not png:
        raise HTTPException(404, "No picture")
    return Response(png, media_type="image/png")


@router.get("/api/v1/archives/{aid}/plates")
@router.get("/api/v1/archives/{aid}/plate-thumbnail/{plate}")
async def archive_plates(aid: int, request: Request, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    raise HTTPException(404, "Not kept here: use files/plates")  # the panel falls back to the SD routes


@router.get("/api/v1/ams-history/{pid}/{ams}")
async def ams_history(pid: int, ams: int, request: Request, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    import time as _t

    from . import amshist

    hours = max(1, min(720, int(_q(request, "hours", 24))))
    pts = amshist.samples(ams, _t.time() - hours * 3600)
    hs = [h for _, h, _ in pts if h is not None]
    ts = [c for _, _, c in pts if c is not None]
    from datetime import datetime, timezone

    return {
        "data": [{"recorded_at": datetime.fromtimestamp(t, timezone.utc).isoformat(), "humidity": h, "temperature": c} for t, h, c in pts],
        "min_humidity": min(hs) if hs else None, "max_humidity": max(hs) if hs else None,
        "avg_humidity": round(sum(hs) / len(hs), 1) if hs else None,
        "min_temperature": min(ts) if ts else None, "max_temperature": max(ts) if ts else None,
        "avg_temperature": round(sum(ts) / len(ts), 1) if ts else None,
    }


# ---------------------------------------------------------------- anything else


@router.api_route("/api/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def not_here(path: str, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    raise HTTPException(404, f"Not available: /api/v1/{path}")

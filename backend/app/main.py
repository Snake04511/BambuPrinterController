"""FastAPI app: REST + WebSocket for the PWA, serves the built frontend."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth, backup, compat, inventory, maintenance, notify, settings
from . import forecast as forecastm
from .db import DATA_DIR
from .poller import Engine

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
# httpx logs every request URL at INFO: noisy, and download URLs carry one-time tokens.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

STATIC_DIR = Path(os.environ.get("STATIC_DIR", Path(__file__).resolve().parent.parent / "static"))
SECURE_COOKIE = os.environ.get("SECURE_COOKIE", "0") == "1"

engine: Engine


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global engine
    settings.migrate()
    engine = Engine()
    compat.init(engine)
    engine.start()
    backup_task = asyncio.create_task(backup.run_daily(), name="backup")
    log.info("started")
    yield
    backup_task.cancel()
    await engine.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
authed = [Depends(auth.require)]


# ---------------------------------------------------------------- health / auth


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "link": engine.link}


@app.get("/api/auth/state")
async def auth_state(request: Request) -> dict:
    return {"setup_needed": not auth.is_setup(), "logged_in": auth.valid(request.cookies.get(auth.COOKIE))}


def _login_response(token: str) -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        auth.COOKIE,
        token,
        max_age=auth.SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=SECURE_COOKIE,
        path="/",
    )
    return resp


def _check_new_password(pw: Any) -> str:
    if not isinstance(pw, str) or len(pw) < 4 or len(pw) > 128:
        raise HTTPException(400, "Use at least 4 characters.")
    return pw


@app.post("/api/auth/setup")
async def auth_setup(body: dict = Body(...)) -> Response:
    if auth.is_setup():
        raise HTTPException(409, "Already set up")
    auth.set_password(_check_new_password(body.get("password")))
    return _login_response(auth.new_session())


@app.post("/api/auth/login")
async def auth_login(body: dict = Body(...)) -> Response:
    if not auth.is_setup():
        raise HTTPException(409, "Set a password first")
    auth.check_password(str(body.get("password") or ""))
    return _login_response(auth.new_session())


@app.post("/api/auth/logout")
async def auth_logout(request: Request) -> Response:
    token = request.cookies.get(auth.COOKIE)
    if token:
        auth.drop_session(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.post("/api/auth/password", dependencies=authed)
async def auth_change(body: dict = Body(...)) -> Response:
    auth.check_password(str(body.get("current") or ""))
    auth.set_password(_check_new_password(body.get("password")))
    return _login_response(auth.new_session())


# ---------------------------------------------------------------- live state


@app.get("/api/state", dependencies=authed)
async def state() -> dict:
    return engine.snapshot()


@app.get("/api/temp-history", dependencies=authed)
async def temp_history() -> dict:
    return {"points": engine.temp_history()}


@app.get("/api/cover", dependencies=authed)
async def cover() -> Response:
    if not engine.cover:
        raise HTTPException(404, "No cover")
    return Response(engine.cover, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@app.websocket("/api/ws")
async def ws(websocket: WebSocket) -> None:
    if not auth.valid(websocket.cookies.get(auth.COOKIE)):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    changed = asyncio.Event()
    unsubscribe = engine.subscribe(changed.set)
    try:
        sent = -1
        while True:
            if engine.version != sent:
                sent = engine.version
                await websocket.send_json(engine.snapshot())
            changed.clear()
            try:
                # Re-send at least every 20 s (keeps proxies / iOS from idling the socket).
                await asyncio.wait_for(changed.wait(), timeout=20)
            except asyncio.TimeoutError:
                sent = -1
            await asyncio.sleep(0.15)  # coalesce bursts of changes
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        unsubscribe()


# ---------------------------------------------------------------- commands


@app.post("/api/cmd/{name}", dependencies=authed)
async def cmd(name: str, body: dict = Body(default={})) -> dict:
    return await engine.command(name, body or {})


# ---------------------------------------------------------------- settings


def _settings_view() -> dict:
    return {
        "settings": settings.get(),
        # secrets: only whether they are set, never any part of them
        "secrets": {k: bool(settings.secret(k)) for k in settings.SECRET_KEYS},
        "mqtt": engine.mqtt.connection(),
        "ftp": engine.ftp.status(),
        "camera": {**engine.camera.status(), "ipcam_record": engine.mqtt.details().get("ipcam_record") if engine.mqtt.connected else None},
        "notify_events": [{"key": k, "label": e.label} for k, e in notify.EVENTS.items()],
        "cloud": engine.cloud.info(),
    }


@app.get("/api/settings", dependencies=authed)
async def get_settings() -> dict:
    return _settings_view()


@app.patch("/api/settings", dependencies=authed)
async def patch_settings(body: dict = Body(...)) -> dict:
    changes = dict(body)
    # secrets: a string sets it, "" leaves it, null clears it
    secrets_in = {k: changes.pop(k) for k in list(changes) if k in settings.SECRET_KEYS}
    for k, v in secrets_in.items():
        if v is not None and not isinstance(v, str):
            raise HTTPException(400, f"{k}: expected text")
        if isinstance(v, str) and len(v) > 512:
            raise HTTPException(400, f"{k}: too long")
    try:
        settings.update(changes)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    for k, v in secrets_in.items():
        if v is None:
            settings.set_secret(k, "")
        elif v.strip():
            settings.set_secret(k, v.strip())
    if "printer_access_code" in secrets_in or any(k in changes for k in ("printer_ip", "printer_serial", "printer_name")):
        engine.apply_printer_settings()
    else:
        engine.bump()
    return _settings_view()


@app.post("/api/printer/access-code-from-cloud", dependencies=authed)
async def access_code_from_cloud() -> dict:
    """Copies the printer's LAN access code from the Bambu Cloud account into the secret store.
    The code itself is never returned, logged or shown."""
    if not engine.cloud.logged_in:
        return {"ok": False, "message": "Log in to Bambu Cloud first"}
    devs, err = await engine.cloud.devices()
    if devs is None:
        return {"ok": False, "message": f"Bambu Cloud: {err}"}
    cfg = settings.get()
    serial = cfg.get("printer_serial") or (engine.printer or {}).get("serial") or ""
    d = next((x for x in devs if x["serial"] == serial), None) if serial else (devs[0] if len(devs) == 1 else None)
    if d is None:
        return {"ok": False, "message": "Printer not found in the cloud account" if serial else "Set the serial number first"}
    if not d["access_code"]:
        return {"ok": False, "message": "The cloud did not send an access code for this printer"}
    settings.set_secret("printer_access_code", d["access_code"])
    if not cfg.get("printer_serial"):
        settings.update({"printer_serial": d["serial"]})
    engine.apply_settings()
    return {"ok": True, "message": f"Access code of {d['name'] or d['serial']} stored"}


@app.get("/api/obico/frame/{nonce}")
async def obico_frame(nonce: str):
    """The Obico ML server fetches the frame to analyse from here (single-use random nonce, no login)."""
    return await engine.obico_det.frames.response(nonce)


@app.post("/api/obico/test", dependencies=authed)
async def obico_test() -> dict:
    return await engine.obico_det.test_connection()


@app.get("/api/notify/texts", dependencies=authed)
async def notify_texts_get() -> dict:
    """The notification texts: built-in defaults and the user's changes (Settings editor)."""
    from . import notify_texts

    return {"defaults": notify_texts.DEFAULTS, "custom": notify_texts.overrides()}


@app.put("/api/notify/texts", dependencies=authed)
async def notify_texts_put(body: dict = Body(...)) -> dict:
    from . import notify_texts

    if not isinstance(body, dict):
        raise HTTPException(400, "Expected an object")
    return {"custom": notify_texts.save(body)}


@app.post("/api/notify/summary", dependencies=authed)
async def notify_summary() -> dict:
    """Send the daily / weekly summary now (try it out)."""
    engine.push.send_summary_now()
    return {"ok": True, "message": "Summary sent"}


@app.post("/api/panel-key/generate", dependencies=authed)
async def panel_key_generate() -> dict:
    """New API key for the panel: shown ONCE here (to type into the panel), stored as a secret."""
    import secrets as _s

    key = "pd_" + _s.token_urlsafe(24)
    settings.set_secret("panel_api_key", key)
    return {"key": key}


@app.post("/api/camera/restart", dependencies=authed)
async def camera_restart() -> dict:
    """Turn the printer's camera off and on (MQTT) when it gives no picture."""
    return await engine.restart_camera()


@app.post("/api/notify/test", dependencies=authed)
async def notify_test(body: dict = Body(default={})) -> dict:
    """Test button in Settings: sends one test message to the chosen channel."""
    ch = body.get("channel")
    res = await engine.push.n.send_test(ch if ch in ("discord", "ntfy") else None)
    return {k: v.to_dict() for k, v in res.items()}


# ---------------------------------------------------------------- Bambu Cloud


def _cn(body: dict) -> bool:
    return bool(body.get("china"))


@app.post("/api/cloud/login", dependencies=authed)
async def cloud_login(body: dict = Body(...)) -> dict:
    email, pw = str(body.get("email") or "").strip(), str(body.get("password") or "")
    if not email or not pw:
        return {"step": "error", "message": "Enter the Bambu account email and password."}
    res = await engine.cloud.login(email, pw, _cn(body))
    engine.bump()
    return res


@app.post("/api/cloud/code", dependencies=authed)
async def cloud_code(body: dict = Body(...)) -> dict:
    res = await engine.cloud.verify_email_code(
        str(body.get("email") or "").strip(), str(body.get("code") or "").strip(), _cn(body)
    )
    engine.bump()
    return res


@app.post("/api/cloud/totp", dependencies=authed)
async def cloud_totp(body: dict = Body(...)) -> dict:
    res = await engine.cloud.verify_totp(
        str(body.get("email") or "").strip(),
        str(body.get("tfa_key") or ""),
        str(body.get("code") or "").strip(),
        _cn(body),
    )
    engine.bump()
    return res


@app.post("/api/cloud/token", dependencies=authed)
async def cloud_token(body: dict = Body(...)) -> dict:
    ok = engine.cloud.set_token(str(body.get("token") or ""), str(body.get("email") or ""), _cn(body))
    engine.bump()
    return {"step": "done" if ok else "error", "message": "Bambu Cloud token saved." if ok else "That does not look like a token."}


@app.post("/api/cloud/logout", dependencies=authed)
async def cloud_logout() -> dict:
    engine.cloud.logout()
    engine.bump()
    return {"ok": True, "message": "Logged out of Bambu Cloud."}


# ---------------------------------------------------------------- printer SD card


def _trays(v: Any) -> list[int]:
    if not isinstance(v, list) or len(v) > 16:
        raise HTTPException(400, "Bad tray mapping")
    out = []
    for t in v:
        if isinstance(t, bool) or not isinstance(t, int):
            raise HTTPException(400, "Bad tray mapping")
        if not (t == -1 or 0 <= t < 16 or 24 <= t < 28 or 128 <= t < 136 or t == 254):
            raise HTTPException(400, f"Bad tray id {t}")
        out.append(t)
    return out


def _speed(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 4 else 0


@app.get("/api/sd/files", dependencies=authed)
async def sd_files() -> dict:
    return engine.sd.listing()


@app.post("/api/sd/refresh", dependencies=authed)
async def sd_refresh(soft: bool = False) -> dict:
    if engine.link != "online":
        raise HTTPException(409, "Printer not connected")
    if soft:
        engine.sd.list_if_old()  # FILES page opened
    else:
        engine.sd.refresh()
    return {"ok": True}


@app.post("/api/sd/fetch-new", dependencies=authed)
async def sd_fetch_new() -> dict:
    if engine.link != "online":
        raise HTTPException(409, "Printer not connected")
    return {"ok": True, "count": engine.sd.fetch_new()}


@app.post("/api/sd/download", dependencies=authed)
async def sd_download_start(body: dict = Body(...)) -> dict:
    name = str(body.get("name") or "")
    if engine.link != "online" or not name or "/" in name:
        raise HTTPException(400, "Bad file / not connected")
    return {"id": engine.sd.start_download(name, int(body.get("size") or 0))}


@app.get("/api/sd/download/{did}", dependencies=authed)
async def sd_download_status(did: str) -> dict:
    st = engine.sd.download_status(did)
    if st is None:
        raise HTTPException(404, "Unknown download")
    return st


@app.get("/api/sd/download/{did}/file", dependencies=authed)
async def sd_download_file(did: str) -> Response:
    res = await engine.sd.download_stream(did)
    if res is None:
        raise HTTPException(409, "The file is not ready")
    it, name, length = res
    from urllib.parse import quote

    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}", "Cache-Control": "no-store"}
    if length:
        headers["Content-Length"] = length
    return StreamingResponse(it, media_type="application/octet-stream", headers=headers)


@app.get("/api/sd/detail", dependencies=authed)
async def sd_detail(name: str, size: int, force: bool = False) -> dict:
    res = engine.sd.detail(name, size)
    if force or res["state"] != "ready":
        if force or res["state"] != "error":
            engine.sd.request(name, size, force)
            if force:
                res = {"state": "loading"}
    return res


@app.post("/api/sd/retry", dependencies=authed)
async def sd_retry(body: dict = Body(...)) -> dict:
    engine.sd.request(str(body.get("name") or ""), int(body.get("size") or 0), bool(body.get("force")))
    return {"ok": True}


@app.get("/api/sd/thumb", dependencies=authed)
async def sd_thumb(name: str, size: int, plate: int | None = None) -> Response:
    """plate omitted: the file's first plate (a single-plate file can be plate 4)."""
    f = engine.sd.thumb(name, size, plate if plate is not None else engine.sd.first_plate(name, size))
    if not f:
        raise HTTPException(404, "No preview")
    return FileResponse(f, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


def _live_by_tray() -> dict[str, float]:
    return (engine.job.use_view() or {}).get("by_tray") or {}


@app.post("/api/sd/forecast", dependencies=authed)
async def sd_forecast(body: dict = Body(...)) -> dict:
    """AMS auto mapping + filament check for an SD file (thin clients). Body: name, size,
    plates [indexes of the run, first = the one started] (default: the first plate), trays
    [per filament slot: tray id or -1 = auto]. While the file is read: {"state": "loading"}."""
    name, size = str(body.get("name") or ""), int(body.get("size") or 0)
    if not name or "/" in name:
        raise HTTPException(400, "Bad file name")
    res = engine.sd.detail(name, size)
    if res["state"] != "ready":
        if res["state"] != "error":
            engine.sd.request(name, size, False)
        return {"state": res["state"], "error": res.get("error")}
    plates = res["detail"].get("plates") or []
    want = body.get("plates") or ([plates[0]["index"]] if plates else [])
    if not isinstance(want, list) or not all(isinstance(i, int) for i in want):
        raise HTTPException(400, "Bad plate list")
    run = [p for i in want for p in plates if p["index"] == i]
    if not run:
        raise HTTPException(400, "No such plate")
    if not engine.status:
        raise HTTPException(409, "No printer status")
    fils = forecastm.merge_plates(run)
    return {"state": "ready", **forecastm.build(engine.status, engine.assignments, fils, forecastm.as_int_list(body.get("trays")), _live_by_tray())}


@app.post("/api/sd/delete", dependencies=authed)
async def sd_delete(body: dict = Body(...)) -> dict:
    name = str(body.get("name") or "")
    if not name or "/" in name:
        raise HTTPException(400, "Bad file name")
    return await engine.sd.delete(name)


@app.post("/api/sd/print", dependencies=authed)
async def sd_print(body: dict = Body(...)) -> dict:
    name = str(body.get("name") or "")
    plate = body.get("plate")
    if not name or "/" in name or not isinstance(plate, int) or not 1 <= plate <= 64:
        raise HTTPException(400, "Bad file / plate")
    opts = {
        "timelapse": bool(body.get("timelapse")),
        "bed_levelling": bool(body.get("bed_levelling", True)),
        "flow_cali": bool(body.get("flow_cali")),
    }
    then = body.get("then") or []
    if not isinstance(then, list) or len(then) > 63 or not all(isinstance(p, int) and 1 <= p <= 64 and p != plate for p in then):
        raise HTTPException(400, "Bad plate list")
    names = {str(k): str(v)[:80] for k, v in (body.get("names") or {}).items()} if isinstance(body.get("names"), dict) else {}
    return await engine.sd_print(name, plate, _trays(body.get("trays")), opts, _speed(body.get("speed_l1")), then=then, names=names)


@app.post("/api/plate-seq/{action}", dependencies=authed)
async def plate_seq(action: str) -> dict:
    """Multi-plate run: next (start the next plate), skip (drop it), stop (end the run)."""
    if action not in ("next", "skip", "stop"):
        raise HTTPException(404, "Unknown action")
    return await engine.plate_seq_command(action)


# ---------------------------------------------------------------- Bambu Cloud history / reprint


@app.get("/api/cloud/tasks", dependencies=authed)
async def cloud_tasks() -> dict:
    tasks = [{k: v for k, v in t.items() if k != "cover"} | {"has_cover": bool(t.get("cover"))} for t in engine.cloud_tasks]
    return {"loaded": engine.cloud_tasks_loaded, "tasks": tasks}


@app.get("/api/cloud/task/{task_id}", dependencies=authed)
async def cloud_task(task_id: str) -> dict:
    if not task_id.isalnum():
        raise HTTPException(400, "Bad task id")
    if not engine.cloud.logged_in:
        return {"ok": False, "error": "Log in to Bambu Cloud in Settings first"}
    return await engine.cloud_task_detail(task_id)


@app.get("/api/cloud/cover/{task_id}", dependencies=authed)
async def cloud_cover(task_id: str) -> Response:
    data = await engine.cloud_cover(task_id) if task_id.isalnum() else None
    if not data:
        raise HTTPException(404, "No cover")
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/cloud/reprint", dependencies=authed)
async def cloud_reprint(body: dict = Body(...)) -> dict:
    task_id = str(body.get("task_id") or "")
    if not task_id.isalnum():
        raise HTTPException(400, "Bad task id")
    return await engine.cloud_reprint(task_id, _trays(body.get("trays")), _speed(body.get("speed_l1")))


@app.post("/api/cloud/forecast", dependencies=authed)
async def cloud_forecast(body: dict = Body(...)) -> dict:
    """Same as /api/sd/forecast for a cloud reprint. Body: task_id, trays."""
    task_id = str(body.get("task_id") or "")
    if not task_id.isalnum():
        raise HTTPException(400, "Bad task id")
    d = engine._task_details.get(task_id)
    if d is None:
        if not engine.cloud.logged_in:
            return {"state": "error", "error": "Log in to Bambu Cloud in Settings first"}
        d = await engine.cloud_task_detail(task_id)
        if not d.get("ok"):
            return {"state": "error", "error": d.get("error") or "Cloud task not available"}
    if not engine.status:
        raise HTTPException(409, "No printer status")
    fils = forecastm.cloud_filaments(d)
    return {"state": "ready"} | forecastm.build(engine.status, engine.assignments, fils, forecastm.as_int_list(body.get("trays")), _live_by_tray())


# ---------------------------------------------------------------- running print: skip objects, Obico records


@app.get("/api/print/objects", dependencies=authed)
async def print_objects(refresh: bool = False) -> dict:
    if refresh and engine.link == "online" and engine.printer_id >= 0:
        engine.job.fetch_objects_bg()
    return engine.job.objects_view()


@app.get("/api/print/paths", dependencies=authed)
async def print_paths() -> Response:
    """Wall paths of the running plate for the live 3D view (gzip JSON); 202 while the file
    is being fetched / read, 404 when there is nothing to show."""
    job = (engine.status or {}).get("job") or ""
    jf = engine.sd.job_file(job) if job else None
    if not jf:
        raise HTTPException(404, "No SD file for this print")
    f, key, detail = jf
    plate = engine.sd.job_plate(detail, [o["id"] for o in engine.job.objects["items"]]) if detail else None
    idx = (plate or {}).get("index") or engine.plate_index() or 1
    state, data = await asyncio.to_thread(engine.sd.paths, key, int(idx))
    if state == "missing":
        if f.get("size"):
            engine.sd.request(f["name"], f["size"], force=True)  # fetch it once; the copy is kept
        return JSONResponse({"state": "loading"}, status_code=202)
    if state == "none" or data is None:
        raise HTTPException(404, "This file has no G-code to draw")
    return Response(data, media_type="application/json",
                    headers={"Content-Encoding": "gzip", "Cache-Control": "private, max-age=3600", "X-Plate": str(idx)})


@app.get("/api/print/pick", dependencies=authed)
async def print_pick() -> Response:
    if not engine.job.pick:
        raise HTTPException(404, "No object map")
    return Response(engine.job.pick, media_type="image/png", headers={"Cache-Control": "no-cache"})


@app.post("/api/print/skip", dependencies=authed)
async def print_skip(body: dict = Body(...)) -> dict:
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise HTTPException(400, "ids must be a list of object ids")
    st = engine.status or {}
    if engine.link != "online" or not st.get("connected"):
        return {"ok": False, "message": "Printer is offline"}
    if st.get("state") not in ("RUNNING", "PAUSE", "PREPARE"):
        return {"ok": False, "message": "Objects can only be skipped while printing"}
    return await engine.job.skip(ids)


# ---------------------------------------------------------------- AMS / STATS pages


@app.get("/api/maintenance", dependencies=authed)
async def maintenance_list() -> dict:
    return maintenance.listing()


@app.post("/api/maintenance/item", dependencies=authed)
async def maintenance_save(body: dict = Body(...)) -> dict:
    r = maintenance.save_item(body)
    engine._later("maint", 0)
    return r


@app.delete("/api/maintenance/item/{item_id}", dependencies=authed)
async def maintenance_delete(item_id: int) -> dict:
    r = maintenance.delete_item(item_id)
    engine._later("maint", 0)
    return r


@app.post("/api/maintenance/hours", dependencies=authed)
async def maintenance_hours(body: dict = Body(...)) -> dict:
    try:
        maintenance.set_hours(float(body.get("hours")))
    except (TypeError, ValueError) as e:
        raise HTTPException(400, "hours must be a number") from e
    engine._later("maint", 0)
    return {"ok": True, "message": "Saved"}


@app.post("/api/maintenance/defaults", dependencies=authed)
async def maintenance_defaults() -> dict:
    """The usual P1S items, counted from now."""
    maintenance.defaults()
    engine._later("maint", 0)
    return {"ok": True, "message": "Default items added"}


@app.get("/api/inventory/spools", dependencies=authed)
async def inventory_spools() -> dict:
    return await engine.stats.spools()


@app.get("/api/spools", dependencies=authed)
async def spools_page(archived: bool = False) -> dict:
    """Spools tab: the own inventory with tray, usage totals and (optionally) archived spools."""
    slot = {a["spool"]["id"]: (a["ams_id"], a["tray_id"]) for a in inventory.assignments()}
    from .printjob import tray_label

    out = []
    live = engine.live_used()
    from . import db as _db

    wet = _db.kv_get("spool_wet") or {}
    dry_h = settings.get().get("dry_hours", 24)
    for sp in inventory.list_spools(include_archived=archived):
        a = slot.get(sp["id"])
        used_now = live.get(sp["id"], 0.0)
        sp["printing_used"] = used_now  # this print so far, booked when it ends
        w = wet.get(str(sp["id"])) or {}
        sp["wet_hours"] = round(w.get("min", 0) / 60, 1)
        sp["dried_at"] = w.get("dried") or None
        sp["needs_drying"] = sp["wet_hours"] >= dry_h
        sp["remaining"] = round(max(0.0, sp["remaining"] - used_now), 1)
        sp["tray"] = None if a is None else tray_label(254 if a[0] == 255 else a[0] if a[0] >= 128 else a[0] * 4 + a[1])
        out.append(sp)
    return {"own": True, "spools": out, "usage": inventory.usage_history(limit=60)}


@app.get("/api/ams/history", dependencies=authed)
async def ams_history(ams: int, hours: int = 6) -> dict:
    if hours not in (6, 24, 72, 168) or not (0 <= ams <= 3 or 128 <= ams <= 135):
        raise HTTPException(400, "Bad AMS / hours")
    return await engine.stats.ams_history(ams, hours)


@app.get("/api/stats", dependencies=authed)
async def statistics(refresh: bool = False) -> dict:
    return await engine.stats.statistics(refresh)


@app.get("/api/history", dependencies=authed)
async def history(offset: int = 0) -> dict:
    return await engine.stats.history(max(0, offset))


@app.delete("/api/history/{archive_id}", dependencies=authed)
async def history_delete(archive_id: int) -> dict:
    return await engine.stats.delete_archive(archive_id)


@app.get("/api/history/{archive_id}/thumb", dependencies=authed)
async def history_thumb(archive_id: int) -> Response:
    png = await engine.stats.thumb(archive_id)
    if not png:
        raise HTTPException(404, "No preview")
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


# ---------------------------------------------------------------- camera


@app.get("/api/camera/snapshot", dependencies=authed)
async def camera_snapshot() -> Response:
    jpg, err = await engine.camera.snapshot()
    if not jpg:
        raise HTTPException(503, err)
    return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/camera/stream", dependencies=authed)
async def camera_stream(fps: int = 5) -> Response:
    it, ctype, err = await engine.camera.open_stream(max(1, min(15, fps)))
    if it is None:
        raise HTTPException(503, err)
    return StreamingResponse(it, media_type=ctype, headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/obico/current", dependencies=authed)
async def obico_current() -> dict:
    """Scores recorded so far for the running print (the open span included)."""
    r = engine.job.rec
    if not r:
        return {"record": None}
    pts = list(r["points"]) + ([r["span_max"]] if r["span_count"] else [])
    return {"record": {**r, "points": pts}}


@app.get("/api/usage", dependencies=authed)
async def usage_list() -> dict:
    return {"items": engine.usage.listing()}


@app.post("/api/usage/{rid}/{action}", dependencies=authed)
async def usage_action(rid: str, action: str) -> dict:
    if action == "sync":
        return await engine.usage.sync(rid)
    if action in ("ignore", "unignore"):
        return engine.usage.ignore(rid, action == "ignore")
    raise HTTPException(404, "Unknown action")


@app.get("/api/backup", dependencies=authed)
async def backup_status() -> dict:
    return await asyncio.to_thread(backup.status)


@app.post("/api/backup", dependencies=authed)
async def backup_now() -> dict:
    return await asyncio.to_thread(backup.make)


@app.get("/api/backup/{name}", dependencies=authed)
async def backup_download(name: str):
    import re as _re

    if not _re.fullmatch(r"printerdash-[0-9-]+\.tar\.gz", name) or not (backup.BACKUP_DIR / name).is_file():
        raise HTTPException(404, "No such backup")
    return FileResponse(backup.BACKUP_DIR / name, media_type="application/gzip", filename=name)


@app.get("/api/storage", dependencies=authed)
async def storage() -> dict:
    return await asyncio.to_thread(engine.timelapse.storage)


@app.post("/api/storage/prune", dependencies=authed)
async def storage_prune() -> dict:
    """Apply the (possibly lowered) limits now instead of at the next recording."""
    await asyncio.to_thread(engine.timelapse.prune_now)
    return await asyncio.to_thread(engine.timelapse.storage)


@app.get("/api/timelapse", dependencies=authed)
async def timelapse_list() -> dict:
    return {"items": engine.timelapse.listing(), "current": engine.timelapse.current_id()}


@app.post("/api/timelapse/import", dependencies=authed)
async def timelapse_import(request: Request, archive_id: int) -> dict:
    """The printer's own timelapse (.avi, e.g. saved from Bambu Studio) for a History print."""
    from .stats import _epoch

    from . import printlog

    a = printlog.get(archive_id)
    if not a:
        raise HTTPException(404, "Print not found in the print log")
    start = _epoch(a.get("started_at") or a.get("created_at"))
    end = _epoch(a.get("completed_at"))
    if start is None:
        raise HTTPException(400, "The print has no start time")
    if end is None or end <= start:
        secs = a.get("actual_time_seconds") or a.get("print_time_seconds") or 0
        end = start + float(secs or 3600)
    tmp = DATA_DIR / "tmp" / "import.avi"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with tmp.open("wb") as f:
        async for chunk in request.stream():
            size += len(chunk)
            if size > 4 * 1024**3:
                raise HTTPException(413, "File too large")
            f.write(chunk)
    try:
        m = await asyncio.to_thread(engine.timelapse.import_avi, tmp, a, engine.job.obico_record(archive_id), start, end)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": True, "id": m["id"], "frames": len(m["frames"])}


def _fps(v) -> int:
    try:
        fps = int(v)
    except (TypeError, ValueError):
        fps = 15
    return fps if fps in (5, 10, 15, 24, 30, 60) else 15


@app.post("/api/timelapse/{tid}/video", dependencies=authed)
async def timelapse_video_start(tid: str, body: dict = Body(default={})) -> dict:
    """Make (or reuse) the MP4 of a recording; poll GET .../video/status, then download."""
    return engine.timelapse.start_export(tid, _fps(body.get("fps")), bool(body.get("overlay")))


@app.get("/api/timelapse/{tid}/video/status", dependencies=authed)
async def timelapse_video_status(tid: str, fps: int = 15, overlay: bool = False) -> dict:
    return engine.timelapse.export_status(tid, _fps(fps), overlay)


@app.get("/api/timelapse/{tid}/video", dependencies=authed)
async def timelapse_video(tid: str, fps: int = 15, overlay: bool = False):
    f = engine.timelapse.video_file(tid, _fps(fps), overlay)
    m = engine.timelapse.meta(tid)
    if f is None or m is None:
        raise HTTPException(404, "Video not made yet")
    import re as _re

    name = _re.sub(r"[^\w.-]+", "_", m.get("job") or "timelapse").strip("_")[:80] or "timelapse"
    return FileResponse(f, media_type="video/mp4", filename=f"{name}_timelapse.mp4")


@app.get("/api/timelapse/{tid}", dependencies=authed)
async def timelapse_meta(tid: str) -> dict:
    m = engine.timelapse.meta(tid)
    if m is None:
        raise HTTPException(404, "No such recording")
    return m


@app.get("/api/timelapse/{tid}/frame/{n}", dependencies=authed)
async def timelapse_frame(tid: str, n: int) -> Response:
    f = engine.timelapse.frame(tid, n)
    if f is None:
        raise HTTPException(404, "No such frame")
    return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=31536000, immutable"})


@app.get("/api/obico/records", dependencies=authed)
async def obico_records() -> dict:
    return {"ids": engine.job.obico_record_ids()}


@app.get("/api/obico/record/{archive_id}", dependencies=authed)
async def obico_record(archive_id: int) -> dict:
    r = engine.job.obico_record(archive_id)
    if r is None:
        raise HTTPException(404, "No Obico record for this print")
    return r


# ---------------------------------------------------------------- frontend (built PWA)

# ---------------------------------------------------------------- panel API (/api/v1, ESP32 panel)

app.include_router(compat.router)


if STATIC_DIR.is_dir():
    _gz_cache: dict[str, bytes] = {}

    @app.get("/assets/{name}", include_in_schema=False)
    async def asset(name: str, request: Request) -> Response:
        """Build assets: hashed names never change, so cached for a year, and gzip-ed
        (a third of the size; only these files - the camera stream must not be buffered)."""
        f = (STATIC_DIR / "assets" / name).resolve()
        if not f.is_file() or (STATIC_DIR / "assets").resolve() not in f.parents:
            raise HTTPException(404)
        headers = {"Cache-Control": "public, max-age=31536000, immutable", "Vary": "Accept-Encoding"}
        media = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}.get(f.suffix)
        if media and "gzip" in request.headers.get("accept-encoding", ""):
            if name not in _gz_cache:
                import gzip as _gz

                _gz_cache[name] = await asyncio.to_thread(_gz.compress, f.read_bytes(), 9)
            return Response(_gz_cache[name], media_type=media, headers={**headers, "Content-Encoding": "gzip"})
        return FileResponse(f, headers=headers)

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> Response:
        if path.startswith("api/"):
            raise HTTPException(404)
        f = (STATIC_DIR / path).resolve()
        if path and f.is_file() and STATIC_DIR.resolve() in f.parents:
            headers = {"Cache-Control": "no-cache"} if f.name in ("sw.js", "manifest.webmanifest") else None
            return FileResponse(f, headers=headers)
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

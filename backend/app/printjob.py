"""Background logic around the running / last print (panel src/app/Backend.cpp,
ui/Alerts.cpp, ui/PrintScreens.cpp, ui/pages/SkipObjects.cpp):

- print log entry of the running job (run-out forecast, Obico pairing)
- Obico score recording per print (stored by archive id for STATS / History)
- filament used of a finished print (print log, else the Bambu Cloud task)
- objects of the running print + the slicer's pick image (skip objects)
- alerts: low filament, run-out forecast, AMS humidity, Bambu Cloud login expiry
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from . import cloud as cloudm
from . import db
from . import printer_mqtt as pm
from .db import DATA_DIR
from .sdcard import same_job_name

if TYPE_CHECKING:
    from .poller import Engine

log = logging.getLogger("printjob")

OBICO_DIR = DATA_DIR / "obico"
OBICO_DIR.mkdir(parents=True, exist_ok=True)
OBICO_POINTS = 120
OBICO_SAVE_S = 180
OBICO_PENDING_TRIES = 10

ARCHIVE_AFTER_START_S = 20
ARCHIVE_RETRY_S = 120

END_FIL_FIRST_S = 3
END_FIL_STEP_S = 6
END_FIL_WINDOW_S = 120

LOW_REMAIN_PCT = 10
LOW_SPOOL_G = 50
HUMID_HIGH = 40
HUMID_HOLD_S = 10 * 60
CLOUD_WARN_EVERY_S = 24 * 3600

PICK_SIZE = 360
SKIP_VERIFY_S = 10
OBJECTS_START_DELAY_S = 15
MAX_OBJECTS = 64


def tray_label(tid: int) -> str:
    if tid == 254:
        return "Ext"
    if tid >= 128:
        return f"HT{tid - 127}"
    if tid >= 0:
        return f"{chr(65 + tid // 4)}{tid % 4 + 1}"
    return "-"


def _f(v: Any, default: float = -1.0) -> float:
    try:
        return float(v) if v is not None and not isinstance(v, bool) else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- archives


def archive_matches(a: dict | None, job: str) -> bool:
    """The print log entry of this job? Its title often differs, the file name does not."""
    return bool(a) and bool(job) and (same_job_name(a["name"], job) or same_job_name(a["filename"], job))


# ---------------------------------------------------------------- pick map (Pillow)


def decode_pick_map(png: bytes, ids: list[int]) -> tuple[bytes | None, str]:
    """Slicer pick image (object id packed into R, G, B) -> 360x360 index map as an 8-bit
    grayscale PNG: value = object index + 1, 0 = no object. Cropped to the objects."""
    from PIL import Image  # imported lazily: only needed while a print runs

    try:
        img = Image.open(io.BytesIO(png)).convert("RGBA")
    except Exception as e:  # noqa: BLE001
        return None, f"Pick image unreadable: {e}"
    w, h = img.size
    px = img.tobytes()
    index = {oid: k for k, oid in enumerate(ids)}
    hits_rgb = hits_bgr = 0
    for p in range(0, w * h, 7):
        o = p * 4
        if px[o + 3] < 128:
            continue
        if (px[o] | px[o + 1] << 8 | px[o + 2] << 16) in index:
            hits_rgb += 1
        if (px[o + 2] | px[o + 1] << 8 | px[o] << 16) in index:
            hits_bgr += 1
    if not hits_rgb and not hits_bgr:
        return None, "Pick image does not match the objects"
    rgb = hits_rgb >= hits_bgr

    # Index of every source pixel (-1: none), plus the objects' bounding box.
    src = bytearray(w * h)  # 0 = none, else index + 1
    x0, y0, x1, y1 = w, h, -1, -1
    for y in range(h):
        row = y * w
        for x in range(w):
            o = (row + x) * 4
            if px[o + 3] < 128:
                continue
            col = (px[o] | px[o + 1] << 8 | px[o + 2] << 16) if rgb else (px[o + 2] | px[o + 1] << 8 | px[o] << 16)
            k = index.get(col)
            if k is None:
                continue
            src[row + x] = k + 1
            if x < x0:
                x0 = x
            if x > x1:
                x1 = x
            if y < y0:
                y0 = y
            if y > y1:
                y1 = y
    if x1 < 0:
        return None, "Pick image does not match the objects"
    side = max(x1 - x0 + 1, y1 - y0 + 1) * 1.12 + 6
    cx, cy = (x0 + x1 + 1) / 2, (y0 + y1 + 1) / 2
    step = side / PICK_SIZE
    out = bytearray(PICK_SIZE * PICK_SIZE)
    for y in range(PICK_SIZE):
        sy = int(cy - side / 2 + (y + 0.5) * step)
        if sy < 0 or sy >= h:
            continue
        for x in range(PICK_SIZE):
            sx = int(cx - side / 2 + (x + 0.5) * step)
            if 0 <= sx < w:
                out[y * PICK_SIZE + x] = src[sy * w + sx]
    buf = io.BytesIO()
    Image.frombytes("L", (PICK_SIZE, PICK_SIZE), bytes(out)).save(buf, "PNG", optimize=True)
    log.info("pick map %dx%d: %s order, %d sampled pixels matched", w, h, "RGB" if rgb else "BGR", hits_rgb if rgb else hits_bgr)
    return buf.getvalue(), ""


# ---------------------------------------------------------------- the job watcher


class PrintJob:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self.reset()

    def reset(self) -> None:
        # archive of the running job
        self.archive: dict | None = None
        self.archive_for_job = False
        self._next_archive = 0.0  # monotonic, 0 = not needed
        # Obico record of the running print + one finished record still looking for its archive
        self.rec: dict | None = None
        self._rec_frames = -1
        self._rec_saved = 0.0
        self.pending_rec: dict | None = None
        self._pending_tries = 0
        self._pending_at = 0.0
        # finished print: filament used (filled into engine.last_end)
        self._end: dict | None = None
        # objects of the running print
        self.objects: dict = {"job": "", "items": [], "bbox": None, "error": "", "loaded": False}
        self.objects_version = 0
        self.pick: bytes | None = None
        self.pick_job = ""
        self.pick_error = ""
        self._objects_asked = ""
        self._objects_task: asyncio.Task | None = None
        self._skip_check: dict | None = None
        self._objects_not_before = 0.0
        self._sd_reread = ""  # job whose SD file was (re)read for its objects
        self._sd_reread_at = 0.0
        self._sd_seen = -1
        self._sd_next = 0.0
        # filament used by the running print so far, per tray (estimate)
        self.use: dict = self._new_use("")
        self._use_saved = 0.0
        # alerts
        self._humid_since: dict[int, float] = {}
        self.humid_alarm = -1
        self._runout_warned = ""
        self._cloud_warned = 0.0
        self._next_cloud_check = time.monotonic() + 60

    # ------------------------------------------------------------ lifecycle hooks (poller._watch_print)

    def on_boot(self, st: dict, active: bool) -> None:
        saved = db.kv_get("filament_use") or {}
        self.use = saved if active and saved.get("job") == st["job"] else self._new_use(st["job"] if active else "")
        self.e.timelapse.on_boot(st, active)
        if active:
            self._next_archive = time.monotonic() + 5  # restarted during a print: forecast it too
            self._obico_start(st["job"], resume=True)

    def on_start(self, job: str) -> None:
        self.use = self._new_use(job)
        self.use["spools_start"] = self.e.usage.spools_now()
        self._save_use()
        self.e.timelapse.on_start(job)
        self._objects_not_before = time.monotonic() + OBJECTS_START_DELAY_S
        self.archive_for_job = False
        self._next_archive = time.monotonic() + ARCHIVE_AFTER_START_S
        self._runout_warned = ""
        self._obico_start(job, resume=False)

    def on_end(self, st: dict, end: dict) -> None:
        end["filament"] = []
        end["filament_state"] = "loading"
        end["archive_sec"] = -1
        now = time.monotonic()
        self._end = {
            "job": end["job"],
            "at": now + END_FIL_FIRST_S,
            "until": now + END_FIL_WINDOW_S,
            "tries": 0,
            "use_cloud": False,
            "task_id": "",
        }
        self._next_archive = 0.0
        self.e.timelapse.on_end(st)
        self._record_use(st, end["job"])
        asyncio.create_task(self._obico_finish(st))

    # ------------------------------------------------------------ tick (poller._tick, online only)

    async def tick(self, st: dict, active: bool) -> None:
        now = time.monotonic()
        if self._next_archive and now >= self._next_archive:
            if not active:
                self._next_archive = 0.0
            else:
                a = await self.fetch_latest_archive(with_filament=True)
                if a is not None:
                    self.archive = a
                self.archive_for_job = archive_matches(self.archive, st.get("job", ""))
                self._next_archive = 0.0 if self.archive_for_job else time.monotonic() + ARCHIVE_RETRY_S
                if self.archive_for_job and self.rec and self.archive:
                    self.rec["archive_id"] = self.archive["id"]
                if self.archive_for_job and self.archive:
                    self.e.timelapse.set_archive(st.get("job", ""), self.archive["id"])
                    self.e.usage.set_archive(st.get("job", ""), self.archive["id"])
                self.e.bump()
        if self._end and now >= self._end["at"]:
            await self._end_filament_step()
        if self.pending_rec and now >= self._pending_at:
            await self._obico_pending_step()
        # objects of a new print, once (the Skip button needs more than one object)
        job = st.get("job", "")
        if active and job and job != self._objects_asked and now >= self._objects_not_before and not self._objects_busy():
            self._objects_asked = job
            self.fetch_objects_bg()
        # No shapes yet and the SD listing / cache changed (listed after start-up, or the job's file
        # was just read): look again, the SD copy of the file may complete the objects.
        if (
            active
            and job
            and self.pick is None
            and self._objects_asked == job
            and self.e.sd.version != self._sd_seen
            and now >= self._sd_next
            and not self._objects_busy()
        ):
            self._sd_seen = self.e.sd.version
            self._sd_next = now + 30  # the SD cache changes often while files are read
            self.fetch_objects_bg()
        if self._skip_check and now >= self._skip_check["at"] and not self._objects_busy():
            self.fetch_objects_bg()
        self._track_use(st, active)
        self.e.timelapse.tick(st, active)
        self._check_humidity(st)
        self._check_runout(st, active)
        self._check_cloud_expiry()

    # ------------------------------------------------------------ archive

    async def fetch_latest_archive(self, with_filament: bool) -> dict | None:
        """The running / last print in the print log (with_filament: kept for the callers)."""
        return self.e.log_archive()

    # ------------------------------------------------------------ finished print: filament used

    async def _end_filament_step(self) -> None:
        v = self._end
        end = self.e.last_end
        if v is None or end is None:
            self._end = None
            return
        now = time.monotonic()
        if now >= v["until"]:
            return self._end_done("none")
        if not v["use_cloud"]:
            a = await self.fetch_latest_archive(with_filament=True)
            ok = a is not None and archive_matches(a, v["job"])
            final = ok and a["status"] not in ("printing", "")
            if ok and a["fil"] and (final or v["tries"] >= 4):
                end["archive_sec"] = a["actual_sec"]
                end["filament"] = a["fil"]
                end["archive_id"] = a["id"]
                return self._end_done("done")
            v["tries"] += 1
            if v["tries"] < 5:
                v["at"] = now + END_FIL_STEP_S
                return
            v["use_cloud"] = self.e.cloud.logged_in
            if not v["use_cloud"]:
                return self._end_done("none")
        # Bambu Cloud fallback: the job in the history (refreshed 20 s after the end)
        v["at"] = now + 10
        if not v["task_id"]:
            task = next((t for t in self.e.cloud_tasks if same_job_name(t.get("title", ""), v["job"])), None)
            if not task:
                return
            v["task_id"] = task["id"]
        d = await cloudm.task_detail(self.e.cloud, v["task_id"])
        if not d.get("ok"):
            return self._end_done("none")
        fil = []
        for r in d.get("rows") or []:
            where = ""
            if r["ams_id"] == 255:
                where = "Ext"
            elif r["ams_id"] >= 0 and r["slot_id"] >= 0:
                where = tray_label(r["ams_id"] if r["ams_id"] >= 128 else r["ams_id"] * 4 + r["slot_id"])
            fil.append(
                {
                    "slot": 0,
                    "type": r["filament_type"],
                    "color": pm.parse_color(r["source_color"]),
                    "grams": round(r["weight"], 2),
                    "meters": 0,
                    "where": where,
                }
            )
        end["filament"] = fil
        self._end_done("done" if fil else "none")

    def _end_done(self, state: str) -> None:
        self._end = None
        if self.e.last_end is not None:
            self.e.last_end["filament_state"] = state
        self.e.bump()

    # ------------------------------------------------------------ Obico recording

    def _obico_path(self, archive_id: int | None) -> Any:
        return OBICO_DIR / ("current.json" if archive_id is None else f"{int(archive_id)}.json")

    def _save_rec(self, rec: dict, archive_id: int | None) -> None:
        try:
            self._obico_path(archive_id).write_text(json.dumps(rec))
        except OSError as e:
            log.warning("obico save failed: %s", e)

    def _remove_current(self) -> None:
        self._obico_path(None).unlink(missing_ok=True)

    def _obico_start(self, job: str, resume: bool) -> None:
        rec = None
        if resume:
            try:
                rec = json.loads(self._obico_path(None).read_text())
                if rec.get("job") != job:
                    rec = None
                else:
                    log.info("Obico record of '%s' resumed (%d samples)", job, rec.get("samples", 0))
            except (OSError, ValueError):
                rec = None
        if rec is None:
            rec = {
                "job": job,
                "archive_id": -1,
                "start": 0 if resume else int(time.time()),
                "end": 0,
                "result": "",
                "samples": 0,
                "warn": 0,
                "fail": 0,
                "dark": 0,
                "max": 0.0,
                "sum": 0.0,
                "low": 0.0,
                "high": 0.0,
                "frames": 0,
                "stride": 1,
                "points": [],
                "span_count": 0,
                "span_max": 0,
            }
        self.rec = rec
        self._rec_frames = -1
        self._rec_saved = time.monotonic()

    def _push(self, v: int, rec: dict | None = None) -> None:
        r = rec if rec is not None else self.rec
        if len(r["points"]) >= OBICO_POINTS:  # full: halve the resolution
            p = r["points"]
            r["points"] = [max(p[2 * i], p[2 * i + 1]) for i in range(OBICO_POINTS // 2)]
            r["stride"] *= 2
        r["points"].append(v)

    def obico_sample(self, o: dict | None, st: dict | None) -> None:
        """Every newly analysed frame of the running print (called after each Obico poll)."""
        r = self.rec
        if not r or not o or not st or st.get("state") not in ("PREPARE", "SLICING", "RUNNING", "PAUSE"):
            return
        if not o.get("enabled") or not o.get("has_printer") or not o.get("cls"):
            return
        if o["frames"] == self._rec_frames:
            return  # no new frame analysed
        self._rec_frames = o["frames"]
        score = float(o.get("score") or 0)
        r["samples"] += 1
        r["sum"] += score
        r["max"] = max(r["max"], score)
        r["warn"] += o["cls"] == "warning"
        r["fail"] += o["cls"] == "failure"
        r["dark"] += not st.get("light")
        r["low"], r["high"], r["frames"] = o.get("low", 0), o.get("high", 0), o["frames"]
        v = max(0, min(255, round(score * 200)))
        r["span_max"] = max(r["span_max"], v)
        r["span_count"] += 1
        if r["span_count"] >= r["stride"]:
            self._push(r["span_max"])
            r["span_count"] = r["span_max"] = 0
        if time.monotonic() - self._rec_saved > OBICO_SAVE_S:
            self._rec_saved = time.monotonic()
            self._save_rec(r, None)

    async def _obico_finish(self, st: dict) -> None:
        r, self.rec = self.rec, None
        if not r:
            return
        if not r["samples"]:
            self._remove_current()
            return
        if r["span_count"]:
            self._push(r["span_max"], r)  # self.rec is already cleared here
        r["span_count"] = r["span_max"] = 0
        r["end"] = int(time.time())
        r["result"] = st.get("state", "")
        if r["archive_id"] < 0:
            a = await self.fetch_latest_archive(with_filament=False)
            if archive_matches(a, r["job"]):
                r["archive_id"] = a["id"]
        if r["archive_id"] >= 0:
            self.e.timelapse.set_archive(r["job"], r["archive_id"])
            self.e.usage.set_archive(r["job"], r["archive_id"])
            self._save_rec(r, r["archive_id"])
            self._remove_current()
            log.info("Obico record saved for archive %d: %d samples, max %.2f", r["archive_id"], r["samples"], r["max"])
        else:
            log.info("Obico record of '%s': archive not there yet, looking again for 10 minutes", r["job"])
            self._save_rec(r, None)
            self.pending_rec = r
            self._pending_tries = 0
            self._pending_at = time.monotonic() + 60

    async def _obico_pending_step(self) -> None:
        r = self.pending_rec
        self._pending_at = time.monotonic() + 60
        a = await self.fetch_latest_archive(with_filament=False)
        done = False
        if archive_matches(a, r["job"]):
            r["archive_id"] = a["id"]
            self.e.timelapse.set_archive(r["job"], a["id"])
            self._save_rec(r, a["id"])
            log.info("Obico record saved for archive %d (found later): %d samples", a["id"], r["samples"])
            done = True
        else:
            self._pending_tries += 1
            if self._pending_tries >= OBICO_PENDING_TRIES:
                log.info("Obico record of '%s': no archive found, not kept", r["job"])
                done = True
        if done:
            self.pending_rec = None
            if not self.rec:
                self._remove_current()

    def obico_record(self, archive_id: int) -> dict | None:
        try:
            return json.loads(self._obico_path(archive_id).read_text())
        except (OSError, ValueError):
            return None

    def obico_record_ids(self) -> list[int]:
        return sorted(int(p.stem) for p in OBICO_DIR.glob("*.json") if p.stem.isdigit())

    # ------------------------------------------------------------ print objects (skip)

    def _objects_busy(self) -> bool:
        return self._objects_task is not None and not self._objects_task.done()

    def fetch_objects_bg(self) -> asyncio.Task:
        if not self._objects_busy():
            self._objects_task = asyncio.create_task(self._fetch_objects())
        return self._objects_task

    async def _fetch_objects(self) -> None:
        """Objects of the running plate + its pick image, from this app's SD copy of the file."""
        job = (self.e.status or {}).get("job", "")
        items: list[dict] = []
        bbox = None
        err = ""
        sd_pick = None
        jf = self.e.sd.job_file(job) if job else None
        if not jf:
            err = "The print's file is not on the SD card"
        else:
            f, key, detail = jf
            plate = self.e.sd.job_plate(detail, []) if detail else None
            if detail is None or (plate is not None and "objects" not in plate):
                # Not cached, or cached before objects were kept: read it (again after 30 min if
                # the printer's FTP refused, as it may during a print).
                err = "Reading the print's file..."
                if self._sd_reread != job or time.monotonic() - self._sd_reread_at > 1800:
                    self._sd_reread, self._sd_reread_at = job, time.monotonic()
                    self.e.sd.request(f["name"], f["size"], force=detail is not None)
                    log.info("objects: reading '%s' from the SD card for the object list", f["name"])
            elif plate and plate.get("objects"):
                items = [{"id": o["id"], "name": o["name"], "x": -1.0, "y": -1.0, "skipped": False}
                         for o in plate["objects"]][:MAX_OBJECTS]
                if plate.get("has_pick"):
                    sd_pick = self.e.sd.png(key, f"pick{plate['index']}")
            else:
                err = "No objects in the print's file"
        # Which objects the printer really skips: its own report (s_obj), straight over MQTT.
        live = self.e.mqtt.connected
        if live:
            s_obj = set(self.e.mqtt.details().get("skipped_objects") or [])
            for o in items:
                o["skipped"] = o["skipped"] or o["id"] in s_obj
        ok = bool(items)
        # Object shapes (pick image): once per job; a missing one is retried on every fetch.
        if items and (self.pick_job != job or self.pick is None):
            self.pick_job = job
            self.pick, self.pick_error = None, ""
            ids = [o["id"] for o in items]
            if sd_pick:
                self.pick, self.pick_error = await asyncio.to_thread(decode_pick_map, sd_pick, ids)
            else:
                self.pick_error = "No pick image"
            if self.pick_error:
                log.info("pick map: %s", self.pick_error)
        chk = self._skip_check
        if chk and time.monotonic() >= chk["at"] and live:
            self._skip_check = None
            skipped = {o["id"] for o in items if o["skipped"]}
            missing = sum(1 for i in chk["ids"] if i not in skipped)
            if missing:
                self.e.notify(False, f"The printer did not skip {missing} object(s) - try again")
            else:
                self.e.notify(True, f"Printer confirmed: {len(chk['ids'])} object(s) skipped")
        if ok or not self.objects["loaded"] or self.objects["job"] != job:
            self.objects = {"job": job, "items": items, "bbox": bbox, "error": err, "loaded": ok}
        self.objects_version += 1
        self.e.bump()

    def objects_count(self) -> int:
        st = self.e.status or {}
        o = self.objects
        if not o["loaded"] or o["job"] != st.get("job"):
            return -1
        return len(o["items"])

    def objects_view(self) -> dict:
        st = self.e.status or {}
        o = self.objects
        current = o["job"] == st.get("job")
        return {
            "job": o["job"],
            "current": current,
            "loaded": o["loaded"] and current,
            "loading": self._objects_busy(),
            "error": o["error"],
            "items": o["items"] if current else [],
            "bbox": o["bbox"] if current else None,
            "has_pick": bool(self.pick) and self.pick_job == st.get("job"),
            "version": self.objects_version,
        }

    async def skip(self, ids: list[int]) -> dict:
        known = {o["id"] for o in self.objects["items"] if not o["skipped"]}
        ids = [i for i in ids if i in known][:24]
        if not ids:
            return {"ok": False, "message": "No objects selected"}
        r = self.e.mqtt.skip_objects(ids)
        log.info("skip %d object(s) %s -> %s", len(ids), ids, "OK" if r.ok else r.message)
        if r.ok:
            self.e.notify(True, f"Skipping {len(ids)} object(s)...")
            self._skip_check = {"ids": ids, "at": time.monotonic() + SKIP_VERIFY_S}
        else:
            self.e.notify(False, f"Skip failed: {r.message}")
        self.fetch_objects_bg()  # show the new state
        return {"ok": r.ok, "message": r.message}

    # ------------------------------------------------------------ alerts

    # ------------------------------------------------------------ filament used so far (estimate)
    # The print's filament is booked on the spool when the print ends. Meanwhile: the
    # print's total grams x the progress made, credited to the tray feeding at that moment.

    @staticmethod
    def _new_use(job: str) -> dict:
        return {"job": job, "progress": None, "total": 0.0, "by_tray": {}, "map": None, "free": 0.0, "extra": {}}

    def _save_use(self) -> None:
        db.kv_set("filament_use", self.use)
        self._use_saved = time.monotonic()

    def _print_filaments(self, job: str) -> list[dict]:
        """Filaments of the running print: [{type, color, grams}] (archive, else our SD copy)."""
        a = self.archive
        if self.archive_for_job and a and a["fil"]:
            return [{"type": f["type"], "color": f["color"], "grams": f["grams"]} for f in a["fil"]]
        jf = self.e.sd.job_file(job)
        if jf and jf[2]:
            plate = self.e.sd.job_plate(jf[2], [o["id"] for o in self.objects["items"]])
            if plate:
                fil = [f for f in plate.get("filaments") or [] if f.get("grams", 0) > 0]
                if fil:
                    return [{"type": f["type"], "color": f["color"], "grams": f["grams"]} for f in fil]
                if plate.get("grams", 0) > 0:
                    return [{"type": "", "color": "", "grams": plate["grams"]}]
        return []

    def _match_tray(self, st: dict, f: dict, taken: set) -> int:
        """Loaded tray of a print filament: same type + colour, else same type, closest colour."""
        def rgb(c: str) -> tuple[int, int, int]:
            c = (c or "#000000").lstrip("#")
            return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)

        best, dist = -1, 1 << 30
        for tid, t in self._loaded(st):
            if tid in taken or (f["type"] and t["type"].lower() != f["type"].lower()):
                continue
            if not f["color"] or not t["color"]:
                d = 1 << 29
            else:
                d = sum((x - y) ** 2 for x, y in zip(rgb(t["color"]), rgb(f["color"])))
            if d < dist:
                best, dist = tid, d
        return best

    def _track_use(self, st: dict, active: bool) -> None:
        """Each filament of the print is used up in proportion to the progress (its grams from
        the file x progress), on the tray it is loaded in (matched by type + colour). Sampling
        the feeding tray at each progress step is wrong for multi-colour prints: the colours
        alternate within every layer, so whichever is feeding at the step gets all of it.
        Filaments without a matching tray: credited to the feeding tray as progress grows."""
        if not active or st.get("state") != "RUNNING" or not st.get("connected"):
            return
        u = self.use
        job = st.get("job", "")
        if u.get("job") != job or "map" not in u:
            self.use = u = self._new_use(job)
        p = float(st.get("progress") or 0)
        if not u.get("spools_start") and self.e.assignments:
            u["spools_start"] = self.e.usage.spools_now()
        if u["map"] is None:
            fil = self._print_filaments(job)
            if not fil:
                return  # nothing to base an estimate on (yet)
            taken: set = set()
            u["map"], u["free"] = [], 0.0
            for f in fil:
                tid = self._match_tray(st, f, taken)
                if tid >= 0:
                    taken.add(tid)
                    u["map"].append([tid, f["grams"]])
                else:
                    u["free"] += f["grams"]
            u["total"] = round(sum(f["grams"] for f in fil), 1)
            # Started before this app ran: the unmatched part already printed goes to the feeding tray.
            u["progress"] = p
            tray = st.get("tray_now", 255)
            if u["free"] > 0 and tray not in (255, -1):
                u["extra"][str(tray)] = round(u["free"] * p / 100, 2)
        else:
            if p < u["progress"]:
                u["progress"] = p  # progress went back (printer report glitch): just follow
                return
            if p == u["progress"]:
                return
            tray = st.get("tray_now", 255)
            if u["free"] > 0 and tray not in (255, -1):
                k = str(tray)
                u["extra"][k] = round(u["extra"].get(k, 0) + u["free"] * (p - u["progress"]) / 100, 2)
            u["progress"] = p
        by: dict[str, float] = {}
        for tid, grams in u["map"]:
            by[str(tid)] = by.get(str(tid), 0) + grams * min(100.0, p) / 100
        for k, g in u["extra"].items():
            by[k] = by.get(k, 0) + g
        u["by_tray"] = {k: round(v, 1) for k, v in by.items()}
        if time.monotonic() - self._use_saved > 60:
            self._save_use()
        self.e.bump()

    def _record_use(self, st: dict, job: str) -> None:
        """Print over: what it used from which spool, kept by this app (Usage)."""
        u = self.use
        if u.get("job") != job:
            return
        if st.get("state") == "FINISH" and u.get("map"):
            grams: dict[str, float] = {}
            for tid, g in u["map"]:
                grams[str(tid)] = grams.get(str(tid), 0) + g
            for k, g in (u.get("extra") or {}).items():
                grams[k] = grams.get(k, 0) + g
        else:
            grams = dict(u.get("by_tray") or {})  # stopped / failed: what was used until then
        self.e.usage.record(job, st.get("state", ""), grams, u.get("spools_start") or {})

    def used_by(self, tid: int) -> float:
        st = self.e.status or {}
        if st.get("state") not in ("PREPARE", "SLICING", "RUNNING", "PAUSE") or self.use.get("job") != st.get("job"):
            return 0.0
        return float(self.use["by_tray"].get(str(tid), 0.0))

    def use_view(self) -> dict | None:
        st = self.e.status or {}
        if st.get("state") not in ("PREPARE", "SLICING", "RUNNING", "PAUSE") or self.use.get("job") != st.get("job"):
            return None
        need: dict[str, float] = {}
        for tid, grams in self.use.get("map") or []:
            need[str(tid)] = round(need.get(str(tid), 0) + grams, 1)
        return {"total": self.use["total"], "progress": self.use["progress"], "by_tray": self.use["by_tray"], "need_by_tray": need}

    def _spool_for(self, tid: int) -> dict | None:
        if tid == 254:
            ams, tray = 255, 0
        elif tid >= 128:
            ams, tray = tid, 0
        else:
            ams, tray = tid // 4, tid % 4
        a = next((x for x in self.e.assignments if x["ams_id"] == ams and x["tray_id"] == tray), None)
        return a["spool"] if a else None

    def _loaded(self, st: dict) -> list[tuple[int, dict]]:
        out = []
        for unit in st.get("ams") or []:
            for slot, t in enumerate(unit["trays"]):
                if t and t["has_filament"]:
                    out.append((unit["id"] if unit["id"] >= 128 else unit["id"] * 4 + slot, t))
        ext = (st.get("ext") or [None])[0]
        if ext and ext["has_filament"]:
            out.append((254, ext))
        return out

    def low_trays(self, st: dict) -> list[dict]:
        """Loaded trays under 10 % (AMS estimate) or whose spool has less than 50 g left."""
        out = []
        for tid, t in self._loaded(st):
            sp = self._spool_for(tid)
            left = max(0.0, sp["label_weight"] - sp["weight_used"] - self.used_by(tid)) if sp and sp["label_weight"] > 0 else None
            low_pct = 0 <= t["remain"] <= LOW_REMAIN_PCT
            low_spool = left is not None and left < LOW_SPOOL_G
            if not low_pct and not low_spool:
                continue
            out.append(
                {
                    "id": tid,
                    "label": tray_label(tid),
                    "type": t["type"],
                    "sub_brands": t["sub_brands"],
                    "color": t["color"],
                    "remain": t["remain"],
                    "spool_left": round(left) if left is not None else None,
                    "active": st.get("tray_now") == tid,
                }
            )
        return out

    def runout(self, st: dict) -> dict | None:
        """Run-out forecast of the running print (single-filament prints only)."""
        a = self.archive
        if not st.get("connected") or st.get("state") not in ("PREPARE", "SLICING", "RUNNING", "PAUSE"):
            return None
        if not self.archive_for_job or not a or len(a["fil"]) != 1:
            return None
        total = a["fil"][0]["grams"] if a["fil"][0]["grams"] > 0 else a["grams"]
        tid = st.get("tray_now", 255)
        if total <= 0 or tid in (255, -1):
            return None
        sp = self._spool_for(tid)
        if not sp or sp["label_weight"] <= 0:
            return None
        left = sp["label_weight"] - sp["weight_used"]
        at_pct = 0.0 if left <= 0 else 100.0 * left / total
        p = float(st.get("progress") or 0)
        minutes = -1
        if at_pct < 100 and st.get("remaining_min", 0) > 0 and p < 100:
            minutes = 0 if at_pct <= p else int(st["remaining_min"] * (at_pct - p) / (100 - p))
        label = tray_label(tid)
        if at_pct >= 100:
            text = f"{label} has enough for this print ({left:.0f} g left, the print needs {total:.0f} g)."
        elif minutes == 0:
            text = f"{label} may run out any moment ({left:.0f} g left at the start, the print needs {total:.0f} g)."
        elif minutes > 0:
            text = (
                f"{label} runs out at ~{at_pct:.0f}% of this print, in ~{minutes // 60}h {minutes % 60:02d}m "
                f"({left:.0f} g left, needs {total:.0f} g)."
            )
        else:
            text = f"{label} runs out at ~{at_pct:.0f}% of this print ({left:.0f} g left, needs {total:.0f} g)."
        return {"tray": tid, "left": round(left), "total": round(total, 1), "at_pct": round(at_pct, 1), "minutes": minutes, "short": at_pct < 100, "text": text}

    def _check_runout(self, st: dict, active: bool) -> None:
        if not active:
            return
        r = self.runout(st)
        job = st.get("job", "")
        if r and r["short"] and self._runout_warned != job:
            self._runout_warned = job
            self.e.notify(False, r["text"])

    def _check_humidity(self, st: dict) -> None:
        """Warn when a unit stays above 40 % for 10 minutes (short spikes are ignored)."""
        now = time.monotonic()
        worst = -1
        seen = set()
        for u in st.get("ams") or []:
            seen.add(u["id"])
            if u["humidity"] <= HUMID_HIGH:
                self._humid_since.pop(u["id"], None)
                continue
            since = self._humid_since.setdefault(u["id"], now)
            if now - since >= HUMID_HOLD_S:
                worst = max(worst, u["humidity"])
        for k in list(self._humid_since):
            if k not in seen:
                del self._humid_since[k]
        if worst != self.humid_alarm:
            self.humid_alarm = worst
            self.e.bump()

    def _check_cloud_expiry(self) -> None:
        """Bambu Cloud login about to expire / expired: remind once a day."""
        now = time.monotonic()
        if now < self._next_cloud_check:
            return
        self._next_cloud_check = now + 60
        info = self.e.cloud.info()
        if info["expiry_warn"] and (not self._cloud_warned or now - self._cloud_warned > CLOUD_WARN_EVERY_S):
            self._cloud_warned = now
            self.e.notify(False, f"Bambu Cloud login: {info['expiry']}")

    def alerts(self) -> dict:
        st = self.e.status or {}
        if self.e.link != "online" or not st:
            return {"low": [], "runout": None, "humid": -1}
        return {"low": self.low_trays(st), "runout": self.runout(st), "humid": self.humid_alarm}

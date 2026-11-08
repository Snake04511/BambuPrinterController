"""Background engine: follows the printer (LAN MQTT), keeps the live snapshot, runs the panel's
background logic (src/app/Backend.cpp) and executes commands.

Runs in the backend process whether a browser is open or not.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import logging
import time
from collections import deque
from typing import Any, Callable

from . import cloud as cloudm
from . import settings
from .cloud import Cloud
from . import db
from . import inventory
from . import printlog
from . import amshist
from .timelapse import ROOT as _TL_ROOT
from . import maintenance as maint
from .db import DATA_DIR
from .printjob import PrintJob, tray_label
from .printer_hms import pause_info
from .stats import Stats
from .camera import Camera
from .timelapse import Timelapse
from .usage import Usage
from .push import Pusher
from .printer_mqtt import PrinterMqtt
from . import printer_mqtt as pm
from .printer_ftp import PrinterFTP
from . import obico_client as obm
from . import sdcard as sdm
from .sdcard import SdCard

log = logging.getLogger("poller")

ACTIVE_STATES = {"PREPARE", "SLICING", "RUNNING", "PAUSE"}
SPEED_NAMES = {1: "Silent", 2: "Standard", 3: "Sport", 4: "Ludicrous"}

STATUS_S = 2.0
AFTER_CMD_S = 0.6
INVENTORY_S = 30.0
TEMP_SAMPLE_S = 10.0
TEMP_HISTORY = 360  # 1 hour
PENDING_S = 20.0
END_LIGHT_S = 120.0
ARMED_L1_S = 15 * 60
TIMELAPSE_LIGHT_WINDOW_S = 5 * 60
COVER_RETRY_S = 60.0
COVER_TRIES = 4
COVER_KEEP = 300
COVER_DIR = DATA_DIR / "cache" / "covers"
COVER_DIR.mkdir(parents=True, exist_ok=True)
CLOUD_TASKS_S = 15 * 60
CLOUD_TASKS_RETRY_S = 120
START_TIMEOUT_S = 120
CLOUD_COVER_DIR = DATA_DIR / "cache" / "cloud"
CLOUD_COVER_DIR.mkdir(parents=True, exist_ok=True)

# pending field -> (status key, tolerance)
PENDING_FIELDS = {
    "nozzle_target": 0.5,
    "bed_target": 0.5,
    "chamber_target": 0.5,
    "light": 0.5,
    "speed_level": 0.5,
    "fan_part": 6,
    "fan_aux": 6,
    "fan_chamber": 6,
}


def is_active(state: str) -> bool:
    return state in ACTIVE_STATES


class Engine:
    def __init__(self) -> None:
        self.cloud = Cloud()
        self._listeners: set[Callable[[], None]] = set()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._cmd_lock = asyncio.Lock()
        self.version = 0
        self.boot_id = secrets.token_hex(4)  # changes on every app start (event counters restart at 0)
        self.last_start_at = 0  # unix time of the last print start this app saw
        self.sd = SdCard(self)
        self.job = PrintJob(self)
        self.stats = Stats(self)
        self.camera = Camera(self)
        self.timelapse = Timelapse(self)
        self.usage = Usage(self)
        self.push = Pusher(self)
        self.mqtt = PrinterMqtt(on_change=self._mqtt_changed)
        self.ftp = PrinterFTP(
            settings.get().get("printer_ip") or "192.168.1.190", lambda: settings.secret("printer_access_code")
        )
        self.obico_det = obm.ObicoDetector(
            lambda: obm.ObicoConfig.from_settings(settings.get(), settings.secret(obm.SECRET_ML_TOKEN)),
            self.camera.snapshot,
            on_event=self._obico_event,
        )
        self.reset_all()

    # ------------------------------------------------------------ state

    def reset_all(self) -> None:
        self.link = "not_configured"  # not_configured (no printer access set) / offline / online
        self.link_text = ""
        self.printer: dict | None = None  # id, name, model, serial, ip
        self.status: dict | None = None
        self.assignments: list[dict] = []
        self._inv_rev = -1
        self.maintenance: dict | None = None
        self.obico: dict | None = None
        self.cover: bytes | None = None
        self.cover_job = ""
        self._cover_tries = 0
        self._next_cover = 0.0
        self._cover_sd_seen = -1
        self._restore_cover()  # the last job's picture survives a restart
        self.notices: deque[dict] = deque(maxlen=20)
        self._notice_seq = 0
        self.busy = 0
        self.temp_hist: deque[list] = deque(maxlen=TEMP_HISTORY)
        self._next_temp = 0.0
        self._pending: dict[str, tuple[float, float]] = {}  # key -> (value, until)
        self._reset_timers()
        self._reset_print_state()
        self.print_starts = 0
        self.print_ends = 0
        self.print_pauses = 0
        self.last_end: dict | None = None
        self._backoff = STATUS_S
        self.cloud_tasks: list[dict] = []
        self.cloud_tasks_loaded = False
        self._next_cloud = time.monotonic() + 15
        self._task_details: dict[str, dict] = {}
        self.start_state = {"phase": "idle", "file": "", "message": ""}
        self._start_deadline = 0.0
        if hasattr(self, "sd"):
            self.sd.reset()
        if hasattr(self, "job"):
            self.job.reset()

    def _reset_timers(self) -> None:
        self._next = {k: 0.0 for k in ("status", "inventory", "maint")}

    def _reset_print_state(self) -> None:
        self._start_seen = False
        self._was_active = False
        self._last_job = ""
        self._prev_state = ""
        self._timelapse_light_done = False
        self._print_light_done = False
        self._print_light_opt = False
        self._obico_light_next = 0.0
        self.end_light_at = 0.0
        self.l1_speed = 0
        self._armed_l1 = 0
        self._armed_l1_at = 0.0
        self._l1_tries = 0
        self._l1_next_try = 0.0
        self._job_start = 0.0
        self._start_observed = False
        self._reset_layers()

    def _reset_layers(self) -> None:
        self._layer_seen = -1
        self._layer_seen_t = 0.0
        self._layer_first = -1
        self._layer_first_t = 0.0
        self.last_layer_sec = -1
        self.avg_layer_sec = -1

    def subscribe(self, fn: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(fn)
        return lambda: self._listeners.discard(fn)

    def bump(self) -> None:
        self.version += 1
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("listener failed")

    def notify(self, ok: bool, text: str) -> None:
        self._notice_seq += 1
        self.notices.append({"id": self._notice_seq, "ok": ok, "text": text, "t": time.time()})
        log.info("notice (%s): %s", "ok" if ok else "error", text)
        self.bump()

    # ------------------------------------------------------------ snapshot for the frontend

    def snapshot(self) -> dict[str, Any]:
        cfg = settings.get()
        now = time.time()
        return {
            "version": self.version,
            "link": self.link,
            "plate_seq": self.plate_seq_view(),
            "link_text": self.link_text,
            "printer": {k: self.printer.get(k) for k in ("id", "name", "model")} if self.printer else None,
            "status": self.status,
            "assignments": self._assignments_live(),
            "pause": pause_info(self.status),
            "boot_id": self.boot_id,
            "last_start_at": self.last_start_at,
            "maintenance": self.maintenance,
            "notifications": self.push.listing(),
            "notif_seen_id": cfg["notif_seen_id"],
            "obico": self.obico,
            "cover_job": self.cover_job if self.cover else "",
            "cover_version": self._cover_version,
            "busy": self.busy > 0,
            "layer_stats": {"avg_sec": self.avg_layer_sec, "last_sec": self.last_layer_sec},
            "l1_speed": self.l1_speed,
            "end_light_remaining": max(0, int(self.end_light_at - now)) if self.end_light_at else 0,
            "print_starts": self.print_starts,
            "print_ends": self.print_ends,
            "print_pauses": self.print_pauses,
            "last_end": self.last_end,
            "notices": list(self.notices),
            "cloud": self.cloud.info(),
            "settings": cfg,
            "start": self.start_state,
            "sd_version": self.sd.version,
            "cloud_tasks_loaded": self.cloud_tasks_loaded,
            "objects_count": self.job.objects_count(),
            "objects_version": self.job.objects_version,
            "alerts": self.job.alerts(),
            "filament_use": self.job.use_view(),
            "timelapse_id": self.timelapse.current_id(),
            "timelapse_version": self.timelapse.version,
            "usage_pending": self.usage.pending(),
            "usage_version": self.usage.version,
        }

    _cover_version = 0
    _cover_sd_seen = -1

    def _assignments_live(self) -> list[dict]:
        """assignments with spool.weight_used_live = booked + what the running print used so far."""
        live = self.live_used()
        if not live:
            return self.assignments
        out = []
        for a in self.assignments:
            sp = a.get("spool") or {}
            g = live.get(sp.get("id"))
            out.append({**a, "spool": {**sp, "weight_used_live": round((sp.get("weight_used") or 0) + g, 1)}} if g else a)
        return out

    def temp_history(self) -> list[list]:
        return list(self.temp_hist)

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self.apply_settings()
        self._task = asyncio.create_task(self._run(), name="poller")
        self.sd.start()
        asyncio.create_task(self.mqtt.start())
        self.obico_det.start(lambda: self.status)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.sd.stop()
        await self.camera.close()
        await self.push.close()
        await self.mqtt.stop()
        self.obico_det.stop()
        await self.obico_det.aclose()
        await self.cloud.close()

    def apply_settings(self) -> None:
        """Re-read the printer settings (after a settings change)."""
        self.apply_printer_settings()
        self._reset_timers()
        self._wake.set()
        self.bump()

    def apply_printer_settings(self) -> None:
        """Direct LAN links (MQTT / FTP / camera) after their settings changed."""
        cfg = settings.get()
        code = settings.secret("printer_access_code")
        self.printer = self._printer_from(cfg)
        self.mqtt.configure(cfg.get("printer_ip", ""), cfg.get("printer_serial", ""), code)
        self.ftp.host = cfg.get("printer_ip") or self.ftp.host
        self.camera.restart()
        self.kick()
        self.bump()

    def kick(self, delay: float = 0.0) -> None:
        """Poll the status again soon (after a command)."""
        self._next["status"] = time.monotonic() + delay
        self._wake.set()

    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("tick failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass

    def _due(self, key: str) -> bool:
        return time.monotonic() >= self._next[key]

    def _later(self, key: str, seconds: float) -> None:
        self._next[key] = time.monotonic() + seconds

    def _set_link(self, link: str, text: str = "") -> None:
        if (link, text) != (self.link, self.link_text):
            self.link, self.link_text = link, text
            self.bump()

    async def _tick(self) -> None:
        cfg = settings.get()
        if not self.mqtt.configured:
            self._set_link("not_configured", "Set the printer's IP, serial number and access code in Settings")
            return
        if self._due("status"):
            await self._poll_status()
            self.push.tick(self.status, self.link == "online")
        if self.link != "online":
            return

        st = self.status or {}
        active = is_active(st.get("state", "")) and st.get("connected")

        if self.cloud.logged_in and self.printer.get("serial") and time.monotonic() >= self._next_cloud:
            await self.fetch_cloud_tasks()
        await self._check_l1_speed(st)
        await self._check_end_light(st)
        await self._poll_cover(st)
        self.camera.keep_open(bool(active))
        await self.job.tick(st, bool(active))

        rev = inventory.revision()
        if self._due("inventory") or rev != self._inv_rev:
            self._inv_rev = rev
            self._later("inventory", INVENTORY_S)
            a = inventory.assignments()
            if a != self.assignments:
                self.assignments = a
                self.bump()
        if self._due("maint"):
            self._later("maint", 30)
            m = maint.view()
            if m != self.maintenance:
                self.maintenance = m
                self.bump()
        if cfg.get("obico_enabled"):
            o = self.obico_det.status()
            if o != self.obico:
                self.obico = o
                self.job.obico_sample(o, self.status)
                self.bump()
        elif self.obico is not None:
            self.obico = None
            self.bump()

    @property
    def _pid(self) -> int:
        return int(self.printer["id"]) if self.printer else -1

    @property
    def printer_id(self) -> int:
        return self._pid

    # ------------------------------------------------------------ own print log (History)

    def _log_current(self) -> dict | None:
        cur = db.kv_get("printlog_current") or {}
        st = self.status or {}
        if not cur.get("id") or cur.get("job") != st.get("job"):
            return None
        return printlog.get(cur["id"])

    def _log_start(self, st: dict) -> None:
        # A previous print that never reached FINISH / FAILED (e.g. the printer went back to
        # idle during preparation): close its entry instead of leaving it "printing" forever.
        for row in printlog.page(0, 5):
            if row.get("status") == "printing":
                printlog.finish(row["id"], failed=True, stopped=True, actual_sec=0, used=[], layer=-1,
                                reason="Did not finish (another print started)")
        job = st.get("job") or ""
        plate = None
        jf = self.sd.job_file(job)
        if jf and jf[2]:
            plates = jf[2].get("plates") or []
            plate = self.sd.job_plate(jf[2], []) or (plates[0] if len(plates) == 1 else None)
        est = st.get("remaining_min", -1)
        row = printlog.start(job, jf[0]["name"] if jf else job, est * 60 if est and est > 0 else -1, plate, st.get("total_layers") or 0)
        db.kv_set("printlog_current", {"id": row["id"], "job": job})

    def _is_resume(self, st: dict) -> bool:
        """A 'start' of the print this app already follows (same job, well under way, its log
        entry / recording still open)?"""
        if float(st.get("progress") or 0) < 2:
            return False
        cur = db.kv_get("printlog_current") or {}
        if cur.get("job") == st.get("job"):
            row = printlog.get(cur["id"]) if cur.get("id") else None
            if row and row.get("status") == "printing":
                return True
        tl = self.timelapse
        return any(
            (m := tl._load(d)) and m.get("job") == st.get("job") and not m.get("end")
            for d in sorted(p for p in _TL_ROOT.iterdir() if p.is_dir())[-3:]
        )

    def _log_elapsed_min(self) -> int:
        """Print time from the log entry's start (the app was restarted during the print)."""
        cur = db.kv_get("printlog_current") or {}
        row = printlog.get(cur["id"]) if cur.get("id") else None
        if not row or not row.get("started_at"):
            return -1
        from datetime import datetime, timezone

        try:
            t0 = datetime.fromisoformat(row["started_at"]).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return -1
        return int((time.time() - t0) / 60)

    def _log_end(self, st: dict, end: dict) -> None:
        cur = db.kv_get("printlog_current") or {}
        if not cur.get("id") or cur.get("job") != end.get("job"):
            return
        rec = self.usage.items[-1] if self.usage.items and self.usage.items[-1].get("job") == end["job"] else None
        hms = "; ".join(h["text"] for h in st.get("hms") or [] if h.get("text"))[:200]
        stopped = time.monotonic() < self.push._user_stop_until
        row = printlog.finish(
            cur["id"], failed=end.get("failed", False), stopped=stopped,
            actual_sec=end["duration_min"] * 60 if end.get("duration_min", -1) >= 0 else 0,
            used=(rec or {}).get("items", []), layer=end.get("layer_reached", -1), reason=hms,
        )
        if row:
            self._log_cost(row)
        if row and self.cover and self.cover_job == end["job"]:
            from .stats import THUMB_DIR

            (THUMB_DIR / f"{row['id']}.png").write_bytes(self.cover)
        if row:
            end["archive_id"] = row["id"]
            self.usage.set_archive(end["job"], row["id"])

    def _log_cost(self, row: dict) -> None:
        """Cost of the print: filament (spool price / kg x grams used) + electricity."""
        cfg = settings.get()
        fil = 0.0
        for u in row.get("used") or []:
            sp = inventory.get_spool(u["spool_id"]) if u.get("spool_id") is not None else None
            if sp and sp.get("cost_per_kg"):
                fil += (u.get("grams") or 0) * sp["cost_per_kg"] / 1000
        kwh = (cfg.get("power_watts") or 0) * (row.get("actual_time_seconds") or 0) / 3600 / 1000
        energy = kwh * float(cfg.get("kwh_price") or 0)
        printlog.update(row["id"], cost=round(fil + energy, 2), cost_filament=round(fil, 2), cost_energy=round(energy, 2),
                        energy_kwh=round(kwh, 3))

    # ------------------------------------------------------------ drying reminder

    def _wet_tick(self, st: dict) -> None:
        """Every AMS sample (5 min): a moisture-sensitive spool in a humid unit collects time."""
        cfg = settings.get()
        limit = cfg.get("dry_humidity", 40)
        mats = [m.strip().upper() for m in str(cfg.get("dry_materials") or "").split(",") if m.strip()]
        hum = {u["id"]: u.get("humidity", -1) for u in st.get("ams") or []}
        wet = db.kv_get("spool_wet") or {}
        changed = False
        for a in self.assignments:
            sp = a["spool"]
            h = hum.get(a["ams_id"], -1)
            if h is None or h < limit or not any(str(sp.get("material", "")).upper().startswith(m) for m in mats):
                continue
            w = wet.setdefault(str(sp["id"]), {"min": 0.0, "dried": 0, "warned": False})
            w["min"] += amshist.SAMPLE_S / 60
            changed = True
            if not w["warned"] and w["min"] / 60 >= cfg.get("dry_hours", 24):
                w["warned"] = True
                from .printjob import tray_label

                tid = 254 if a["ams_id"] == 255 else a["ams_id"] if a["ams_id"] >= 128 else a["ams_id"] * 4 + a["tray_id"]
                name = " ".join(x for x in (sp.get("brand"), sp.get("material"), sp.get("subtype"), sp.get("color_name")) if x)
                self.push.spool_wet(name, w["min"] / 60, h, tray_label(tid), sp.get("color", ""), sp["id"])
        if changed:
            db.kv_set("spool_wet", wet)

    def spool_dried(self, sid: int) -> dict:
        wet = db.kv_get("spool_wet") or {}
        wet[str(int(sid))] = {"min": 0.0, "dried": int(time.time()), "warned": False}
        db.kv_set("spool_wet", wet)
        self.bump()
        return {"ok": True, "message": "Marked as dried"}

    async def restart_camera(self) -> dict:
        """Camera gives no picture (the printer's camera service hangs now and then): turn the
        printer's camera off and on over MQTT, then connect again and wait for a frame."""
        if not self.mqtt.connected:
            return {"ok": False, "message": "No MQTT link to the printer (Settings > Status straight from the printer)"}
        r = self.mqtt.set_ipcam(False)
        if not r.ok:
            return {"ok": False, "message": r.message}
        await asyncio.sleep(5)
        self.mqtt.set_ipcam(True)
        await asyncio.sleep(4)
        self.mqtt.request_pushall()
        self.camera.restart()
        img, err = await self.camera.snapshot()
        ok = bool(img)
        state = self.mqtt.details().get("ipcam_record")
        msg = "Camera is back" if ok else f"Still no picture ({err or 'no frame'}) - restart the printer if it stays like this"
        log.info("camera restart over MQTT: %s (ipcam_record=%s)", "ok" if ok else "no picture", state)
        self.notify(ok, msg)
        return {"ok": ok, "message": msg, "ipcam_record": state}

    def live_used(self) -> dict[int, float]:
        """Spool id -> grams the running print has used from it so far (not booked yet)."""
        out: dict[int, float] = {}
        for a in self.assignments:
            ams, tray = a["ams_id"], a["tray_id"]
            tid = 254 if ams == 255 else ams if ams >= 128 else ams * 4 + tray
            g = self.job.used_by(tid)
            if g > 0:
                out[a["spool"]["id"]] = round(g, 1)
        return out

    def log_archive(self) -> dict | None:
        """The running / last print in printjob's archive shape (own History)."""
        row = self._log_current()
        if row is None:
            cur = db.kv_get("printlog_current") or {}
            row = printlog.get(cur["id"]) if cur.get("id") else None
        if row is None:
            return None
        return {
            "id": row["id"],
            "name": row.get("print_name") or "",
            "filename": row.get("filename") or "",
            "status": row.get("status") or "",
            "actual_sec": int(row.get("actual_time_seconds") or 0),
            "grams": row.get("filament_used_grams") or 0.0,
            "fil": [{**f, "where": ""} for f in row.get("filaments") or []],
        }

    def plate_index(self) -> int | None:
        """Plate of the running job: the printer's gcode_file (".../Metadata/plate_3.gcode"),
        else the multi-plate run or the print log entry this app started."""
        import re

        if self.mqtt.connected:
            m = re.search(r"plate_(\d+)\.gcode", self.mqtt.details().get("gcode_file") or "")
            if m:
                return int(m.group(1))
        q = db.kv_get("plate_seq")
        if q and q.get("state") == "printing" and q.get("current"):
            return int(q["current"])
        row = self._log_current()
        return int(row["plate_id"]) if row and row.get("plate_id") else None

    @staticmethod
    def _printer_from(cfg: dict) -> dict:
        """The printer from Settings (id 1: the panel API's /printers/{id})."""
        return {
            "id": 1,
            "name": cfg.get("printer_name") or "P1S",
            "model": "P1S",
            "serial": cfg.get("printer_serial", ""),
            "ip": cfg.get("printer_ip", ""),
        }

    async def _obico_event(self, ev) -> None:
        """Own Obico verdict. Pushes go out from push.py; here only the 'pause' action."""
        log.warning("Obico %s on '%s' (score %.2f)", ev.kind, ev.job, ev.score)
        if ev.kind == "failure" and ev.action == "pause":
            st = self.status or {}
            if st.get("state") == "RUNNING" and st.get("job") == ev.job:
                r = await self.command("pause", {})
                self.notify(r.get("ok", False), "Obico: failure detected - print paused" if r.get("ok") else f"Obico: pause failed: {r.get('message')}")

    def _mqtt_changed(self) -> None:
        """A report from the printer: process it soon (at most twice a second)."""
        if self.mqtt.connected:
            self._next["status"] = min(self._next["status"], time.monotonic() + 0.5)
            self._wake.set()
        else:
            self.bump()  # link state shown in Settings

    def _cloud_gcode(self, name: str, args: dict) -> str | None:
        """Heater / fan G-code to send through Bambu Cloud (None: not such a command or no cloud)."""
        if not self.cloud.logged_in or not settings.get().get("printer_serial"):
            return None
        if name == "set_temp":
            t = int(args["target"])
            return {"nozzle": pm.gcode_nozzle, "bed": pm.gcode_bed, "chamber": pm.gcode_chamber}[args["which"]](t)
        if name == "fan":
            return pm.gcode_fan(args["fan"], int(args["speed"]))
        return None

    def _mqtt_cmd(self, name: str, args: dict):
        """The MQTT call of an (already validated) printer command."""
        m = self.mqtt
        if name == "set_temp":
            fn = {"nozzle": m.set_nozzle_temp, "bed": m.set_bed_temp, "chamber": m.set_chamber_temp}[args["which"]]
            return lambda: fn(int(args["target"]))
        if name == "light":
            return lambda: m.set_light(bool(args["on"]))
        if name == "pause":
            return m.pause
        if name == "resume":
            return m.resume
        if name == "stop":
            return m.stop_print
        if name == "speed":
            return lambda: m.set_speed_level(int(args["mode"]))
        if name == "fan":
            return lambda: m.set_fan(args["fan"], int(args["speed"]))
        if name == "home":
            return m.home
        if name == "jog_xy":
            return lambda: m.jog_xy(float(args.get("x", 0)), float(args.get("y", 0)))
        if name == "jog_z":
            return lambda: m.jog_z(float(args["distance"]))
        if name == "extrude":
            return lambda: m.extrude(float(args["distance"]))
        if name == "clear_hms":
            return m.clear_hms
        if name == "refresh":
            return m.request_pushall
        if name == "ams_load":
            return lambda: m.ams_load(int(args["tray"]))
        if name == "ams_unload":
            return m.ams_unload
        if name == "slot_configure":
            ams, tray = _slot_args(args)
            return lambda: m.set_slot(
                ams, tray, info_idx=str(args.get("info_idx") or "GFL99"), tray_type=str(args.get("type") or "PLA"),
                sub_brands=str(args.get("sub_brands") or ""), color=str(args.get("color", "")),
                temp_min=int(args["temp_min"]), temp_max=int(args["temp_max"]),
            )
        if name == "slot_reset":
            ams, tray = _slot_args(args)
            return lambda: m.reset_slot(ams, tray)
        if name == "slot_rfid":
            ams, tray = _slot_args(args)
            return lambda: m.refresh_rfid(ams, tray)
        raise ValueError(f"no printer command: {name}")

    @property
    def ftp_direct(self) -> bool:
        """SD card access is set up (printer IP + access code)."""
        return bool(settings.get().get("printer_ip") and settings.secret("printer_access_code"))

    async def _poll_status(self) -> None:
        self._later("status", STATUS_S)
        if not self.mqtt.connected:
            if self.start_state["phase"] == "waiting" and time.monotonic() > self._start_deadline:
                self._set_start("failed", "Could not confirm the print start (printer not connected)")
            self._set_link("offline", f"Printer not connected: {self.mqtt.last_error or self.mqtt.link}")
            return
        st = self.mqtt.status()
        if st.get("state") in ("", "unknown", None):
            # Connected, but the printer's first full report (pushall) has not arrived yet:
            # an empty state here would look like "idle" and the real one a new print start.
            return
        await self._process_status(st)

    async def _process_status(self, st: dict) -> None:
        if amshist.record(st):
            self._wet_tick(st)
        self._apply_pending(st)
        self._sample_temps(st)
        if st["connected"] and is_active(st["state"]):
            self._track_layers(st)
        await self._watch_print(st)
        self._watch_start(st)
        changed = st != self.status
        self.status = st
        if self.link != "online":
            self._set_link("online")
        elif changed:
            self.bump()

    # ------------------------------------------------------------ pending values

    def set_pending(self, key: str, value: float) -> None:
        self._pending[key] = (value, time.monotonic() + PENDING_S)
        if self.status is not None:
            self.status[key] = bool(value) if key == "light" else value
            self.bump()

    def _apply_pending(self, st: dict) -> None:
        now = time.monotonic()
        for key, (value, until) in list(self._pending.items()):
            polled = st.get(key)
            polled_f = float(polled) if isinstance(polled, (int, float)) else -999.0
            if now >= until or abs(polled_f - value) <= PENDING_FIELDS[key]:
                del self._pending[key]
                continue
            st[key] = bool(value) if key == "light" else value

    # ------------------------------------------------------------ temperature history / layers

    def _sample_temps(self, st: dict) -> None:
        now = time.monotonic()
        if not st["connected"] or now < self._next_temp:
            return
        self._next_temp = now + TEMP_SAMPLE_S
        self.temp_hist.append(
            [
                int(time.time()),
                round(st["nozzle"], 1),
                round(st["nozzle_target"], 1),
                round(st["bed"], 1),
                round(st["bed_target"], 1),
                round(st["chamber"], 1) if st["has_chamber"] else None,
            ]
        )

    def _track_layers(self, st: dict) -> None:
        layer = st["layer"]
        if layer <= 0 or layer == self._layer_seen:
            return
        now = time.monotonic()
        if self._layer_seen > 0 and layer == self._layer_seen + 1:
            self.last_layer_sec = int(now - self._layer_seen_t)
            if self._layer_first < 0:
                self._layer_first = self._layer_seen
                self._layer_first_t = self._layer_seen_t
            self.avg_layer_sec = int((now - self._layer_first_t) / (layer - self._layer_first))
        self._layer_seen = layer
        self._layer_seen_t = now

    # ------------------------------------------------------------ print lifecycle (Backend.cpp watchPrintStart)

    async def _watch_print(self, st: dict) -> None:
        active = st["connected"] and is_active(st["state"])
        cfg = settings.get()
        if not self._start_seen and st.get("state") in ("", "unknown", None):
            return  # no real status yet: no baseline from it
        if not self._start_seen:  # first status: baseline, not an event
            self._start_seen = True
            self._was_active = active
            self._last_job = st["job"]
            self._prev_state = st["state"]
            self._timelapse_light_done = True
            self._print_light_done = True
            self._print_light_opt = cfg["light_on_print"]
            self.job.on_boot(st, active)
            return
        if not st["connected"]:
            return  # keep the last known state across printer disconnects
        if st["state"] == "PAUSE" and self._prev_state != "PAUSE" and self._was_active:
            self.print_pauses += 1
            log.info("print paused: %s", st["job"])
            self.push.on_pause(st)
        self._prev_state = st["state"]

        if self._was_active and not active and st["state"] in ("FINISH", "FAILED"):
            self.last_end = {
                "failed": st["state"] == "FAILED",
                "job": st["job"] or self._last_job,
                "duration_min": int((time.monotonic() - self._job_start) / 60) if self._start_observed else self._log_elapsed_min(),
                "layers": st["total_layers"] if st["total_layers"] > 0 else st["layer"],
                "layer_reached": st["layer"],
                "avg_layer_sec": self.avg_layer_sec,
                "ended_at": int(time.time()),
                "started_at": self.last_start_at,
                # grams per tray the app counted during the print (live estimate, final values in "filament")
                "used_by_tray": [{"tray": int(t), "label": tray_label(int(t)), "grams": round(g, 1)}
                                 for t, g in sorted((self.job.use.get("by_tray") or {}).items(), key=lambda x: int(x[0])) if g > 0]
                if self.job.use.get("job") == (st["job"] or self._last_job) else [],
            }
            self.job.on_end(st, self.last_end)
            self._log_end(st, self.last_end)
            self._plate_seq_end(st, self.last_end)
            if self.last_end["duration_min"] > 0:
                maint.add_print(self.last_end["duration_min"] * 60)
                self._later("maint", 0)
            self.push.on_end(st, self.last_end)
            if cfg["light_off_on_end"]:
                self.restart_end_light(True)
            self.print_ends += 1
            self._next_cloud = time.monotonic() + 20  # the finished job shows up in the cloud history
            self._start_observed = False
            log.info("print ended (%s): %s", st["state"], st["job"])

        if active and (not self._was_active or st["job"] != self._last_job) and self._is_resume(st):
            # Not a new print: the one this app already follows (e.g. a status glitch after a
            # restart). Carry on with its recording, log entry and filament count.
            self._last_job = st["job"]
            if not self.last_start_at:
                self.last_start_at = int(time.time() - self._log_elapsed_min() * 60)
            self.job.on_boot(st, True)
            log.info("print continues (not a new start): %s", st["job"])
        elif active and (not self._was_active or st["job"] != self._last_job):
            self.print_starts += 1
            self.last_start_at = int(time.time())
            self._last_job = st["job"]
            self._timelapse_light_done = False
            self._print_light_done = False
            self.restart_end_light(False)
            armed = self._armed_l1
            self._armed_l1 = 0
            self.l1_speed = armed if armed and time.monotonic() - self._armed_l1_at < ARMED_L1_S else 0
            self._l1_tries = 0
            self._job_start = time.monotonic()
            self._start_observed = True
            self._reset_layers()
            self.job.on_start(st["job"])
            self._log_start(st)
            self.push.on_start(st)
            log.info("print started: %s", st["job"])
        self._was_active = active

        # Timelapse needs light: once at the start of the job (the flag may show up a
        # little after the state change). If the user turns it off later, it stays off.
        if (
            active
            and st["timelapse"]
            and not self._timelapse_light_done
            and time.monotonic() - self._job_start < TIMELAPSE_LIGHT_WINDOW_S
        ):
            self._timelapse_light_done = True
            if not st["light"] and self.mqtt.set_light(True).ok:
                self.set_pending("light", 1)
                st["light"] = True
                self.notify(True, "Timelapse print: light switched on")

        # "Light on during every print": once per job, or right away when switched on mid-print.
        opt = cfg["light_on_print"]
        if opt and not self._print_light_opt:
            self._print_light_done = False
        self._print_light_opt = opt
        if active and opt and not self._print_light_done:
            self._print_light_done = True
            if not st["light"] and self.mqtt.set_light(True).ok:
                self.set_pending("light", 1)
                st["light"] = True

        # Obico watching: the camera needs light the whole print. On at the start and back on
        # (at most every 2 min) when something switched it off meanwhile.
        o = self.obico or {}
        if (
            active
            and cfg.get("light_with_obico", True)
            and o.get("enabled")
            and o.get("running")
            and not st["light"]
            and time.monotonic() >= self._obico_light_next
        ):
            self._obico_light_next = time.monotonic() + 120
            if self.mqtt.set_light(True).ok:
                self.set_pending("light", 1)
                st["light"] = True
                self.notify(True, "Obico is watching: light switched on")

    async def _check_l1_speed(self, st: dict) -> None:
        level = self.l1_speed
        if not level:
            return
        if not is_active(st.get("state", "")):
            self.l1_speed = 0  # print over before the first layer was done
            self.bump()
            return
        if st.get("state") != "RUNNING" or st.get("layer", -1) < 2:
            return
        if time.monotonic() < self._l1_next_try:
            return
        self._l1_next_try = time.monotonic() + 3.0
        r = self.mqtt.set_speed_level(level)
        self._l1_tries += 1
        if r.ok or self._l1_tries >= 3:
            self.l1_speed = 0
        if r.ok:
            self.set_pending("speed_level", level)
            self.notify(True, f"First layer done: {SPEED_NAMES.get(level, 'new')} speed")
        elif not self.l1_speed:
            self.notify(False, f"Speed change after layer 1 failed: {r.message}")

    def set_first_layer_speed(self, level: int) -> None:
        self.l1_speed = level
        self._l1_tries = 0
        self.bump()

    def arm_next_print_speed(self, level: int) -> None:
        self._armed_l1 = level
        self._armed_l1_at = time.monotonic()

    def restart_end_light(self, on: bool) -> None:
        self.end_light_at = time.time() + END_LIGHT_S if on else 0.0
        self.bump()

    async def _check_end_light(self, st: dict) -> None:
        if not self.end_light_at or time.time() < self.end_light_at:
            return
        self.end_light_at = 0.0
        self.bump()
        if not st.get("light") or is_active(st.get("state", "")):
            return
        if self.mqtt.set_light(False).ok:
            self.set_pending("light", 0)

    # ------------------------------------------------------------ cover

    # Covers are kept on disk by job name: after a restart the running / last job has its
    # picture at once; otherwise it comes from the SD copy or the Bambu Cloud task.

    @staticmethod
    def _cover_file(job: str):
        return COVER_DIR / (hashlib.sha1(job.encode()).hexdigest()[:24] + ".png")

    def _restore_cover(self) -> None:
        job = db.kv_get("cover_job", "") or ""
        f = self._cover_file(job) if job else None
        if f and f.is_file():
            self.cover_job, self.cover = job, f.read_bytes()
            self._cover_version += 1

    def _set_cover(self, job: str, png: bytes, source: str) -> None:
        if job != self.cover_job:
            return
        self.cover = png
        self._cover_version += 1
        try:
            tmp = self._cover_file(job).with_suffix(".tmp")
            tmp.write_bytes(png)
            tmp.replace(self._cover_file(job))
            db.kv_set("cover_job", job)
            old = sorted(COVER_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)
            for p in old[:-COVER_KEEP]:
                p.unlink(missing_ok=True)
        except OSError as e:
            log.warning("cover not saved: %s", e)
        log.info("cover of '%s' from %s", job, source)
        self.bump()

    async def _poll_cover(self, st: dict) -> None:
        has_job = (
            st.get("connected")
            and st.get("job")
            and st.get("state") in ("RUNNING", "PAUSE", "PREPARE", "FINISH", "FAILED")
        )
        if not has_job:
            return  # keep the last cover (finish screen / HOME "last print")
        job = st["job"]
        if job != self.cover_job:
            self.cover_job = job
            self.cover = None
            self._cover_tries = 0
            self._cover_sd_seen = -1
            self._cover_version += 1
            f = self._cover_file(job)
            if f.is_file():  # seen before (restart, reprint): no request at all
                self.cover = f.read_bytes()
                db.kv_set("cover_job", job)
                self.bump()
                return
            self._next_cover = 0.0
            self.bump()
        if self.cover is not None:
            return
        # Our SD copy of the file may show up any time (listing after start-up, file read later).
        if self.sd.version != self._cover_sd_seen:
            self._cover_sd_seen = self.sd.version
            await self._cover_fallback(job, cloud=self._cover_tries >= COVER_TRIES)
            if self.cover is not None:
                return
        if self._cover_tries >= COVER_TRIES or time.monotonic() < self._next_cover:
            return
        self._cover_tries += 1
        self._next_cover = time.monotonic() + COVER_RETRY_S
        await self._cover_fallback(job)

    async def _cover_fallback(self, job: str, cloud: bool = True) -> None:
        """The plate image of the
        SD file of the same name (cached by this app), else the Bambu Cloud task's cover."""
        jf = self.sd.job_file(job)
        if jf and jf[2]:
            _, key, detail = jf
            plate = self.sd.job_plate(detail, [o["id"] for o in self.job.objects["items"]]) or (detail.get("plates") or [None])[0]
            png = self.sd.png(key, plate["index"]) if plate else None
            if png:
                return self._set_cover(job, png, "the SD file")
        if cloud:
            task = next((t for t in self.cloud_tasks if sdm.same_job_name(t.get("title", ""), job)), None)
            png = await self.cloud_cover(task["id"]) if task else None
            if png:
                self._set_cover(job, png, "Bambu Cloud")

    # ------------------------------------------------------------ commands

    async def command(self, name: str, args: dict) -> dict:
        """Runs one control command. Returns {"ok", "message"}; also posts a notice."""
        def fail(msg: str) -> dict:
            self.notify(False, msg)
            return {"ok": False, "message": msg}

        if name.startswith("spool_") and name != "spool_dried":
            return self._spool_local(name, args)
        if name == "spool_dried":
            r = self.spool_dried(int(args.get("id", -1)))
            self.notify(True, "Spool marked as dried")
            return r
        if name == "maintenance_done":
            r = maint.done(int(args.get("id", -1)))
            self._later("maint", 0)
            self.notify(r["ok"], "Maintenance marked as done" if r["ok"] else r["message"])
            return r
        st = self.status or {}
        pending: tuple[str, float] | None = None
        try:
            if name == "set_first_layer_speed":
                level = int(args["level"])
                if level not in (0, 2, 3, 4):
                    raise ValueError("level must be 0, 2, 3 or 4")
                self.set_first_layer_speed(level)
                return {"ok": True, "message": "Speed switch cancelled" if not level else "Speed switch armed"}
            if name == "refresh_inventory":
                self._next["inventory"] = 0.0
                self._wake.set()
                return {"ok": True, "message": ""}
            if name == "restart_end_light":
                self.restart_end_light(bool(args.get("on", True)))
                return {"ok": True, "message": ""}
            if name == "clear_plate":
                q = db.kv_get("plate_seq")
                if q and q.get("state") in ("waiting", "paused"):
                    return await self.plate_seq_command("next")
                self.mqtt.awaiting_plate_clear = False
                if self.status:
                    self.status["awaiting_plate_clear"] = False
                self.bump()
                return {"ok": True, "message": "Plate marked as cleared"}
            if not self.mqtt.connected or not st.get("connected"):
                return fail("Printer is offline")
            if name == "set_temp":
                which = args["which"]
                target = int(args["target"])
                limits = {"nozzle": 320, "bed": 140, "chamber": 65}
                if which not in limits or not 0 <= target <= limits[which]:
                    raise ValueError("target out of range")
                what = f"{which.capitalize()} target {target}°C"
                pending = (f"{which}_target", float(target))
            elif name == "light":
                on = bool(args["on"])
                what = f"Light {'on' if on else 'off'}"
                o = self.obico or {}
                if not on and is_active(st.get("state", "")) and settings.get().get("light_with_obico", True) and o.get("enabled") and o.get("running"):
                    what = "Light off - Obico is watching, it comes back on in 2 min (Settings > Features)"
                    self._obico_light_next = time.monotonic() + 120
                pending = ("light", 1.0 if on else 0.0)
            elif name in ("pause", "resume", "stop"):
                what = {"pause": "Print paused", "resume": "Print resumed", "stop": "Print stopped"}[name]
                if name == "stop":
                    self.push.user_stopped()
            elif name == "speed":
                mode = int(args["mode"])
                if mode not in SPEED_NAMES:
                    raise ValueError("mode must be 1..4")
                self.l1_speed = 0  # a speed picked by hand cancels the pending switch
                what = f"Speed mode {mode}"
                pending = ("speed_level", float(mode))
            elif name == "fan":
                fan = args["fan"]
                speed = int(args["speed"])
                if fan not in ("part", "aux", "chamber") or not 0 <= speed <= 100:
                    raise ValueError("bad fan / speed")
                what = f"{fan} fan {speed}%"
                pending = (f"fan_{fan}", float(speed))
            elif name in ("home", "jog_xy", "jog_z", "extrude"):
                # Same rule as the panel's Move page: no movement while a print runs.
                if is_active(st.get("state", "")):
                    return fail("Movement is disabled while a print is running.")
                if name == "home":
                    what = "Homing"  # always all axes (partial homing skips the toolhead park)
                elif name == "jog_xy":
                    x, y = float(args.get("x", 0)), float(args.get("y", 0))
                    if abs(x) > 200 or abs(y) > 200:
                        raise ValueError("max 200 mm")
                    what = f"Move X{x:+.1f} Y{y:+.1f}"
                elif name == "jog_z":
                    d = float(args["distance"])
                    if abs(d) > 200:
                        raise ValueError("max 200 mm")
                    what = f"Move bed {d:+.1f} mm"
                else:
                    d = float(args["distance"])
                    if abs(d) > 100:
                        raise ValueError("max 100 mm")
                    if st.get("nozzle", 0) < 170:
                        return fail("Nozzle too cold to extrude (min 170°C)")
                    what = f"Extruder {d:+.0f} mm"
            elif name == "clear_hms":
                what = "Errors cleared"
            elif name == "refresh":
                what = "Status refreshed"
            elif name in ("ams_load", "ams_unload", "slot_configure", "slot_reset", "slot_rfid"):
                # Same rule as the panel's AMS page: printer-side slot actions are off while printing.
                if is_active(st.get("state", "")):
                    return fail("Not possible while a print is running.")
                if name == "ams_load":
                    tray = int(args["tray"])
                    if not (0 <= tray <= 15 or 128 <= tray <= 135 or tray == 254):
                        raise ValueError("bad tray")
                    what = "Loading filament"
                elif name == "ams_unload":
                    what = "Unloading filament"
                else:
                    ams, tray = _slot_args(args)
                    if name == "slot_configure":
                        color = str(args.get("color", "")).lstrip("#").upper()
                        tmin, tmax = int(args["temp_min"]), int(args["temp_max"])
                        if len(color) != 6 or any(c not in "0123456789ABCDEF" for c in color):
                            raise ValueError("bad colour")
                        if not (150 <= tmin <= tmax <= 350):
                            raise ValueError("bad temperatures")
                        args = {**args, "color": color}
                        what = "Slot updated"
                    elif ams == 255:
                        raise ValueError("not for the external spool")
                    elif name == "slot_reset":
                        what = "Slot reset"
                    else:
                        what = "Re-reading RFID"
            else:
                return fail(f"Unknown command: {name}")
        except (KeyError, ValueError, TypeError) as e:
            return fail(f"Bad request: {e}")

        mq = self._mqtt_cmd(name, args)
        cloud_gcode = self._cloud_gcode(name, args)
        self.busy += 1
        self.bump()
        try:
            async with self._cmd_lock:
                if cloud_gcode is not None:
                    # The P1S ignores heater / fan G-code from LAN MQTT; Bambu Handy's route works.
                    ok, msg = await cloudm.send_gcode(self.cloud, settings.get().get("printer_serial", ""), cloud_gcode)
                    r = pm.SendResult(ok, msg or what)
                    log.info("command %s sent through Bambu Cloud", name)
                else:
                    r = mq()  # straight to the printer (LAN MQTT)
                    log.info("command %s sent over MQTT", name)
        finally:
            self.busy -= 1
        log.info("command %s -> %s", what, "OK" if r.ok else r.message)
        if r.ok:
            if pending:
                self.set_pending(*pending)
            if name == "slot_configure":
                self._later("inventory", AFTER_CMD_S)
            self.notify(True, what)
        else:
            self.notify(False, f"{what} failed: {r.message}")
        self.kick(AFTER_CMD_S)
        self.bump()
        return {"ok": r.ok, "message": what if r.ok else r.message}

    def _spool_local(self, name: str, args: dict) -> dict:
        """Spool commands against this app's own inventory (Settings > own inventory)."""
        try:
            if name == "spool_patch":
                r, what = inventory.patch_spool(args.get("id"), {k: v for k, v in args.items() if k != "id"}), "Spool saved"
            elif name == "spool_assign":
                ams, tray = _slot_args(args)
                r, what = inventory.assign(args.get("spool_id"), ams, tray), "Spool assigned"
            elif name == "spool_unassign":
                ams, tray = _slot_args(args)
                r, what = inventory.unassign(ams, tray), "Spool unassigned"
            elif name == "spool_create":
                r, what = inventory.create_spool(args), "Spool added"
            elif name == "spool_archive":
                r, what = inventory.archive_spool(args.get("id"), bool(args.get("archived", True))), "Spool archived"
            else:
                r, what = {"ok": False, "message": f"Unknown command: {name}"}, name
        except (KeyError, ValueError, TypeError) as e:
            r, what = {"ok": False, "message": f"Bad request: {e}"}, name
        self._inv_rev = -1  # re-read the trays on the next tick
        self._wake.set()
        if r.get("ok"):
            self.notify(True, what)
        else:
            self.notify(False, f"{what} failed: {r.get('message')}")
        self.bump()
        return {**r, "message": what if r.get("ok") else r.get("message", "")}

    # ------------------------------------------------------------ Bambu Cloud history (reprint, SD covers)

    async def fetch_cloud_tasks(self) -> None:
        serial = (self.printer or {}).get("serial", "")
        if not serial or not self.cloud.logged_in:
            return
        tasks, err = await self.cloud.tasks(serial)
        if tasks is None:
            self._next_cloud = time.monotonic() + CLOUD_TASKS_RETRY_S
            log.info("cloud tasks failed: %s", err)
            self.bump()
            return
        self._next_cloud = time.monotonic() + CLOUD_TASKS_S
        self.cloud_tasks = tasks
        self.cloud_tasks_loaded = True
        self.bump()

    async def cloud_cover(self, task_id: str) -> bytes | None:
        """Plate render of a cloud task (signed CDN link, no token), cached on disk."""
        safe = "".join(c for c in task_id if c.isalnum())[:40]
        f = CLOUD_COVER_DIR / f"{safe}.png"
        if f.is_file():
            return f.read_bytes()
        task = next((t for t in self.cloud_tasks if t["id"] == task_id), None)
        if not task or not task.get("cover"):
            return None
        try:
            r = await self.cloud._http.get(task["cover"], headers={"Accept": "image/png,image/*"}, follow_redirects=True)
        except Exception:  # noqa: BLE001
            return None
        if r.status_code != 200 or not r.content or len(r.content) > 4_000_000:
            return None
        f.write_bytes(r.content)
        return r.content

    async def cloud_task_detail(self, task_id: str) -> dict:
        d = await cloudm.task_detail(self.cloud, task_id)
        if d.get("ok"):
            self._task_details[task_id] = d
        return d

    # ------------------------------------------------------------ print start (SD file / cloud reprint)

    def _set_start(self, phase: str, message: str, file: str | None = None) -> None:
        self.start_state = {
            "phase": phase,
            "file": self.start_state["file"] if file is None else file,
            "message": message,
            "t": time.time(),
        }
        if phase == "failed":
            self._armed_l1 = 0
        self.bump()

    def _watch_start(self, st: dict) -> None:
        if self.start_state["phase"] != "waiting":
            return
        if st["connected"] and is_active(st["state"]):
            self._set_start("started", f"Print started ({st['state']})")
            self.notify(True, f"Print started: {self.start_state['file']}")
        elif time.monotonic() > self._start_deadline:
            self._set_start("failed", f"Bambu Cloud accepted the job but the printer did not start it (state {st['state'] or '?'})")

    def _tray_for(self, tid: int) -> dict | None:
        """Chosen tray -> {ams_id, slot_id, tray_info_idx, tray_color}; None when it has no filament."""
        st = self.status or {}
        if tid == 254:
            ext = (st.get("ext") or [None])[0]
            if not ext or not ext["has_filament"]:
                return None
            return {"ams_id": 255, "slot_id": 0, "tray_info_idx": ext["info_idx"], "tray_color": _rgba(ext["color"])}
        for unit in st.get("ams") or []:
            for slot, tr in enumerate(unit["trays"]):
                if not tr or not tr["has_filament"]:
                    continue
                gid = unit["id"] if unit["id"] >= 128 else unit["id"] * 4 + slot
                if gid == tid:
                    return {
                        "ams_id": unit["id"],
                        "slot_id": 0 if unit["id"] >= 128 else slot,
                        "tray_info_idx": tr["info_idx"],
                        "tray_color": _rgba(tr["color"]),
                    }
        return None

    def _precheck(self) -> str:
        st = self.status or {}
        if self.link != "online" or not st.get("connected"):
            return "Printer is offline"
        if is_active(st.get("state", "")):
            return f"Printer is busy ({st['state']})"
        if not (self.printer or {}).get("serial"):
            return "Set the printer serial number in Settings"
        if not self.cloud.logged_in:
            return "Log in to Bambu Cloud in Settings first"
        return ""

    async def _sent(self, msg: str, speed_l1: int) -> None:
        self._start_deadline = time.monotonic() + START_TIMEOUT_S
        if speed_l1:
            self.arm_next_print_speed(speed_l1)
        self._set_start("waiting", msg)
        self.kick(1.5)

    # ------------------------------------------------------------ multi-plate run (one 3MF, plate after plate)

    def plate_seq_view(self) -> dict | None:
        q = db.kv_get("plate_seq")
        if not q:
            return None
        nxt = q["plates"][0] if q["plates"] else None
        names = q.get("names") or {}
        return {"file": q["file"], "state": q["state"], "current": q.get("current"), "next": nxt,
                "next_name": names.get(str(nxt), "") if nxt else "", "done": q["done"], "total": q["total"],
                "message": q.get("message", "")}

    def _plate_seq_end(self, st: dict, end: dict) -> None:
        q = db.kv_get("plate_seq")
        if not q or q["state"] != "printing" or not sdm.same_job_name(q["file"], end.get("job") or ""):
            return
        if end.get("failed"):
            q["state"], q["message"] = "paused", f"Plate {q['current']} did not finish - continue with the next one?"
        else:
            q["done"].append(q["current"])
            if q["plates"]:
                q["state"], q["message"] = "waiting", "Clear the plate, then start the next one"
            else:
                q["state"], q["message"] = "complete", "All plates done"
            nxt = q["plates"][0] if q["plates"] else None
            self.push.plate_done(q["file"], len(q["done"]), q["total"], nxt, (q.get("names") or {}).get(str(nxt), ""))
        db.kv_set("plate_seq", q)
        self.bump()

    async def plate_seq_command(self, action: str) -> dict:
        q = db.kv_get("plate_seq")
        if not q:
            return {"ok": False, "message": "No multi-plate run"}
        if action == "stop":
            db.kv_delete("plate_seq")
            self.bump()
            return {"ok": True, "message": "Multi-plate run ended"}
        if action == "skip":
            if q["plates"]:
                q["plates"].pop(0)
            if not q["plates"]:
                db.kv_delete("plate_seq")
            else:
                db.kv_set("plate_seq", q)
            self.bump()
            return {"ok": True, "message": "Plate skipped"}
        if action != "next" or q["state"] not in ("waiting", "paused") or not q["plates"]:
            return {"ok": False, "message": "Nothing to start"}
        st = self.status or {}
        if not st.get("connected") or is_active(st.get("state", "")):
            return {"ok": False, "message": "The printer is busy or offline"}
        plate = q["plates"][0]
        r = await self.sd_print(q["file"], plate, q["trays"], q["opts"], q["speed"], keep_seq=True)
        if r["ok"]:
            q["plates"].pop(0)
            q["current"], q["state"], q["message"] = plate, "printing", ""
            db.kv_set("plate_seq", q)
            self.bump()
        return r

    async def sd_print(self, name: str, plate: int, trays: list[int], opts: dict, speed_l1: int,
                       then: list[int] | None = None, names: dict | None = None, keep_seq: bool = False) -> dict:
        """``then``: more plates of the same file, each started by the user's button after this one."""
        if not keep_seq:
            db.kv_delete("plate_seq")
        if self.start_state["phase"] in ("sending", "waiting"):
            self.notify(False, "A print start is already in progress")
            return {"ok": False, "message": "A print start is already in progress"}
        self._set_start("sending", "Checking printer...", file=name)
        err = self._precheck()
        if not err:
            for i, t in enumerate(trays):
                if t >= 0 and self._tray_for(t) is None:
                    err = f"Filament {i + 1}: the chosen tray is empty"
                    break
        if err:
            self._set_start("failed", err)
            return {"ok": False, "message": err}
        self._set_start("sending", "Sending via Bambu Cloud...")
        code, msg = await cloudm.start_sd_file(self.cloud, self.printer["serial"], name, plate, trays, opts)
        log.info("start SD '%s' plate %d -> %d %s", name, plate, code, msg)
        if code < 0:
            self._set_start("failed", msg)
            return {"ok": False, "message": msg}
        await self._sent(f"{msg}, waiting for the print to start...", speed_l1)
        if then:
            db.kv_set("plate_seq", {
                "file": name, "plates": [int(p) for p in then], "current": plate, "done": [], "total": 1 + len(then),
                "state": "printing", "trays": trays, "opts": opts, "speed": speed_l1, "names": names or {}, "message": "",
            })
            self.bump()
        return {"ok": True, "message": msg}

    async def cloud_reprint(self, task_id: str, trays: list[int], speed_l1: int) -> dict:
        task = next((t for t in self.cloud_tasks if t["id"] == task_id), None)
        if self.start_state["phase"] in ("sending", "waiting"):
            self.notify(False, "A print start is already in progress")
            return {"ok": False, "message": "A print start is already in progress"}
        self._set_start("sending", "Checking printer...", file=(task or {}).get("title", ""))
        detail = self._task_details.get(task_id)
        err = ""
        if not task or not detail or not detail.get("rows"):
            err = "Job details not loaded - reopen it"
        err = err or self._precheck()
        rows = []
        if not err:
            n = min(len(detail["rows"]), len(trays))
            if not n:
                err = "This job has no filament mapping - cannot reprint"
            for i in range(n):
                tr = self._tray_for(trays[i])
                if tr is None:
                    err = f"Filament {i + 1}: the chosen tray is empty"
                    break
                rows.append({**tr, "src": detail["rows"][i]})
        if err:
            self._set_start("failed", err)
            return {"ok": False, "message": err}
        self._set_start("sending", "Sending to Bambu Cloud...")
        ok, msg = await cloudm.reprint(self.cloud, task, detail, self.printer["serial"], rows)
        if not ok:
            self._set_start("failed", msg)
            return {"ok": False, "message": msg}
        await self._sent("Sent via Bambu Cloud, waiting for the printer to start...", speed_l1)
        return {"ok": True, "message": msg}


def _slot_args(args: dict) -> tuple[int, int]:
    """AMS unit (0-3, 128-135 AMS-HT, 255 external) + tray index of a slot command."""
    ams, tray = int(args["ams_id"]), int(args["tray_id"])
    if not (0 <= ams <= 3 or 128 <= ams <= 135 or ams == 255) or not 0 <= tray <= 3:
        raise ValueError("bad slot")
    return ams, tray


def _rgba(color: str) -> str:
    """'#RRGGBB' -> 'RRGGBBFF' (Bambu Cloud colour format)."""
    c = (color or "").lstrip("#").upper()
    return (c + "FF")[:8] if len(c) == 6 else "FFFFFFFF"

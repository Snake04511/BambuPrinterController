"""Phone pushes (Discord / ntfy) from the engine's print events.

The engine calls the hooks below at the points where it already sees the event; everything
here is fire-and-forget (notify.Notifier never raises and runs the send in the background),
so a dead webhook can never stall the poller.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from . import db
from . import notify as nt
from . import settings

if TYPE_CHECKING:
    from .poller import Engine

log = logging.getLogger("push")

OFFLINE_AFTER_S = 120  # printer unreachable this long -> push
LOG_DAYS = 7
LOG_KEEP = 100
LOW_REARM_S = 600  # a tray must look fine this long before a new "low" push (no flapping, no restart repeats)
OBICO_WARN_HOLD = 3  # consecutive "warning" frames before a warning push
# HMS codes the printer raises for a stop / cancel the user did themselves (not a fault).
_USER_ACTION_HINTS = ("cancel", "stopped by user", "user stop")


class Pusher:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self.n = nt.Notifier.from_app()
        # Already pushed (kept across restarts, so a restart does not repeat them).
        saved = db.kv_get("push_seen") or {}
        self._hms_seen: set[str] = set(saved.get("hms") or [])
        self._offline_since = 0.0
        self._offline_sent = False
        self._was_printing = False
        self._last_job = ""
        self._obico_warn_run = 0
        self._obico_frames = -1
        self._obico_failure_job = ""
        self._low_sent: set[int] = set(saved.get("low") or [])
        self._low_ok_since: dict[int, float] = {}
        self._seen_saved = (sorted(self._hms_seen), sorted(self._low_sent))
        self._runout_job = ""
        self._user_stop_until = 0.0

    async def close(self) -> None:
        await self.n.aclose()

    # ------------------------------------------------------------ helpers

    def _url(self) -> str | None:
        return (settings.get().get("notify_app_url") or "").strip() or None

    def _fire(self, n: nt.Notification) -> None:
        if not n.url:
            n.url = self._url()
        self._log(n)
        self.n.fire(n)

    # ---- the list behind the bell in the top bar (every event, sent or not)

    def _log(self, n: nt.Notification) -> None:
        log_ = db.kv_get("notif_log") or []
        # ids are epoch seconds (unique, increasing): always above older ids,
        # so the "seen" marker of the top bar keeps working after the switch
        nid = max(int(time.time()), (log_[-1]["id"] + 1) if log_ else 0)
        log_.append({
            "id": nid, "title": n.title, "message": n.message,
            "time": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"),
            "success": n.level in ("info", "success"),
        })
        cutoff = time.time() - LOG_DAYS * 86400
        db.kv_set("notif_log", [x for x in log_ if x["id"] >= cutoff][-LOG_KEEP:])
        self.e.bump()

    def listing(self) -> dict:
        """parse_notifications() shape: newest first, max 10."""
        log_ = db.kv_get("notif_log") or []
        items = list(reversed(log_))[:10]
        return {"items": items, "newest_id": log_[-1]["id"] if log_ else -1}

    def _cover(self, job: str) -> bytes | None:
        return self.e.cover if self.e.cover and self.e.cover_job == job else None

    async def _snapshot(self) -> bytes | None:
        try:
            img, _ = await asyncio.wait_for(self.e.camera.snapshot(), timeout=8)
            return img
        except Exception:  # noqa: BLE001 - the push goes out without a picture
            return None

    def _fire_with_snapshot(self, build) -> None:
        async def run() -> None:
            self._fire(build(await self._snapshot()))

        asyncio.create_task(run())

    def user_stopped(self) -> None:
        """The stop came from this app: no 'printer error' push for the HMS echo of it."""
        self._user_stop_until = time.monotonic() + 120

    # ------------------------------------------------------------ print lifecycle

    def on_start(self, st: dict) -> None:
        job = st.get("job") or ""
        self._obico_warn_run = 0
        self._obico_failure_job = ""
        self._runout_job = ""
        # The slicer's plan from this app's copy of the file: grams per filament, matched to
        # the tray it will come from, and the plate's print time (the printer reports 0 min
        # remaining while it prepares).
        from .printjob import tray_label

        fil, est = [], None
        jf = self.e.sd.job_file(job)
        plate = self.e.sd.job_plate(jf[2], []) if jf and jf[2] else None
        if plate:
            taken: set = set()
            for f in plate.get("filaments") or []:
                if (f.get("grams") or 0) <= 0:
                    continue
                tid = self.e.job._match_tray(st, f, taken)
                if tid is None or tid < 0 or tid == 255:
                    tid = None
                sp = self.e.job._spool_for(tid) if tid is not None else None
                name = " ".join(x for x in ((sp or {}).get("brand"), f.get("type"), (sp or {}).get("color_name")) if x) or f.get("type", "")
                fil.append({"label": tray_label(tid) if tid is not None else "", "name": name, "color": f.get("color", ""),
                            "grams": f["grams"]})
            if (plate.get("print_sec") or 0) > 0:
                est = plate["print_sec"]
        if est is None and (st.get("remaining_min") or 0) > 0:
            est = st["remaining_min"] * 60
        self._fire(nt.build_print_started(
            job, estimate_s=est, total_layers=st.get("total_layers"), filaments=fil, image=self._cover(job),
        ))

    def on_pause(self, st: dict) -> None:
        hms = [h["text"] for h in st.get("hms") or [] if h.get("text")]
        self._fire_with_snapshot(lambda img: nt.build_print_paused(
            st.get("job") or "", reason="; ".join(hms[:2]) or None, progress=st.get("progress"),
            layer=st.get("layer"), total_layers=st.get("total_layers"), image=img,
        ))

    def on_end(self, st: dict, end: dict) -> None:
        job = end.get("job") or ""
        rec = self.e.usage.items[-1] if self.e.usage.items and self.e.usage.items[-1].get("job") == job else None
        fil = [{"label": i["label"], "spool_name": i["spool_name"], "grams": i["grams"]} for i in (rec or {}).get("items", [])]
        dur = end["duration_min"] * 60 if end.get("duration_min", -1) >= 0 else None
        if not end.get("failed"):
            cover = self._cover(job)
            self._fire_with_snapshot(lambda img: nt.build_print_finished(
                job, duration_s=dur, filaments=fil, layer=end.get("layer_reached"),
                total_layers=end.get("layers"), image=img or cover, thumb=cover if img else None,
            ))
            return
        stopped = time.monotonic() < self._user_stop_until
        hms = [h["text"] for h in st.get("hms") or [] if h.get("text")]
        self._fire_with_snapshot(lambda img: nt.build_print_failed(
            job, stopped=stopped, reason=None if stopped else ("; ".join(hms[:2]) or None),
            progress=st.get("progress"), layer=end.get("layer_reached"), total_layers=end.get("layers"),
            duration_s=dur, filaments=fil, image=img,
        ))

    def plate_done(self, file: str, done: int, total: int, next_plate: int | None, next_name: str = "") -> None:
        """Multi-plate run: a plate finished, the next waits for the button."""
        self._fire_with_snapshot(lambda img: nt.build_plate_done(
            file, done=done, total=total, next_plate=next_plate, next_name=next_name, image=img,
        ))

    def spool_wet(self, spool: str, hours: float, humidity: float | None, tray: str, color: str, spool_id: int) -> None:
        self._fire(nt.build_spool_wet(spool, hours=hours, humidity=humidity, tray=tray, color=color, spool_id=spool_id))

    def usage_not_booked(self, rec: dict) -> None:
        self._fire(nt.build_usage_not_booked(rec.get("job", ""), items=rec.get("items", [])))

    # ------------------------------------------------------------ every status poll

    def tick(self, st: dict | None, online: bool) -> None:
        try:
            self._tick(st, online)
            self._summary_tick(st)
        except Exception:  # noqa: BLE001 - never break the poller
            log.exception("push tick failed")

    # ---- daily / weekly digest

    def _summary_tick(self, st: dict | None) -> None:
        cfg = settings.get()
        mode = cfg.get("summary_mode", "daily")
        now = datetime.now()
        if mode not in ("daily", "weekly") or now.hour != int(cfg.get("summary_hour", 21)):
            return
        if mode == "weekly" and now.weekday() != 6:
            return
        tag = now.strftime("%Y-%m-%d")
        if db.kv_get("summary_last") == tag:
            return
        db.kv_set("summary_last", tag)
        self._fire(self.build_summary(mode, st))

    def build_summary(self, mode: str, st: dict | None) -> nt.Notification:
        from datetime import timedelta

        from . import inventory, maintenance, printlog
        from .printjob import tray_label  # noqa: F401 - labels come with low_trays

        now = datetime.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) if mode == "daily" else now - timedelta(days=7)
        start_utc = start.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        rows = [r for r in printlog.since(start_utc) if r.get("status") != "printing"]
        ok = sum(r.get("status") == "completed" for r in rows)
        failed = sum(r.get("status") in ("failed", "aborted") for r in rows)
        stopped = sum(r.get("status") in ("stopped", "cancelled") for r in rows)
        hours = sum((r.get("actual_time_seconds") or 0) for r in rows) / 3600
        grams = sum((r.get("filament_used_grams") or 0) for r in rows)
        cost = sum((r.get("cost") or 0) for r in rows)
        used: dict[str, float] = {}
        for u in inventory.usage_history(limit=1000):
            if u["booked_at"] >= start.timestamp():
                used[u["spool_name"] or f"#{u['spool_id']}"] = used.get(u["spool_name"] or f"#{u['spool_id']}", 0) + u["grams"]
        spools = [f"{k}: {v:.0f} g" for k, v in sorted(used.items(), key=lambda kv: -kv[1])[:6]]
        low = [f"{t['label']} {t.get('type', '')}: {t['spool_left']} g" if t.get("spool_left") is not None else f"{t['label']} {t.get('type', '')}: {t['remain']}%"
               for t in (self.e.job.low_trays(st) if st else [])]
        maint = []
        if settings.get().get("own_maintenance"):
            maint = [f"{m['name']}" + (" (esedékes)" if m["due"] else "") for m in maintenance.view()["items"]]
        period = nt.T("summary.today") if mode == "daily" else nt.T("summary.week")
        return nt.build_summary(period, prints=len(rows), ok=ok, failed=failed, stopped=stopped, hours=hours, grams=grams,
                                cost=cost or None, currency=settings.get().get("currency", "Ft"), spools=spools, low=low, maint=maint)

    def send_summary_now(self) -> None:
        """Settings > 'Send a summary now' (for trying it)."""
        mode = settings.get().get("summary_mode", "daily")
        self._fire(self.build_summary("weekly" if mode == "weekly" else "daily", self.e.status))

    def _tick(self, st: dict | None, online: bool) -> None:
        now = time.monotonic()
        reachable = online and bool(st) and bool(st.get("connected"))
        printing = bool(st) and st.get("state") in ("PREPARE", "SLICING", "RUNNING", "PAUSE")
        if reachable:
            self._offline_since = 0.0
            self._offline_sent = False
            self._was_printing = printing
            self._last_job = st.get("job") or ""
        else:
            if not self._offline_since:
                self._offline_since = now
            elif not self._offline_sent and now - self._offline_since >= OFFLINE_AFTER_S:
                self._offline_sent = True
                self._fire(nt.build_printer_offline(
                    offline_s=now - self._offline_since, job=self._last_job, printing=self._was_printing,
                ))
            return

        self._check_hms(st, now)
        self._check_obico(st, printing)
        self._check_filament(st, printing)
        seen = (sorted(self._hms_seen), sorted(self._low_sent))
        if seen != self._seen_saved:
            self._seen_saved = seen
            db.kv_set("push_seen", {"hms": seen[0], "low": seen[1]})

    def _check_hms(self, st: dict, now: float) -> None:
        cur = {}
        for h in st.get("hms") or []:
            text = (h.get("text") or "").strip()
            if text:
                cur[text] = h
        for text, h in cur.items():
            if text in self._hms_seen:
                continue
            self._hms_seen.add(text)
            if now < self._user_stop_until or any(x in text.lower() for x in _USER_ACTION_HINTS):
                continue
            job = st.get("job") if st.get("state") in ("RUNNING", "PAUSE", "PREPARE") else None
            self._fire(nt.build_hms_error(text, severity=h.get("severity"), job=job))
        self._hms_seen &= set(cur)

    def _check_obico(self, st: dict, printing: bool) -> None:
        o = self.e.obico or {}
        if not printing or not o.get("enabled") or not o.get("has_printer"):
            self._obico_warn_run = 0
            return
        frames = o.get("frames", 0)
        if frames == self._obico_frames:
            return  # no new verdict since the last look
        self._obico_frames = frames
        job = st.get("job") or ""
        cls = o.get("cls")
        if cls == "failure" and self._obico_failure_job != job:
            self._obico_failure_job = job
            self._fire_with_snapshot(lambda img: nt.build_obico_failure(
                job, score=o.get("score"), action=o.get("action"), threshold=o.get("high"), image=img,
            ))
        if cls == "warning":
            self._obico_warn_run += 1
            if self._obico_warn_run == OBICO_WARN_HOLD:
                self._fire_with_snapshot(lambda img: nt.build_obico_warning(
                    job, score=o.get("score"), threshold=o.get("low"), image=img,
                ))
        else:
            self._obico_warn_run = 0

    def _check_filament(self, st: dict, printing: bool) -> None:
        low = {t["id"]: t for t in self.e.job.low_trays(st)}
        for tid, t in low.items():
            if tid in self._low_sent:
                continue
            self._low_sent.add(tid)
            sp = self.e.job._spool_for(tid) or {}
            name = " ".join(x for x in (sp.get("brand"), sp.get("material"), sp.get("subtype")) if x) or \
                " ".join(x for x in (t.get("type"), t.get("sub_brands")) if x)
            left = t.get("spool_left")
            pct = (100 * left / sp["label_weight"]) if left is not None and sp.get("label_weight") else (
                t["remain"] if (t.get("remain") or -1) >= 0 else None)
            self._fire(nt.build_filament_low(
                t["label"], spool_name=name, remaining_g=left, spool_id=sp.get("id"),
                color=sp.get("color") or t.get("color", ""), color_name=sp.get("color_name", ""), remaining_pct=pct,
            ))
        now = time.monotonic()
        for tid in list(self._low_sent):
            if tid in low:
                self._low_ok_since.pop(tid, None)
            elif now - self._low_ok_since.setdefault(tid, now) >= LOW_REARM_S:
                self._low_sent.discard(tid)  # refilled for a while -> may warn again later
                self._low_ok_since.pop(tid, None)
        if not printing:
            return
        r = self.e.job.runout(st)
        job = st.get("job") or ""
        if r and r["short"] and self._runout_job != job:
            self._runout_job = job
            sp = self.e.job._spool_for(r["tray"]) or {}
            name = f"{sp.get('brand', '')} {sp.get('material', '')}".strip()
            from .printjob import tray_label

            self._fire(nt.build_runout_forecast(
                job, label=tray_label(r["tray"]), spool_name=name, remaining_g=r["left"], needed_g=r["total"],
                color=sp.get("color", ""), color_name=sp.get("color_name", ""),
            ))

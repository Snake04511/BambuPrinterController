"""Read-only data for the AMS and STATS pages (panel AmsPage.cpp history chart,
StatsPage.cpp / StatsView.cpp): inventory spools, AMS humidity / temperature history,
statistics (all time + this month), print history pages and archive previews."""

from __future__ import annotations

import calendar
import json
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from . import amshist, inventory, printlog
from .printer_mqtt import parse_color
from .db import DATA_DIR

if TYPE_CHECKING:
    from .poller import Engine

HIST_BINS = 144
STATS_MAX_AGE_S = 600
MONTH_LIMIT = 1000
HISTORY_PAGE = 20
THUMB_DIR = DATA_DIR / "cache" / "archive"
THUMB_DIR.mkdir(parents=True, exist_ok=True)


def _f(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _epoch(iso: Any) -> float | None:
    """ISO timestamps, naive = UTC."""
    if not isinstance(iso, str) or not iso:
        return None
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def _colors(v: Any) -> list[str]:
    out = []
    for tok in str(v or "").split(","):
        c = parse_color(tok.strip())
        if c:
            out.append(c)
    return out


class Stats:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self._stats: dict | None = None
        self._sd_thumb: dict[int, tuple[str, int]] = {}  # archive id -> SD cache key, plate
        self._stats_at = 0.0

    # ------------------------------------------------------------ inventory spools (assign dialog)

    async def spools(self) -> dict:
        assigned = {a["spool"]["id"] for a in self.e.assignments}
        out = []
        for sp in inventory.list_spools():
            sp["assigned"] = sp["id"] in assigned
            out.append(sp)
        out.sort(key=lambda s: (s["assigned"], s["material"].lower(), s["brand"].lower(), s["color_name"].lower()))
        return {"ok": True, "error": "", "spools": out}

    # ------------------------------------------------------------ AMS humidity / temperature history

    async def ams_history(self, ams_id: int, hours: int) -> dict:
        end = time.time()
        span = hours * 3600
        start = end - span
        pts = amshist.samples(ams_id, start)
        bins = min(HIST_BINS, hours * 12)  # one sample every 5 min: never finer than that
        sum_h = [0.0] * bins
        sum_t = [0.0] * bins
        cnt_h = [0] * bins
        cnt_t = [0] * bins
        for t, h, tc in pts:
            b = min(bins - 1, max(0, int((t - start) * bins / span)))
            if h is not None:
                sum_h[b] += h
                cnt_h[b] += 1
            if tc is not None:
                sum_t[b] += tc
                cnt_t[b] += 1
        hs = [h for _, h, _ in pts if h is not None]
        ts = [tc for _, _, tc in pts if tc is not None]
        stat = {
            "min_humidity": min(hs) if hs else None, "max_humidity": max(hs) if hs else None,
            "avg_humidity": round(sum(hs) / len(hs), 1) if hs else None,
            "min_temperature": min(ts) if ts else None, "max_temperature": max(ts) if ts else None,
            "avg_temperature": round(sum(ts) / len(ts), 1) if ts else None,
        }
        return {
            "ok": True,
            "error": "",
            "hours": hours,
            "start": int(start),
            "end": int(end),
            "samples": len(pts),
            "hum": [round(sum_h[i] / cnt_h[i], 1) if cnt_h[i] else None for i in range(bins)],
            "temp": [round(sum_t[i] / cnt_t[i], 1) if cnt_t[i] else None for i in range(bins)],
            **stat,
        }

    # ------------------------------------------------------------ statistics

    async def statistics(self, refresh: bool) -> dict:
        if self._stats and not refresh and time.monotonic() - self._stats_at < STATS_MAX_AGE_S:
            return {**self._stats, "age": int(time.monotonic() - self._stats_at)}
        out: dict[str, Any] = {"ok": True, "error": "", "all": None, "month": None}
        d = printlog.statistics()
        if isinstance(d, dict):
            types = sorted(
                ((str(k), int(v)) for k, v in (d.get("prints_by_filament_type") or {}).items() if isinstance(v, (int, float))),
                key=lambda kv: -kv[1],
            )
            out["all"] = {
                "total": int(d.get("total_prints") or 0),
                "ok": int(d.get("successful_prints") or 0),
                "failed": int(d.get("failed_prints") or 0),
                "cancelled": int(d.get("cancelled_prints") or 0),
                "hours": _f(d.get("total_print_time_hours")),
                "grams": _f(d.get("total_filament_grams")),
                "cost": _f(d.get("total_cost")),
                "types": [{"type": k, "prints": v} for k, v in types],
            }
        now = datetime.now()
        month_start = now.strftime("%Y-%m-01")
        days = calendar.monthrange(now.year, now.month)[1]
        out["month"] = month_stats(printlog.since(month_start, MONTH_LIMIT), now.strftime("%Y-%m"), days)
        self._stats = out
        self._stats_at = time.monotonic()
        return {**out, "age": 0}

    # ------------------------------------------------------------ history

    async def history(self, offset: int) -> dict:
        rows = printlog.page(offset, HISTORY_PAGE + 1)
        obico = set(self.e.job.obico_record_ids())
        lapse = {t["archive_id"]: t["id"] for t in self.e.timelapse.listing() if (t.get("archive_id") or -1) >= 0}
        items = []
        for a in rows[:HISTORY_PAGE]:
            if not isinstance(a, dict):
                continue
            aid = int(a.get("id") or -1)
            items.append(
                {
                    "id": aid,
                    "name": str(a.get("print_name") or a.get("filename") or ""),
                    "status": str(a.get("status") or ""),
                    "started": str(a.get("started_at") or a.get("created_at") or ""),
                    "actual_sec": int(_f(a.get("actual_time_seconds")) or -1),
                    "est_sec": int(_f(a.get("print_time_seconds")) or -1),
                    "grams": _f(a.get("filament_used_grams")),
                    "cost": _f(a.get("cost")),
                    "cost_filament": _f(a.get("cost_filament")),
                    "cost_energy": _f(a.get("cost_energy")),
                    "energy_kwh": _f(a.get("energy_kwh")),
                    "type": str(a.get("filament_type") or ""),
                    "colors": _colors(a.get("filament_color"))[:4],
                    "layers": int(_f(a.get("total_layers")) or -1),
                    "layer_height": _f(a.get("layer_height")),
                    "failure": str(a.get("failure_reason") or ""),
                    "has_obico": aid in obico,
                    "timelapse": lapse.get(aid, ""),
                    "from_sd": False,
                }
            )
            self._fill_from_sd(items[-1], a)
        return {"ok": True, "error": "", "items": items, "offset": offset, "more": len(rows) > HISTORY_PAGE}

    def _sd_copy(self, a: dict) -> tuple[str, dict, dict] | None:
        """This app's cached details of the SD file a print came from: (cache key, detail, plate)."""
        from .sdcard import cache_key, same_job_name

        names = [str(a.get("filename") or ""), str(a.get("print_name") or "")]
        for f in self.e.sd.files:
            if any(n and same_job_name(f["name"], n) for n in names):
                key = cache_key(f["name"], f["size"])
                d = self.e.sd._load_detail(key)
                plates = (d or {}).get("plates") or []
                if not plates:
                    return None
                want = int(_f(a.get("plate_id")) or 1)
                plate = next((p for p in plates if p.get("index") == want), plates[0])
                return key, d, plate
        return None

    def _fill_from_sd(self, item: dict, a: dict) -> None:
        """Filament and picture missing from the log entry: from our SD copy of the same file."""
        if item["grams"] is not None and item["colors"]:
            return
        sd = self._sd_copy(a)
        if not sd:
            return
        key, _, plate = sd
        fil = [f for f in plate.get("filaments") or [] if f.get("grams", 0) > 0]
        if item["grams"] is None and plate.get("grams", 0) > 0:
            item["grams"] = round(plate["grams"], 1)
        if not item["colors"]:
            item["colors"] = [f["color"] for f in fil if f.get("color")][:4]
        if not item["type"]:
            item["type"] = ",".join(dict.fromkeys(f["type"] for f in fil if f.get("type")))
        item["filaments"] = [{"color": f["color"], "type": f["type"], "grams": round(f["grams"], 1)} for f in fil]
        item["from_sd"] = True
        self._sd_thumb[item["id"]] = (key, plate["index"])

    async def thumb(self, archive_id: int) -> bytes | None:
        f = THUMB_DIR / f"{int(archive_id)}.png"
        if f.is_file():
            return f.read_bytes()
        sd = self._sd_thumb.get(int(archive_id))
        if sd:
            return self.e.sd.png(sd[0], sd[1])
        # the plate picture from this app's copy of the SD file
        row = printlog.get(int(archive_id))
        jf = self.e.sd.job_file(str(row.get("filename") or row.get("print_name") or "")) if row else None
        if jf:
            png = self.e.sd.png(jf[1], int(row.get("plate_id") or 1))
            if png:
                f.write_bytes(png)  # keep it with the History pictures
                return png
        return None

    # ------------------------------------------------------------ delete a History entry

    async def delete_archive(self, archive_id: int) -> dict:
        """Deletes a print from the print log. This app's Obico record / timelapse / filament
        record of it move to another entry of the same print (same name or file), if there is one."""
        from .sdcard import same_job_name

        rows = printlog.page(0, 100)
        me = next((a for a in rows if int(a.get("id") or -1) == archive_id), None)
        if not printlog.delete(archive_id):
            return {"ok": False, "message": "Not found"}
        (THUMB_DIR / f"{int(archive_id)}.png").unlink(missing_ok=True)
        moved_to = -1
        if me:
            names = [str(me.get("print_name") or ""), str(me.get("filename") or "")]

            def same(a: dict) -> bool:
                return any(same_job_name(x, n) for x in (str(a.get("print_name") or ""), str(a.get("filename") or "")) for n in names if n)

            others = [a for a in rows if int(a.get("id") or -1) != archive_id and same(a)]
            others.sort(key=lambda a: (a.get("status") == "completed", str(a.get("started_at") or a.get("created_at") or "")), reverse=True)
            if others:
                moved_to = int(others[0]["id"])
        note = ""
        if moved_to >= 0:
            src = self.e.job._obico_path(archive_id)
            dst = self.e.job._obico_path(moved_to)
            if src.is_file() and not dst.is_file():
                rec = json.loads(src.read_text())
                rec["archive_id"] = moved_to
                dst.write_text(json.dumps(rec))
                src.unlink()
                note = f" - Obico scores moved to #{moved_to}"
            self.e.timelapse.relink(archive_id, moved_to)
            for u in self.e.usage.items:
                if u.get("archive_id") == archive_id:
                    u["archive_id"] = moved_to
            self.e.usage._save()
        self.e.notify(True, f"Print #{archive_id} deleted{note}")
        return {"ok": True, "message": "Deleted", "moved_to": moved_to}


def month_stats(runs: list, month: str, days: int) -> dict:
    """This month summed from the print log, like the panel's StatsView.cpp."""
    m: dict[str, Any] = {
        "month": month,
        "days": days,
        "runs": 0,
        "ok": 0,
        "failed": 0,
        "cancelled": 0,
        "other": 0,
        "seconds": 0.0,
        "timed": 0,
        "grams": 0.0,
        "weighed": 0,
        "cost": 0.0,
        "day_hours": [0.0] * days,
        "mixed_grams": 0.0,
        "mixed_runs": 0,
        "truncated": len(runs) >= MONTH_LIMIT,
    }
    mats: dict[str, dict] = {}
    cols: dict[tuple, dict] = {}
    for e in runs:
        if not isinstance(e, dict):
            continue
        m["runs"] += 1
        st = str(e.get("status") or "")
        if st == "completed":
            m["ok"] += 1
        elif st in ("failed", "aborted"):
            m["failed"] += 1
        elif st in ("stopped", "cancelled", "skipped"):
            m["cancelled"] += 1
        else:
            m["other"] += 1
        sec = _f(e.get("actual_time_seconds"))
        if sec and sec > 0:
            m["seconds"] += sec
            m["timed"] += 1
            at = str(e.get("started_at") or e.get("created_at") or "")
            try:
                day = int(at[8:10])
                if 1 <= day <= days:
                    m["day_hours"][day - 1] += sec / 3600
            except ValueError:
                pass
        m["cost"] += _f(e.get("cost")) or 0.0
        g = _f(e.get("filament_used_grams"))
        if g is not None:
            m["grams"] += g
            m["weighed"] += 1
            types = str(e.get("filament_type") or "")
            colors = str(e.get("filament_color") or "")
            if "," not in colors and "," not in types:  # single colour: several colours have one total
                c = parse_color(colors)
                if c:
                    k = (c, types.lower())
                    row = cols.setdefault(k, {"color": c, "type": types, "grams": 0.0, "runs": 0})
                    row["grams"] += g
                    row["runs"] += 1
            if "," in types:
                m["mixed_grams"] += g  # several materials: the log has one total, no split
                m["mixed_runs"] += 1
            elif types:
                row = mats.setdefault(types.lower(), {"type": types, "grams": 0.0, "runs": 0})
                row["grams"] += g
                row["runs"] += 1
    m["day_hours"] = [round(h, 2) for h in m["day_hours"]]
    m["materials"] = sorted(mats.values(), key=lambda r: -r["grams"])
    m["colors"] = sorted(cols.values(), key=lambda r: -r["grams"])[:16]
    return m

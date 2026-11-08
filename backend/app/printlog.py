"""This app's print log (History and Stats).

Every row is a JSON "archive" (print_name, filename, status, started_at,
actual_time_seconds, filament_used_grams, filament_type, filament_color, ...); stats.py's
history / month statistics, the Obico records and the timelapse links are keyed by its id.

Ids: rows imported from an earlier tool keep their old ids; prints logged here start at
OWN_ID_BASE.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from . import db

log = logging.getLogger("printlog")

OWN_ID_BASE = 1_000_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pl_print (
    id         INTEGER PRIMARY KEY,
    started_at TEXT    NOT NULL DEFAULT '',
    status     TEXT    NOT NULL DEFAULT '',
    source     TEXT    NOT NULL DEFAULT 'app',
    data       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS pl_print_by_start ON pl_print(started_at);
"""
_ready = False


def _ensure() -> None:
    global _ready
    if not _ready:
        with db._lock:  # noqa: SLF001 - same pattern as inventory.py
            db._conn.executescript(_SCHEMA)  # noqa: SLF001
        _ready = True


def _iso(t: float | None = None) -> str:
    """Naive UTC ISO (stats.py parses them as UTC)."""
    return datetime.fromtimestamp(t if t is not None else time.time(), timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _write(row: dict, source: str = "app") -> None:
    _ensure()
    with db._lock:  # noqa: SLF001
        db._conn.execute(  # noqa: SLF001
            "INSERT OR REPLACE INTO pl_print (id, started_at, status, source, data) VALUES (?, ?, ?, ?, ?)",
            (int(row["id"]), str(row.get("started_at") or row.get("created_at") or ""), str(row.get("status") or ""), source, json.dumps(row)),
        )
        db._conn.commit()  # noqa: SLF001


def get(pid: int) -> dict | None:
    _ensure()
    with db._lock:  # noqa: SLF001
        r = db._conn.execute("SELECT data FROM pl_print WHERE id=?", (int(pid),)).fetchone()  # noqa: SLF001
    return json.loads(r[0]) if r else None


def page(offset: int = 0, limit: int = 20) -> list[dict]:
    """Newest first, in the /archives/ shape."""
    _ensure()
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute(  # noqa: SLF001
            "SELECT data FROM pl_print ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?", (int(limit), max(0, int(offset)))
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def since(iso_day: str, limit: int = 2000) -> list[dict]:
    _ensure()
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute(  # noqa: SLF001
            "SELECT data FROM pl_print WHERE started_at >= ? ORDER BY started_at LIMIT ?", (iso_day, int(limit))
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def count() -> int:
    _ensure()
    with db._lock:  # noqa: SLF001
        return int(db._conn.execute("SELECT COUNT(*) FROM pl_print").fetchone()[0])  # noqa: SLF001


def delete(pid: int) -> bool:
    _ensure()
    with db._lock:  # noqa: SLF001
        cur = db._conn.execute("DELETE FROM pl_print WHERE id=?", (int(pid),))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    return cur.rowcount > 0


def _next_id() -> int:
    _ensure()
    with db._lock:  # noqa: SLF001
        m = db._conn.execute("SELECT MAX(id) FROM pl_print WHERE id >= ?", (OWN_ID_BASE,)).fetchone()[0]  # noqa: SLF001
    return int(m) + 1 if m else OWN_ID_BASE


# ---------------------------------------------------------------- a print of this app


def start(job: str, file: str, est_sec: int, plate: dict | None, total_layers: int) -> dict:
    """A print started: new row 'printing'. ``plate`` = this app's SD-cache plate (filaments)."""
    fil = [f for f in (plate or {}).get("filaments") or [] if (f.get("grams") or 0) > 0]
    row = {
        "id": _next_id(),
        "print_name": job,
        "filename": file or job,
        "plate_id": (plate or {}).get("index", 1),
        "status": "printing",
        "created_at": _iso(),
        "started_at": _iso(),
        "completed_at": None,
        "actual_time_seconds": None,
        "print_time_seconds": est_sec if est_sec and est_sec > 0 else None,
        "filament_used_grams": round(sum(f["grams"] for f in fil), 2) if fil else None,
        "filament_type": ",".join(dict.fromkeys(f.get("type", "") for f in fil if f.get("type"))),
        "filament_color": ",".join(f.get("color", "") for f in fil if f.get("color")),
        "filaments": [{"slot": f.get("slot", 0), "type": f.get("type", ""), "color": f.get("color", ""),
                       "grams": round(f["grams"], 2), "meters": round(f.get("meters", 0) or 0, 2)} for f in fil],
        "total_layers": total_layers if total_layers and total_layers > 0 else None,
        "layer_height": None,
        "failure_reason": "",
        "cost": None,
    }
    _write(row)
    log.info("print log #%d started: %s", row["id"], job)
    return row


def finish(pid: int, *, failed: bool, stopped: bool, actual_sec: int, used: list[dict], layer: int, reason: str = "") -> dict | None:
    """The print ended. ``used`` = usage.py items (label, grams, spool, colour) - what was really used."""
    row = get(pid)
    if row is None:
        return None
    row["status"] = "stopped" if stopped else "failed" if failed else "completed"
    row["completed_at"] = _iso()
    if actual_sec and actual_sec > 0:
        row["actual_time_seconds"] = int(actual_sec)
    if used:
        row["filament_used_grams"] = round(sum(i.get("grams") or 0 for i in used), 2)
        row["used"] = [{"label": i.get("label", ""), "spool_id": i.get("spool_id"), "spool_name": i.get("spool_name", ""),
                        "color": i.get("color", ""), "grams": i.get("grams")} for i in used]
    if failed or stopped:
        row["layer_reached"] = layer
        row["failure_reason"] = reason or ("Stopped" if stopped else "Failed")
    _write(row)
    log.info("print log #%d %s: %s", pid, row["status"], row["print_name"])
    return row


def update(pid: int, **fields: Any) -> None:
    row = get(pid)
    if row is not None:
        row.update(fields)
        _write(row)


def statistics() -> dict:
    """Totals over all rows (the panel API's /archives/stats shape)."""
    _ensure()
    with db._lock:  # noqa: SLF001
        rows = [json.loads(r[0]) for r in db._conn.execute("SELECT data FROM pl_print").fetchall()]  # noqa: SLF001
    st = {"total_prints": 0, "successful_prints": 0, "failed_prints": 0, "cancelled_prints": 0,
          "total_print_time_hours": 0.0, "total_filament_grams": 0.0, "total_cost": 0.0, "prints_by_filament_type": {}}
    for r in rows:
        s = r.get("status")
        if s == "printing":
            continue
        st["total_prints"] += 1
        if s == "completed":
            st["successful_prints"] += 1
        elif s in ("failed", "aborted"):
            st["failed_prints"] += 1
        elif s in ("stopped", "cancelled"):
            st["cancelled_prints"] += 1
        st["total_print_time_hours"] += (r.get("actual_time_seconds") or 0) / 3600
        st["total_filament_grams"] += r.get("filament_used_grams") or 0
        st["total_cost"] += r.get("cost") or 0
        for t in dict.fromkeys(x for x in str(r.get("filament_type") or "").split(",") if x):
            st["prints_by_filament_type"][t] = st["prints_by_filament_type"].get(t, 0) + 1
    st["total_print_time_hours"] = round(st["total_print_time_hours"], 1)
    st["total_filament_grams"] = round(st["total_filament_grams"], 1)
    st["total_cost"] = round(st["total_cost"], 2)
    return st

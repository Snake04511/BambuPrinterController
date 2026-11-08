"""Maintenance counters kept by this app.

Each item is due after N print hours or N days since it was last done. The printer's total
print hours are counted here: every finished / failed / stopped print adds its duration.
view() gives {due_count, warning_count, items[]} for the TopBar / screens. Small data,
kept in the kv store.
"""

from __future__ import annotations

import logging
import time

from . import db

log = logging.getLogger("maintenance")

KEY = "maintenance"
WARN_SHARE = 0.1  # warning when less than 10 % of the interval is left
WARN_DAYS = 3

DEFAULT_ITEMS = [
    ("Clean Build Plate", "hours", 25),
    ("Clean Carbon Rods", "hours", 100),
    ("Clean Nozzle/Hotend", "hours", 100),
    ("Calibration", "hours", 100),
    ("Check Belt Tension", "hours", 200),
    ("Check PTFE Tube", "hours", 500),
]


def _load() -> dict:
    d = db.kv_get(KEY) or {}
    d.setdefault("hours", 0.0)
    d.setdefault("items", [])
    d.setdefault("seq", max([i["id"] for i in d["items"]] or [0]))
    return d


def _save(d: dict) -> None:
    db.kv_set(KEY, d)


def configured() -> bool:
    return bool((db.kv_get(KEY) or {}).get("items"))


def total_hours() -> float:
    return float(_load()["hours"])


def add_print(seconds: float) -> None:
    """A print ended: its time counts towards every hour-based item."""
    if not seconds or seconds <= 0:
        return
    d = _load()
    d["hours"] = round(d["hours"] + seconds / 3600, 4)
    _save(d)


def _until(it: dict, hours: float, now: float) -> float:
    if it["type"] == "days":
        return it["interval"] - (now - it["last_at"]) / 86400
    return it["interval"] - (hours - it["last_hours"])


def view() -> dict:
    """The parse_maintenance() shape: due first, then warnings (only those two are listed)."""
    d = _load()
    now = time.time()
    due_items, warn_items = [], []
    for it in d["items"]:
        if not it.get("enabled", True):
            continue
        until = _until(it, d["hours"], now)
        warn_at = WARN_DAYS if it["type"] == "days" else it["interval"] * WARN_SHARE
        row = {"id": it["id"], "name": it["name"], "due": until <= 0, "warning": 0 < until <= warn_at,
               "by_days": it["type"] == "days", "until": round(until, 2)}
        if row["due"]:
            due_items.append(row)
        elif row["warning"]:
            warn_items.append(row)
    return {"due_count": len(due_items), "warning_count": len(warn_items), "items": due_items + warn_items}


def listing() -> dict:
    """Every item (Settings page)."""
    d = _load()
    now = time.time()
    return {
        "hours": round(d["hours"], 1),
        "items": [{**it, "until": round(_until(it, d["hours"], now), 1)} for it in d["items"]],
    }


def done(item_id: int) -> dict:
    d = _load()
    it = next((i for i in d["items"] if i["id"] == int(item_id)), None)
    if it is None:
        return {"ok": False, "message": "Unknown maintenance item"}
    it["last_hours"], it["last_at"] = d["hours"], time.time()
    _save(d)
    log.info("maintenance done: %s at %.1f h", it["name"], d["hours"])
    return {"ok": True, "message": f"{it['name']} done"}


def save_item(data: dict) -> dict:
    """Add (no id) or change an item: name, type hours|days, interval, enabled."""
    d = _load()
    name = str(data.get("name") or "").strip()[:60]
    typ = data.get("type", "hours")
    try:
        interval = float(data.get("interval"))
    except (TypeError, ValueError):
        return {"ok": False, "message": "Interval must be a number"}
    if typ not in ("hours", "days") or not 0 < interval <= 100000:
        return {"ok": False, "message": "Bad interval"}
    if data.get("id") is None:
        if not name:
            return {"ok": False, "message": "Name is required"}
        d["seq"] += 1
        d["items"].append({"id": d["seq"], "name": name, "type": typ, "interval": interval, "enabled": True,
                           "last_hours": d["hours"], "last_at": time.time()})
    else:
        it = next((i for i in d["items"] if i["id"] == int(data["id"])), None)
        if it is None:
            return {"ok": False, "message": "Unknown maintenance item"}
        it.update({"name": name or it["name"], "type": typ, "interval": interval, "enabled": bool(data.get("enabled", True))})
    _save(d)
    return {"ok": True, "message": "Saved"}


def delete_item(item_id: int) -> dict:
    d = _load()
    n = len(d["items"])
    d["items"] = [i for i in d["items"] if i["id"] != int(item_id)]
    _save(d)
    return {"ok": len(d["items"]) < n, "message": "Deleted"}


def set_hours(hours: float) -> None:
    d = _load()
    d["hours"] = max(0.0, float(hours))
    _save(d)


def defaults() -> None:
    """Fresh start: the usual P1S items, counted from now."""
    d = _load()
    for name, typ, interval in DEFAULT_ITEMS:
        save_item({"name": name, "type": typ, "interval": interval})
        d = _load()
    _save(d)

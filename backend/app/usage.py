"""Filament used by every print, kept by this app: per tray / spool, booked on the spools of
the inventory when the print ends. A line that could not be booked stays with a "Book" action.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from . import inventory
from .db import DATA_DIR

if TYPE_CHECKING:
    from .poller import Engine

log = logging.getLogger("usage")

FILE = DATA_DIR / "usage.json"
KEEP = 200


class Usage:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self.items: list[dict] = self._load()
        self.version = 0

    def _load(self) -> list[dict]:
        try:
            return json.loads(FILE.read_text())
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        self.items = self.items[-KEEP:]
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items))
        tmp.replace(FILE)
        self.version += 1
        self.e.bump()

    # ------------------------------------------------------------ recording

    def spools_now(self) -> dict[str, dict]:
        """Spool of every loaded tray right now: tray id -> {spool_id, weight_used, ...}."""
        out = {}
        for a in self.e.assignments:
            ams, tray = a["ams_id"], a["tray_id"]
            tid = 254 if ams == 255 else ams if ams >= 128 else ams * 4 + tray
            sp = a["spool"]
            out[str(tid)] = {
                "spool_id": sp["id"],
                "weight_used": sp["weight_used"],
                "label_weight": sp["label_weight"],
                "name": f"{sp['brand']} {sp['material']} {sp['subtype']}".strip(),
                "color": sp["color"],
                "ams_id": ams,
                "tray_id": tray,
            }
        return out

    def record(self, job: str, result: str, grams_by_tray: dict[str, float], spools_start: dict[str, dict]) -> None:
        """A print ended: what it used from which spool."""
        from .printjob import tray_label

        items = []
        for tid, g in grams_by_tray.items():
            if g < 0.5:
                continue
            sp = spools_start.get(tid)
            items.append(
                {
                    "tray": int(tid),
                    "label": tray_label(int(tid)),
                    "grams": round(g, 1),
                    "spool_id": sp["spool_id"] if sp else None,
                    "spool_name": sp["name"] if sp else "",
                    "color": sp["color"] if sp else "",
                    "weight_used_start": sp["weight_used"] if sp else None,
                    "state": "checking" if sp else "no_spool",  # checking -> local (booked) / missing; no_spool
                }
            )
        if not items:
            return
        now = time.time()
        self.items.append(
            {"id": str(int(now * 1000)), "job": job, "result": result, "ended_at": int(now), "archive_id": -1,
             "items": items, "ignored": False}
        )
        log.info("filament use of '%s' recorded: %s", job, ", ".join(f"{i['label']} {i['grams']} g" for i in items))
        if self._book_local(self.items[-1]):
            self.e.push.usage_not_booked(self.items[-1])
        self._save()

    def _book_local(self, r: dict) -> list[str]:
        """Own inventory: book the record's lines on the spools here (idempotent per record)."""
        todo = [i for i in r["items"] if i["state"] in ("checking", "missing") and i["spool_id"] is not None]
        if not todo:
            return []
        res = inventory.book_usage(
            [{"spool_id": i["spool_id"], "grams": i["grams"]} for i in todo], job=r["job"], ref=f"usage:{r['id']}", source="print"
        )
        bad = {e for e in res.get("errors", [])}
        now = int(time.time())
        for i in todo:
            if not any(f"#{i['spool_id']} " in e for e in bad):
                i["state"], i["synced_at"] = "local", now
            else:
                i["state"] = "missing"
        r["own"] = True
        self.e._inv_rev = -1
        return list(bad)

    def set_archive(self, job: str, archive_id: int) -> None:
        for r in reversed(self.items[-5:]):
            if r["job"] == job and r.get("archive_id", -1) < 0:
                r["archive_id"] = archive_id
                self._save()
                return

    # ------------------------------------------------------------ actions

    def _get(self, rid: str) -> dict | None:
        return next((r for r in self.items if r["id"] == rid), None)

    async def sync(self, rid: str) -> dict:
        """Books the lines that are not booked yet on the inventory spools."""
        r = self._get(rid)
        if r is None:
            return {"ok": False, "message": "Record not found"}
        errors = self._book_local(r)
        self._save()
        if errors:
            self.e.notify(False, "Booking failed - " + "; ".join(errors))
        else:
            self.e.notify(True, "Booked: " + ", ".join(f"{i['label']} {i['grams']} g" for i in r["items"] if i["state"] == "local"))
        return {"ok": not errors, "message": "; ".join(errors) or "Booked"}

    def ignore(self, rid: str, ignored: bool) -> dict:
        r = self._get(rid)
        if r is None:
            return {"ok": False, "message": "Record not found"}
        r["ignored"] = ignored
        self._save()
        return {"ok": True, "message": ""}

    def pending(self) -> int:
        return sum(1 for r in self.items if not r.get("ignored") and any(i["state"] == "missing" for i in r["items"]))

    def listing(self) -> list[dict]:
        return list(reversed(self.items))

"""Daily backup of this app's data (database, settings, records) to /backups.

/backups is a bind mount of the Home Assistant share folder (share/printerdash/backups),
so the files are reachable over Samba and survive a lost Docker volume. Timelapse frames
and caches are left out (big, and rebuilt / not worth keeping); everything else that is
the user's data is in.

A backup is one .tar.gz: app.db (SQLite online backup, consistent while the app writes),
usage.json, notify_texts.json, obico/ records and the History pictures (cache/archive).
Restore: stop the container, unpack into the data volume, start it.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sqlite3
import tarfile
import time
from datetime import datetime
from pathlib import Path

from . import db, settings
from .db import DATA_DIR

log = logging.getLogger("backup")

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/backups"))
EXTRA_FILES = ("usage.json", "notify_texts.json")
EXTRA_DIRS = ("obico", "cache/archive")
RUN_HOUR = 3  # local time


def available() -> bool:
    return BACKUP_DIR.is_dir() and os.access(BACKUP_DIR, os.W_OK)


def listing() -> list[dict]:
    if not BACKUP_DIR.is_dir():
        return []
    out = []
    for p in sorted(BACKUP_DIR.glob("printerdash-*.tar.gz"), reverse=True):
        st = p.stat()
        out.append({"name": p.name, "size": st.st_size, "time": int(st.st_mtime)})
    return out


def _db_bytes() -> bytes:
    """Consistent copy of app.db while the app keeps using it (SQLite backup API)."""
    tmp = DATA_DIR / "tmp" / "backup.db"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.unlink(missing_ok=True)
    dst = sqlite3.connect(tmp)
    try:
        with db._lock:  # noqa: SLF001 - one writer at a time on the shared connection
            db._conn.backup(dst)  # noqa: SLF001
    finally:
        dst.close()
    data = tmp.read_bytes()
    tmp.unlink(missing_ok=True)
    return data


def make() -> dict:
    """Writes one backup now (blocking: run in a thread)."""
    if not available():
        return {"ok": False, "message": f"{BACKUP_DIR} is not mounted / not writable"}
    name = f"printerdash-{datetime.now():%Y%m%d-%H%M%S}.tar.gz"
    part = BACKUP_DIR / (name + ".part")
    t0 = time.monotonic()
    with tarfile.open(part, "w:gz") as tar:
        data = _db_bytes()
        info = tarfile.TarInfo("app.db")
        info.size, info.mtime = len(data), int(time.time())
        tar.addfile(info, io.BytesIO(data))
        for f in EXTRA_FILES:
            if (DATA_DIR / f).is_file():
                tar.add(DATA_DIR / f, arcname=f)
        for d in EXTRA_DIRS:
            if (DATA_DIR / d).is_dir():
                tar.add(DATA_DIR / d, arcname=d)
    part.replace(BACKUP_DIR / name)
    keep = int(settings.get().get("backup_keep") or 14)
    for old in listing()[keep:]:
        (BACKUP_DIR / old["name"]).unlink(missing_ok=True)
    size = (BACKUP_DIR / name).stat().st_size
    log.info("backup %s written (%d KB, %.1f s)", name, size // 1024, time.monotonic() - t0)
    db.kv_set("backup_last", {"name": name, "time": int(time.time()), "ok": True})
    return {"ok": True, "message": f"Backup saved: {name}", "name": name}


def status() -> dict:
    return {"available": available(), "dir": str(BACKUP_DIR), "last": db.kv_get("backup_last"), "items": listing()}


async def run_daily() -> None:
    """Background task: one backup a day at RUN_HOUR (and at start if the last is old)."""
    while True:
        try:
            last = (db.kv_get("backup_last") or {}).get("time", 0)
            now = datetime.now()
            if settings.get().get("backup_enabled", True) and available() and (
                (now.hour == RUN_HOUR and time.time() - last > 20 * 3600) or time.time() - last > 36 * 3600
            ):
                r = await asyncio.to_thread(make)
                if not r["ok"]:
                    log.warning("backup failed: %s", r["message"])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("backup failed")
            db.kv_set("backup_last", {**(db.kv_get("backup_last") or {}), "ok": False})
        await asyncio.sleep(600)

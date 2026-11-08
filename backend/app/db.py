"""SQLite storage: key/value settings (incl. secrets), login sessions.

Small, rare writes -> one shared connection behind a lock is plenty.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "app.db"

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.execute("PRAGMA journal_mode=WAL")
_conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        created REAL NOT NULL,
        last_seen REAL NOT NULL
    );
    """
)


def kv_get(key: str, default: Any = None) -> Any:
    with _lock:
        row = _conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except ValueError:
        return default


def kv_set(key: str, value: Any) -> None:
    with _lock:
        _conn.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


def kv_delete(key: str) -> None:
    with _lock:
        _conn.execute("DELETE FROM kv WHERE key=?", (key,))


def execute(sql: str, params: tuple = ()) -> list[tuple]:
    with _lock:
        return _conn.execute(sql, params).fetchall()

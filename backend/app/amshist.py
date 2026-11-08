"""AMS humidity / temperature history recorded by this app.

One sample per AMS unit every SAMPLE_S from the live status; kept KEEP_DAYS.
"""

from __future__ import annotations

import time

from . import db

SAMPLE_S = 300
KEEP_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ams_hist (
    ams_id INTEGER NOT NULL,
    t      INTEGER NOT NULL,
    hum    REAL,
    temp   REAL,
    PRIMARY KEY (ams_id, t)
);
"""
_ready = False
_next = 0.0


def _ensure() -> None:
    global _ready
    if not _ready:
        with db._lock:  # noqa: SLF001
            db._conn.executescript(_SCHEMA)  # noqa: SLF001
        _ready = True


def record(st: dict | None) -> bool:
    """Called with every status; stores a sample per unit every SAMPLE_S (True when it did)."""
    global _next
    now = time.time()
    if now < _next or not st or not st.get("connected"):
        return False
    rows = []
    for u in st.get("ams") or []:
        h = u.get("humidity")
        t = u.get("temp")
        h = float(h) if isinstance(h, (int, float)) and h >= 0 else None
        t = float(t) if isinstance(t, (int, float)) and t > -50 else None
        if h is not None or t is not None:
            rows.append((int(u["id"]), int(now), h, t))
    if not rows:
        return False
    _next = now + SAMPLE_S
    _ensure()
    with db._lock:  # noqa: SLF001
        db._conn.executemany("INSERT OR REPLACE INTO ams_hist (ams_id, t, hum, temp) VALUES (?, ?, ?, ?)", rows)  # noqa: SLF001
        db._conn.execute("DELETE FROM ams_hist WHERE t < ?", (int(now - KEEP_DAYS * 86400),))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    return True


def samples(ams_id: int, start: float) -> list[tuple[float, float | None, float | None]]:
    _ensure()
    with db._lock:  # noqa: SLF001
        return [
            (float(t), h, tc)
            for t, h, tc in db._conn.execute(  # noqa: SLF001
                "SELECT t, hum, temp FROM ams_hist WHERE ams_id=? AND t>=? ORDER BY t", (int(ams_id), int(start))
            ).fetchall()
        ]

"""This app's own spool inventory, AMS tray assignments and filament booking.

Spools, tray assignments and usage history for the single P1S this app drives, so there is
no printer_id.

Storage is the app's shared SQLite file (db.py). The tables are created lazily on first use
and prefixed ``inv_`` so they cannot collide with anything else in app.db.

``spool_dict`` is the spool shape the frontend and the panel API use; ``assignments()``
returns [{ams_id, tray_id, spool_id, spool}].

All functions are synchronous: every call is a handful of indexed
statements on a local SQLite file (well under a millisecond), the same trade-off db.py
already makes. Nothing here raises into the caller: bad input and database errors come
back as values (``{"ok": False, "message": ...}``; reads give ``[]`` / ``None``), because
the callers are the engine's poll loop and command handler, which must never die.
"""

from __future__ import annotations

import functools
import logging
import math
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from . import db

log = logging.getLogger("inventory")

_T = TypeVar("_T")

# ---------------------------------------------------------------------------- slots

AMS_UNITS = range(0, 4)  # regular AMS units: ams_id 0-3, trays 0-3
AMS_HT_UNITS = range(128, 136)  # AMS-HT: ams_id 128+, a single tray 0
EXTERNAL_AMS_ID = 255  # external spool holder; tray 0 (P1S has one), tray 1 on dual-nozzle models

# Field limits. Generous, they only exist to keep garbage (and huge strings) out of the DB.
_TEXT_LIMITS = {"material": 50, "subtype": 50, "brand": 100, "color_name": 100, "note": 500, "location": 60}
MAX_GRAMS = 100_000.0
MAX_ID = 2**31 - 1  # ids beyond this cannot be ours (and >2**63 would overflow sqlite3 binding)


def _as_int(v: Any, lo: int, hi: int) -> int | None:
    """int / integral float / numeric string within [lo, hi], else None.

    Every id or slot that reaches SQL goes through this: ``int("x")`` raises and an int above
    2**63 makes sqlite3 raise OverflowError at bind time, both of which would otherwise
    escape into the engine's command handler.
    """
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        n = v
    else:
        f = _num(v)
        if f is None or f != int(f):
            return None
        n = int(f)
    return n if lo <= n <= hi else None


def valid_slot(ams_id: int, tray_id: int) -> bool:
    """True for a tray that can exist on a P1S (+AMS / AMS-HT / external holder)."""
    if ams_id in AMS_UNITS:
        return 0 <= tray_id <= 3
    if ams_id in AMS_HT_UNITS:
        return tray_id == 0
    if ams_id == EXTERNAL_AMS_ID:
        return tray_id in (0, 1)
    return False


# ---------------------------------------------------------------------------- schema

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inv_spool (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    material        TEXT    NOT NULL DEFAULT '',
    subtype         TEXT    NOT NULL DEFAULT '',
    brand           TEXT    NOT NULL DEFAULT '',
    color_name      TEXT    NOT NULL DEFAULT '',
    color           TEXT    NOT NULL DEFAULT '',
    label_weight    INTEGER NOT NULL DEFAULT 1000,
    core_weight     INTEGER NOT NULL DEFAULT 250,
    weight_used     REAL    NOT NULL DEFAULT 0,
    cost_per_kg     REAL,
    nozzle_temp_min INTEGER,
    nozzle_temp_max INTEGER,
    note            TEXT    NOT NULL DEFAULT '',
    last_used       INTEGER,
    origin          TEXT    NOT NULL DEFAULT 'local',
    archived_at     INTEGER,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS inv_assignment (
    ams_id      INTEGER NOT NULL,
    tray_id     INTEGER NOT NULL,
    spool_id    INTEGER NOT NULL UNIQUE REFERENCES inv_spool(id),
    assigned_at INTEGER NOT NULL,
    PRIMARY KEY (ams_id, tray_id)
);
CREATE TABLE IF NOT EXISTS inv_usage (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    spool_id  INTEGER NOT NULL REFERENCES inv_spool(id),
    grams     REAL    NOT NULL,
    cost      REAL,
    job       TEXT    NOT NULL DEFAULT '',
    ref       TEXT    NOT NULL DEFAULT '',
    source    TEXT    NOT NULL DEFAULT '',
    usage_key TEXT,
    booked_at INTEGER NOT NULL,
    UNIQUE (usage_key, spool_id)
);
CREATE INDEX IF NOT EXISTS inv_usage_by_spool ON inv_usage(spool_id, booked_at);
CREATE INDEX IF NOT EXISTS inv_usage_by_time ON inv_usage(booked_at);
"""

_ready = False
_rev = 0


def _ensure() -> None:
    """Create the tables once per process; cheap no-op afterwards."""
    global _ready
    if _ready:
        return
    with db._lock:  # noqa: SLF001 - db.py exposes no transaction helper; see module notes
        db._conn.executescript(_SCHEMA)  # noqa: SLF001
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(inv_spool)")}  # noqa: SLF001
        if "location" not in cols:  # added later: where the spool is (AMS / shelf / dryer ...)
            db._conn.execute("ALTER TABLE inv_spool ADD COLUMN location TEXT NOT NULL DEFAULT ''")  # noqa: SLF001
            db._conn.commit()  # noqa: SLF001
    _ready = True


@contextmanager
def _tx() -> Iterator[sqlite3.Connection]:
    """One write transaction on the shared connection.

    db.execute() locks per statement only, so a multi-statement change (book usage + bump
    weight, move an assignment, import) needs the lock held for the whole BEGIN..COMMIT,
    otherwise another thread's statement could land inside our transaction.
    BEGIN IMMEDIATE takes the write lock up front so we never fail half-way on SQLITE_BUSY.
    """
    global _rev
    _ensure()
    with db._lock:  # noqa: SLF001
        conn = db._conn  # noqa: SLF001
        before = conn.total_changes
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            # COMMIT inside the try: if it fails (SQLITE_BUSY / disk full) the transaction is
            # still open, and leaving it open would make every later BEGIN fail forever.
            conn.execute("COMMIT")
        except BaseException:
            # Some errors already rolled SQLite back; a blind ROLLBACK would then raise
            # "no transaction is active" and hide the real error.
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error as e:
                    log.error("rollback failed: %s", e)
            raise
        # Only a transaction that really changed rows bumps the revision: no-op commands
        # ("Already assigned", "Spool not found", a repeated sync) must not make the engine
        # push a fresh snapshot to every client.
        if conn.total_changes != before:
            _rev += 1


def _db_safe(on_error: Callable[[], _T]) -> Callable[[Callable[..., _T]], Callable[..., _T]]:
    """Turn a database exception into a logged error value (callers run in the app loop)."""

    def deco(fn: Callable[..., _T]) -> Callable[..., _T]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> _T:
            try:
                return fn(*args, **kwargs)
            except sqlite3.Error as e:
                log.error("inventory.%s: database error: %s", fn.__name__, e)
                return on_error()
            except Exception:  # noqa: BLE001 - a bug here must not kill the engine loop either
                # The engine's command handler only catches KeyError/ValueError/TypeError, so
                # anything else (OverflowError on a bind, a programming error) would escape.
                log.exception("inventory.%s: unexpected error", fn.__name__)
                return on_error()

        return wrapper

    return deco


def _db_fail() -> dict[str, Any]:
    return _fail("Database error")


def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Read query -> list of column dicts (without touching the shared connection's row_factory)."""
    _ensure()
    with db._lock:  # noqa: SLF001
        cur = db._conn.execute(sql, params)  # noqa: SLF001
        cols = [c[0] for c in cur.description or ()]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _rows_tx(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description or ()]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def revision() -> int:
    """Bumped after every committed write: lets the engine know its snapshot is stale."""
    return _rev


def _now() -> int:
    return int(time.time())


def _ok(message: str = "", **extra: Any) -> dict[str, Any]:
    return {"ok": True, "message": message, **extra}


def _fail(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "message": message, **extra}


# ---------------------------------------------------------------------------- value parsing


def normalize_color(v: Any) -> str | None:
    """'#RRGGBB' / 'RRGGBB' / Bambu 'RRGGBBAA' -> '#RRGGBB'; '' for empty; None when invalid."""
    if v is None:
        return ""
    if not isinstance(v, str):
        return None
    s = v.strip().lstrip("#")
    if not s:
        return ""
    if len(s) not in (6, 8) or any(c not in "0123456789abcdefABCDEF" for c in s):
        return None
    return "#" + s[:6].upper()


def _num(v: Any) -> float | None:
    """A finite number (int/float or numeric string) or None. bool is not a number here."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# Accepted input keys -> column. The aliases let the frontend send back a spool dict it got
# from us (temp_min/temp_max) or panel-style keys (rgba) without a translation layer.
_KEY_ALIASES = {
    "temp_min": "nozzle_temp_min",
    "temp_max": "nozzle_temp_max",
    "rgba": "color",
}
_EDITABLE = (
    "material", "subtype", "brand", "color_name", "color", "label_weight", "core_weight",
    "weight_used", "cost_per_kg", "nozzle_temp_min", "nozzle_temp_max", "note", "location",
)


def _clean_fields(data: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Validate the editable fields present in ``data``; returns (columns, error)."""
    out: dict[str, Any] = {}
    for raw_key, v in data.items():
        key = _KEY_ALIASES.get(raw_key, raw_key)
        if key not in _EDITABLE:
            continue  # read-only / unknown keys (id, remaining, created_at, ...) are ignored
        if key in _TEXT_LIMITS:
            if v is None:
                v = ""
            if not isinstance(v, str):
                return {}, f"{key} must be text"
            v = v.strip()
            if len(v) > _TEXT_LIMITS[key]:
                return {}, f"{key} is too long (max {_TEXT_LIMITS[key]})"
            out[key] = v
        elif key == "color":
            c = normalize_color(v)
            if c is None:
                return {}, "color must be #RRGGBB"
            out[key] = c
        elif key in ("label_weight", "core_weight"):
            n = _num(v)
            if n is None or not (0 <= n <= 10_000):
                return {}, f"{key} must be 0-10000 g"
            out[key] = int(round(n))
        elif key == "weight_used":
            n = _num(v)
            if n is None or not (0 <= n <= MAX_GRAMS):
                return {}, "weight_used must be >= 0 g"
            out[key] = round(n, 2)
        elif key == "cost_per_kg":
            if v is None or v == "":
                out[key] = None
                continue
            n = _num(v)
            if n is None or not (0 <= n <= 100_000):
                return {}, "cost_per_kg must be >= 0"
            out[key] = round(n, 4)
        elif key in ("nozzle_temp_min", "nozzle_temp_max"):
            n = _num(v)
            if v is None or v == "" or n == 0:
                out[key] = None  # 0 (also "0" from a form) is what parse_spool reports for "not set"
                continue
            if n is None or not (100 <= n <= 400):
                return {}, f"{key} must be 100-400 C"
            out[key] = int(round(n))
    return out, ""


# ---------------------------------------------------------------------------- output shapes


def spool_dict(row: dict[str, Any]) -> dict[str, Any]:
    """DB row -> the spool dict the frontend / panel API use."""
    label = int(row.get("label_weight") or 0)
    used = float(row.get("weight_used") or 0.0)
    return {
        # --- the base fields
        "id": int(row["id"]),
        "material": row.get("material") or "",
        "subtype": row.get("subtype") or "",
        "brand": row.get("brand") or "",
        "color_name": row.get("color_name") or "",
        "color": row.get("color") or "",
        "label_weight": label,
        "weight_used": used,
        "temp_min": int(row.get("nozzle_temp_min") or 0),
        "temp_max": int(row.get("nozzle_temp_max") or 0),
        # --- extras
        "core_weight": int(row.get("core_weight") or 0),
        "remaining": round(max(0.0, label - used), 1),
        "cost_per_kg": row.get("cost_per_kg"),
        "note": row.get("note") or "",
        "location": row.get("location") or "",
        "last_used": row.get("last_used"),
        "archived": row.get("archived_at") is not None,
        "archived_at": row.get("archived_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "origin": row.get("origin") or "local",
    }


def spool_name(sp: dict[str, Any]) -> str:
    """'Brand Material Subtype' as usage.py already labels spools."""
    return " ".join(f"{sp.get('brand') or ''} {sp.get('material') or ''} {sp.get('subtype') or ''}".split())


# ---------------------------------------------------------------------------- spools


@_db_safe(list)
def list_spools(include_archived: bool = False) -> list[dict[str, Any]]:
    """All spools, id order (the UI sorts itself, as stats.spools() does today)."""
    where = "" if include_archived else "WHERE archived_at IS NULL"
    return [spool_dict(r) for r in _rows(f"SELECT * FROM inv_spool {where} ORDER BY id")]


def _get_spool_raw(sid: int) -> dict[str, Any] | None:
    """get_spool without the error wrapper: write paths need a database error to stay an
    error, not turn into "Spool not found" (which would tell the user the spool is gone)."""
    rows = _rows("SELECT * FROM inv_spool WHERE id=?", (sid,))
    return spool_dict(rows[0]) if rows else None


@_db_safe(lambda: None)
def get_spool(spool_id: int) -> dict[str, Any] | None:
    """One spool (archived ones too), or None when unknown / the id is not a valid id."""
    sid = _as_int(spool_id, 1, MAX_ID)
    if sid is None:
        return None
    return _get_spool_raw(sid)


@_db_safe(_db_fail)
def create_spool(data: dict[str, Any]) -> dict[str, Any]:
    """New local spool. ``material`` is required; everything else has a sane default."""
    if not isinstance(data, dict):
        return _fail("Bad request")
    fields, err = _clean_fields(data)
    if err:
        return _fail(err)
    if not fields.get("material"):
        return _fail("material is required")
    err = _temp_order_error(fields.get("nozzle_temp_min"), fields.get("nozzle_temp_max"))
    if err:
        return _fail(err)
    now = _now()
    fields.update(created_at=now, updated_at=now, origin="local")
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    with _tx() as conn:
        cur = conn.execute(f"INSERT INTO inv_spool ({cols}) VALUES ({marks})", tuple(fields.values()))
        sid = int(cur.lastrowid or 0)
    sp = _get_spool_raw(sid)
    log.info("spool #%d created (%s)", sid, spool_name(sp or {}))
    return _ok("Spool created", spool=sp)


def _temp_order_error(tmin: Any, tmax: Any) -> str:
    if tmin is not None and tmax is not None and tmin > tmax:
        return "temp_min must not be above temp_max"
    return ""


@_db_safe(_db_fail)
def patch_spool(spool_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """Update only the fields present in ``data``; unknown / read-only keys are ignored."""
    if not isinstance(data, dict):
        return _fail("Bad request")
    sid = _as_int(spool_id, 1, MAX_ID)
    if sid is None:
        return _fail("Spool not found")
    fields, err = _clean_fields(data)
    if err:
        return _fail(err)
    if "material" in fields and not fields["material"]:
        return _fail("material is required")
    cur_sp = _get_spool_raw(sid)
    if cur_sp is None:
        return _fail("Spool not found")
    if not fields:
        return _ok("Nothing changed", spool=cur_sp)
    tmin = fields.get("nozzle_temp_min", cur_sp["temp_min"] or None)
    tmax = fields.get("nozzle_temp_max", cur_sp["temp_max"] or None)
    err = _temp_order_error(tmin, tmax)
    if err:
        return _fail(err)
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k}=?" for k in fields)
    with _tx() as conn:
        conn.execute(f"UPDATE inv_spool SET {sets} WHERE id=?", (*fields.values(), sid))
    return _ok("Spool saved", spool=_get_spool_raw(sid))


@_db_safe(_db_fail)
def archive_spool(spool_id: int, archived: bool = True) -> dict[str, Any]:
    """Archive (used up / shelved) or restore a spool.

    Archiving also frees its tray: an archived spool cannot be assigned, and a stale assignment would keep booking prints onto it.
    Archiving an already archived spool keeps the original archived_at (the "used up on"
    date must not move just because the button was pressed again).
    """
    sid = _as_int(spool_id, 1, MAX_ID)
    if sid is None:
        return _fail("Spool not found")
    now = _now()
    with _tx() as conn:
        if not conn.execute("SELECT 1 FROM inv_spool WHERE id=?", (sid,)).fetchone():
            return _fail("Spool not found")
        if archived:
            conn.execute(
                "UPDATE inv_spool SET archived_at=COALESCE(archived_at, ?), updated_at=? WHERE id=?",
                (now, now, sid),
            )
            conn.execute("DELETE FROM inv_assignment WHERE spool_id=?", (sid,))
        else:
            conn.execute("UPDATE inv_spool SET archived_at=NULL, updated_at=? WHERE id=?", (now, sid))
    return _ok("Spool archived" if archived else "Spool restored", spool=_get_spool_raw(sid))


# ---------------------------------------------------------------------------- assignments


@_db_safe(_db_fail)
def assign(spool_id: int, ams_id: int, tray_id: int) -> dict[str, Any]:
    """Put a spool in a tray, replacing whatever spool was assigned there.

    We also remove the spool from any other tray it was assigned to: a
    physical spool sits in one slot, and a double assignment would book a print twice.
    """
    sid, ams, tray = _as_int(spool_id, 1, MAX_ID), _as_int(ams_id, 0, 255), _as_int(tray_id, 0, 255)
    if sid is None or ams is None or tray is None:
        return _fail("Bad request")
    spool_id, ams_id, tray_id = sid, ams, tray
    if not valid_slot(ams_id, tray_id):
        return _fail(f"Invalid tray (ams {ams_id}, tray {tray_id})")
    with _tx() as conn:
        row = conn.execute("SELECT archived_at FROM inv_spool WHERE id=?", (spool_id,)).fetchone()
        if row is None:
            return _fail("Spool not found")
        if row[0] is not None:
            return _fail("Cannot assign an archived spool")
        prev = conn.execute("SELECT ams_id, tray_id FROM inv_assignment WHERE spool_id=?", (spool_id,)).fetchone()
        if prev is not None and (prev[0], prev[1]) == (ams_id, tray_id):
            return _ok("Already assigned")
        conn.execute("DELETE FROM inv_assignment WHERE spool_id=?", (spool_id,))
        conn.execute("DELETE FROM inv_assignment WHERE ams_id=? AND tray_id=?", (ams_id, tray_id))
        conn.execute(
            "INSERT INTO inv_assignment (ams_id, tray_id, spool_id, assigned_at) VALUES (?, ?, ?, ?)",
            (ams_id, tray_id, spool_id, _now()),
        )
    moved = {"ams_id": prev[0], "tray_id": prev[1]} if prev is not None else None
    return _ok("Spool assigned", moved_from=moved)


@_db_safe(_db_fail)
def unassign(ams_id: int, tray_id: int) -> dict[str, Any]:
    """Empty a tray's assignment (no error when it was already empty: the end state is the same)."""
    ams, tray = _as_int(ams_id, 0, 255), _as_int(tray_id, 0, 255)
    if ams is None or tray is None:
        return _fail("Bad request")
    if not valid_slot(ams, tray):
        return _fail(f"Invalid tray (ams {ams}, tray {tray})")
    with _tx() as conn:
        n = conn.execute("DELETE FROM inv_assignment WHERE ams_id=? AND tray_id=?", (ams, tray)).rowcount
    return _ok("Spool unassigned" if n else "Tray was not assigned")


@_db_safe(list)
def assignments() -> list[dict[str, Any]]:
    """Loaded trays: [{ams_id, tray_id, spool_id, spool{...}}]."""
    rows = _rows(
        "SELECT a.ams_id AS a_ams, a.tray_id AS a_tray, a.assigned_at AS a_at, s.* "
        "FROM inv_assignment a JOIN inv_spool s ON s.id = a.spool_id ORDER BY a.ams_id, a.tray_id"
    )
    return [
        {
            "ams_id": r["a_ams"],
            "tray_id": r["a_tray"],
            "spool": spool_dict(r),
            "spool_id": r["id"],
            "assigned_at": r["a_at"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------- usage booking


def _merge_items(items: Iterable[Any]) -> tuple[dict[int, float], list[str]]:
    """Validate booking items and sum grams per spool (two lines for one spool = one booking)."""
    merged: dict[int, float] = {}
    errors: list[str] = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            errors.append(f"item {i}: not an object")
            continue
        sid = _as_int(it.get("spool_id"), 1, MAX_ID)
        grams = _num(it.get("grams"))
        if sid is None:
            errors.append(f"item {i}: no spool")
            continue
        if grams == 0:
            continue  # a loaded tray the print did not touch: nothing to book, not an error
        if grams is None or not (0 < grams <= MAX_GRAMS):
            errors.append(f"item {i}: bad grams")
            continue
        merged[sid] = merged.get(sid, 0.0) + grams
    return merged, errors


def _book_fail(message: str) -> dict[str, Any]:
    return _fail(message, booked=[], skipped=[], errors=[message])


@_db_safe(lambda: _book_fail("Database error"))
def book_usage(
    items: Iterable[dict[str, Any]],
    job: str = "",
    ref: str = "",
    source: str = "print",
    key: str | None = None,
    default_cost_per_kg: float | None = None,
) -> dict[str, Any]:
    """Book consumed filament: ``weight_used += grams`` per spool plus one history row each.

    ``items`` is a list of ``{"spool_id": int, "grams": float}`` (extra keys such as the
    usage.py ``tray``/``label`` are ignored). Idempotent on ``key`` (default: ``ref``): a
    spool already booked under the same key is skipped, so pressing "sync" twice, or a
    retry after a crash, cannot double-book. With neither key nor ref every call books.

    Everything happens in one transaction: either all valid lines are booked or none.
    Unknown spools are reported in ``errors`` and do not block the other lines. Archived
    spools are still booked: a spool often runs out (and is archived) right after a print.

    Cost: the spool's cost_per_kg, or
    ``default_cost_per_kg`` when the spool has
    none; no cost when the rate is 0.
    """
    usage_key = str((key if key is not None else ref) or "")[:200] or None
    if isinstance(items, (str, bytes, dict)):
        return _book_fail("items is not a list")
    try:
        merged, errors = _merge_items(list(items or []))
    except TypeError:
        return _book_fail("items is not a list")
    default_rate = _num(default_cost_per_kg)
    booked: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    now = _now()
    job, ref, source = str(job or "")[:500], str(ref or "")[:200], str(source or "")[:40]
    with _tx() as conn:
        for sid, grams in merged.items():
            grams = round(grams, 2)
            if grams <= 0:
                continue  # sub-0.005 g dust: not worth a history row
            row = conn.execute("SELECT cost_per_kg FROM inv_spool WHERE id=?", (sid,)).fetchone()
            if row is None:
                errors.append(f"spool #{sid} not found")
                continue
            rate = row[0] if row[0] is not None else default_rate
            cost = round(grams / 1000.0 * rate, 2) if rate and rate > 0 else None
            cur = conn.execute(
                "INSERT OR IGNORE INTO inv_usage (spool_id, grams, cost, job, ref, source, usage_key, booked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, grams, cost, job, ref, source, usage_key, now),
            )
            if cur.rowcount != 1:
                skipped.append({"spool_id": sid, "grams": grams, "reason": "already booked"})
                continue
            conn.execute(
                "UPDATE inv_spool SET weight_used = round(weight_used + ?, 2), last_used=?, updated_at=? WHERE id=?",
                (grams, now, now, sid),
            )
            booked.append({"spool_id": sid, "grams": grams, "cost": cost})
    if booked:
        log.info("booked %s for '%s'", ", ".join(f"#{b['spool_id']} {b['grams']} g" for b in booked), job)
    msg = "; ".join(errors) or ("Booked" if booked else "Already booked" if skipped else "Nothing to book")
    return {"ok": not errors, "message": msg, "booked": booked, "skipped": skipped, "errors": errors}


@_db_safe(list)
def usage_history(spool_id: int | None = None, limit: int = 100, key: str | None = None) -> list[dict[str, Any]]:
    """Newest bookings first, optionally for one spool and/or one usage key."""
    where: list[str] = []
    params: list[Any] = []
    if spool_id is not None:
        sid = _as_int(spool_id, 1, MAX_ID)
        if sid is None:
            return []
        where.append("u.spool_id=?")
        params.append(sid)
    if key is not None:
        where.append("u.usage_key=?")
        params.append(str(key))
    lim = _as_int(limit, -(2**62), 2**62)
    limit = 100 if lim is None else max(1, min(lim, 5000))
    sql = (
        "SELECT u.*, s.brand, s.material, s.subtype, s.color FROM inv_usage u "
        "LEFT JOIN inv_spool s ON s.id = u.spool_id "
        + ("WHERE " + " AND ".join(where) + " " if where else "")
        + "ORDER BY u.booked_at DESC, u.id DESC LIMIT ?"
    )
    out = []
    for r in _rows(sql, (*params, limit)):
        out.append(
            {
                "id": r["id"],
                "spool_id": r["spool_id"],
                "grams": r["grams"],
                "cost": r["cost"],
                "job": r["job"],
                "ref": r["ref"],
                "source": r["source"],
                "key": r["usage_key"],
                "booked_at": r["booked_at"],
                "spool_name": spool_name(r),
                "color": r["color"] or "",
            }
        )
    return out

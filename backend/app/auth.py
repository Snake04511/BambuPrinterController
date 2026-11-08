"""Own login: one password / PIN (scrypt hash in SQLite) + session cookie.

First start: no password stored -> the frontend shows a "set password" screen.
5 wrong attempts lock the login for 60 s (like the panel's web page).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

from fastapi import HTTPException, Request

from . import db

COOKIE = "pd_session"
SESSION_DAYS = 180
_MAX_FAILS = 5
_LOCK_S = 60

_fails = 0
_locked_until = 0.0


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def is_setup() -> bool:
    return bool(db.kv_get("auth:hash"))


def set_password(password: str) -> None:
    salt = os.urandom(16)
    db.kv_set("auth:salt", salt.hex())
    db.kv_set("auth:hash", _hash(password, salt).hex())
    # A new password logs every device out.
    db.execute("DELETE FROM sessions")


def check_password(password: str) -> None:
    """Raises HTTPException on a wrong password / lockout."""
    global _fails, _locked_until
    now = time.time()
    if now < _locked_until:
        raise HTTPException(429, "Too many wrong attempts - wait a minute.")
    salt = bytes.fromhex(db.kv_get("auth:salt", ""))
    ok = hmac.compare_digest(_hash(password, salt).hex(), db.kv_get("auth:hash", ""))
    if not ok:
        _fails += 1
        if _fails >= _MAX_FAILS:
            _fails = 0
            _locked_until = now + _LOCK_S
            raise HTTPException(429, "Too many wrong attempts - wait a minute.")
        raise HTTPException(401, "Wrong password.")
    _fails = 0


def new_session() -> str:
    token = secrets.token_hex(32)
    now = time.time()
    db.execute("INSERT INTO sessions(token, created, last_seen) VALUES(?,?,?)", (token, now, now))
    return token


def drop_session(token: str) -> None:
    db.execute("DELETE FROM sessions WHERE token=?", (token,))


def valid(token: str | None) -> bool:
    if not token:
        return False
    rows = db.execute("SELECT last_seen FROM sessions WHERE token=?", (token,))
    if not rows:
        return False
    now = time.time()
    if now - rows[0][0] > SESSION_DAYS * 86400:
        drop_session(token)
        return False
    if now - rows[0][0] > 3600:  # sliding expiry, written at most hourly
        db.execute("UPDATE sessions SET last_seen=? WHERE token=?", (now, token))
    return True


def require(request: Request) -> None:
    """FastAPI dependency for every protected route: the web login cookie, or the panel's
    API key (X-API-Key, the same key as the panel's /api/v1 routes)."""
    if valid(request.cookies.get(COOKIE)):
        return
    from . import settings

    if settings.api_key_ok(request.headers.get("x-api-key")):
        return
    raise HTTPException(401, "Not logged in")

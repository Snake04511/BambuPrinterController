"""App settings (non-hardware part of the panel's ConfigManager) + secrets.

Settings are returned to the frontend; secrets (printer access code, panel API key,
webhook URLs, tokens) never are - only "is set" flags.
"""

from __future__ import annotations

import os
from typing import Any

from . import db
from .notify import SETTINGS_DEFAULTS as _NOTIFY_DEFAULTS, SETTINGS_RULES as _NOTIFY_RULES
from .obico_client import SETTINGS_DEFAULTS as _OBICO_DEFAULTS, SETTINGS_RULES as _OBICO_RULES

DEFAULTS: dict[str, Any] = {
    "printer_name": "",
    "chamber_control": True,  # show / allow the chamber temperature
    "file_previews": True,  # SD previews (download + cache)
    "sd_while_printing": False,  # read printer-SD files during a print too
    "print_timelapse": False,  # last choice in the SD print summary
    "print_bed_level": True,
    "print_flow_cali": False,
    "print_speed_l1": 0,  # speed mode after layer 1, 0 = keep
    "light_off_on_end": True,  # light off 2 min after a print ends
    "light_on_print": False,  # light on for every print
    "hide_external": False,  # HOME filament card without the external spool
    "record_timelapse": True,  # camera frames of every print (with Obico scores)
    "timelapse_keep_count": 30,  # recordings kept (oldest go first)
    "timelapse_keep_gb": 3,  # all recordings together
    "data_limit_gb": 50,
    "file_cache_gb": 5,  # kept copies of the printer's 3MF files
    "dry_humidity": 40,  # % AMS humidity counted as "humid" for the drying reminder
    "dry_hours": 24,  # humid hours since the last drying before a reminder
    "dry_materials": "PETG,TPU,PA,PC,PVA,PET,ABS,ASA",  # moisture-sensitive materials
    "summary_mode": "daily",  # off / daily / weekly (sent on Sunday)
    "summary_hour": 21,
    "currency": "Ft",
    "power_watts": 120,  # average draw while printing (P1S: ~100-150 W with a heated bed)
    "kwh_price": 70.0,  # electricity price per kWh in the currency above
    "backup_enabled": True,  # daily backup to /backups
    "backup_keep": 14,  # everything in /data; timelapses are dropped to stay under it
    "light_with_obico": True,  # light on during prints while Obico detection is on
    "notif_seen_id": -1,  # newest notification already seen (top bar)
    "printer_ip": os.environ.get("PRINTER_IP", ""),  # LAN connection (MQTT, FTPS, camera)
    "printer_serial": os.environ.get("PRINTER_SERIAL", ""),
    **_NOTIFY_DEFAULTS,
    **_OBICO_DEFAULTS,
    "obico_frame_base_url": os.environ.get("APP_URL", ""),
}

# Validation: type + optional range / allowed values.
_RULES: dict[str, tuple] = {
    "printer_name": (str,),
    "chamber_control": (bool,),
    "file_previews": (bool,),
    "sd_while_printing": (bool,),
    "print_timelapse": (bool,),
    "print_bed_level": (bool,),
    "print_flow_cali": (bool,),
    "print_speed_l1": (int, 0, 4),
    "light_off_on_end": (bool,),
    "light_on_print": (bool,),
    "hide_external": (bool,),
    "record_timelapse": (bool,),
    "timelapse_keep_count": (int, 1, 1000),
    "timelapse_keep_gb": (int, 1, 1000),
    "data_limit_gb": (int, 1, 2000),
    "file_cache_gb": (int, 1, 1000),
    "dry_humidity": (int, 5, 95),
    "dry_hours": (int, 1, 2000),
    "dry_materials": (str,),
    "summary_mode": (str,),
    "summary_hour": (int, 0, 23),
    "currency": (str,),
    "power_watts": (int, 0, 3000),
    "kwh_price": (float, 0, 100000),
    "backup_enabled": (bool,),
    "backup_keep": (int, 1, 365),
    "light_with_obico": (bool,),
    "notif_seen_id": (int, -1, 2**31),
    "printer_ip": (str,),
    "printer_serial": (str,),
    **_NOTIFY_RULES,
    **_OBICO_RULES,
}

# Secrets the Settings page may set (never returned, only "is set" + a masked prefix).
SECRET_KEYS = ("printer_access_code", "discord_webhook_url", "ntfy_token", "obico_ml_token", "panel_api_key")
# A panel set up before it had its own key still sends the key it used before: accepted too
# (never shown, not settable). Generating a new panel key in Settings does not remove it.
LEGACY_PANEL_KEY = "panel_api_key_legacy"
_legacy_logged = False


def api_key_ok(key: str | None) -> bool:
    """X-API-Key of the ESP32 panel (or anything else using the /api/v1 / /api routes)."""
    import secrets as _s

    global _legacy_logged
    if not key:
        return False
    for name in ("panel_api_key", LEGACY_PANEL_KEY):
        want = secret(name)
        if want and _s.compare_digest(key.encode(), want.encode()):
            if name == LEGACY_PANEL_KEY and not _legacy_logged:
                _legacy_logged = True
                import logging

                logging.getLogger("settings").info("a client uses the legacy panel key (Settings > ESP32 panel has the new one)")
            return True
    return False

_cache: dict[str, Any] | None = None


def get() -> dict[str, Any]:
    global _cache
    if _cache is None:
        stored = db.kv_get("settings", {}) or {}
        # Per-channel notification switches came later: start from the old common switch.
        for k in list(DEFAULTS):
            for ch in ("discord", "ntfy"):
                pre = f"notify_{ch}_on_"
                if k.startswith(pre) and k not in stored and f"notify_on_{k[len(pre):]}" in stored:
                    stored[k] = stored[f"notify_on_{k[len(pre):]}"]
        _cache = {k: stored.get(k, v) for k, v in DEFAULTS.items()}
    return dict(_cache)


def update(changes: dict[str, Any]) -> dict[str, Any]:
    """Validates and stores the given keys. Raises ValueError on bad input."""
    cur = get()
    for key, value in changes.items():
        rule = _RULES.get(key)
        if rule is None:
            raise ValueError(f"Unknown setting: {key}")
        typ = rule[0]
        if typ is bool:
            if not isinstance(value, bool):
                raise ValueError(f"{key}: expected true/false")
        elif typ is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key}: expected a number")
            value = float(value)
            if not rule[1] <= value <= rule[2]:
                raise ValueError(f"{key}: out of range {rule[1]}..{rule[2]}")
        elif typ is int:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key}: expected a number")
            value = int(value)
            if not rule[1] <= value <= rule[2]:
                raise ValueError(f"{key}: out of range {rule[1]}..{rule[2]}")
        elif typ is str:
            if not isinstance(value, str) or len(value) > 256:
                raise ValueError(f"{key}: expected text")
            value = value.strip()
        cur[key] = value
    db.kv_set("settings", cur)
    global _cache
    _cache = cur
    return dict(cur)


# ---------------------------------------------------------------- secrets


def secret(name: str) -> str:
    return db.kv_get(f"secret:{name}", "") or ""


def set_secret(name: str, value: str) -> None:
    if value:
        db.kv_set(f"secret:{name}", value)
    else:
        db.kv_delete(f"secret:{name}")


def mask(value: str) -> str:
    """'bb_Abc12... (46 chars)' - safe to show (prefix only), like the panel."""
    if not value:
        return "(none)"
    if len(value) <= 8:
        return "****"
    return f"{value[:6]}... ({len(value)} chars)"


def migrate() -> None:
    """Start-up: settings of removed features are dropped (after the values derived from
    them, e.g. the per-channel notification switches, were stored)."""
    stored = db.kv_get("settings", {}) or {}
    if any(k not in DEFAULTS for k in stored):
        db.kv_set("settings", get())

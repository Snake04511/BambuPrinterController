"""Direct LAN MQTT client for a Bambu Lab printer (built for the P1S).

The app talks to the printer's
own broker (TLS :8883, user "bblp", password = LAN access code, self-signed cert)
on ``device/<serial>/report`` and ``device/<serial>/request``.

Threading model - why it looks like this:
  * paho-mqtt runs its own network thread (``loop_start``), so a slow TLS handshake or
    a dead socket never blocks the FastAPI event loop.
  * The paho thread only decodes JSON and hands the dict to the event loop with
    ``call_soon_threadsafe``; ALL state merging and callbacks run on the loop thread,
    so the merged state needs no locks and ``on_change`` runs where the Engine lives.
  * Every client gets a generation number; callbacks of a retired client are ignored,
    which makes "tear down and rebuild" safe even while paho is still delivering.

Reconnects: every paho client connects exactly ONCE (``reconnect_on_failure=False``).
A dropped or refused session is torn down and a fresh client (new client_id, empty
QoS-1 queue) is built by a small asyncio supervisor with its own 2..60 s backoff. Why:
paho's in-place auto-reconnect re-sends every unacknowledged QoS-1 publish, and the
Bambu broker does not reliably PUBACK - so a stale ``stop``, jog or
``project_file`` would be replayed to the printer after a Wi-Fi hiccup (
0500_4003). Only the very first TCP connect is retried by paho itself (nothing is
queued then). The supervisor also handles the P1/P1S firmware bug where the broker
keeps the TCP session alive but stops publishing (nudge with pushall at 30 s of
silence, rebuild the client at 60 s), and a paho loop
that stopped making progress (rebuild after 5 min offline).

The P1S sends a full state only on "pushall"; afterwards every report is a partial
delta. ``ingest`` merges those deltas into one full state (dicts deep-merged, lists
replaced, AMS units/trays merged by id with stale-slot rules), and
``status()`` derives the normalized status dict the rest of the app reads.

Command payloads are the ones Bambu's own clients send:
same commands, sequence ids, G-code strings and field names. Sends return as soon as
paho accepted the publish; the printer's reaction shows up in the next reports.
The access code is never logged.
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import logging
import math
import os
import ssl
import time
from dataclasses import dataclass
from typing import Any, Callable

import paho.mqtt.client as mqtt

log = logging.getLogger("printer_mqtt")

PORT = 8883
USERNAME = "bblp"
KEEPALIVE_S = 30
TICK_S = 5.0  # supervisor period
NUDGE_S = 30.0  # silent this long -> ask for a pushall
STALE_S = 60.0  # silent this long -> rebuild the session (P1 firmware stops publishing)
DEAD_S = 300.0  # offline this long although paho keeps retrying -> rebuild the client
PUSHALL_S = 300.0  # periodic full refresh; Bambu warns P1 firmware lags on frequent pushall
RETRY_MIN_S = 2.0
RETRY_MAX_S = 60.0
MAX_PAYLOAD = 512 * 1024  # a P1S pushall is ~10 KB; anything huge is not a report

ACTIVE_STATES = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE"})

# Model capability tables (display names + internal codes)
# printer_manager.py. The P1S has neither: its chamber_temper is meaningless and
# it is dropped, so has_chamber is False.
CHAMBER_SENSOR_MODELS = frozenset(
    {"X1", "X1C", "X1E", "X2D", "P2S", "H2C", "H2D", "H2DPRO", "H2S",
     "BL-P001", "C13", "N6", "O1D", "O1C", "O1C2", "O1S", "O1E", "O2D", "N7"}
)  # fmt: skip
CHAMBER_HEATER_MODELS = frozenset(
    {"H2C", "H2D", "H2DPRO", "H2S", "X2D", "O1C", "O1C2", "O1D", "O1E", "O2D", "O1S", "N6"}
)
X1_FAMILY = frozenset({"X1", "X1C", "X1E"})

# "Empty" placeholders: a delta carrying these must not wipe known tray data.
_EMPTY_VALUES = (None, "", "0000000000000000", "00000000000000000000000000000000")
_TRAY_ALWAYS_UPDATE = frozenset(
    {"remain", "k", "id", "cali_idx", "tray_type", "tray_sub_brands", "tray_info_idx", "tray_color", "tray_id_name"}
)
_TRAY_CLEARED = {
    "tray_type": "",
    "tray_sub_brands": "",
    "tray_color": "",
    "tray_id_name": "",
    "tray_info_idx": "",
    "tag_uid": "0000000000000000",
    "tray_uuid": "00000000000000000000000000000000",
    "remain": 0,
}


# ---------------------------------------------------------------- small value helpers


def _int(v: Any, default: int | None = None) -> int | None:
    """Printer fields arrive as int, float or numeric string ("255"); bool is not a number."""
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            try:
                return int(float(v))
            except ValueError:
                return default
    return default


def _float(v: Any, default: float | None = None) -> float | None:
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return default
    return default


def _hex(v: Any) -> int | None:
    """Bit masks (tray_exist_bits, stat...) are hex strings; ints tolerated."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v:
        try:
            return int(v, 16)
        except ValueError:
            return None
    return None


def _s(v: Any) -> str:
    return v if isinstance(v, str) else ""


def parse_color(v: Any) -> str:
    """Bambu 'RRGGBBAA' (or '#RRGGBB') -> '#RRGGBB' ('' when unknown)."""
    s = _s(v).lstrip("#")
    if len(s) >= 6 and all(c in "0123456789abcdefABCDEF" for c in s[:6]):
        return "#" + s[:6].upper()
    return ""


_color = parse_color


def fan_percent(v: Any) -> int | None:
    """Fan value -> %: the printer reports 0-15 levels (some firmware 0-255)."""
    if v is None:
        return None
    try:
        speed = int(v)
    except (TypeError, ValueError):
        return None
    if speed <= 15:
        return round(speed * 100 / 15)
    if speed <= 255:
        return round(speed * 100 / 255)
    return speed


def wifi_dbm(v: Any) -> int | None:
    """'-45dBm' / -45 -> -45."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try:
            return int(v.replace("dBm", "").strip())
        except ValueError:
            return None
    return None


def _deep_merge(old: Any, new: dict) -> dict:
    """Deltas carry only changed keys: nested dicts merge, lists and scalars replace."""
    out = dict(old) if isinstance(old, dict) else {}
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def apply_tray_exist_bits(units: list[dict], bits_raw: Any, power_on_flag: Any = True) -> int:
    """Wipe filament fields of slots whose ``tray_exist_bits`` bit is 0 (firmware's
    authoritative "spool present" mask - Bambu Studio uses it the same way).

    Skipped on the power-off pattern (all-zero bits with power_on_flag False), which
    would otherwise blank every slot when the AMS is unpowered. AMS-HT (128-135) has
    one presence bit at 16 + (id - 128). Returns the number of slots cleared.
    """
    bits = _hex(bits_raw)
    if bits is None or (bits == 0 and power_on_flag is False):
        return 0
    cleared = 0
    for unit in units:
        uid = _int(unit.get("id"))
        if uid is None:
            continue
        is_ht = 128 <= uid <= 135
        if not is_ht and not 0 <= uid <= 15:
            continue
        for tray in unit.get("tray") or []:
            if not isinstance(tray, dict):
                continue
            tid = _int(tray.get("id"))
            if tid is None:
                continue
            bit = (16 + uid - 128) if is_ht else (uid * 4 + tid)
            if (bits >> bit) & 1:
                continue
            tray["state"] = 9
            if tray.get("tray_type"):
                tray.update(_TRAY_CLEARED)
                cleared += 1
    return cleared


def _merge_trays(old: list, new: list, is_ht: bool) -> list:
    """Per-tray merge: placeholder values in a delta never wipe known data,
    an explicit tray_type "" clears the slot, and a non-HT slot reported with
    state != 11 (not loaded) and no tray_type drops its stale filament. In addition,
    trays missing from the delta are kept (a slot does not vanish)."""
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for t in old:
        if isinstance(t, dict) and t.get("id") is not None:
            key = str(t["id"])
            by_id[key] = dict(t)
            order.append(key)
    for nt in new:
        if not isinstance(nt, dict) or nt.get("id") is None:
            continue
        key = str(nt["id"])
        cur = by_id.get(key)
        if cur is None:
            by_id[key] = dict(nt)
            order.append(key)
            continue
        nt = dict(nt)
        clearing = nt.get("tray_type") == ""
        st = _int(nt.get("state"))
        if st is not None and st != 11 and not is_ht and "tray_type" not in nt and cur.get("tray_type"):
            clearing = True
            nt.update(_TRAY_CLEARED)
            nt.update({"k": None, "cali_idx": None})
        for k, v in nt.items():
            if k in _TRAY_ALWAYS_UPDATE or clearing or v not in _EMPTY_VALUES:
                cur[k] = v
    return sorted((by_id[k] for k in order), key=lambda t: _int(t.get("id"), 0) or 0)


def _unit_attached(unit: dict, ams_exist_bits: int) -> bool:
    """Regular AMS 0-15 have one bit each in ams_exist_bits; other ids (HT) are kept."""
    uid = _int(unit.get("id"))
    if uid is None or not 0 <= uid <= 15:
        return True
    return bool((ams_exist_bits >> uid) & 1)


def _tray_status(t: dict) -> dict:
    """One tray in status() form, from raw MQTT tray fields.

    tag_uid "000..." becomes null, coerces numeric strings and
    defaults a missing remain to 0 - mirrored here so the values match exactly.
    """
    uid = _s(t.get("tag_uid"))
    if uid in ("", "0000000000000000"):
        uid = ""
    ttype = _s(t.get("tray_type"))
    remain = _int(t.get("remain"), -1) if "remain" in t else 0
    return {
        "id": _int(t.get("id"), 0),
        "type": ttype,
        "sub_brands": _s(t.get("tray_sub_brands")),
        "info_idx": _s(t.get("tray_info_idx")),
        "has_filament": bool(ttype),
        "color": _color(t.get("tray_color")),
        "remain": remain,
        "state": _int(t.get("state"), -1),
        "temp_min": _int(t.get("nozzle_temp_min"), 0),
        "temp_max": _int(t.get("nozzle_temp_max"), 0),
        "k": _float(t.get("k"), 0.0),
        "rfid": bool(uid) and uid.strip("0") != "",
    }


# ---------------------------------------------------------------- HMS


def _hms_lookup(code: str) -> str | None:
    """Lazy import: the ~800 KB text table only loads once a fault actually shows up."""
    try:
        from . import printer_hms
    except ImportError:  # pragma: no cover - data module missing
        return None
    return printer_hms.lookup(code)


def _print_error_level(error: int) -> int:
    return {0x4: 1, 0x8: 2, 0xC: 3}.get((error >> 12) & 0xF, 0)


_USER_ACTION_CODES = frozenset({"0300_400C", "0500_400E"})  # cancel echoes, not faults


def parse_hms(hms_list: Any, print_error: Any) -> list[dict]:
    """``hms[]`` + ``print_error`` -> fault rows {full_code, short_code, severity, description}.

    Codes below 0x4000 are status/phase indicators, the two cancel
    echoes are dropped, hms severity = code >> 16 (1 error, 2 warning, 3 info, 0 invalid),
    print_error severity from its error word's first hex digit, and a print_error that
    duplicates an hms[] entry (same MMMM_EEEE) is not listed twice.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for h in hms_list if isinstance(hms_list, list) else []:
        if not isinstance(h, dict):
            continue
        attr, code = _hex_or_int(h.get("attr")), _hex_or_int(h.get("code"))
        if code < 0x4000:
            continue
        short = f"{(attr >> 16) & 0xFFFF:04X}_{code & 0xFFFF:04X}"
        if short in _USER_ACTION_CODES:
            continue
        full = f"{attr & 0xFFFFFFFF:08X}{code & 0xFFFFFFFF:08X}"
        seen.add(short)
        out.append(
            {"full_code": full, "short_code": short, "severity": (code >> 16) & 0xFFFF, "description": _hms_lookup(full)}
        )
    pe = _int(print_error, 0) or 0
    if pe:
        pe &= 0xFFFFFFFF
        module, error = (pe >> 16) & 0xFFFF, pe & 0xFFFF
        short = f"{module:04X}_{error:04X}"
        if error >= 0x4000 and short not in _USER_ACTION_CODES and short not in seen:
            full = f"{pe:08X}"
            out.append(
                {"full_code": full, "short_code": short, "severity": _print_error_level(error), "description": _hms_lookup(full)}
            )
    return out


def _hex_or_int(v: Any) -> int:
    """HMS attr/code are ints on the P1S; some firmware sends hex strings ("0x0300...")."""
    if isinstance(v, bool) or v is None:
        return 0
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v:
        try:
            return int(v.replace("0x", ""), 16)
        except ValueError:
            return 0
    return 0


# ---------------------------------------------------------------- command payloads


def cmd_pushall() -> dict:
    return {"pushing": {"command": "pushall", "sequence_id": "0"}}


def cmd_get_version(seq: int) -> dict:
    return {"info": {"sequence_id": str(seq), "command": "get_version"}}


def cmd_print(command: str) -> dict:
    """pause / resume / stop / clean_print_error: sent with sequence_id "0"."""
    return {"print": {"command": command, "sequence_id": "0"}}


def cmd_gcode(gcode: str, seq: int) -> dict:
    return {"print": {"command": "gcode_line", "param": gcode, "sequence_id": str(seq)}}


def cmd_print_speed(level: int) -> dict:
    return {"print": {"command": "print_speed", "param": str(level), "sequence_id": "0"}}


def cmd_ledctrl(node: str, on: bool, seq: int) -> dict:
    return {
        "system": {
            "command": "ledctrl",
            "led_node": node,
            "led_mode": "on" if on else "off",
            "led_on_time": 500,
            "led_off_time": 500,
            "loop_times": 0,
            "interval_time": 0,
            "sequence_id": str(seq),
        }
    }


def cmd_skip_objects(obj_ids: list[int], seq: int) -> dict:
    return {"print": {"sequence_id": str(seq), "command": "skip_objects", "obj_list": list(obj_ids)}}


def cmd_ams_load(tray: int, seq: int) -> dict:
    """ams_change_filament to a global tray: 0-15 AMS slot, 128-135 AMS-HT, 254 external."""
    if tray == 254:
        ams_id, slot_id = 255, 254  # external-spool form
    elif 128 <= tray <= 135:
        ams_id, slot_id = tray, 0
    else:
        ams_id, slot_id = tray // 4, tray % 4
    return {
        "print": {
            "command": "ams_change_filament",
            "sequence_id": str(seq),
            "ams_id": ams_id,
            "slot_id": slot_id,
            "target": tray,
            "curr_temp": -1,
            "tar_temp": -1,
        }
    }


def cmd_ams_unload(tray_now: int, nozzle_temp: float, seq: int) -> dict:
    """Unload = ams_change_filament with slot/target 255, addressed to the loaded tray's AMS,
    at the current nozzle temperature (210 when the nozzle is cold)."""
    if tray_now in (254, 255) or tray_now < 0:
        ams_id = 255
    elif 128 <= tray_now <= 135:
        ams_id = tray_now
    else:
        ams_id = tray_now // 4
    temp = int(nozzle_temp or 0)
    if temp < 180:
        temp = 210
    return {
        "print": {
            "command": "ams_change_filament",
            "sequence_id": str(seq),
            "ams_id": ams_id,
            "slot_id": 255,
            "target": 255,
            "curr_temp": temp,
            "tar_temp": temp,
        }
    }


_TRISTATE = {"off": 0, "on": 1, "auto": 2}


def _tristate(v: bool | str) -> str:
    if isinstance(v, bool):
        return "on" if v else "off"
    v = str(v).lower()
    return v if v in _TRISTATE else "auto"


def ams_mapping_wire(trays: list[int]) -> tuple[list[int], list[dict]]:
    """Global tray per filament -> (ams_mapping, ams_mapping2) as the printer expects them
    on a single-nozzle printer: -1 unused, external spool = -1 / {255, 0},
    AMS-HT = its id / {id, 0}, AMS slot = global id / {id // 4, id % 4}."""
    flat: list[int] = []
    m2: list[dict] = []
    for t in trays:
        t = -1 if t is None else int(t)
        if t < 0:
            flat.append(-1)
            m2.append({"ams_id": 255, "slot_id": 255})
        elif t >= 254:
            flat.append(-1)
            m2.append({"ams_id": 255, "slot_id": 0})
        elif t >= 128:
            flat.append(t)
            m2.append({"ams_id": t, "slot_id": 0})
        else:
            flat.append(t)
            m2.append({"ams_id": t // 4, "slot_id": t % 4})
    return flat, m2


def job_name(file: str) -> str:
    """'dir/Benchy.gcode.3mf' -> 'Benchy' (subtask_name the printer shows)."""
    name = file.rsplit("/", 1)[-1]
    for ext in (".gcode.3mf", ".3mf", ".gcode"):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name


def cmd_project_file(
    file: str,
    plate: int,
    ams_mapping: list[int] | None,
    *,
    timelapse: bool,
    bed_levelling: bool | str,
    flow_cali: bool | str,
    vibration_cali: bool,
    layer_inspect: bool,
    use_ams: bool | None,
    submission_id: str,
) -> dict:
    """Local ``project_file`` (start a 3MF that is already on the SD card).

    bed_levelling / flow_cali: True="on", False="off", or "auto" (firmware decides, Bambu
    Studio's default). Both the legacy bool and the newer int field are sent, as Bambu Studio
    does, so old and new firmware read the same choice. use_ams=None means True
    (the default), then two single-nozzle corrections apply: a real AMS
    tray (0-253) forces it on, and only an all-EXTERNAL mapping (254/255, or None) turns
    it off. An unresolved -1 does neither: treating it as "external"
    silently started prints against an empty feed.
    """
    path = file.lstrip("/")
    bl, fc = _tristate(bed_levelling), _tristate(flow_cali)
    if use_ams is None:
        use_ams = True
    if ams_mapping:
        real = any(t is not None and 0 <= int(t) <= 253 for t in ams_mapping)
        all_external = all(t is None or int(t) >= 254 for t in ams_mapping)
        if real and not use_ams:
            use_ams = True
        elif use_ams and all_external:
            use_ams = False
    payload = {
        "sequence_id": "20000",
        "command": "project_file",
        "param": f"Metadata/plate_{int(plate)}.gcode",
        "url": f"ftp://{path}",
        "file": path,
        "md5": "",
        "bed_type": "auto",
        "timelapse": bool(timelapse),
        "bed_leveling": bl == "on",
        "auto_bed_leveling": _TRISTATE[bl],
        "flow_cali": fc == "on",
        "vibration_cali": bool(vibration_cali),
        "layer_inspect": bool(layer_inspect),
        "use_ams": bool(use_ams),
        "extrude_cali_flag": _TRISTATE[fc],
        "extrude_cali_manual_mode": 0,
        "nozzle_offset_cali": 0,
        "subtask_name": job_name(path),
        "profile_id": "0",
        "project_id": submission_id,
        "subtask_id": submission_id,
        "task_id": submission_id,
    }
    if ams_mapping is not None:
        payload["ams_mapping"], payload["ams_mapping2"] = ams_mapping_wire(ams_mapping)
    return {"print": payload}


# G-code strings for the printer's gcode_line command.
def gcode_nozzle(target: int) -> str:
    # Single-nozzle P1/A1 firmware ignores "M104 T0 S..." (the T is for dual-nozzle models):
    # the plain form is the one the printer applies.
    return f"M104 S{target}"


def gcode_bed(target: int) -> str:
    return f"M140 S{target}"


def gcode_chamber(target: int) -> str:
    return f"M141 S{target}"


FAN_INDEX = {"part": 1, "aux": 2, "chamber": 3}


def gcode_fan(fan: str, percent: int) -> str:
    return f"M106 P{FAN_INDEX[fan]} S{round(percent * 255 / 100)}"


def gcode_home() -> str:
    # Always a bare G28: partial homing skips the toolhead park (bed crash risk).
    return "G28"


def gcode_jog_xy(x: float, y: float) -> str:
    axes = []
    if x:
        axes.append(f"X{x:.2f}")
    if y:
        axes.append(f"Y{y:.2f}")
    return "\n".join(["G91", f"G1 {' '.join(axes)} F6000", "G90"])


def gcode_jog_z(distance: float) -> str:
    # Positive = more nozzle-bed gap on every model; no M211 (would disable soft limits globally).
    return "\n".join(["G91", f"G1 Z{distance:.2f} F600", "G90"])


def gcode_extrude(distance: float) -> str:
    return "\n".join(["M83", f"G1 E{distance:.2f} F300", "M82"])


# ---------------------------------------------------------------- client


@dataclass(frozen=True)
class SendResult:
    ok: bool
    message: str

    def as_dict(self) -> dict:
        return {"ok": self.ok, "message": self.message}


def _ok(msg: str) -> SendResult:
    return SendResult(True, msg)


def _err(msg: str) -> SendResult:
    return SendResult(False, msg)


def _finite(v: Any) -> float | None:
    """User input -> finite float; None for junk / NaN / inf (would end up in G-code)."""
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _whole(v: Any) -> int | None:
    """User input -> int (route params may arrive as str/float); None for junk."""
    f = _finite(v)
    return None if f is None or f != int(f) else int(f)


_instance_counter = itertools.count(1)


class PrinterMqtt:
    """One printer's LAN MQTT session + merged state. Create once, ``await start()``.

    on_change(): called on the event loop thread whenever status() or the link changed.
    on_report(payload): optional raw hook for every decoded report (after merging).
    """

    def __init__(
        self,
        host: str = "",
        serial: str = "",
        access_code: str = "",
        *,
        model: str = "P1S",
        port: int = PORT,
        on_change: Callable[[], Any] | None = None,
        on_report: Callable[[dict], Any] | None = None,
        tick_s: float = TICK_S,
        nudge_s: float = NUDGE_S,
        stale_s: float = STALE_S,
        dead_s: float = DEAD_S,
        pushall_s: float = PUSHALL_S,
        retry_min_s: float = RETRY_MIN_S,
        retry_max_s: float = RETRY_MAX_S,
        paho_delay: tuple[int, int] = (1, 30),
    ) -> None:
        self.host, self.serial, self._access_code = host.strip(), serial.strip(), access_code
        self.model = model
        self.port = port
        self.on_change = on_change
        self.on_report = on_report
        self.tick_s, self.nudge_s, self.stale_s, self.dead_s = tick_s, nudge_s, stale_s, dead_s
        self.pushall_s, self.retry_min_s, self.retry_max_s = pushall_s, retry_min_s, retry_max_s
        self.paho_delay = paho_delay
        # App-side gate (not a printer field): the queue sets it
        # after a finished print, "clear plate" resets it. Reported in status().
        self._awaiting_plate_clear = False

        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._running = False
        self._client: mqtt.Client | None = None
        self._gen = 0
        self._seq = itertools.count(1)
        self._connected = False
        self._link = "stopped"  # stopped / not_configured / connecting / online / offline / auth_error
        self.last_error = ""
        self._client_since = 0.0
        self._last_ok = 0.0
        self._last_msg = 0.0
        self._last_pushall = 0.0
        self._nudged = False
        self._next_attempt = 0.0
        self._retry_delay = retry_min_s
        self._restart_requested = False
        self._rebuild_requested = False
        self._bg_tasks: set[asyncio.Task] = set()
        self._last_status: dict | None = None
        self.reset_state()

    # ------------------------------------------------------------ configuration / lifecycle

    @property
    def awaiting_plate_clear(self) -> bool:
        return self._awaiting_plate_clear

    @awaiting_plate_clear.setter
    def awaiting_plate_clear(self, value: bool) -> None:
        """Setting it is a status() change, so on_change fires (call on the loop thread)."""
        self._awaiting_plate_clear = bool(value)
        self._changed()

    @property
    def configured(self) -> bool:
        return bool(self.host and self.serial and self._access_code)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def link(self) -> str:
        return self._link

    @property
    def report_topic(self) -> str:
        return f"device/{self.serial}/report"

    @property
    def request_topic(self) -> str:
        return f"device/{self.serial}/request"

    def connection(self) -> dict:
        """Link state for the UI / diagnostics (never contains the access code)."""
        age = time.monotonic() - self._last_msg if self._last_msg else -1.0
        return {
            "link": self._link,
            "connected": self._connected,
            "error": self.last_error,
            "host": self.host,
            "serial": self.serial,
            "last_message_s": round(age, 1),
        }

    def reset_state(self) -> None:
        """Forget the merged printer state (new printer / serial)."""
        self._raw: dict[str, Any] = {}
        self._ams_units: list[dict] = []
        self._ams_meta: dict[str, Any] = {}
        self._vt: list[dict] = []
        self._lights: dict[str, str] = {}  # node -> mode, merged across deltas
        self._total_refresh_armed = False
        self._timelapse = False
        self._firmware: dict[str, str] = {}
        self._acks: dict[str, dict] = {}
        self._last_status = None

    def configure(self, host: str, serial: str, access_code: str, model: str | None = None) -> None:
        """Apply new connection settings; reconnects on the next supervisor tick when changed."""
        host, serial = host.strip(), serial.strip()
        changed = (host, serial, access_code) != (self.host, self.serial, self._access_code)
        if model:
            self.model = model
        if not changed:
            return
        if serial != self.serial:
            self.reset_state()
        self.host, self.serial, self._access_code = host, serial, access_code
        self._next_attempt = 0.0
        self._retry_delay = self.retry_min_s
        self._restart_requested = True
        self._wake.set()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._running = True
        self._task = asyncio.create_task(self._supervise(), name="printer-mqtt")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        await self._teardown()
        self._set_link("stopped", "")

    async def reconnect(self) -> None:
        """Drop the session and connect again right away (Settings > reconnect)."""
        await self._teardown()
        self._next_attempt = 0.0
        self._wake.set()

    # ------------------------------------------------------------ supervisor (event loop)

    async def _supervise(self) -> None:
        while self._running:
            # Clear BEFORE the tick: a wake-up raised while the tick runs (configure(),
            # a dropped link) must not be lost for a whole tick period.
            self._wake.clear()
            try:
                await self._tick()
            except Exception:  # noqa: BLE001 - the supervisor must never die
                log.exception("printer mqtt supervisor tick failed")
            try:
                await asyncio.wait_for(self._wake.wait(), self.tick_s)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        now = time.monotonic()
        if self._restart_requested:
            self._restart_requested = False
            self._rebuild_requested = False
            await self._teardown()
        if self._rebuild_requested:
            # The session ended (drop / refusal): its paho thread has exited on purpose.
            self._rebuild_requested = False
            if self._client is not None:
                await self._teardown()
                self._schedule_retry()
            now = time.monotonic()
        if not self.configured:
            if self._client is not None:
                await self._teardown()
            self._set_link("not_configured", "Printer IP / serial / access code not set")
            return
        if self._client is None:
            if now >= self._next_attempt:
                self._create_client()
            return
        if self._connected:
            silent = now - self._last_msg
            if silent > self.stale_s:
                log.warning("printer mqtt: no report for %.0f s - rebuilding the session", silent)
                await self._teardown()
                self._set_link("offline", f"No data from the printer for {silent:.0f} s")
                self._schedule_retry()
                return
            if silent > self.nudge_s and not self._nudged:
                self._nudged = True
                self.request_pushall()
            if now - self._last_pushall > self.pushall_s:
                self.request_pushall()
        elif now - max(self._client_since, self._last_ok) > self.dead_s:
            log.warning("printer mqtt: offline for %.0f s - rebuilding the client", self.dead_s)
            await self._teardown()
            self._schedule_retry()

    def _schedule_retry(self) -> None:
        self._next_attempt = time.monotonic() + self._retry_delay
        self._retry_delay = min(self._retry_delay * 2, self.retry_max_s)

    def _create_client(self) -> None:
        self._gen += 1
        gen = self._gen
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"printerdash_{os.getpid()}_{next(_instance_counter)}",
            protocol=mqtt.MQTTv311,
            # One session per client: see the module doc (no QoS-1 replay on reconnect).
            reconnect_on_failure=False,
        )
        client.username_pw_set(USERNAME, self._access_code)
        # PROTOCOL_TLS_CLIENT instead of create_default_context(): the latter loads the
        # system CA store (blocking file I/O on the event loop) that CERT_NONE never uses.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # the printer's cert is self-signed
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        client.tls_set_context(ctx)
        # Bambu's broker matches PUBACKs racily; paho's default 20 in-flight wedges the
        # session after ~20 commands.
        client.max_inflight_messages_set(1000)
        client.reconnect_delay_set(min_delay=self.paho_delay[0], max_delay=self.paho_delay[1])
        client.on_connect = lambda c, u, f, rc, p=None: self._cb_connect(gen, c, rc)
        client.on_connect_fail = lambda c, u: self._post(self._on_link_failed, gen, "Cannot reach the printer")
        client.on_disconnect = lambda c, u, f, rc=None, p=None: self._cb_disconnect(gen, rc)
        client.on_message = lambda c, u, msg: self._cb_message(gen, msg)
        self._client = client
        self._client_since = time.monotonic()
        self._set_link("connecting", self.last_error)
        try:
            client.connect_async(self.host, self.port, keepalive=KEEPALIVE_S)
            client.loop_start()
        except Exception as e:  # noqa: BLE001 - bad host string, thread start failure...
            self._client = None
            self._set_link("offline", f"Cannot start the MQTT client ({type(e).__name__})")
            self._schedule_retry()

    async def _teardown(self) -> None:
        client, self._client = self._client, None
        self._gen += 1  # late callbacks of the old client are ignored from now on
        was = self._connected
        self._connected = False
        if client is not None:
            def close() -> None:
                try:
                    client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    client.loop_stop()
                except Exception:  # noqa: BLE001
                    pass

            try:
                await asyncio.wait_for(asyncio.to_thread(close), 5.0)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                log.warning("printer mqtt: old client did not stop in time")
        if was:
            self._changed()

    # ------------------------------------------------------------ paho thread -> loop

    def _post(self, fn: Callable, *args: Any) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # loop closed during shutdown
            pass

    def _cb_connect(self, gen: int, client: mqtt.Client, rc: Any) -> None:
        try:
            if getattr(rc, "is_failure", False):
                self._post(self._on_link_failed, gen, _connack_text(rc))
                return
            client.subscribe(self.report_topic, qos=0)
            client.publish(self.request_topic, json.dumps(cmd_pushall()), qos=1)
            client.publish(self.request_topic, json.dumps(cmd_get_version(next(self._seq))), qos=1)
            self._post(self._on_link_up, gen)
        except Exception:  # noqa: BLE001 - an exception here would kill paho's thread
            log.exception("printer mqtt: on_connect failed")

    def _cb_disconnect(self, gen: int, rc: Any) -> None:
        text = ""
        if rc is not None and getattr(rc, "is_failure", False):
            text = f"Connection lost ({rc})"
        self._post(self._on_link_down, gen, text or "Connection lost")

    def _cb_message(self, gen: int, msg: Any) -> None:
        # Catch everything: paho re-raises callback exceptions on its network thread,
        # which kills the session (e.g. RecursionError from a deeply nested frame).
        try:
            if msg.topic != self.report_topic or len(msg.payload) > MAX_PAYLOAD:
                return
            data = json.loads(msg.payload.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            return
        if isinstance(data, dict):
            self._post(self._on_message, gen, data)

    def _on_link_up(self, gen: int) -> None:
        if gen != self._gen:
            return
        now = time.monotonic()
        self._connected = True
        self._last_ok = self._last_msg = self._last_pushall = now
        self._nudged = False
        self._retry_delay = self.retry_min_s
        self.last_error = ""
        log.info("printer mqtt: connected to %s", self.host)
        self._set_link("online", "")
        self._changed()

    def _on_link_failed(self, gen: int, text: str) -> None:
        if gen != self._gen:
            return
        auth = "access code" in text
        if self.last_error != text:
            log.warning("printer mqtt: %s", text)
        self._connected = False
        self._set_link("auth_error" if auth else "offline", text)
        self._changed()
        if auth:
            # A refused CONNACK ends this client's thread (no paho retry): rebuild
            # with the supervisor's backoff instead of hammering the printer.
            self._request_rebuild()

    def _on_link_down(self, gen: int, text: str) -> None:
        if gen != self._gen:
            return
        was = self._connected
        self._connected = False
        if was:
            log.warning("printer mqtt: %s", text)
            self._last_ok = time.monotonic()
            self._set_link("offline", text)
            self._changed()
        # The session is over either way (reconnect_on_failure=False): fresh client.
        self._request_rebuild()

    def _request_rebuild(self) -> None:
        self._rebuild_requested = True
        self._wake.set()

    def _on_message(self, gen: int, data: dict) -> None:
        if gen != self._gen:
            return
        self._last_msg = time.monotonic()
        self._nudged = False
        self.ingest(data)

    def _set_link(self, link: str, error: str) -> None:
        if (link, error) == (self._link, self.last_error):
            return
        self._link, self.last_error = link, error
        self._fire_change()

    # ------------------------------------------------------------ state merging

    def ingest(self, payload: dict) -> bool:
        """Merges one decoded report into the full state. Returns True when status() changed.

        Never raises: a malformed field is skipped instead of dropping the whole frame.
        """
        if not isinstance(payload, dict):
            return False
        try:
            p = payload.get("print")
            if isinstance(p, dict):
                command = p.get("command")
                if command in (None, "push_status"):
                    self._merge_print(p)
                else:
                    # Command acks echo our own request fields (timelapse, cfg...); they
                    # are not printer state. Kept for whoever wants the answer.
                    self._acks[str(command)] = {**p, "_t": time.time()}
                    result = _s(p.get("result")).lower()
                    if result and result != "success":
                        log.info("printer refused %s: %s %s", command, p.get("result"), p.get("reason", ""))
            info = payload.get("info")
            if isinstance(info, dict) and info.get("command") == "get_version":
                for m in info.get("module") or []:
                    if isinstance(m, dict) and m.get("name"):
                        self._firmware[str(m["name"])] = _s(m.get("sw_ver"))
        except Exception:  # noqa: BLE001
            log.exception("printer mqtt: cannot merge a report")
        if self.on_report is not None:
            try:
                self.on_report(payload)
            except Exception:  # noqa: BLE001
                log.exception("printer mqtt: on_report failed")
        return self._changed()

    def _merge_print(self, p: dict) -> None:
        raw = self._raw
        prev_state = raw.get("gcode_state")
        for key, val in p.items():
            if key in ("command", "sequence_id"):
                continue
            try:
                if key == "ams":
                    self._merge_ams(val)
                elif key == "vir_slot":
                    if isinstance(val, list) and val:
                        slots = [dict(v) for v in val if isinstance(v, dict)]
                        # Single-external printers list their slot as 255; tray_now says 254.
                        if len(slots) == 1 and str(slots[0].get("id", "")) == "255":
                            slots[0]["id"] = "254"
                        self._vt = slots
                elif key == "vt_tray":
                    if "vir_slot" not in p:
                        self._merge_vt(val)
                elif key == "lights_report":
                    # The last known mode is kept per node: a delta that lists
                    # only some nodes must not switch the chamber light "off".
                    if isinstance(val, list):
                        for light in val:
                            if isinstance(light, dict) and isinstance(light.get("node"), str):
                                self._lights[light["node"]] = _s(light.get("mode"))
                        raw[key] = val
                elif key == "total_layer_num":
                    n = _int(val, 0) or 0
                    if n > 0:  # P1S resets it to 0 at print end; keep the last good total
                        raw[key] = n
                elif key == "layer_num":
                    n = _int(val)
                    if n is not None:
                        old = _int(raw.get("layer_num"), 0) or 0
                        raw[key] = n
                        if (
                            n > old
                            and self._total_refresh_armed
                            and not raw.get("total_layer_num")
                            and "total_layer_num" not in p
                        ):
                            # The print is laying down layers but the
                            # start-of-print pushall never brought a total. Ask once more.
                            self._total_refresh_armed = False
                            self.request_pushall()
                elif isinstance(val, dict):
                    raw[key] = _deep_merge(raw.get(key), val)
                else:
                    raw[key] = val
            except Exception:  # noqa: BLE001
                log.debug("printer mqtt: skipped field %s", key, exc_info=True)

        # Timelapse: whichever source arrived last wins.
        xcam = p.get("xcam")
        if isinstance(xcam, dict) and "timelapse" in xcam:
            self._timelapse = xcam.get("timelapse") == "enable"
        if "timelapse" in p:
            self._timelapse = p["timelapse"] is True
        ipcam = p.get("ipcam")
        if isinstance(ipcam, dict) and "timelapse" in ipcam:
            self._timelapse = ipcam.get("timelapse") == "enable"

        state = raw.get("gcode_state")
        if (
            "gcode_state" in p
            and prev_state is not None
            and prev_state not in ACTIVE_STATES
            and state in ("PREPARE", "RUNNING")
        ):
            # New print: the old job's total and faults must not leak into it.
            # A starting frame may carry the new total (keep it) or a firmware 0 (which
            # the merge above ignored, so the OLD print's total would still be there).
            if (_int(p.get("total_layer_num"), 0) or 0) <= 0:
                raw.pop("total_layer_num", None)
                self._total_refresh_armed = True  # one more try at the first layer advance
                self.request_pushall()  # the total comes with a full report
            else:
                self._total_refresh_armed = False
            if "hms" not in p:
                raw["hms"] = []
            if "print_error" not in p:
                raw["print_error"] = 0

    def _merge_vt(self, val: Any) -> None:
        items = [val] if isinstance(val, dict) else val if isinstance(val, list) else []
        by_id = {str(t.get("id", "254")): t for t in self._vt}
        if len(self._vt) > 1 and isinstance(val, dict):
            return  # a single active-spool dict must not overwrite multi-slot vir_slot data
        for t in items:
            if not isinstance(t, dict):
                continue
            key = str(t.get("id", "254"))
            # A vt_tray without "id" is the single external slot: it
            # reports it as 254, so status()["ext"][0]["id"] must not fall back to 0.
            by_id[key] = {"id": key, **by_id.get(key, {}), **t}
        self._vt = sorted(by_id.values(), key=lambda t: _int(t.get("id"), 254) or 0)

    def _merge_ams(self, val: Any) -> None:
        if isinstance(val, list):
            meta, units = {}, val
        elif isinstance(val, dict):
            meta = {k: v for k, v in val.items() if k != "ams"}
            units = val.get("ams")
        else:
            return
        self._ams_meta.update(meta)
        if isinstance(units, list):
            by_id = {str(u.get("id")): u for u in self._ams_units}
            for nu in units:
                if not isinstance(nu, dict) or nu.get("id") is None:
                    continue
                key = str(nu["id"])
                old = by_id.get(key) or {}
                merged = {**old, **{k: v for k, v in nu.items() if k != "tray"}}
                if isinstance(nu.get("tray"), list):
                    uid = _int(nu["id"], 0) or 0
                    merged["tray"] = _merge_trays(old.get("tray") or [], nu["tray"], 128 <= uid <= 135)
                by_id[key] = merged
            exist = _hex(meta.get("ams_exist_bits"))
            if exist is not None and not (exist == 0 and meta.get("power_on_flag") is False):
                # A unit that is no longer attached disappears from the mask; drop it.
                by_id = {k: u for k, u in by_id.items() if _unit_attached(u, exist)}
            self._ams_units = sorted(by_id.values(), key=lambda u: _int(u.get("id"), 0) or 0)
        if "tray_exist_bits" in meta:
            apply_tray_exist_bits(self._ams_units, meta.get("tray_exist_bits"), meta.get("power_on_flag", True))

    # ------------------------------------------------------------ derived state

    def _changed(self) -> bool:
        st = self.status()
        if st == self._last_status:
            return False
        self._last_status = st
        self._fire_change()
        return True

    def _fire_change(self) -> None:
        cb = self.on_change
        if cb is None:
            return
        try:
            res = cb()
            if inspect.isawaitable(res):
                if self._loop is None or self._loop.is_closed():
                    if inspect.iscoroutine(res):
                        res.close()  # nowhere to run it; avoid "never awaited" warnings
                    return
                # Keep a reference: the loop only holds weak refs to tasks, so an
                # unreferenced task can be garbage-collected mid-run.
                task = asyncio.ensure_future(res, loop=self._loop)
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_done)
        except Exception:  # noqa: BLE001
            log.exception("printer mqtt: on_change failed")

    def _bg_done(self, task: asyncio.Future) -> None:
        self._bg_tasks.discard(task)  # type: ignore[arg-type]
        if not task.cancelled() and task.exception() is not None:
            log.error("printer mqtt: async on_change failed", exc_info=task.exception())

    @property
    def state(self) -> str:
        return _s(self._raw.get("gcode_state")) or "unknown"

    @property
    def tray_now(self) -> int:
        v = _int(self._ams_meta.get("tray_now"), 255)
        return 255 if v is None else v

    def _chamber(self) -> tuple[float | None, float]:
        """(current, target) for models with a real chamber sensor; (None, 0) otherwise."""
        if self.model.strip().upper() not in CHAMBER_SENSOR_MODELS:
            return None, 0.0
        cur: float | None = None
        target = 0.0
        v = _float(self._raw.get("chamber_temper"))
        if v is not None:
            if -50 < v < 100:
                cur = v
            elif v > 500:  # H2 encoding: target * 65536 + current
                c, t = int(v) % 65536, int(v) // 65536
                cur = float(c) if -50 < c < 100 else None
                target = float(t) if 0 <= t <= 60 else 0.0
        mt = _float(self._raw.get("mc_target_cham"))
        if mt is not None and 0 <= mt <= 60:
            target = mt
        return cur, target

    def _door_open(self) -> bool:
        if self.model.strip().upper() in X1_FAMILY:
            hf = _int(self._raw.get("home_flag"))
            return hf is not None and bool((hf & 0xFFFFFFFF) & 0x00800000)
        stat = _hex(self._raw.get("stat"))
        return stat is not None and bool(stat & 0x00800000)

    def _light(self) -> bool:
        return self._lights.get("chamber_light") == "on"

    def faults(self) -> list[dict]:
        """Active faults with codes (hms[] + print_error) - richer than status()["hms"]."""
        return parse_hms(self._raw.get("hms"), self._raw.get("print_error"))

    def status(self) -> dict:
        """The normalized status dict (see module doc)."""
        r = self._raw
        job = _s(r.get("subtask_name")) or _s(r.get("gcode_file"))
        ams = []
        for a in self._ams_units[:4]:
            trays: list[dict | None] = [None, None, None, None]
            all_trays = [t for t in a.get("tray") or [] if isinstance(t, dict)]
            for tr in all_trays:
                tid = _int(tr.get("id"), -1)
                if tid is not None and 0 <= tid < 4:
                    trays[tid] = _tray_status(tr)
            hum = _float(a.get("humidity_raw"))  # %, never the inverted 1-5 index
            ams.append(
                {
                    "id": _int(a.get("id"), 0),
                    "humidity": int(round(hum)) if hum is not None else -1,
                    "temp": _float(a.get("temp")),
                    "is_ht": len(all_trays) == 1,
                    "trays": trays,
                }
            )
        hms = []
        worst = 0
        for f in self.faults():
            sev = f["severity"]
            if not hms or sev < worst:
                worst = sev
            hms.append({"severity": sev, "text": f["description"] or f["full_code"]})
        chamber, chamber_target = self._chamber()
        wifi = wifi_dbm(r.get("wifi_signal"))
        fans = {k: fan_percent(r.get(k)) for k in ("cooling_fan_speed", "big_fan1_speed", "big_fan2_speed")}
        speed = _int(r.get("spd_lvl"), 2)
        return {
            "connected": self._connected,
            "state": self.state,
            "job": job,
            "progress": _float(r.get("mc_percent"), 0.0),
            "remaining_min": _int(r.get("mc_remaining_time"), 0),
            "layer": _int(r.get("layer_num"), 0),
            "total_layers": _int(r.get("total_layer_num"), 0),
            "nozzle": _float(r.get("nozzle_temper"), 0.0),
            "nozzle_target": _float(r.get("nozzle_target_temper"), 0.0),
            "bed": _float(r.get("bed_temper"), 0.0),
            "bed_target": _float(r.get("bed_target_temper"), 0.0),
            "has_chamber": chamber is not None,
            "chamber": chamber if chamber is not None else 0.0,
            "chamber_target": chamber_target if chamber is not None else 0.0,
            "chamber_heater": self.model.strip().upper() in CHAMBER_HEATER_MODELS,
            "light": self._light(),
            "timelapse": self._timelapse,
            "speed_level": 2 if speed is None else speed,
            "fan_part": -1 if fans["cooling_fan_speed"] is None else fans["cooling_fan_speed"],
            "fan_aux": -1 if fans["big_fan1_speed"] is None else fans["big_fan1_speed"],
            "fan_chamber": -1 if fans["big_fan2_speed"] is None else fans["big_fan2_speed"],
            "wifi_signal": str(wifi or ""),
            "door_open": self._door_open(),
            "awaiting_plate_clear": self._awaiting_plate_clear,
            "tray_now": self.tray_now,
            "ams": ams,
            "ext": [_tray_status(t) for t in self._vt[:2]],
            "hms": hms,
            "hms_severity": worst,
        }

    def details(self) -> dict:
        """Extra printer facts that status() leaves out."""
        r = self._raw
        # SD card: the explicit "sdcard" field wins (heartbeat pushes clear
        # home_flag bits 8-9 even with a card inserted, so those bits flap). home_flag
        # is only a fallback for firmware that never sends "sdcard".
        raw_sd = r.get("sdcard")
        if isinstance(raw_sd, str):
            sd = "HAS_SDCARD" in raw_sd.upper() or raw_sd.lower() in ("true", "normal", "1")
        elif raw_sd is not None:
            sd = bool(raw_sd)
        else:
            hf = _int(r.get("home_flag"))
            sd = hf is not None and bool((hf & 0xFFFFFFFF) & 0x300)
        s_obj = r.get("s_obj") if isinstance(r.get("s_obj"), list) else []
        ipcam = r.get("ipcam") if isinstance(r.get("ipcam"), dict) else {}
        return {
            "gcode_state": _s(r.get("gcode_state")),
            "gcode_file": _s(r.get("gcode_file")),
            "subtask_name": _s(r.get("subtask_name")),
            "subtask_id": _s(r.get("subtask_id")),
            "task_id": _s(r.get("task_id")),
            "print_type": _s(r.get("print_type")),
            "stg_cur": _int(r.get("stg_cur"), -1),
            "mc_print_sub_stage": _int(r.get("mc_print_sub_stage"), 0),
            "print_error": _int(r.get("print_error"), 0),
            "faults": self.faults(),
            "skipped_objects": [x for x in (_int(o) for o in s_obj) if x is not None],
            "sdcard": bool(sd),
            "nozzle_diameter": _s(r.get("nozzle_diameter")) or str(r.get("nozzle_diameter") or ""),
            "nozzle_type": _s(r.get("nozzle_type")),
            "ipcam_record": _s(ipcam.get("ipcam_record")) == "enable",
            "ams_status": _int(self._ams_meta.get("ams_status"), _int(r.get("ams_status"), 0)),
            "tray_tar": _int(self._ams_meta.get("tray_tar"), 255),
            "tray_pre": _int(self._ams_meta.get("tray_pre"), 255),
            "firmware": dict(self._firmware),
        }

    def last_ack(self, command: str) -> dict | None:
        """Last command ack the printer sent (e.g. "project_file"), with a "_t" timestamp."""
        return self._acks.get(command)

    # ------------------------------------------------------------ sending

    def _publish(self, payload: dict, what: str) -> SendResult:
        client = self._client
        if client is None or not self._connected:
            return _err("Printer is not connected")
        try:
            info = client.publish(self.request_topic, json.dumps(payload), qos=1)
        except Exception as e:  # noqa: BLE001
            return _err(f"{what} failed: {type(e).__name__}")
        rc = getattr(info, "rc", mqtt.MQTT_ERR_SUCCESS)
        if rc != mqtt.MQTT_ERR_SUCCESS:
            return _err(f"{what} failed: {mqtt.error_string(rc)}")
        log.info("printer command: %s", what)
        return _ok(what)

    def set_ipcam(self, enable: bool) -> SendResult:
        """The printer's camera (recording / liveview) on or off. Sent in both forms seen in
        the wild: "xcam" and "camera" (OpenBambuAPI)."""
        ctl = "enable" if enable else "disable"
        r1 = self._publish({"xcam": {"command": "ipcam_record_set", "control": ctl, "sequence_id": "0"}}, f"Camera {ctl}")
        r2 = self._publish({"camera": {"command": "ipcam_record_set", "control": ctl, "sequence_id": "0"}}, f"Camera {ctl}")
        return r1 if r1.ok else r2

    def request_pushall(self) -> SendResult:
        """Full state refresh. Thread-safe (paho publish takes its own locks)."""
        self._last_pushall = time.monotonic()
        res = self._publish(cmd_pushall(), "Status refresh")
        return res

    def send_gcode(self, gcode: str, what: str = "G-code") -> SendResult:
        # The firmware runs a gcode_line only up to the last newline: without a trailing
        # "\n" a single-line command (M104 / M140 / M106) is silently dropped and the last
        # line of a multi-line one (the G90 after a jog) never runs.
        if not gcode.endswith("\n"):
            gcode += "\n"
        return self._publish(cmd_gcode(gcode, next(self._seq)), what)

    def pause(self) -> SendResult:
        return self._publish(cmd_print("pause"), "Print paused")

    def resume(self) -> SendResult:
        return self._publish(cmd_print("resume"), "Print resumed")

    def stop_print(self) -> SendResult:
        return self._publish(cmd_print("stop"), "Print stopped")

    def clear_hms(self) -> SendResult:
        res = self._publish(cmd_print("clean_print_error"), "Errors cleared")
        if res.ok:  # clear locally now, the printer re-reports what persists
            self._raw["hms"] = []
            self._raw["print_error"] = 0
            self._changed()
        return res

    def set_light(self, on: bool) -> SendResult:
        """Chamber light; both nodes are switched (chamber_light2 exists on H2)."""
        for node in ("chamber_light", "chamber_light2"):
            res = self._publish(cmd_ledctrl(node, on, next(self._seq)), f"Light {'on' if on else 'off'}")
            if not res.ok:
                return res
        return res

    def set_nozzle_temp(self, target: int) -> SendResult:
        t = _whole(target)
        if t is None or not 0 <= t <= 320:
            return _err("Nozzle target must be 0-320 °C")
        return self.send_gcode(gcode_nozzle(t), f"Nozzle target {t}°C")

    def set_bed_temp(self, target: int) -> SendResult:
        t = _whole(target)
        if t is None or not 0 <= t <= 140:
            return _err("Bed target must be 0-140 °C")
        return self.send_gcode(gcode_bed(t), f"Bed target {t}°C")

    def set_chamber_temp(self, target: int) -> SendResult:
        if self.model.strip().upper() not in CHAMBER_HEATER_MODELS:
            return _err(f"{self.model} has no chamber heater")
        t = _whole(target)
        if t is None or not 0 <= t <= 65:
            return _err("Chamber target must be 0-65 °C")
        return self.send_gcode(gcode_chamber(t), f"Chamber target {t}°C")

    def set_fan(self, fan: str, percent: int) -> SendResult:
        if fan not in FAN_INDEX:
            return _err("fan must be part, aux or chamber")
        pc = _whole(percent)
        if pc is None or not 0 <= pc <= 100:
            return _err("Fan speed must be 0-100 %")
        return self.send_gcode(gcode_fan(fan, pc), f"{fan} fan {pc}%")

    def set_speed_level(self, level: int) -> SendResult:
        lv = _whole(level)
        if lv not in (1, 2, 3, 4):
            return _err("Speed level must be 1-4")
        return self._publish(cmd_print_speed(lv), f"Speed mode {lv}")

    def home(self) -> SendResult:
        return self.send_gcode(gcode_home(), "Homing")

    def jog_xy(self, x: float, y: float) -> SendResult:
        fx, fy = _finite(x), _finite(y)
        if fx is None or fy is None or (not fx and not fy) or abs(fx) > 200 or abs(fy) > 200:
            return _err("X/Y move must be non-zero and at most 200 mm")
        return self.send_gcode(gcode_jog_xy(fx, fy), f"Move X{fx:+.1f} Y{fy:+.1f}")

    def jog_z(self, distance: float) -> SendResult:
        d = _finite(distance)
        if not d or abs(d) > 200:
            return _err("Z move must be non-zero and at most 200 mm")
        return self.send_gcode(gcode_jog_z(d), f"Move bed {d:+.1f} mm")

    def extrude(self, distance: float) -> SendResult:
        """Positive extrudes, negative retracts (firmware refuses cold extrusion itself)."""
        d = _finite(distance)
        if not d or abs(d) > 100:
            return _err("Extrude distance must be non-zero and at most 100 mm")
        return self.send_gcode(gcode_extrude(d), f"Extruder {d:+.0f} mm")

    def ams_load(self, tray: int) -> SendResult:
        t = _whole(tray)
        if t is None or not (0 <= t <= 15 or 128 <= t <= 135 or t == 254):
            return _err("Bad tray")
        return self._publish(cmd_ams_load(t, next(self._seq)), "Loading filament")

    def ams_unload(self) -> SendResult:
        nozzle = _float(self._raw.get("nozzle_temper"), 0.0) or 0.0
        return self._publish(cmd_ams_unload(self.tray_now, nozzle, next(self._seq)), "Unloading filament")

    # ---- AMS slot settings (payloads as Bambu Studio sends them)

    @staticmethod
    def _slot_wire(ams_id: int, tray_id: int) -> tuple[int, int, int] | None:
        """(ams_id, tray_id, slot_id) on the wire. External spool on a single-nozzle printer:
        ams 255 with the GLOBAL tray index 254 (tray 0 is refused)."""
        if ams_id == 255:
            return (255, 254, 0) if tray_id == 0 else None
        if 0 <= ams_id <= 3 and 0 <= tray_id <= 3:
            return ams_id, tray_id, tray_id
        if 128 <= ams_id <= 135 and tray_id == 0:
            return ams_id, 0, 0
        return None

    def set_slot(self, ams_id: int, tray_id: int, *, info_idx: str, tray_type: str, sub_brands: str,
                 color: str, temp_min: int, temp_max: int) -> SendResult:
        wire = self._slot_wire(ams_id, tray_id)
        if wire is None:
            return _err("Bad AMS slot")
        c = (color or "").lstrip("#").upper()  # P1S reads lowercase hex as zeros
        if len(c) == 6:
            c += "FF"
        if len(c) != 8 or any(x not in "0123456789ABCDEF" for x in c):
            return _err("Colour must be RRGGBB")
        a, t, sl = wire
        return self._publish({"print": {
            "command": "ams_filament_setting", "ams_id": a, "tray_id": t, "slot_id": sl,
            "tray_info_idx": str(info_idx or "GFL99")[:16], "tray_type": str(tray_type or "PLA")[:16],
            "tray_sub_brands": str(sub_brands or "")[:40], "tray_color": c,
            "nozzle_temp_min": int(temp_min), "nozzle_temp_max": int(temp_max), "sequence_id": "0",
        }}, "Slot updated")

    def reset_slot(self, ams_id: int, tray_id: int) -> SendResult:
        wire = self._slot_wire(ams_id, tray_id)
        if wire is None:
            return _err("Bad AMS slot")
        a, t, sl = wire
        return self._publish({"print": {
            "command": "ams_filament_setting", "ams_id": a, "tray_id": t, "slot_id": sl,
            "tray_info_idx": "", "tray_type": "", "tray_sub_brands": "", "tray_color": "00000000",
            "nozzle_temp_min": 0, "nozzle_temp_max": 0, "sequence_id": "0",
        }}, "Slot reset")

    def refresh_rfid(self, ams_id: int, tray_id: int) -> SendResult:
        if not (0 <= ams_id <= 3 or 128 <= ams_id <= 135):
            return _err("Not for the external spool")
        if self.tray_now not in (255, -1):
            return _err("Unload the filament first (the AMS has to move it to read the tag)")
        return self._publish({"print": {"command": "ams_get_rfid", "ams_id": ams_id, "slot_id": tray_id,
                                        "sequence_id": str(next(self._seq))}}, "Re-reading RFID")

    def skip_objects(self, obj_ids: list[int]) -> SendResult:
        if self.state not in ("RUNNING", "PAUSE"):
            return _err("Objects can only be skipped during a print")
        try:
            ids = [int(o) for o in obj_ids]
        except (TypeError, ValueError):
            return _err("Bad object id")
        if not ids:
            return _err("No objects given")
        return self._publish(cmd_skip_objects(ids, next(self._seq)), f"Skipping {len(ids)} object(s)")

    def start_sd_print(
        self,
        file: str,
        plate: int = 1,
        ams_mapping: list[int] | None = None,
        *,
        timelapse: bool = False,
        bed_levelling: bool | str = "auto",
        flow_cali: bool | str = "auto",
        vibration_cali: bool = False,
        layer_inspect: bool = False,
        use_ams: bool | None = None,
    ) -> SendResult:
        """Start a 3MF already on the SD card (root path like 'Benchy.gcode.3mf').

        Only publishes; whether the printer took it shows up as gcode_state PREPARE /
        RUNNING or as a "project_file" ack with a failure result (see last_ack()).
        """
        if self.state in ACTIVE_STATES:
            return _err(f"Printer is busy ({self.state})")
        if self.state == "unknown":
            # Connected but no report yet: it may well be printing.
            return _err("Printer state not known yet - try again in a moment")
        file = (file or "").strip() if isinstance(file, str) else ""
        pl = _whole(plate)
        if not file or pl is None or pl < 1:
            return _err("No file / plate")
        try:
            mapping = None if ams_mapping is None else [-1 if t is None else int(t) for t in ams_mapping]
        except (TypeError, ValueError):
            return _err("Bad AMS mapping")
        submission = str(int(time.time() * 1000) % 2_147_483_647 or 1)
        payload = cmd_project_file(
            file,
            pl,
            mapping,
            timelapse=timelapse,
            bed_levelling=bed_levelling,
            flow_cali=flow_cali,
            vibration_cali=vibration_cali,
            layer_inspect=layer_inspect,
            use_ams=use_ams,
            submission_id=submission,
        )
        log.info(
            "sd print (lan): '%s' plate %d, ams_mapping %s, timelapse %s, bed levelling %s, flow cali %s",
            file, pl, payload["print"].get("ams_mapping"), timelapse, bed_levelling, flow_cali,
        )  # fmt: skip
        self._acks.pop("project_file", None)
        return self._publish(payload, "Print sent to the printer")


def _connack_text(rc: Any) -> str:
    """CONNACK refusal -> user text. MQTT 3.1.1 codes 4/5 arrive as 134/135 in paho 2."""
    code = getattr(rc, "value", rc)
    if code in (4, 5, 134, 135):
        return "Printer refused the access code (re-read it on the printer screen)"
    return f"Printer refused the connection ({rc})"

"""Bambu Cloud account: login (password / email code / TOTP / pasted token), token
expiry, account uid, print history (tasks).

Ported from the panel's src/cloud/BambuCloud.cpp. The token lives only in SQLite
and is never returned to the frontend or logged.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

import httpx

from . import db, settings

log = logging.getLogger("cloud")

USER_AGENT = "PrinterDash/1.0"
DEFAULT_LIFE_DAYS = 30  # unknown token lifetime: treat as expired after 30 days


def api_base(cn: bool) -> str:
    import os

    if override := os.environ.get("BAMBU_API_OVERRIDE"):  # local UI tests only
        return override
    return "https://api.bambulab.cn" if cn else "https://api.bambulab.com"


def web_base(cn: bool) -> str:
    return "https://bambulab.cn" if cn else "https://bambulab.com"


def mqtt_host(cn: bool) -> str:
    return "cn.mqtt.bambulab.com" if cn else "us.mqtt.bambulab.com"


def _jwt_exp(token: str) -> int:
    parts = token.split(".")
    if len(parts) != 3:
        return 0
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        return int(exp) if isinstance(exp, (int, float)) else 0
    except (ValueError, TypeError):
        return 0


def _error_text(resp: httpx.Response | None, status: int) -> str:
    if resp is None:
        return "Cannot reach Bambu Cloud"
    try:
        d = resp.json()
    except ValueError:
        d = None
    if isinstance(d, dict):
        if d.get("code") == 4:
            return "Bambu Cloud session expired - log in again"
        m = d.get("message") or d.get("error")
        if isinstance(m, str) and m:
            return m
    return f"Bambu Cloud error (HTTP {status})"


class Cloud:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=15.0, headers={"User-Agent": USER_AGENT})
        self._state: dict[str, Any] = db.kv_get("cloud", {}) or {}
        self.rejected = False
        self._uid = ""

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------ account state

    @property
    def token(self) -> str:
        return settings.secret("cloud_token")

    @property
    def logged_in(self) -> bool:
        return bool(self.token)

    @property
    def cn(self) -> bool:
        return bool(self._state.get("cn"))

    def _store(self, token: str, email: str, cn: bool, expires_in: int = -1) -> None:
        now = int(time.time())
        est = False
        if expires_in and expires_in > 0:
            exp = now + expires_in
        elif j := _jwt_exp(token):
            exp = j
        else:
            exp = now + DEFAULT_LIFE_DAYS * 86400
            est = True
        settings.set_secret("cloud_token", token)
        self._state = {"email": email, "cn": cn, "exp": exp, "exp_est": est}
        db.kv_set("cloud", self._state)
        self.rejected = False
        self._uid = ""
        log.info("Bambu Cloud: logged in (%s)", "China" if cn else "global")

    def logout(self) -> None:
        settings.set_secret("cloud_token", "")
        self._state = {}
        db.kv_set("cloud", {})
        self._uid = ""
        self.rejected = False

    def expiry(self) -> tuple[str, bool]:
        """(text, warn) like the panel's expiryText()."""
        if not self.logged_in:
            return "", False
        if self.rejected:
            return "EXPIRED - log in again", True
        exp = int(self._state.get("exp") or 0)
        if not exp:
            return "expiry unknown - log in once more to track it", False
        now = time.time()
        if exp <= now:
            return "EXPIRED - log in again", True
        days = int((exp - now) // 86400)
        if days < 1:
            return f"expires in {int((exp - now) // 3600)} h - log in again soon", True
        est = "~" if self._state.get("exp_est") else ""
        return f"expires in {est}{days} day{'' if days == 1 else 's'}", days < 7

    def info(self) -> dict:
        text, warn = self.expiry()
        return {
            "logged_in": self.logged_in,
            "email": self._state.get("email", "") if self.logged_in else "",
            "china": self.cn,
            "expiry": text,
            "expiry_warn": warn,
        }

    # ------------------------------------------------------------ HTTP

    async def _req(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        bearer: bool = True,
        headers: dict | None = None,
        cookies: dict | None = None,
    ) -> tuple[int, httpx.Response | None]:
        h = {"Accept": "application/json"}
        if bearer:
            h["Authorization"] = f"Bearer {self.token}"
        if headers:
            h.update(headers)
        try:
            resp = await self._http.request(method, url, json=json_body, headers=h, cookies=cookies)
        except httpx.HTTPError as e:
            log.warning("Bambu Cloud %s failed: %s", url.split("?")[0], type(e).__name__)
            return -1, None
        if bearer:
            if resp.status_code == 401:
                self.rejected = True
            elif 200 <= resp.status_code < 300:
                self.rejected = False
        return resp.status_code, resp

    # ------------------------------------------------------------ login

    async def login(self, email: str, password: str, cn: bool) -> dict:
        st, r = await self._req(
            "POST",
            f"{api_base(cn)}/v1/user-service/user/login",
            json_body={"account": email, "password": password},
            bearer=False,
        )
        d = _json(r) if st == 200 else None
        if isinstance(d, dict):
            ltype = d.get("loginType") or ""
            tfa = d.get("tfaKey") or ""
            if ltype == "tfa" or (tfa and ltype != "verifyCode"):
                return {"step": "totp", "tfa_key": tfa, "message": "Enter the code from your authenticator app"}
            if ltype == "verifyCode":
                return {"step": "email_code", "message": "Bambu sent a verification code to your email"}
            token = d.get("accessToken") or ""
            if token:
                self._store(token, email, cn, int(d.get("expiresIn") or -1))
                return {"step": "done", "message": "Logged in"}
        return {"step": "error", "message": _error_text(r, st)}

    async def verify_email_code(self, email: str, code: str, cn: bool) -> dict:
        st, r = await self._req(
            "POST",
            f"{api_base(cn)}/v1/user-service/user/login",
            json_body={"account": email, "code": code},
            bearer=False,
        )
        d = _json(r) if st == 200 else None
        if isinstance(d, dict) and d.get("accessToken"):
            self._store(d["accessToken"], email, cn, int(d.get("expiresIn") or -1))
            return {"step": "done", "message": "Logged in"}
        return {"step": "error", "message": _error_text(r, st) or "Verification failed"}

    async def verify_totp(self, email: str, tfa_key: str, code: str, cn: bool) -> dict:
        # Web origin is CSRF protected (double submit): cookie + x-bbl-csrf-token header.
        st, r = await self._req("GET", f"{web_base(cn)}/api/csrf", bearer=False)
        csrf = r.cookies.get("bbl_csrf_token") if r is not None else None
        if not csrf:
            return {"step": "error", "message": "Could not get a security token from Bambu Cloud"}
        st, r = await self._req(
            "POST",
            f"{web_base(cn)}/api/sign-in/tfa",
            json_body={"tfaKey": tfa_key, "tfaCode": code},
            bearer=False,
            headers={"x-bbl-csrf-token": csrf},
            cookies={"bbl_csrf_token": csrf},
        )
        d = _json(r) if st == 200 else None
        token = ""
        if isinstance(d, dict):
            token = d.get("accessToken") or d.get("token") or ""
        if not token and r is not None:
            token = r.cookies.get("token") or ""
        if st == 200 and token:
            self._store(token, email, cn, int((d or {}).get("expiresIn") or -1))
            return {"step": "done", "message": "Logged in"}
        return {"step": "error", "message": _error_text(r, st)}

    def set_token(self, token: str, email: str, cn: bool) -> bool:
        token = token.strip()
        if len(token) < 20:
            return False
        self._store(token, email.strip(), cn)
        return True

    # ------------------------------------------------------------ account data

    async def uid(self) -> tuple[str, str]:
        """(uid, error) - the MQTT user is u_<uid>."""
        if self._uid:
            return self._uid, ""
        st, r = await self._req("GET", f"{api_base(self.cn)}/v1/design-user-service/my/preference")
        d = _json(r) if st == 200 else None
        if isinstance(d, dict) and d.get("uid") not in (None, ""):
            self._uid = str(d["uid"])
            return self._uid, ""
        return "", _error_text(r, st)

    async def devices(self) -> tuple[list[dict] | None, str]:
        """Printers bound to the account: [{serial, name, model, access_code}] (code: secret!)."""
        st, r = await self._req("GET", f"{api_base(self.cn)}/v1/iot-service/api/user/bind")
        d = _json(r) if st == 200 else None
        if not isinstance(d, dict) or not isinstance(d.get("devices"), list):
            return None, _error_text(r, st)
        out = []
        for x in d["devices"]:
            if isinstance(x, dict) and x.get("dev_id"):
                out.append({"serial": str(x["dev_id"]), "name": str(x.get("name") or ""),
                            "model": str(x.get("dev_product_name") or x.get("dev_model_name") or ""),
                            "access_code": str(x.get("dev_access_code") or "")})
        return out, ""

    async def tasks(self, serial: str) -> tuple[list[dict] | None, str]:
        st, r = await self._req(
            "GET", f"{api_base(self.cn)}/v1/user-service/my/tasks?limit=20&deviceId={serial}"
        )
        d = _json(r) if st == 200 else None
        if not isinstance(d, dict):
            return None, _error_text(r, st)
        out = []
        for h in (d.get("hits") or [])[:20]:
            start, end = h.get("startTime") or "", h.get("endTime") or ""
            dur = _iso_diff(start, end)
            if dur < 0 and isinstance(h.get("costTime"), (int, float)):
                dur = int(h["costTime"])
            out.append(
                {
                    "id": str(h.get("id") or ""),
                    "title": h.get("title") or "",
                    "model_id": str(h.get("modelId") or ""),
                    "cover": h.get("cover") or "",
                    "device_id": h.get("deviceId") or "",
                    "start_time": start,
                    "cost_sec": dur,
                    "profile_id": int(h.get("profileId") or 0),
                    "plate_index": int(h.get("plateIndex") or 1),
                    "status": int(h.get("status") or 0),
                    "printable": bool(h.get("isPrintable")),
                }
            )
        return out, ""


def _json(r: httpx.Response | None) -> Any:
    if r is None:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def _iso_diff(a: str, b: str) -> int:
    from datetime import datetime

    try:
        ta = datetime.fromisoformat(a.replace("Z", "+00:00"))
        tb = datetime.fromisoformat(b.replace("Z", "+00:00"))
    except ValueError:
        return -1
    d = int((tb - ta).total_seconds())
    return d if d > 0 else -1


# ---------------------------------------------------------------- printing through Bambu Cloud


def flat_tray(ams_id: int, slot_id: int) -> int:
    """Flat tray index the printer loads from: A1=0.., AMS-HT = its id, external = 254."""
    if ams_id == 255:
        return 254
    if ams_id >= 128:
        return ams_id
    return ams_id * 4 + slot_id


def mapping2(t: int) -> dict:
    if t < 0:
        return {"ams_id": 255, "slot_id": 255}
    if t == 254:
        return {"ams_id": 255, "slot_id": 0}
    if t >= 128:
        return {"ams_id": t, "slot_id": 0}
    return {"ams_id": t // 4, "slot_id": t % 4}


async def task_detail(cloud: Cloud, task_id: str) -> dict:
    st, r = await cloud._req("GET", f"{api_base(cloud.cn)}/v1/user-service/my/task/{task_id}")
    d = _json(r) if st == 200 else None
    if not isinstance(d, dict):
        return {"ok": False, "error": _error_text(r, st)}

    def ji(v: Any) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return -1

    rows = []
    for m in (d.get("amsDetailMapping") or [])[:16]:
        rows.append(
            {
                "ams": ji(m.get("ams")),
                "ams_id": ji(m.get("amsId")),
                "slot_id": ji(m.get("slotId")),
                "nozzle_id": ji(m.get("nozzleId")),
                "weight": float(m.get("weight") or 0),
                "filament_id": str(m.get("filamentId") or ""),
                "filament_type": str(m.get("filamentType") or ""),
                "target_filament_type": str(m.get("targetFilamentType") or ""),
                "source_color": str(m.get("sourceColor") or ""),
            }
        )
    ams_mapping = [v if isinstance(v, int) and not isinstance(v, bool) else -1 for v in (d.get("amsMapping") or [])[:16]]
    out = {
        "ok": True,
        "id": task_id,
        "rows": rows,
        "ams_mapping": ams_mapping,
        "use_ams": d.get("useAms") if isinstance(d.get("useAms"), bool) else None,
        "error": "" if rows else "This job has no filament mapping - cannot reprint",
    }
    return out


async def reprint(cloud: Cloud, task: dict, detail: dict, serial: str, rows: list[dict]) -> tuple[bool, str]:
    """rows: per filament row {src, ams_id, slot_id, tray_info_idx, tray_color} (chosen tray).

    Same body as xPTouch's reprint (mode lan_file) PLUS the AMS mapping table: without it a
    P1S on current firmware stops with HMS 0700-8012 "Failed to get AMS mapping table".
    """
    if not task.get("model_id") or not rows:
        return False, "Job cannot be reprinted (no model / filament mapping)"
    flat = [flat_tray(r["ams_id"], r["slot_id"]) for r in rows]
    detail_rows = []
    for r, fl in zip(rows, flat):
        src = r["src"]
        detail_rows.append(
            {
                "ams": fl,
                "nozzleId": src["nozzle_id"],
                "weight": src["weight"],
                "filamentId": r["tray_info_idx"],
                "filamentType": src["filament_type"],
                "targetFilamentType": src["target_filament_type"],
                "amsId": r["ams_id"],
                "slotId": r["slot_id"],
                "sourceColor": (src["source_color"].lstrip("#") + "FF")[:8].upper() if src["source_color"] else "00000000",
                "targetColor": r["tray_color"],
            }
        )
    job_map = detail.get("ams_mapping") or []
    used = sum(1 for v in job_map if v >= 0)
    if job_map and used == len(rows):
        it = iter(flat)
        table = [next(it) if v >= 0 else -1 for v in job_map]
        guessed = False
    else:
        table = list(flat)  # rows in slot order
        guessed = True
    body = {
        "modelId": task["model_id"],
        "title": task.get("title") or "Reprint",
        "profileId": task.get("profile_id", 0),
        "plateIndex": task.get("plate_index", 1),
        "deviceId": serial,
        "amsDetailMapping": detail_rows,
        "amsMapping": table,
        "amsMapping2": [{"amsId": m["ams_id"], "slotId": m["slot_id"]} for m in map(mapping2, table)],
        "useAms": any(r["ams_id"] != 255 for r in rows),
        "mode": "lan_file",
    }
    if task.get("cover"):
        body["cover"] = task["cover"]
    log.info("reprint '%s': ams mapping %s%s", task.get("title"), table, " (slot order guessed)" if guessed else "")
    st, r = await cloud._req("POST", f"{api_base(cloud.cn)}/v1/user-service/my/task", json_body=body)
    if 200 <= st < 300:
        return True, "Sent to the printer through Bambu Cloud"
    return False, _error_text(r, st)


def _start_sd_blocking(
    host: str, uid: str, token: str, serial: str, file: str, plate: int, trays: list[int], opts: dict
) -> tuple[int, str]:
    """Bambu Studio's "print from storage" over the Bambu Cloud broker (blocking, run in a thread).

    Returns (code, message): 200 accepted / preparing, 202 sent without an answer, <0 failure.
    """
    import random
    import ssl
    import threading

    import paho.mqtt.client as mqtt

    seq = str(random.randint(10000, 99999))
    name = file
    for ext in (".gcode.3mf", ".3mf"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    any_ams = any(t >= 0 and t != 254 for t in trays)
    payload = {
        "print": {
            "sequence_id": seq,
            "command": "project_file",
            "param": f"Metadata/plate_{plate}.gcode",
            "project_id": "0",
            "profile_id": "0",
            "task_id": "0",
            "subtask_id": "0",
            "subtask_name": name,
            "file": "",
            "url": f"ftp:///{file}",
            "md5": "",
            "timelapse": bool(opts.get("timelapse")),
            "bed_type": "auto",
            "bed_levelling": bool(opts.get("bed_levelling", True)),
            "flow_cali": bool(opts.get("flow_cali")),
            "vibration_cali": False,
            "layer_inspect": False,
            "use_ams": any_ams,
            "ams_mapping": trays,
            "ams_mapping2": [mapping2(t) for t in trays],
        }
    }
    connected, subscribed, answered, failed = (threading.Event() for _ in range(4))
    state = {"reply": "", "started": False}

    def on_connect(_c, _u, _f, rc, _p=None):
        (connected if not rc.is_failure else failed).set()

    def on_subscribe(_c, _u, _mid, codes, _p=None):
        if any(getattr(c, "is_failure", False) for c in codes):
            failed.set()
        else:
            subscribed.set()

    def on_disconnect(*_a):
        if not answered.is_set():
            failed.set()

    def on_message(_c, _u, msg):
        if len(msg.payload) >= 48 * 1024:
            return
        try:
            d = json.loads(msg.payload)
        except ValueError:
            return
        p = d.get("print") if isinstance(d, dict) else None
        if not isinstance(p, dict):
            return
        if p.get("command") == "project_file" and str(p.get("sequence_id")) == seq:
            state["reply"] = f"{p.get('result', '')} {p.get('reason', '')}".strip()
            answered.set()
        elif p.get("gcode_state") in ("PREPARE", "RUNNING"):
            state["started"] = True
            answered.set()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"bbweb_{random.getrandbits(32):08x}")
    client.username_pw_set(f"u_{uid}", token)
    client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    client.on_connect, client.on_subscribe = on_connect, on_subscribe
    client.on_message, client.on_disconnect = on_message, on_disconnect
    try:
        client.connect_async(host, 8883, keepalive=30)
        client.loop_start()
        if not connected.wait(15) or failed.is_set():
            return -22, "Bambu Cloud MQTT login failed - log in again in Settings"
        client.subscribe(f"device/{serial}/report", qos=0)
        if not subscribed.wait(10) or failed.is_set():
            return -23, "Bambu Cloud did not give access to this printer"
        log.info(
            "sd print: '%s' plate %d, ams_mapping %s, timelapse %s, bed levelling %s, flow cali %s",
            file, plate, trays, opts.get("timelapse"), opts.get("bed_levelling"), opts.get("flow_cali"),
        )
        client.publish(f"device/{serial}/request", json.dumps(payload), qos=1)
        answered.wait(12)
        if state["reply"] and "fail" in state["reply"].lower():
            return -25, f"Printer refused: {state['reply']}"
        if answered.is_set():
            return 200, "Printer is preparing" if state["started"] else "Printer accepted the job"
        if failed.is_set():
            return -24, "Bambu Cloud MQTT connection dropped"
        return 202, "Sent, no answer from the printer yet"
    except OSError as e:
        return -21, f"Cannot reach Bambu Cloud MQTT ({type(e).__name__})"
    finally:
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def start_sd_file(cloud: Cloud, serial: str, file: str, plate: int, trays: list[int], opts: dict) -> tuple[int, str]:
    import asyncio

    uid, err = await cloud.uid()
    if not uid:
        return -20, err or "Bambu Cloud account id not available"
    return await asyncio.to_thread(_start_sd_blocking, mqtt_host(cloud.cn), uid, cloud.token, serial, file, plate, trays, opts)


def _gcode_blocking(host: str, uid: str, token: str, serial: str, gcode: str) -> tuple[bool, str]:
    """One gcode_line over the Bambu Cloud broker (what Bambu Handy does); blocking."""
    import random
    import ssl
    import threading

    import paho.mqtt.client as mqtt

    connected, failed, published = threading.Event(), threading.Event(), threading.Event()

    def on_connect(_c, _u, _f, rc, _p=None):
        (connected if not rc.is_failure else failed).set()

    def on_publish(*_a):
        published.set()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"bbweb_{random.getrandbits(32):08x}")
    client.username_pw_set(f"u_{uid}", token)
    client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    client.on_connect, client.on_publish = on_connect, on_publish
    payload = {"print": {"command": "gcode_line", "param": gcode if gcode.endswith("\n") else gcode + "\n",
                         "sequence_id": str(random.randint(10000, 99999))}}
    try:
        client.connect_async(host, 8883, keepalive=30)
        client.loop_start()
        if not connected.wait(15) or failed.is_set():
            return False, "Bambu Cloud MQTT login failed - log in again in Settings"
        client.publish(f"device/{serial}/request", json.dumps(payload), qos=1)
        if not published.wait(10):
            return False, "Bambu Cloud did not take the command"
        log.info("cloud gcode_line sent: %r", gcode)
        return True, ""
    except OSError as e:
        return False, f"Cannot reach Bambu Cloud MQTT ({type(e).__name__})"
    finally:
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def send_gcode(cloud: Cloud, serial: str, gcode: str) -> tuple[bool, str]:
    import asyncio

    uid, err = await cloud.uid()
    if not uid:
        return False, err or "Bambu Cloud account id not available"
    return await asyncio.to_thread(_gcode_blocking, mqtt_host(cloud.cn), uid, cloud.token, serial, gcode)

"""Tests for app.notify against a local fake HTTP server (never Discord / ntfy.sh)."""

from __future__ import annotations

import asyncio
import base64
import email.header
import json
import logging
import socket
import threading
from collections import deque
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from app import notify
from app.notify import Notifier

JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF" + b"x" * 200 + b"\xff\xd9"
WEBHOOK_PATH = "/api/webhooks/123/SECRETTOKEN"
NTFY_TOKEN = "tk_secret_ntfy_token"
FIXED_NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- fake server


class FakeServer:
    """Captures every request; replies from a queue (status, headers, body) or a default."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.replies: deque[tuple[int, dict[str, str], bytes]] = deque()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.requests.append(
                    {"method": self.command, "path": self.path, "headers": httpx.Headers(list(self.headers.items())), "body": body}
                )
                if outer.replies:
                    status, headers, rbody = outer.replies.popleft()
                elif self.path.startswith("/api/webhooks/"):
                    status, headers, rbody = 204, {}, b""
                else:
                    status, headers, rbody = 200, {"Content-Type": "application/json"}, b'{"id":"x"}'
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(rbody)))
                self.end_headers()
                if rbody:
                    self.wfile.write(rbody)

            do_POST = do_PUT = _handle

            def log_message(self, *args: Any) -> None:  # keep test output quiet
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.close()


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make(server: FakeServer, *, discord=True, ntfy=True, clock=None, secrets=None, **settings) -> Notifier:
    cfg = {
        "notify_discord_enabled": discord,
        "notify_ntfy_enabled": ntfy,
        "ntfy_server": server.base + "/",
        "ntfy_topic": "p1s_alerts",
        **settings,
    }
    sec = {"discord_webhook_url": server.base + WEBHOOK_PATH, "ntfy_token": NTFY_TOKEN} if secrets is None else secrets
    return Notifier(
        cfg,
        sec,
        attempts=3,
        backoff_s=0.01,
        strict_discord_url=False,
        clock=clock or Clock(),
        wall_clock=lambda: FIXED_NOW,
    )


def run(coro):
    return asyncio.run(coro)


async def _send_and_close(n: Notifier, *args, **kwargs):
    try:
        return await n.send(*args, **kwargs)
    finally:
        await n.aclose()


def by_kind(server: FakeServer) -> tuple[list[dict], list[dict]]:
    d = [r for r in server.requests if r["path"].startswith("/api/webhooks/")]
    t = [r for r in server.requests if not r["path"].startswith("/api/webhooks/")]
    return d, t


def multipart(req: dict) -> dict[str, tuple[str | None, str, bytes]]:
    """name -> (filename, content type, payload) of a multipart/form-data request."""
    raw = b"Content-Type: " + req["headers"]["content-type"].encode() + b"\r\n\r\n" + req["body"]
    msg = BytesParser(policy=HTTP).parsebytes(raw)
    out = {}
    for part in msg.iter_parts():
        out[part.get_param("name", header="content-disposition")] = (
            part.get_filename(),
            part.get_content_type(),
            part.get_payload(decode=True),
        )
    return out


# ---------------------------------------------------------------- Discord


def test_discord_json_exact(server):
    n = make(server, ntfy=False)
    res = run(_send_and_close(n, "print_finished", "Print finished: Benchy", "Benchy_PLA finished.",
                              level="success", fields=[("Time", "1h 05m"), ("Filament", "A1 PLA: 12.3 g\nA2 PETG: 1.0 g")],
                              url="http://dash.local/"))
    assert res.delivered and res.channels["discord"].ok and res.channels["discord"].attempts == 1
    (req,), _ = by_kind(server)
    assert req["method"] == "POST" and req["path"] == WEBHOOK_PATH
    assert req["headers"]["content-type"] == "application/json"
    assert json.loads(req["body"]) == {
        "username": "Printer Dashboard",
        "embeds": [
            {
                "title": "Print finished: Benchy",
                "description": "Benchy_PLA finished.",  # Markdown already (builders escape values)
                "color": 0x38FF9C,
                "timestamp": "2026-11-07T12:00:00+00:00",
                "url": "http://dash.local/",
                "fields": [
                    {"name": "Time", "value": "1h 05m", "inline": True},
                    {"name": "Filament", "value": "A1 PLA: 12.3 g\nA2 PETG: 1.0 g", "inline": False},
                ],
                "footer": {"text": "PrinterDash · P1S"},
            }
        ],
        "allowed_mentions": {"parse": []},
    }


def test_discord_multipart_with_image(server):
    n = make(server, ntfy=False)
    res = run(_send_and_close(n, "obico_failure", "Failure", "Spaghetti!", level="critical", image=JPEG))
    assert res.delivered
    (req,), _ = by_kind(server)
    assert req["headers"]["content-type"].startswith("multipart/form-data; boundary=")
    parts = multipart(req)
    assert set(parts) == {"payload_json", "files[0]"}
    fname, ctype, data = parts["files[0]"]
    assert (fname, ctype, data) == ("frame.jpg", "image/jpeg", JPEG)
    payload = json.loads(parts["payload_json"][2])
    assert payload["embeds"][0]["image"] == {"url": "attachment://frame.jpg"}
    assert payload["embeds"][0]["color"] == 0xFF2D95
    assert payload["attachments"] == [{"id": 0, "filename": "frame.jpg"}]


def test_discord_413_falls_back_to_text(server):
    server.replies.append((413, {}, b'{"message":"Request entity too large"}'))
    n = make(server, ntfy=False)
    res = run(_send_and_close(n, "obico_failure", "Failure", "x", image=JPEG))
    assert res.delivered
    d, _ = by_kind(server)
    assert len(d) == 2
    assert d[0]["headers"]["content-type"].startswith("multipart/")
    assert d[1]["headers"]["content-type"] == "application/json"
    assert "image" not in json.loads(d[1]["body"])["embeds"][0]


def test_discord_strict_url_rejects_non_discord(server):
    n = Notifier({"notify_discord_enabled": True}, {"discord_webhook_url": server.base + WEBHOOK_PATH})
    res = run(_send_and_close(n, "print_finished", "t", "m"))
    assert not res.delivered and "Not a Discord webhook" in res.channels["discord"].detail
    assert server.requests == []


# ---------------------------------------------------------------- ntfy


def test_ntfy_text_post_headers_and_body(server):
    n = make(server, discord=False)
    res = run(_send_and_close(n, "print_paused", "Print paused: Benchy", "Benchy is paused.",
                              fields={"Reason": "Filament runout", "Layer": "12/200", "Empty": ""},
                              url="http://dash.local/"))
    assert res.delivered
    _, (req,) = by_kind(server)
    h = req["headers"]
    assert req["method"] == "POST" and req["path"] == "/p1s_alerts"
    assert h["Title"] == "Print paused: Benchy"
    assert h["Priority"] == "4"
    assert h["Tags"] == "pause_button,print_paused"
    assert h["Click"] == "http://dash.local/"
    assert h["Authorization"] == f"Bearer {NTFY_TOKEN}"
    assert h["Content-Type"] == "text/plain; charset=utf-8"
    assert "Filename" not in h and "Message" not in h
    assert req["body"] == b"Benchy is paused.\n\nReason: Filament runout\nLayer: 12/200"


def test_ntfy_image_put_with_filename_and_message_header(server):
    n = make(server, discord=False, notify_app_url="http://dash.local/")
    res = run(_send_and_close(n, "obico_failure", "Hiba: Csónak_ő", "Line one\nLine two", fields=[("Score", "0.91")],
                              image=JPEG))
    assert res.delivered
    _, (req,) = by_kind(server)
    h = req["headers"]
    assert req["method"] == "PUT" and req["path"] == "/p1s_alerts"
    assert req["body"] == JPEG
    assert h["Filename"] == "frame.jpg"
    assert h["Message"] == "Line one\\nLine two\\n\\nScore: 0.91"
    assert h["Priority"] == "5" and h["Tags"] == "rotating_light,obico_failure"
    assert h["Click"] == "http://dash.local/"  # falls back to notify_app_url
    # non-ASCII title is RFC 2047 encoded, which ntfy decodes
    assert h["Title"].startswith("=?UTF-8?B?")
    (decoded, charset), = email.header.decode_header(h["Title"])
    assert decoded.decode(charset) == "Hiba: Csónak_ő"


def test_ntfy_attachments_disabled_falls_back_to_post(server):
    server.replies.append((400, {"Content-Type": "application/json"},
                           b'{"code":40014,"http":400,"error":"invalid request: attachments not allowed"}'))
    n = make(server, discord=False)
    res = run(_send_and_close(n, "obico_failure", "t", "msg", image=JPEG))
    assert res.delivered and res.channels["ntfy"].attempts == 1
    _, reqs = by_kind(server)
    assert [r["method"] for r in reqs] == ["PUT", "POST"]
    assert reqs[1]["body"] == b"msg" and "Filename" not in reqs[1]["headers"]


def test_ntfy_basic_auth_and_no_token(server):
    n = make(server, discord=False, secrets={"ntfy_token": "user:pass"})
    run(_send_and_close(n, "print_finished", "t", "m"))
    n2 = make(server, discord=False, secrets={})
    run(_send_and_close(n2, "print_failed", "t", "m"))
    a, b = server.requests
    assert a["headers"]["Authorization"] == "Basic " + base64.b64encode(b"user:pass").decode()
    assert "Authorization" not in b["headers"]


def test_ntfy_bad_topic_is_config_error(server):
    n = make(server, discord=False, ntfy_topic="bad topic/x")
    res = run(_send_and_close(n, "print_finished", "t", "m"))
    assert not res.delivered and "topic" in res.channels["ntfy"].detail
    assert server.requests == []


# ---------------------------------------------------------------- toggles, dedupe, rate limit


def test_both_channels_and_toggles(server):
    n = make(server)
    res = run(_send_and_close(n, "print_started", "t", "m"))
    assert set(res.channels) == {"discord", "ntfy"} and res.delivered
    assert len(server.requests) == 2

    server.requests.clear()
    n = make(server, notify_on_print_started=False)
    res = run(_send_and_close(n, "print_started", "t", "m"))
    assert res.skipped == "disabled" and server.requests == []

    # obico_warning defaults to off
    res = run(_send_and_close(make(server), "obico_warning", "t", "m"))
    assert res.skipped == "disabled"

    res = run(_send_and_close(make(server, discord=False, ntfy=False), "print_finished", "t", "m"))
    assert res.skipped == "no_channel"

    res = run(_send_and_close(make(server), "no_such_event", "t", "m"))
    assert res.skipped == "unknown_event"
    assert server.requests == []


def test_dedupe_window_and_keys(server):
    clock = Clock()

    async def go():
        n = make(server, discord=False, clock=clock)
        try:
            r1 = await n.send("hms_error", "t", "m", dedupe_key="0300_0100")
            r2 = await n.send("hms_error", "t", "m", dedupe_key="0300_0100")
            r3 = await n.send("hms_error", "t", "m", dedupe_key="0700_2000")
            clock.t += notify.EVENTS["hms_error"].dedupe_s + 1
            r4 = await n.send("hms_error", "t", "m", dedupe_key="0300_0100")
            r5 = await n.send("hms_error", "t", "m", dedupe_key="0300_0100", dedupe_s=0)
            return r1, r2, r3, r4, r5
        finally:
            await n.aclose()

    r1, r2, r3, r4, r5 = run(go())
    assert r1.delivered and r2.skipped == "duplicate" and r3.delivered and r4.delivered and r5.delivered
    assert len(server.requests) == 4


def test_global_rate_limit(server):
    async def go():
        n = make(server, discord=False)
        n.rate_limit = (2, 60.0)
        try:
            return [await n.send("hms_error", "t", "m", dedupe_key=str(i)) for i in range(3)]
        finally:
            await n.aclose()

    res = run(go())
    assert [r.skipped for r in res] == [None, None, "rate_limited"]


# ---------------------------------------------------------------- retries / errors


def test_retry_on_5xx_then_success(server):
    server.replies.extend([(503, {}, b"busy"), (502, {}, b"bad gateway")])
    n = make(server, ntfy=False)
    res = run(_send_and_close(n, "print_failed", "t", "m"))
    assert res.delivered
    assert res.channels["discord"].attempts == 3 and res.channels["discord"].status == 204
    assert len(server.requests) == 3


def test_retry_gives_up_after_attempts(server):
    server.replies.extend([(500, {}, b"x")] * 3)
    n = make(server, discord=False)
    res = run(_send_and_close(n, "print_failed", "t", "m"))
    ch = res.channels["ntfy"]
    assert not res.delivered and ch.attempts == 3 and ch.status == 500 and "HTTP 500" in ch.detail


def test_no_retry_on_4xx(server):
    server.replies.append((401, {}, b'{"code":40101,"error":"unauthorized"}'))
    n = make(server, discord=False)
    res = run(_send_and_close(n, "print_failed", "t", "m"))
    assert not res.delivered and res.channels["ntfy"].attempts == 1 and res.channels["ntfy"].status == 401
    assert len(server.requests) == 1


def test_429_honours_retry_after(server):
    server.replies.append((429, {"Content-Type": "application/json"}, b'{"retry_after": 0.05, "global": false}'))
    n = make(server, ntfy=False)
    res = run(_send_and_close(n, "print_failed", "t", "m"))
    assert res.delivered and res.channels["discord"].attempts == 2


def test_network_error_never_raises_and_hides_secrets(caplog):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    base = f"http://127.0.0.1:{dead}"
    n = Notifier(
        {"notify_discord_enabled": True, "notify_ntfy_enabled": True, "ntfy_server": base, "ntfy_topic": "t1"},
        {"discord_webhook_url": base + WEBHOOK_PATH, "ntfy_token": NTFY_TOKEN},
        attempts=2, backoff_s=0.01, strict_discord_url=False,
    )
    with caplog.at_level(logging.DEBUG):
        res = run(_send_and_close(n, "printer_offline", "t", "m"))
    assert not res.delivered and res.skipped is None
    for ch in ("discord", "ntfy"):
        assert not res.channels[ch].ok and res.channels[ch].attempts == 2 and res.channels[ch].status is None
    text = caplog.text + json.dumps(res.to_dict())
    assert "SECRETTOKEN" not in text and NTFY_TOKEN not in text


def test_broken_settings_getter_does_not_raise():
    def boom() -> dict:
        raise RuntimeError("db locked")

    n = Notifier(boom, lambda name: (_ for _ in ()).throw(RuntimeError("nope")))
    res = run(_send_and_close(n, "print_finished", "t", "m"))
    assert res.skipped == "no_channel"  # defaults: both channels off


def test_bad_image_is_dropped(server):
    n = make(server, discord=False)
    res = run(_send_and_close(n, "print_finished", "t", "m", image=b"GIF89a....."))
    assert res.delivered
    assert server.requests[0]["method"] == "POST"


# ---------------------------------------------------------------- send_test / fire


def test_send_test_specific_channel_even_if_disabled(server):
    n = make(server, discord=False, ntfy=False)

    async def go():
        try:
            return await n.send_test("discord", with_image=True), await n.send_test()
        finally:
            await n.aclose()

    one, none = run(go())
    assert one["discord"].ok
    assert set(none) == {"all"} and not none["all"].ok and "No notification channel" in none["all"].detail
    (req,) = server.requests
    parts = multipart(req)
    fname, ctype, data = parts["files[0]"]
    assert fname == "frame.png" and ctype == "image/png" and data.startswith(b"\x89PNG")


def test_fire_runs_in_background(server):
    n = make(server, discord=False)

    async def go():
        task = n.fire(notify.build_printer_offline(offline_s=300))
        await n.aclose()
        return task.result()

    res = run(go())
    assert res.delivered
    assert server.requests[0]["headers"]["Title"].startswith("=?UTF-8?B?")  # Hungarian title, RFC 2047


# ---------------------------------------------------------------- builders


def test_builders_cover_catalogue():
    fil = [{"label": "A1", "spool_name": "Bambu PLA Basic", "grams": 12.34}, {"label": "Ext", "grams": 0}]
    built = [
        notify.build_print_started("Benchy", estimate_s=3900, filaments=fil, total_layers=200),
        notify.build_print_finished("Benchy", duration_s=3900, filaments=fil, layer=200, total_layers=200),
        notify.build_print_failed("Benchy", reason="Nozzle clog", progress=40, layer=80, total_layers=200),
        notify.build_print_failed("Benchy", stopped=True, progress=10),
        notify.build_print_paused("Benchy", reason="Filament runout", progress=55.4, layer=110, total_layers=200),
        notify.build_obico_warning("Benchy", score=0.41, threshold=0.38),
        notify.build_obico_failure("Benchy", score=0.9, action="pause", image=JPEG),
        notify.build_filament_low("A2", spool_name="eSun PETG", remaining_g=35, spool_id=7),
        notify.build_runout_forecast("Benchy", label="A1", spool_name="PLA", remaining_g=20, needed_g=45.5),
        notify.build_usage_not_booked("Benchy", items=fil),
        notify.build_hms_error("Nozzle temperature malfunction", code="0300_0100_0001_0001", severity=2, job="Benchy"),
        notify.build_printer_offline(offline_s=600, job="Benchy", printing=True),
        notify.build_plate_done("Benchy.3mf", done=1, total=3, next_plate=2, next_name="Arms"),
        notify.build_summary("ma", prints=2, ok=1, failed=1, stopped=0, hours=3.5, grams=120, cost=900),
        notify.build_spool_wet("SUNLU PETG", hours=30, humidity=45, tray="A2", color="#FF0000"),
    ]
    events = {b.event for b in built}
    assert events == set(notify.EVENTS)
    for b in built:
        assert b.title and b.message and b.level in notify.LEVEL_COLOR

    fin = built[1]
    assert dict(fin.fields) == {"⏱️ Nyomtatási idő": "1 óra 05 perc", "🧱 Rétegek": "200/200",
                                "🧵 Felhasznált filament": "A1 Bambu PLA Basic: 12.3 g", "⚖️ Összesen": "12.3 g"}
    assert fin.message == "Elkészült 1 óra 05 perc alatt. Leveheted a tálcáról."
    assert dict(built[4].fields)["❗ Ok"] == "Filament runout" and dict(built[4].fields)["📊 Haladás"] == "55%"
    assert built[6].image == JPEG and built[6].level == "critical"
    assert built[3].event == "print_stopped" and built[2].event == "print_failed"
    assert dict(built[8].fields)["❗ Hiányzik"] == "25.5 g"
    assert built[10].level == "error" and built[10].dedupe_key == "0300_0100_0001_0001"
    assert built[11].level == "critical" and "Benchy" in built[11].message


def test_settings_catalogue_consistent():
    for k in notify.EVENTS:
        assert notify.SETTINGS_DEFAULTS[f"notify_on_{k}"] is notify.EVENTS[k].default_on
    assert set(notify.SETTINGS_DEFAULTS) == set(notify.SETTINGS_RULES)


# ---------------------------------------------------------------- review fixes


def test_discord_400_bad_embed_url_drops_link(server):
    # Discord's real error shape for a LAN link it considers malformed.
    server.replies.append((400, {"Content-Type": "application/json"},
                           b'{"message":"Invalid Form Body","code":50035,"errors":{"embeds":{"0":{"url":'
                           b'{"_errors":[{"code":"URL_TYPE_INVALID_URL","message":"Not a well formed URL."}]}}}}}'))
    n = make(server, ntfy=False, notify_app_url="http://printer-dash:8080/")
    res = run(_send_and_close(n, "print_finished", "t", "m"))
    assert res.delivered and res.channels["discord"].attempts == 1
    d, _ = by_kind(server)
    assert json.loads(d[0]["body"])["embeds"][0]["url"] == "http://printer-dash:8080/"
    assert "url" not in json.loads(d[1]["body"])["embeds"][0]


def test_discord_400_without_link_is_not_resent(server):
    server.replies.append((400, {}, b'{"message":"Invalid Form Body","code":50035}'))
    n = make(server, ntfy=False)
    res = run(_send_and_close(n, "print_finished", "t", "m"))
    assert not res.delivered and len(server.requests) == 1


def test_discord_empty_message_has_no_empty_strings(server):
    n = make(server, ntfy=False)
    run(_send_and_close(n, "print_finished", "  ", ""))
    embed = json.loads(server.requests[0]["body"])["embeds"][0]
    assert "description" not in embed and embed["title"] == "Printer notification"


def test_strict_discord_url_variants():
    def err(url: str) -> str | None:
        return Notifier({}, {"discord_webhook_url": url})._discord_config_error()

    assert err("https://discord.com/api/webhooks/123/abcDEF_-x") is None
    assert err("https://discord.com/api/v10/webhooks/123/abcDEF") is None
    assert err("https://canary.discord.com/api/webhooks/123/abc") is None
    assert err("http://discord.com/api/webhooks/123/abc") is not None
    assert err("https://discord.com.evil.io/api/webhooks/123/abc") is not None
    assert err("https://discord.com/api/webhooks/") is not None
    assert err("") == "Discord webhook URL is not set"


def test_failed_delivery_rearms_dedupe_sooner(server):
    clock = Clock()
    server.replies.extend([(500, {}, b"x")] * 3)

    async def go():
        n = make(server, discord=False, clock=clock)
        try:
            r1 = await n.send("hms_error", "t", "m", dedupe_key="k")  # 3x 500 -> lost
            r2 = await n.send("hms_error", "t", "m", dedupe_key="k")  # still inside re-arm time
            clock.t += notify._FAIL_REARM_S + 1
            r3 = await n.send("hms_error", "t", "m", dedupe_key="k")  # one more try, works
            r4 = await n.send("hms_error", "t", "m", dedupe_key="k")  # success -> full window
            clock.t += notify._FAIL_REARM_S + 1
            r5 = await n.send("hms_error", "t", "m", dedupe_key="k")
            return r1, r2, r3, r4, r5
        finally:
            await n.aclose()

    r1, r2, r3, r4, r5 = run(go())
    assert not r1.delivered and r1.skipped is None
    assert r2.skipped == "duplicate"
    assert r3.delivered
    assert r4.skipped == "duplicate" and r5.skipped == "duplicate"


def test_408_is_retried(server):
    server.replies.append((408, {}, b"timeout"))
    n = make(server, discord=False)
    res = run(_send_and_close(n, "print_failed", "t", "m"))
    assert res.delivered and res.channels["ntfy"].attempts == 2


def test_ntfy_long_text_is_clipped_and_click_is_ascii(server):
    n = make(server, discord=False)
    long_msg = "ő" * 5000  # 10000 UTF-8 bytes: ntfy would turn it into attachment.txt
    res = run(_send_and_close(n, "print_finished", "t", long_msg, url="http://dash.local/fájl?x=ő"))
    assert res.delivered
    req = server.requests[0]
    assert len(req["body"]) <= notify._NTFY_MAX_TEXT
    req["body"].decode("utf-8")  # cut on a character boundary
    assert req["headers"]["Click"] == "http://dash.local/f%C3%A1jl?x=%C5%91"

    server.requests.clear()
    n = make(server, discord=False)
    res = run(_send_and_close(n, "obico_failure", "t", long_msg, image=JPEG))
    assert res.delivered
    (title, charset), = email.header.decode_header(server.requests[0]["headers"]["Message"])
    assert len(title) <= notify._NTFY_MAX_TEXT


def test_scrub_hides_webhook_token_alone():
    n = Notifier({}, {"discord_webhook_url": "https://discord.com/api/webhooks/123/SECRETTOKEN_abc",
                      "ntfy_token": NTFY_TOKEN})
    out = n._scrub(f"POST /api/webhooks/123/SECRETTOKEN_abc failed, auth {NTFY_TOKEN}")
    assert "SECRETTOKEN" not in out and NTFY_TOKEN not in out


def test_aclose_cancels_stuck_background_send(server):
    # Discord asks to wait 20 s; shutdown must not hang on it nor close the client under it.
    server.replies.append((429, {"Content-Type": "application/json"}, b'{"retry_after": 20}'))
    n = make(server, ntfy=False)

    async def go():
        task = n.fire(notify.build_printer_offline(offline_s=300))
        await asyncio.sleep(0.2)
        t0 = asyncio.get_running_loop().time()
        await n.aclose(timeout_s=0.1)
        return task, asyncio.get_running_loop().time() - t0

    task, took = run(go())
    assert task.done() and task.cancelled() and took < 2
    assert n._client is None


def test_builders_tolerate_unknown_values():
    # parse_status() reports unknowns as -1; Obico score may be None before the first frame.
    st = notify.build_print_started("", estimate_s=-60, total_layers=-1)
    assert dict(st.fields) == {} and st.title == "▶️ Nyomtatás elindult: Nyomtatás"
    fin = notify.build_print_finished("Benchy", duration_s=None, layer=-1, total_layers=200)
    assert dict(fin.fields) == {"🧱 Rétegek": "200/200"}
    fail = notify.build_print_failed("Benchy", progress=-1, layer=-1, total_layers=-1, duration_s=-1)
    assert dict(fail.fields) == {}
    assert dict(notify.build_obico_warning("B", score=None).fields) == {}
    assert dict(notify.build_obico_failure("B", score=None, action="pause_and_off").fields) == {"Teendő": "pause_and_off"}
    assert "konnektor" in notify.build_obico_failure("B", score=0.8, action="pause_and_off").message
    rf = notify.build_runout_forecast("B", label="A1", remaining_g=None, needed_g=30)
    assert "❗ Hiányzik" not in dict(rf.fields) and dict(rf.fields)["📐 Szükséges"] == "30.0 g"
    assert notify.fmt_duration(20) == "<1 perc" and notify.fmt_duration(-1) == "?"
    assert notify.fmt_duration(3600) == "1 óra 00 perc"


def test_hms_levels_follow_bambu_alert_level():
    # code >> 16: 1 error (task stopped), 2 warning (paused), 3 notification, 0 invalid.
    lv = {s: notify.build_hms_error("x", code="c", severity=s) for s in (0, 1, 2, 3, None)}
    sev = "Súlyosság"
    assert lv[1].level == "critical" and dict(lv[1].fields)[sev] == "Hiba (a nyomtatás leállt)"
    assert lv[2].level == "error" and dict(lv[2].fields)[sev] == "Figyelmeztetés (a nyomtatás szünetel)"
    assert lv[3].level == "warning" and dict(lv[3].fields)[sev] == "Értesítés"
    assert lv[0].level == "warning" and sev not in dict(lv[0].fields)
    assert lv[None].level == "warning"
    empty = notify.build_hms_error("", code="0300010000010001", severity=1)
    assert empty.title == "🛠️ Nyomtatóhiba: 0300010000010001" and empty.message == "0300010000010001"


def test_usage_not_booked_lists_only_missing_items():
    items = [
        {"label": "A1", "spool_name": "PLA", "grams": 10.0, "state": "missing"},
        {"label": "A2", "spool_name": "PETG", "grams": 5.0, "state": "booked"},
        {"label": "A3", "spool_name": "", "grams": 2.0, "state": "no_spool"},
    ]
    n = notify.build_usage_not_booked("Benchy", items=items)
    assert dict(n.fields) == {"Nincs lekönyvelve": "A1 PLA: 10.0 g", "⚖️ Összesen": "10.0 g"}

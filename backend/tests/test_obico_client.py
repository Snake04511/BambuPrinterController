"""Tests for app.obico_client - all against local fakes (MockTransport / 127.0.0.1 servers).

Run: cd backend && ../.venv/bin/python -m pytest tests/test_obico_client.py -q
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app import obico_client as oc  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" * 10 + b"\xff\xd9"
RUNNING = {"connected": True, "state": "RUNNING", "job": "benchy"}


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def cfg(**kw) -> oc.ObicoConfig:
    base = dict(enabled=True, ml_url="http://ml.test:3333", frame_base_url="http://app.test:8080", poll_interval=10)
    base.update(kw)
    return oc.ObicoConfig(**base)


async def jpeg_source() -> bytes:
    return JPEG


def det(*confs: float) -> dict:
    return {"detections": [["failure", c, [10, 10, 5, 5]] for c in confs]}


def parse_obico_equiv(d: oc.ObicoDetector) -> None:
    """status() is the dict Engine.obico holds and survives JSON (the API path)."""
    st = d.status()
    assert {"enabled", "running", "cls", "score", "frames", "low", "high"} <= set(st)
    assert json.loads(json.dumps(st)) == st


# ---------------------------------------------------------------- smoothing math


def test_smoothing_vectors_from_formulas() -> None:
    """Independent check (no reference import): closed-form EWM, warm-up and thresholds."""
    s = oc.PrintState()
    for _ in range(oc.WARMUP_FRAMES):
        assert s.update(0.9) == 0.0  # warm-up always 0 -> safe
    assert s.ewm_mean == pytest.approx(0.9)
    s = oc.PrintState()
    for _ in range(30):
        s.update(0.0)
    alpha = 2 / 13
    for n in range(1, 11):
        score = s.update(1.0)
        # short and long buffers are identical below 310 frames -> difference is 0, score = ewm
        assert score == pytest.approx(1 - (1 - alpha) ** n)
    assert oc.thresholds("medium") == (0.38, 0.78)
    assert oc.thresholds("high") == pytest.approx((0.285, 0.585))
    assert oc.thresholds("low") == pytest.approx((0.475, 0.975))
    assert oc.classify(0.379, "medium") == "safe"
    assert oc.classify(0.38, "medium") == "warning"
    assert oc.classify(0.78, "medium") == "failure"
    assert oc.classify(0.5, "low") == "warning" and oc.classify(0.6, "high") == "failure"


def test_rolling_mean_term_wins_after_baseline() -> None:
    """Long quiet baseline then a moderate rise: short-long can exceed the EWM path."""
    s = oc.PrintState()
    for _ in range(2000):
        s.update(0.0)
    for _ in range(310):
        score = s.update(0.5)
    short_mean, long_mean = 0.5, 0.5 * 310 / 2310
    assert score == pytest.approx(max(s.ewm_mean, short_mean - long_mean))


# ---------------------------------------------------------------- detector with a MockTransport ML server


class FakeML:
    """httpx MockTransport handler that behaves like Obico's /p/: fetches the frame from our store."""

    def __init__(self, detector_ref: list, script: list) -> None:
        self.detector_ref = detector_ref
        self.script = list(script)
        self.requests: list[httpx.Request] = []
        self.fetched: list[bytes | None] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.path == "/p/"
        img = request.url.params["img"]
        prefix = "http://app.test:8080/api/obico/frame/"
        assert img.startswith(prefix)
        d: oc.ObicoDetector = self.detector_ref[0]
        data = d.frames.pop(img[len(prefix):])
        self.fetched.append(data)
        assert d.frames.pop(img[len(prefix):]) is None  # single use
        item = self.script.pop(0) if self.script else det()
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)


def make(script: list, **kw) -> tuple[oc.ObicoDetector, FakeML, list, FakeClock]:
    ref: list = []
    ml = FakeML(ref, script)
    events: list[oc.ObicoEvent] = []
    clock = FakeClock()
    conf = kw.pop("config", cfg())

    async def on_event(e: oc.ObicoEvent) -> None:
        events.append(e)

    d = oc.ObicoDetector(
        conf,
        kw.pop("source", jpeg_source),
        kw.pop("on_event", on_event),
        http=httpx.AsyncClient(transport=httpx.MockTransport(ml)),
        clock=clock,
    )
    ref.append(d)
    return d, ml, events, clock


def test_safe_warning_failure_sequence() -> None:
    async def main() -> None:
        # 30 warm-up frames with high noise (must stay safe), then rising failure confidence
        script = [det(0.9)] * 30 + [det(0.0)] * 5 + [det(0.6, 0.4)] * 15
        d, ml, events, _ = make(script)
        mirror = oc.PrintState()
        classes = []
        for item in script:
            assert await d.analyse(RUNNING) is True
            expected = oc.classify(mirror.update(oc.score_from_detections(item["detections"])), "medium")
            st = d.status()
            classes.append(st["cls"])
            assert st["cls"] == expected
            assert st["frames"] == mirror.frame_count
            assert st["score"] == round(mirror.ewm_mean, 4)
            parse_obico_equiv(d)
        assert classes[:30] == ["safe"] * 30
        assert "warning" in classes and classes[-1] == "failure"
        assert classes.index("warning") < classes.index("failure")
        assert [e.kind for e in events] == ["warning", "failure"]  # once each per print
        fail = events[1]
        assert fail.frame == JPEG and fail.job == "benchy" and fail.action == "notify"
        assert fail.score >= fail.high == 0.78
        assert all(f == JPEG for f in ml.fetched)
        assert len(d.frames) == 0
        assert "authorization" not in ml.requests[0].headers  # no token configured
        assert d.history() and d.history()[0]["class"] == "failure"
        await d.aclose()

    asyncio.run(main())


def test_failure_without_warning_step_fires_failure_only() -> None:
    async def main() -> None:
        d, _, events, _ = make([det(0.0)] * 30 + [det(6.0)])  # alpha*6 = 0.92 >= 0.78 at once
        for _ in range(31):
            await d.analyse(RUNNING)
        assert d.status()["cls"] == "failure"
        assert [e.kind for e in events] == ["failure"]
        await d.aclose()

    asyncio.run(main())


def test_reset_on_state_change_and_new_job() -> None:
    async def main() -> None:
        d, _, events, clock = make([det(0.0)] * 30 + [det(6.0)] * 3)
        for _ in range(33):
            clock.t += 11
            assert await d.step(RUNNING)
        assert d.status()["cls"] == "failure" and len(events) == 1
        # pause -> state dropped (only RUNNING is monitored)
        assert await d.step({**RUNNING, "state": "PAUSE"}) is False
        st = d.status()
        assert st["has_printer"] is False and st["cls"] == "" and st["frames"] == 0
        parse_obico_equiv(d)
        # disconnected / unknown status keeps whatever state exists
        clock.t += 11
        await d.step(RUNNING)
        assert d.status()["frames"] == 1
        await d.step({**RUNNING, "connected": False})
        await d.step(None)
        assert d.status()["frames"] == 1
        # a different job name while RUNNING = new print = fresh state
        clock.t += 11
        await d.step({**RUNNING, "job": "cube"})
        assert d.status()["frames"] == 1 and d.status()["cls"] == "safe"
        await d.aclose()

    asyncio.run(main())


def test_step_rate_limit_and_disabled() -> None:
    async def main() -> None:
        conf = {"c": cfg(poll_interval=10)}
        d, ml, _, clock = make([], config=lambda: conf["c"])
        assert await d.step(RUNNING) is True
        assert await d.step(RUNNING) is False  # not due yet
        clock.t += 9.9
        assert await d.step(RUNNING) is False
        clock.t += 0.2
        assert await d.step(RUNNING) is True
        assert len(ml.requests) == 2
        conf["c"] = cfg(poll_interval=1)  # clamped to MIN_POLL_S
        clock.t += 10
        assert await d.step(RUNNING) is True
        clock.t += 4
        assert await d.step(RUNNING) is False
        assert d.running is True
        clock.t += 100
        assert d.running is False  # nobody drives it any more
        conf["c"] = cfg(enabled=False)
        clock.t += 10
        assert await d.step(RUNNING) is False
        st = d.status()
        assert st["enabled"] is False and st["has_printer"] is False
        parse_obico_equiv(d)
        await d.aclose()

    asyncio.run(main())


def test_ml_errors_give_error_class_not_safe() -> None:
    async def main() -> None:
        script = [
            httpx.Response(401),
            httpx.Response(500, text="Internal Server Error"),
            httpx.Response(200, text="not json"),
            det(0.1),
        ]
        d, _, _, _ = make(script, config=cfg(ml_token="s3cr3t-token"))
        assert await d.analyse(RUNNING) is False
        st = d.status()
        assert st["cls"] == "error" and "401" in st["error"] and st["frames"] == 0
        parse_obico_equiv(d)
        assert await d.analyse(RUNNING) is False
        assert "HTTP 500" in d.status()["error"]
        assert await d.analyse(RUNNING) is False
        assert "JSON" in d.status()["error"]
        assert await d.analyse(RUNNING) is True  # recovers, error cleared
        st = d.status()
        assert st["cls"] == "safe" and st["error"] == "" and st["frames"] == 1
        assert "s3cr3t-token" not in json.dumps(st)
        await d.aclose()

    asyncio.run(main())


def test_frame_source_failures_and_missing_frame_url() -> None:
    async def main() -> None:
        async def tuple_err():
            return None, "Camera token rejected"

        async def boom():
            raise RuntimeError("camera exploded")

        d, ml, _, _ = make([], source=tuple_err)
        assert await d.analyse(RUNNING) is False
        assert d.status()["cls"] == "error" and "Camera token rejected" in d.status()["error"]
        await d.aclose()

        d, ml, _, _ = make([], source=boom)
        assert await d.analyse(RUNNING) is False
        assert "RuntimeError" in d.status()["error"]
        await d.aclose()

        async def tuple_ok():
            return JPEG, ""

        d, ml, _, _ = make([], source=tuple_ok, config=cfg(frame_base_url=""))
        assert await d.analyse(RUNNING) is False
        assert "Frame URL" in d.status()["error"] and not ml.requests
        await d.aclose()

    asyncio.run(main())


def test_unknown_before_first_verdict_and_callback_errors_are_contained() -> None:
    async def main() -> None:
        def bad_callback(_e):
            raise ValueError("handler bug")

        d, _, _, _ = make([det(0.0)] * 30 + [det(9.0)] * 2, on_event=bad_callback)
        d._state, d._job = oc.PrintState(), "benchy"  # print seen, nothing analysed yet
        assert d.status()["cls"] == "unknown"
        for _ in range(31):
            await d.analyse(RUNNING)
        st = d.status()
        assert st["cls"] == "failure" and "event handler failed" in st["error"]
        await d.analyse(RUNNING)  # the next good poll clears the global error
        assert d.status()["error"] == "" and d.status()["cls"] == "failure"
        await d.aclose()

    asyncio.run(main())


# ---------------------------------------------------------------- frame store + FastAPI route


def test_frame_store_single_use_ttl_and_cap() -> None:
    clock = FakeClock()
    fs = oc.FrameStore(ttl=30, max_items=3, clock=clock)
    n = fs.put(b"a")
    assert len(n) >= 40
    assert fs.pop(n) == b"a" and fs.pop(n) is None
    n = fs.put(b"b")
    clock.t += 31
    assert fs.pop(n) is None
    ns = [fs.put(bytes([i])) for i in range(5)]
    assert len(fs) == 3 and fs.pop(ns[0]) is None and fs.pop(ns[4]) == b"\x04"
    assert fs.pop("nope") is None


def test_frame_route_via_fastapi() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    fs = oc.FrameStore()
    app = FastAPI()

    @app.get(oc.FRAME_ROUTE, include_in_schema=False)
    async def obico_frame(nonce: str):
        return await fs.response(nonce)

    n = fs.put(JPEG)
    with TestClient(app) as c:
        r = c.get(f"/api/obico/frame/{n}")
        assert r.status_code == 200 and r.content == JPEG
        assert r.headers["content-type"] == "image/jpeg" and r.headers["cache-control"] == "no-store"
        assert c.get(f"/api/obico/frame/{n}").status_code == 404


# ---------------------------------------------------------------- real sockets: ML server down / fetching via URL


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_ml_server_down_real_socket() -> None:
    async def main() -> None:
        port = _free_port()  # nothing listens here -> connection refused
        d = oc.ObicoDetector(cfg(ml_url=f"http://127.0.0.1:{port}"), jpeg_source)
        assert await d.analyse(RUNNING) is False
        st = d.status()
        assert st["cls"] == "error" and "unreachable" in st["error"].lower()
        assert st["frames"] == 0 and len(d.frames) == 0
        r = await d.test_connection()
        assert r["ok"] is False and r["error"].startswith("Unreachable")
        await d.aclose()

    asyncio.run(main())


class _MLHandler(BaseHTTPRequestHandler):
    """Fake Obico ML API: /hc/ and /p/?img=URL (fetches the URL like the real server)."""

    server: "_MLServer"

    def log_message(self, *a) -> None:  # keep test output quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        u = urllib.parse.urlparse(self.path)
        if u.path == "/hc/":
            return self._send(200, b"ok", "text/plain")
        if u.path != "/p/":
            return self._send(404, b"{}")
        self.server.auths.append(self.headers.get("Authorization"))
        if self.server.token and self.headers.get("Authorization") != f"Bearer {self.server.token}":
            return self._send(401, b"")
        img = urllib.parse.parse_qs(u.query).get("img", [None])[0]
        if not img:
            return self._send(422, b'{"error": "Invalid request params"}')
        try:
            with urllib.request.urlopen(img, timeout=5) as r:
                data = r.read()
        except Exception:
            return self._send(500, b"")
        self.server.fetched.append(data)
        conf = self.server.confs.pop(0) if self.server.confs else 0.0
        dets = [["failure", conf, [1, 1, 1, 1]]] if conf else []
        self._send(200, json.dumps({"detections": dets}).encode())


class _MLServer(ThreadingHTTPServer):
    def __init__(self, token: str, confs: list[float]) -> None:
        super().__init__(("127.0.0.1", 0), _MLHandler)
        self.token, self.confs = token, list(confs)
        self.auths: list = []
        self.fetched: list[bytes] = []


def test_end_to_end_real_ml_server_fetches_frame_url() -> None:
    """Real uvicorn app serving the frame route + real ML fake that GETs the img URL."""
    import uvicorn
    from fastapi import FastAPI

    ml = _MLServer("tok-123", [0.0] * 30 + [1.0] * 12)
    th = threading.Thread(target=ml.serve_forever, daemon=True)
    th.start()

    async def main() -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        app_port = sock.getsockname()[1]
        events: list[oc.ObicoEvent] = []
        d = oc.ObicoDetector(
            cfg(
                ml_url=f"http://127.0.0.1:{ml.server_address[1]}",
                frame_base_url=f"http://127.0.0.1:{app_port}",
                ml_token="tok-123",
            ),
            jpeg_source,
            events.append,  # sync callbacks work too
        )
        app = FastAPI()

        @app.get(oc.FRAME_ROUTE)
        async def obico_frame(nonce: str):
            return await d.frames.response(nonce)

        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(50):
                if server.started:
                    break
                await asyncio.sleep(0.05)
            r = await d.test_connection()
            assert r == {"ok": True, "status_code": 200, "body": "ok", "error": None, "auth_ok": True}
            bad = await d.test_connection(token="wrong")
            assert bad["ok"] is False and bad["auth_ok"] is False
            for _ in range(42):
                assert await d.analyse(RUNNING) is True
            st = d.status()
            assert st["cls"] == "failure" and st["frames"] == 42
            assert [e.kind for e in events] == ["warning", "failure"]
            assert len(ml.fetched) == 42 and all(f == JPEG for f in ml.fetched)
            assert set(ml.auths[-42:]) == {"Bearer tok-123"}
        finally:
            server.should_exit = True
            await task
            sock.close()
            await d.aclose()

    try:
        asyncio.run(main())
    finally:
        ml.shutdown()
        ml.server_close()


def test_config_from_settings() -> None:
    c = oc.ObicoConfig.from_settings(
        {"obico_enabled": True, "obico_ml_url": " http://x:3333/ ", "obico_sensitivity": "weird", "obico_poll_interval": "abc"},
        token=" t ",
    )
    assert c.enabled and c.ml_url == "http://x:3333" and c.sensitivity == "medium"
    assert c.poll_interval == 10 and c.ml_token == "t" and c.action == "notify" and c.frame_base_url == ""
    assert oc.ObicoConfig.from_settings({}).ml_url == oc.DEFAULT_ML_URL
    assert set(oc.SETTINGS_DEFAULTS) == set(oc.SETTINGS_RULES)


def test_background_task_start_stop() -> None:
    async def main() -> None:
        d, ml, _, _ = make([det(0.0)])
        d.start(lambda: RUNNING)
        d.start(lambda: RUNNING)  # idempotent
        for _ in range(40):
            if ml.requests:
                break
            await asyncio.sleep(0.01)
        assert len(ml.requests) == 1 and d.status()["running"] is True
        task = d._task
        await d.aclose()
        await asyncio.sleep(0)
        assert task is not None and task.cancelled()

    asyncio.run(main())


# ---------------------------------------------------------------- review fixes


def test_config_and_event_repr_hide_secrets_and_bytes() -> None:
    c = cfg(ml_token="s3cr3t-token")
    assert "s3cr3t-token" not in repr(c) and "s3cr3t-token" not in str(c)
    ev = oc.ObicoEvent(
        kind="failure", job="j", score=1.0, ewm=1.0, current_p=1.0, frame_count=31,
        low=0.38, high=0.78, sensitivity="medium", action="notify", frame=JPEG * 100,
    )
    assert "fake-jpeg-body" not in repr(ev) and len(repr(ev)) < 400


def test_url_normalization_in_settings_and_test_connection() -> None:
    c = oc.ObicoConfig.from_settings(
        {"obico_ml_url": " 192.168.1.120:3333/ ", "obico_frame_base_url": "192.168.1.120:8080/"}
    )
    assert c.ml_url == "http://192.168.1.120:3333" and c.frame_base_url == "http://192.168.1.120:8080"
    assert oc.normalize_url("https://x:1/") == "https://x:1" and oc.normalize_url(None) == ""
    assert oc.normalize_url("ftp://x") == "ftp://x"  # foreign scheme kept, then rejected below

    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(200, text="ok") if req.url.path == "/hc/" else httpx.Response(422)

    async def main() -> None:
        d = oc.ObicoDetector(cfg(), jpeg_source, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        r = await d.test_connection("ml.test:3333")
        assert r["ok"] is True and r["auth_ok"] is True
        assert seen == ["http://ml.test:3333/hc/", "http://ml.test:3333/p/"]
        bad = await d.test_connection("ftp://ml.test")
        assert bad["ok"] is False and "http" in bad["error"]
        empty = await d.test_connection("")
        assert empty["ok"] is False
        http = d._http
        await d.aclose()
        assert http is not None
        await http.aclose()  # injected client: the caller owns it

    asyncio.run(main())


def test_missing_frame_url_does_not_touch_camera() -> None:
    calls: list[int] = []

    async def counting_source() -> bytes:
        calls.append(1)
        return JPEG

    async def main() -> None:
        d, ml, _, _ = make([], source=counting_source, config=cfg(frame_base_url=""))
        assert await d.analyse(RUNNING) is False
        assert calls == [] and not ml.requests and d.status()["cls"] == "error"
        await d.aclose()

    asyncio.run(main())


def test_reset_during_inflight_ml_call_drops_verdict() -> None:
    """The print ends (reset) while the ML call is in flight: no state update, no event."""

    async def main() -> None:
        ref: list = []
        events: list = []

        def handler(req: httpx.Request) -> httpx.Response:
            ref[0].reset()  # e.g. FINISH seen / integrator reset during the request
            return httpx.Response(200, json=det(9.0))

        d = oc.ObicoDetector(
            cfg(), jpeg_source, events.append, http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        ref.append(d)
        d._state, d._job = oc.PrintState(), "benchy"
        d._state.frame_count = 40  # past warm-up: a 9.0 frame would be a failure
        assert await d.analyse(RUNNING) is False
        assert events == [] and d.status()["has_printer"] is False and len(d.frames) == 0
        assert d.status()["error"] == ""  # a dropped verdict is not an error (was AssertionError)
        await d.aclose()

    asyncio.run(main())


def test_reset_during_capture_drops_frame() -> None:
    async def main() -> None:
        ref: list = []

        async def resetting_source() -> bytes:
            ref[0].reset()
            return JPEG

        d, ml, events, _ = make([det(9.0)], source=resetting_source)
        ref.append(d)
        assert await d.analyse(RUNNING) is False
        assert not ml.requests and events == [] and d.status()["has_printer"] is False
        await d.aclose()

    asyncio.run(main())


def test_empty_job_blip_keeps_state_and_late_name_is_adopted() -> None:
    async def main() -> None:
        d, _, _, _ = make([det(0.0)] * 6)
        await d.analyse({**RUNNING, "job": ""})  # name not known yet
        await d.analyse(RUNNING)  # name arrives: same print
        assert d.status()["frames"] == 2 and d._job == "benchy"
        await d.analyse({**RUNNING, "job": ""})  # transient blank name mid-print
        assert d.status()["frames"] == 3 and d._job == "benchy"
        await d.analyse({**RUNNING, "job": "cube"})  # a real different name = new print
        assert d.status()["frames"] == 1 and d._job == "cube"
        await d.aclose()

    asyncio.run(main())


def test_aclose_waits_for_inflight_task_and_closes_client() -> None:
    async def main() -> None:
        started = asyncio.Event()
        cancelled: list[bool] = []

        async def hanging_source() -> bytes:
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            return JPEG

        d = oc.ObicoDetector(cfg(), hanging_source)  # owns its http client
        d._client()
        d.start(lambda: RUNNING)
        await asyncio.wait_for(started.wait(), 2)
        task = d._task
        await asyncio.wait_for(d.aclose(), 2)
        assert task is not None and task.done() and cancelled == [True]
        assert d._http is None and d._task is None
        assert d.status()["running"] is False  # stopped means not watching, right away

    asyncio.run(main())

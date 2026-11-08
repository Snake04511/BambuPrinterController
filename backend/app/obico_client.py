"""This app's own Obico AI failure detection.

How the self-hosted Obico ML API works:

- It is GET-only: ``GET {ml_url}/p/?img=<URL>`` and the ML server *fetches the
  image itself* from that URL (with a hard-coded ~5 s read timeout). There is no
  upload variant. So this app must serve the JPEG over HTTP to the ML server.
- Optional auth: when the server runs with ``ML_API_TOKEN`` it answers 401 to any
  ``/p/`` request without ``Authorization: Bearer <token>``. ``/hc/`` (health) is
  never gated, which is why ``test_connection`` also probes ``/p/``.
- Response: ``{"detections": [[label, confidence, [x, y, w, h]], ...]}``.

To keep the 5 s fetch timeout from racing the camera, the frame is captured
first, stashed in :class:`FrameStore` under a random single-use nonce, and the ML
server gets a URL to an unauthenticated route that returns the cached bytes
instantly (``/api/obico/frame/{nonce}``). The nonce is 32 random bytes, valid
for 30 s and served once, so the open route does not widen camera access.

The scoring math: EWM span 12, rolling means of 310 / 7200 frames, 30 warm-up frames,
score = max(ewm, short_mean - long_mean), thresholds 0.38 / 0.78 scaled by the
sensitivity multiplier. The status dict is the shape
``Engine.obico`` holds (see ``status``).

This module never sends printer commands: failure / warning events go to a
callback and the integrator decides whether to notify or pause.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Union

import httpx

log = logging.getLogger("obico")

# ---------------------------------------------------------------- smoothing

EWM_SPAN = 12
EWM_ALPHA = 2.0 / (EWM_SPAN + 1)
ROLLING_SHORT = 310  # ~52 min at 10 s/frame: recent activity
ROLLING_LONG = 7200  # long-term baseline noise
WARMUP_FRAMES = 30  # first frames always "safe" while the state settles

BASE_LOW = 0.38
BASE_HIGH = 0.78
SENSITIVITY_MULT: dict[str, float] = {
    "low": 1.25,  # harder to trigger - higher thresholds
    "medium": 1.0,
    "high": 0.75,  # easier to trigger - lower thresholds
}


def thresholds(sensitivity: str) -> tuple[float, float]:
    """(low, high) for a sensitivity; unknown values fall back to medium."""
    mult = SENSITIVITY_MULT.get(sensitivity, 1.0)
    return BASE_LOW * mult, BASE_HIGH * mult


def classify(score: float, sensitivity: str) -> str:
    """'safe' / 'warning' / 'failure' for a smoothed score (>= low warns, >= high fails)."""
    low, high = thresholds(sensitivity)
    if score >= high:
        return "failure"
    if score >= low:
        return "warning"
    return "safe"


def score_from_detections(detections: Any) -> float:
    """Per-frame ``current_p``: the sum of detection confidences.

    The model is single-class ("failure"), so only the confidence column matters.
    Malformed rows and NaN / inf values are skipped instead of poisoning the EWM.
    """
    total = 0.0
    for det in detections or []:
        try:
            value = float(det[1])
        except (IndexError, KeyError, TypeError, ValueError):
            continue
        if math.isnan(value) or math.isinf(value):
            continue
        total += value
    return total


@dataclass
class PrintState:
    """Per-print smoothing state; a new one is created for every print.

    Running sums are kept (instead of re-summing the buffers) to stay O(1) per
    frame; the arithmetic order is fixed so results are reproducible.
    """

    frame_count: int = 0
    ewm_mean: float = 0.0
    short_sum: float = 0.0
    long_sum: float = 0.0
    short_buf: deque = field(default_factory=lambda: deque(maxlen=ROLLING_SHORT))
    long_buf: deque = field(default_factory=lambda: deque(maxlen=ROLLING_LONG))

    def update(self, current_p: float) -> float:
        """Feed one per-frame score, return the smoothed score (0.0 during warm-up)."""
        self.frame_count += 1

        if self.frame_count == 1:
            self.ewm_mean = current_p
        else:
            self.ewm_mean = EWM_ALPHA * current_p + (1 - EWM_ALPHA) * self.ewm_mean

        if len(self.short_buf) == self.short_buf.maxlen:
            self.short_sum -= self.short_buf[0]
        self.short_buf.append(current_p)
        self.short_sum += current_p

        if len(self.long_buf) == self.long_buf.maxlen:
            self.long_sum -= self.long_buf[0]
        self.long_buf.append(current_p)
        self.long_sum += current_p

        if self.frame_count <= WARMUP_FRAMES:
            return 0.0

        short_mean = self.short_sum / len(self.short_buf)
        long_mean = self.long_sum / len(self.long_buf)
        return max(self.ewm_mean, short_mean - long_mean)


# ---------------------------------------------------------------- settings

DEFAULT_ML_URL = "http://192.168.1.120:3333"
FRAME_ROUTE = "/api/obico/frame/{nonce}"
SECRET_ML_TOKEN = "obico_ml_token"
MIN_POLL_S = 5
ACTIONS = ("notify", "pause")

# Merge into settings.DEFAULTS / settings._RULES at integration time.
SETTINGS_DEFAULTS: dict[str, Any] = {
    "obico_enabled": False,
    "obico_ml_url": DEFAULT_ML_URL,
    "obico_sensitivity": "medium",  # low | medium | high
    "obico_action": "notify",  # passed through to the event callback: notify | pause
    "obico_poll_interval": 10,
    # Base URL of THIS app as reachable from the ML server (it fetches the frame from it),
    # e.g. "http://192.168.1.120:8080". Empty = detection cannot run.
    "obico_frame_base_url": "",
}
SETTINGS_RULES: dict[str, tuple] = {
    "obico_enabled": (bool,),
    "obico_ml_url": (str,),
    "obico_sensitivity": (str,),
    "obico_action": (str,),
    "obico_poll_interval": (int, MIN_POLL_S, 300),
    "obico_frame_base_url": (str,),
}


def normalize_url(url: Any) -> str:
    """'host:port' / ' http://host:port/ ' -> 'http://host:port'.

    WHY: settings are free text; without a scheme httpx raises UnsupportedProtocol /
    InvalidURL on every poll, and a frame URL without a scheme makes the ML server
    fail with an opaque HTTP 500. Lenient about a missing scheme / trailing slash.
    """
    url = str(url or "").strip().rstrip("/ ")
    if url and "://" not in url:
        url = "http://" + url
    return url


@dataclass(frozen=True)
class ObicoConfig:
    """Everything the detector needs; re-read every step so settings apply live."""

    enabled: bool = False
    ml_url: str = DEFAULT_ML_URL
    # secret: never logged or returned by status(); repr=False keeps it out of
    # tracebacks / debug logs that print the config object.
    ml_token: str = field(default="", repr=False)
    sensitivity: str = "medium"
    action: str = "notify"
    poll_interval: int = 10
    frame_base_url: str = ""

    @classmethod
    def from_settings(cls, cfg: dict[str, Any], token: str = "") -> ObicoConfig:
        """Build from the app's settings dict (see SETTINGS_DEFAULTS) + the token secret."""

        def pick(key: str) -> Any:
            return cfg.get(key, SETTINGS_DEFAULTS[key])

        try:
            interval = int(pick("obico_poll_interval"))
        except (TypeError, ValueError):
            interval = 10
        sens = str(pick("obico_sensitivity") or "medium")
        action = str(pick("obico_action") or "notify")
        return cls(
            enabled=bool(pick("obico_enabled")),
            ml_url=normalize_url(pick("obico_ml_url")),
            ml_token=(token or "").strip(),
            sensitivity=sens if sens in SENSITIVITY_MULT else "medium",
            action=action if action in ACTIONS else "notify",
            poll_interval=interval,
            frame_base_url=normalize_url(pick("obico_frame_base_url")),
        )


def auth_headers(token: str | None) -> dict[str, str]:
    """Bearer header only when a token is set (an unset ML_API_TOKEN ignores the header)."""
    token = (token or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


# ---------------------------------------------------------------- frame store (for the ML server's fetch)

FRAME_TTL_S = 30.0  # the ML server fetches within ~1 s of getting the URL
FRAME_MAX = 8  # bound memory if the ML server never fetches


class FrameStore:
    """Single-use, short-lived JPEG cache behind unguessable nonces.

    Only touched from the event loop thread, so plain dict operations are atomic
    and no lock is needed.
    """

    def __init__(self, ttl: float = FRAME_TTL_S, max_items: int = FRAME_MAX, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._max = max_items
        self._clock = clock
        self._items: dict[str, tuple[bytes, float]] = {}

    def __len__(self) -> int:
        self._prune()
        return len(self._items)

    def _prune(self) -> None:
        now = self._clock()
        for k in [k for k, (_d, ts) in self._items.items() if now - ts > self._ttl]:
            self._items.pop(k, None)

    def put(self, data: bytes) -> str:
        """Stash a frame, return its nonce. Oldest entries are evicted past FRAME_MAX."""
        self._prune()
        while len(self._items) >= self._max:
            self._items.pop(next(iter(self._items)))
        nonce = secrets.token_urlsafe(32)
        self._items[nonce] = (data, self._clock())
        return nonce

    def pop(self, nonce: str) -> bytes | None:
        """Return and forget a frame; None when unknown, already served or expired."""
        self._prune()
        entry = self._items.pop(nonce, None) if isinstance(nonce, str) else None
        return entry[0] if entry else None

    def discard(self, nonce: str) -> None:
        self._items.pop(nonce, None)

    async def response(self, nonce: str) -> Any:
        """FastAPI handler body for ``GET /api/obico/frame/{nonce}`` (no auth on purpose)."""
        from fastapi import HTTPException, Response

        data = self.pop(nonce)
        if data is None:
            raise HTTPException(status_code=404, detail="Frame not found or expired")
        return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- detector

HISTORY_MAX = 50
DETECTION_TIMEOUT_S = 30.0
HEALTH_TIMEOUT_S = 5.0
FRAME_TIMEOUT_S = 20.0

FrameResult = Union[bytes, None, tuple]
FrameSource = Callable[[], Awaitable[FrameResult]]
EventCallback = Callable[["ObicoEvent"], Union[Awaitable[None], None]]
ConfigSource = Union[ObicoConfig, Callable[[], ObicoConfig]]


@dataclass
class ObicoEvent:
    """A warning or failure verdict, delivered once per print per kind."""

    kind: str  # "warning" | "failure"
    job: str
    score: float  # smoothed score that crossed the threshold
    ewm: float
    current_p: float
    frame_count: int
    low: float
    high: float
    sensitivity: str
    action: str  # configured action; the callback decides what to do with it
    # the exact JPEG the model flagged (attach to the notification); repr=False so
    # logging the event does not dump ~100 kB of bytes into the log.
    frame: bytes = field(repr=False)
    timestamp: str = ""


class ObicoDetector:
    """Polls the Obico ML API with camera frames while a print is RUNNING.

    Lifecycle: a state is created when a RUNNING print is first
    seen (keyed by job name), and dropped as soon as the printer reports any
    other known state (so a pause/resume or a new print starts a fresh warm-up).

    Drive it either with :meth:`start` (own background task) or by awaiting
    :meth:`step` from an existing loop; :meth:`step` is rate-limited internally.
    """

    def __init__(
        self,
        config: ConfigSource,
        frame_source: FrameSource,
        on_event: EventCallback | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        frames: FrameStore | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config_src = config
        self._frame_source = frame_source
        self._on_event = on_event
        self._http = http
        self._own_http = http is None
        self._clock = clock
        self.frames = frames or FrameStore(clock=clock)

        self._state: PrintState | None = None
        self._job: str | None = None
        self._last_class: str | None = None  # only set after a real inference
        self._error: str | None = None  # why the latest poll gave no verdict
        self._last_error: str | None = None
        self._fired: set[str] = set()  # event kinds already delivered this print
        self._history: deque = deque(maxlen=HISTORY_MAX)
        self._next_due = 0.0
        self._last_step = -1e9
        self._busy = False
        self._task: asyncio.Task | None = None
        self._logged_error: str | None = None

    # ---- config / plumbing

    def config(self) -> ObicoConfig:
        src = self._config_src
        try:
            cfg = src() if callable(src) else src
        except Exception as e:  # a broken settings read must not kill the loop
            log.error("Obico config read failed: %s", type(e).__name__)
            return ObicoConfig()
        return cfg if isinstance(cfg, ObicoConfig) else ObicoConfig()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=DETECTION_TIMEOUT_S)
            self._own_http = True
        return self._http

    async def aclose(self) -> None:
        """Stop the loop and WAIT for it before closing the HTTP client.

        WHY: a bare cancel() only schedules the cancellation; closing the client
        while the cancelled task is still mid-request (or leaving the task pending
        at loop shutdown) gives "Task was destroyed but it is pending" warnings and
        closed-client errors.
        """
        task, self._task = self._task, None
        self._last_step = -1e9
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # run() never raises, but shutdown must not either
                log.exception("Obico detection task failed during shutdown")
        if self._own_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    # ---- lifecycle

    def start(self, status_getter: Callable[[], dict | None]) -> None:
        """Run the detection loop as a background task (idempotent)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(status_getter), name="obico-detector")

    def stop(self) -> None:
        """Cancel the loop; ``running`` turns False at once (not after the step window)."""
        self._last_step = -1e9
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def run(self, status_getter: Callable[[], dict | None]) -> None:
        """Ticks every second; :meth:`step` decides whether a frame is due."""
        while True:
            try:
                await self.step(status_getter())
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never die: report and back off (30 s)
                self._last_error = f"Obico loop error: {type(e).__name__}"
                log.exception("Obico detection loop error")
                await asyncio.sleep(30)

    def reset(self) -> None:
        """Forget the current print (next RUNNING print starts a fresh warm-up)."""
        self._state = None
        self._job = None
        self._last_class = None
        self._error = None
        self._fired.clear()

    # ---- the step

    async def step(self, status: dict | None) -> bool:
        """Analyse one frame if a print is RUNNING and the poll interval elapsed.

        ``status`` is the normalized printer status (``PrinterMqtt.status()``
        shape: ``state``, ``job``, ``connected``). Returns True when an ML call
        produced a verdict this step.
        """
        self._last_step = self._clock()
        cfg = self.config()
        if not cfg.enabled or not cfg.ml_url:
            self.reset()
            return False
        if not status or status.get("connected") is False:
            return False  # unknown state: keep the print state, just skip
        if status.get("state") != "RUNNING":
            if self._state is not None:
                self.reset()
            return False
        if self._busy or self._clock() < self._next_due:
            return False
        try:
            return await self.analyse(status, cfg)
        finally:
            self._next_due = self._clock() + max(MIN_POLL_S, int(cfg.poll_interval or 10))

    async def analyse(self, status: dict, cfg: ObicoConfig | None = None) -> bool:
        """One capture + ML call + smoothing, unconditionally (tests / manual checks)."""
        cfg = cfg or self.config()
        if self._busy:
            return False
        self._busy = True
        try:
            return await self._analyse(status, cfg)
        except Exception as e:  # defensive: nothing in here may escape into the app loop
            self._no_verdict(f"Obico check failed: {type(e).__name__}")
            log.exception("Obico check failed")
            return False
        finally:
            self._busy = False

    async def _analyse(self, status: dict, cfg: ObicoConfig) -> bool:
        job = str(status.get("job") or "")
        # An empty job name while a print is already tracked is a transient gap in
        # the status (e.g. right after a reconnect), not a new print: a real new
        # print is always preceded by a non-RUNNING state, which resets anyway.
        # Without this a blip would throw away the warm-up and the fired flags.
        if self._state is None or (job and self._job and self._job != job):
            self.reset()
            self._state = PrintState()
            self._job = job
        elif job and not self._job:
            self._job = job  # the name arrived late: same print, keep its state
        state = self._state
        job = self._job or ""

        if not cfg.frame_base_url:
            # Checked before capturing so a misconfiguration does not load the
            # (shared) camera every poll for nothing.
            self._no_verdict(
                "Frame URL is not set - the Obico ML server fetches the snapshot from this app. "
                "Set Settings > Obico > Frame base URL (this app's address as seen from the ML server)."
            )
            return False

        frame, why = await self._capture()
        if self._state is not state:
            return False  # print reset while capturing: this frame belongs to no print
        if not frame:
            self._no_verdict(why or "Failed to capture a camera snapshot")
            return False

        payload, err = await self._detect(frame, cfg)
        if self._state is not state:
            # reset() ran during the (up to 30 s) ML call - e.g. the print ended or
            # the integrator reset us. Feeding the verdict into nothing / firing an
            # event for a print that is gone would be wrong, so drop it.
            return False
        if err:
            self._no_verdict(err)
            return False

        detections = payload.get("detections", []) if isinstance(payload, dict) else []
        if not isinstance(detections, list):
            detections = []
        current_p = score_from_detections(detections)
        score = state.update(current_p)
        verdict = classify(score, cfg.sensitivity)
        self._last_class = verdict
        self._error = None
        self._last_error = None
        if self._logged_error:
            log.info("Obico detection is working again")
            self._logged_error = None

        if verdict != "safe" or detections:
            self._history.appendleft(
                {
                    "task_name": job,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "current_p": round(current_p, 4),
                    "score": round(score, 4),
                    "class": verdict,
                    "detections": len(detections),
                }
            )

        kind = verdict if verdict in ("warning", "failure") else None
        if kind and kind not in self._fired:
            self._fired.add(kind)
            if kind == "failure":
                self._fired.add("warning")  # a failure implies the warning; don't send both later
                log.warning("Obico: failure detected (job=%r score=%.3f) - action=%s", job, score, cfg.action)
            low, high = thresholds(cfg.sensitivity)
            await self._emit(
                ObicoEvent(
                    kind=kind,
                    job=job,
                    score=score,
                    ewm=state.ewm_mean,
                    current_p=current_p,
                    frame_count=state.frame_count,
                    low=low,
                    high=high,
                    sensitivity=cfg.sensitivity,
                    action=cfg.action,
                    frame=frame,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
            )
        return True

    async def _capture(self) -> tuple[bytes | None, str]:
        """Calls the frame source; accepts ``bytes``, ``None`` or ``(bytes|None, error)``."""
        try:
            res = await asyncio.wait_for(self._frame_source(), FRAME_TIMEOUT_S)
        except asyncio.TimeoutError:
            return None, "Camera snapshot timed out"
        except Exception as e:
            return None, f"Camera snapshot failed: {type(e).__name__}"
        err = ""
        if isinstance(res, tuple):
            res, err = (res + (None, ""))[:2]
        if isinstance(res, (bytes, bytearray)) and res:
            return bytes(res), ""
        return None, (f"Camera snapshot failed: {err}" if err else "Failed to capture a camera snapshot")

    async def _detect(self, frame: bytes, cfg: ObicoConfig) -> tuple[dict | None, str]:
        """GET /p/?img=<frame url>; returns (payload, error). The nonce is discarded afterwards."""
        nonce = self.frames.put(frame)
        img_url = cfg.frame_base_url + FRAME_ROUTE.format(nonce=nonce)
        try:
            resp = await self._client().get(
                f"{cfg.ml_url}/p/",
                params={"img": img_url},
                headers=auth_headers(cfg.ml_token),
                timeout=DETECTION_TIMEOUT_S,
            )
        except httpx.TimeoutException:
            return None, "Obico ML API timed out"
        except httpx.HTTPError as e:
            return None, f"Obico ML API unreachable: {type(e).__name__}"
        finally:
            self.frames.discard(nonce)  # fetched or not, never serve it later
        if resp.status_code == 401:
            return None, (
                "Obico ML API rejected the token (401). Set the ML API token to the server's "
                "ML_API_TOKEN, or clear ML_API_TOKEN on the server."
            )
        if resp.status_code != 200:
            # Obico answers 5xx when it could not fetch / decode the image (bad frame URL).
            return None, f"Obico ML API error (HTTP {resp.status_code})"
        try:
            payload = resp.json()
        except ValueError:
            return None, "Obico ML API returned invalid JSON"
        if not isinstance(payload, dict):
            return None, "Obico ML API returned an unexpected payload"
        return payload, ""

    async def _emit(self, event: ObicoEvent) -> None:
        if self._on_event is None:
            return
        try:
            res = self._on_event(event)
            if inspect.isawaitable(res):
                await res
        except Exception as e:
            self._last_error = f"Obico event handler failed: {type(e).__name__}"
            log.exception("Obico event handler failed")

    def _no_verdict(self, reason: str) -> None:
        """No verdict this poll: status shows 'error' instead of a stale / fake 'safe'."""
        self._error = reason
        self._last_error = reason
        if reason != self._logged_error:  # log changes only, not every 10 s
            log.warning("Obico: %s", reason)
            self._logged_error = reason

    # ---- queries

    @property
    def running(self) -> bool:
        """Enabled and actually being driven (own task alive or step() called recently)."""
        return self._is_running(self.config())

    def _is_running(self, cfg: ObicoConfig) -> bool:
        if not cfg.enabled:
            return False
        if self._task is not None and not self._task.done():
            return True
        window = max(60.0, 3.0 * max(MIN_POLL_S, int(cfg.poll_interval or 10)))
        return self._clock() - self._last_step < window

    def _per_printer(self) -> dict | None:
        if self._state is None:
            return None
        verdict = "error" if self._error else (self._last_class or "unknown")
        return {
            "class": verdict,
            "frame_count": self._state.frame_count,
            # The EWM (not the max() score) is the per-printer score.
            "score": round(self._state.ewm_mean, 4),
            "error": self._error,
        }

    def status(self) -> dict:
        """The dict ``Engine.obico`` holds."""
        cfg = self.config()
        low, high = thresholds(cfg.sensitivity)
        p = self._per_printer()
        out = {
            "enabled": cfg.enabled,
            "running": self._is_running(cfg),
            "low": float(low),
            "high": float(high),
            "sensitivity": cfg.sensitivity,
            "action": cfg.action,
            "error": self._last_error or "",
            "has_printer": p is not None,
            "cls": "",
            "score": 0.0,
            "frames": 0,
        }
        if p is not None:
            out["cls"] = p["class"]
            out["score"] = float(p["score"])
            out["frames"] = int(p["frame_count"])
            if p["error"] is not None:
                out["error"] = p["error"]
        return out

    def history(self) -> list[dict]:
        return list(self._history)

    # ---- settings page helper

    async def test_connection(self, url: str | None = None, token: str | None = None) -> dict:
        """Health check + token probe: {ok, status_code, body, error, auth_ok}.

        ``/hc/`` is never gated, so a second request to ``/p/`` without ``img``
        tells whether the token is accepted (401 = rejected, 4xx/422 = accepted)
        without running any inference.
        """
        cfg = self.config()
        base = normalize_url(url if url is not None else cfg.ml_url)
        tok = cfg.ml_token if token is None else token
        if not base.startswith(("http://", "https://")):
            return {"ok": False, "status_code": None, "body": None, "error": "URL must start with http:// or https://", "auth_ok": None}
        headers = auth_headers(tok)
        try:
            resp = await self._client().get(f"{base}/hc/", headers=headers, timeout=HEALTH_TIMEOUT_S)
        except httpx.HTTPError as e:
            return {"ok": False, "status_code": None, "body": None, "error": f"Unreachable: {type(e).__name__}", "auth_ok": None}
        body = resp.text.strip()[:200]
        if resp.status_code != 200 or body.lower() != "ok":
            return {"ok": False, "status_code": resp.status_code, "body": body, "error": None, "auth_ok": None}
        auth_ok: bool | None
        try:
            probe = await self._client().get(f"{base}/p/", headers=headers, timeout=HEALTH_TIMEOUT_S)
            auth_ok = probe.status_code != 401
        except httpx.HTTPError:
            auth_ok = None
        if auth_ok is False:
            return {
                "ok": False,
                "status_code": 401,
                "body": body,
                "error": "The ML API is reachable but rejected the token (ML_API_TOKEN mismatch).",
                "auth_ok": False,
            }
        return {"ok": True, "status_code": resp.status_code, "body": body, "error": None, "auth_ok": auth_ok}

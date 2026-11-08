"""Direct P1S chamber camera client (TLS on TCP 6000), shared by every consumer.

Why this exists: the A1/P1 "chamber image" camera is not RTSP. The printer serves a
private binary protocol on port 6000 (TLS, self-signed cert): the client sends one
80-byte login packet, then the printer pushes frames forever, each a 16-byte header
(payload length as little-endian uint32 in bytes 0-3) followed by one JPEG.

The printer handles very few concurrent camera sockets (a second one either fails or
kicks the first off), so this module owns exactly ONE upstream connection and fans the
latest frame out to every consumer (browser viewers, timelapse recorder, failure
detection). Consumers register demand with leases; the connection runs while any lease
is held and is closed after a short idle grace period, so a page refresh does not pay a
new TLS handshake but an unwatched camera does not keep a socket open on the printer.

Nothing here raises into the caller's loop: snapshot()/open_stream() return
(value, error) tuples (camera.Camera), and the
connection loop records failures in status() instead of crashing.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import ssl
import struct
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("printer_camera")

CAMERA_PORT = 6000
USERNAME = "bblp"
MJPEG_CONTENT_TYPE = "multipart/x-mixed-replace; boundary=frame"

HEADER_SIZE = 16
MAX_FRAME_BYTES = 10_000_000  # sanity cap: a bogus length means the stream is out of sync
JPEG_SOI = b"\xff\xd8"

# Error "kinds" let callers and status() tell a dead network from a rejected login.
KIND_CONFIG = "config"
KIND_CONNECT = "connect"
KIND_AUTH = "auth"
KIND_STREAM = "stream"
KIND_PROTOCOL = "protocol"

StrSource = str | Callable[[], str]


def build_auth_packet(access_code: str) -> bytes:
    """The 80-byte login packet: <u32 0x40><u32 0x3000><8 zero bytes><user[32]><code[32]>.

    Field values come from the reverse-engineered protocol.
    Overlong fields are truncated rather than raising, because struct's "32s" pads/cuts
    silently and a wrong code is reported later as an auth failure anyway.
    """
    return struct.pack(
        "<II8s32s32s",
        0x40,
        0x3000,
        b"\x00" * 8,
        USERNAME.encode(),
        access_code.encode("utf-8"),
    )


def mjpeg_part(jpeg: bytes) -> bytes:
    """One multipart/x-mixed-replace part, in the usual MJPEG stream layout
    (boundary "frame"), so the existing frontend <img> keeps working unchanged."""
    return (
        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
        + str(len(jpeg)).encode()
        + b"\r\n\r\n"
        + jpeg
        + b"\r\n"
    )


def _insecure_tls_context() -> ssl.SSLContext:
    """The printer presents a self-signed cert with no usable hostname: verification
    cannot succeed, so it is disabled (LAN-only connection, as Bambu Studio does)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _resolve(src: StrSource | Callable[[], Any], failing: set[str] | None = None, name: str = "") -> str:
    """Settings are read on every connect so a changed IP/access code applies on the next
    (re)connect without rebuilding the object. A failing provider counts as empty.

    Non-string values are stringified: an 8-digit access code that went through JSON as
    a number must not crash the connection loop (`int.strip()` would). The failure is
    logged once per failing streak - status() polls this every second - and only by
    exception type, so a message that happens to echo a secret never reaches the log.
    The streak is tracked per source (`failing` set + `name`): with one shared flag a
    healthy host provider reset the flag right before a broken code provider set it
    again, so the "once per streak" log fired on every status() poll."""
    try:
        val = src() if callable(src) else src
    except Exception as ex:  # noqa: BLE001 - a broken settings read must not kill the loop
        if failing is None or name not in failing:
            log.warning("camera setting provider %s failed: %s", name or "?", type(ex).__name__)
        if failing is not None:
            failing.add(name)
        return ""
    if failing is not None:
        failing.discard(name)
    if val is None:
        return ""
    return (val if isinstance(val, str) else str(val)).strip()


@dataclass
class _Lease:
    """One consumer's demand. `ttl` > 0 makes it reapable once its owner has been parked
    (not pulling) for that long - protection against MJPEG generators that a web server
    abandons without closing them."""

    reason: str
    ttl: float | None = None
    parked_since: float | None = None
    created: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        # A TTL lease starts "parked": open_stream() hands out a generator that the web
        # server may never start (client gone before the first pull, route error). An
        # unstarted async generator never runs its `finally`, so without this the lease
        # would be held forever and the printer socket would never idle-close.
        if self.ttl and self.parked_since is None:
            self.parked_since = self.created


class _SessionEnd(Exception):
    """Internal: why one connection ended (message is safe to show; never has secrets)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class PrinterCamera:
    """Single shared chamber-camera connection with latest-frame buffer and fan-out.

    Typical use:
        cam = PrinterCamera(host=lambda: settings.get()["printer_ip"],
                            access_code=lambda: settings.secret("printer_access_code"))
        jpg, err = await cam.snapshot()
        it, ctype, err = await cam.open_stream(fps=5)       # for StreamingResponse
        async with cam.demand("timelapse"): ...              # keep it warm during a print
        await cam.aclose()                                   # app shutdown
    """

    def __init__(
        self,
        host: StrSource,
        access_code: StrSource,
        *,
        port: int = CAMERA_PORT,
        idle_grace: float = 10.0,
        connect_timeout: float = 10.0,
        first_frame_timeout: float = 15.0,
        read_timeout: float = 30.0,
        backoff_min: float = 1.0,
        backoff_max: float = 30.0,
        auth_backoff: float = 60.0,
        stream_lease_ttl: float = 60.0,
        imminent_retry: float = 2.0,
        demand_retry_min: float = 5.0,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._host_src = host
        self._code_src = access_code
        self.port = port
        self.idle_grace = idle_grace
        self.connect_timeout = connect_timeout
        self.first_frame_timeout = first_frame_timeout
        self.read_timeout = read_timeout
        self.backoff_min = backoff_min
        self.backoff_max = backoff_max
        self.auth_backoff = auth_backoff
        self.stream_lease_ttl = stream_lease_ttl
        # A consumer arriving during a backoff waits if the next attempt is at most
        # `imminent_retry` s away; otherwise, for errors that can heal by themselves
        # (printer off/rebooting, dropped stream), it cuts the sleep short - but at most
        # once per `demand_retry_min` s so frequent callers cannot hammer the printer.
        self.imminent_retry = imminent_retry
        self.demand_retry_min = demand_retry_min
        self._ssl = ssl_context or _insecure_tls_context()

        self._leases: dict[int, _Lease] = {}
        self._ids = itertools.count(1)
        self._task: asyncio.Task[None] | None = None
        # Runners that were cancelled but may still be closing their socket; a new runner
        # waits for ALL of them (a chain of quick restarts can leave several) so two of
        # our sockets never overlap on the printer.
        self._retiring: set[asyncio.Task[None]] = set()
        self._idle_task: asyncio.Task[None] | None = None
        self._closed = False

        # Latest frame + a sequence number; waiters watch `_changed`, which is swapped
        # (set + replaced) on every frame and every failed attempt. No per-consumer
        # queues: a slow consumer simply skips to the newest frame (live video).
        self._frame: bytes | None = None
        self._frame_at = 0.0  # monotonic
        self._frame_wall = 0.0  # unix time, for status/UI
        self._seq = 0
        self._changed = asyncio.Event()
        self._wake = asyncio.Event()  # cuts a backoff sleep short (restart())
        self._stamps: deque[float] = deque(maxlen=64)

        self._state = "idle"  # idle | connecting | streaming | backoff
        self._connected = False
        self._fail_seq = 0
        self._fail_streak = 0
        self._last_error = ""
        self._last_error_kind = ""
        self._last_error_at = 0.0
        self._auth_suspect = False
        self._connects = 0
        self._frames_total = 0
        self._bad_frames = 0
        self._failing_providers: set[str] = set()
        # Backoff bookkeeping (monotonic): when the current sleep ends and when the last
        # connection attempt started. Used by snapshot()/open_stream() to decide between
        # "wait for the imminent retry", "retry now" and "fail fast".
        self._backoff_until = 0.0
        self._attempt_at = 0.0

    def _settings(self) -> tuple[str, str]:
        """(host, access code) as configured right now; "" for missing/broken."""
        return (
            _resolve(self._host_src, self._failing_providers, "host"),
            _resolve(self._code_src, self._failing_providers, "access_code"),
        )

    # ------------------------------------------------------------------ demand

    def want(self, reason: str = "consumer", *, ttl: float | None = None) -> int:
        """Register demand; returns a lease id for release(). Starts the connection if
        needed and cancels a pending idle close. Must be called from the event loop."""
        if self._closed:
            return 0
        lid = next(self._ids)
        self._leases[lid] = _Lease(reason=reason, ttl=ttl)
        self._cancel_idle()
        self._ensure_running()
        return lid

    def release(self, lease_id: int) -> None:
        """Drop demand (idempotent). The last release arms the idle-close timer."""
        if self._leases.pop(lease_id, None) is None:
            return
        if not self._leases and not self._closed:
            self._arm_idle()

    @contextlib.asynccontextmanager
    async def demand(self, reason: str = "consumer") -> AsyncIterator[PrinterCamera]:
        """`async with cam.demand("timelapse"):` - keeps the camera connected for the block."""
        lid = self.want(reason)
        try:
            yield self
        finally:
            self.release(lid)

    def _reap(self) -> None:
        """Drop TTL leases whose owner sat parked at a `yield` too long (abandoned stream)."""
        now = time.monotonic()
        dead = [
            lid
            for lid, ls in self._leases.items()
            if ls.ttl and ls.parked_since is not None and now - ls.parked_since > ls.ttl
        ]
        for lid in dead:
            log.info("camera: reaping abandoned %s lease", self._leases[lid].reason)
            self.release(lid)

    def _cancel_idle(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    def _arm_idle(self) -> None:
        self._cancel_idle()
        if self._task is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # release() from an async-generator finalizer outside a running loop (GC at
            # interpreter/loop shutdown): nothing to schedule, aclose() owns the socket.
            return
        self._idle_task = loop.create_task(self._idle_close(), name="camera-idle")

    async def _idle_close(self) -> None:
        await asyncio.sleep(self.idle_grace)
        if self._leases:
            return
        log.info("camera: idle for %.0fs, closing connection", self.idle_grace)
        await self._stop_runner()

    # ------------------------------------------------------------------ runner lifecycle

    def _ensure_running(self) -> None:
        if self._task is not None and not self._task.done():
            return
        prev = {t for t in self._retiring if not t.done()}
        self._task = asyncio.get_running_loop().create_task(self._run(prev), name="camera-conn")
        self._task.add_done_callback(self._runner_done)

    def _runner_done(self, task: asyncio.Task[None]) -> None:
        """Surface a runner that died from a bug (instead of an unretrieved-exception
        warning at GC time); the next want()/snapshot() starts a fresh runner."""
        if task.cancelled():
            return
        ex = task.exception()
        if ex is not None:
            log.error("camera: connection loop crashed: %s", type(ex).__name__, exc_info=ex)
            self._last_error, self._last_error_kind = f"Camera error: {type(ex).__name__}", KIND_STREAM
            self._last_error_at = time.time()
            if self._task is task:
                self._task = None
            self._notify()

    def _is_current(self) -> bool:
        """True when the calling runner is the live one (or none is registered). A runner
        that was retired but outlived the 10 s teardown wait must not overwrite the
        state of its successor when it finally exits."""
        cur = asyncio.current_task()
        return self._task is None or self._task is cur

    def _retire_runner(self) -> asyncio.Task[None] | None:
        """Cancel the current runner and remember it until its socket is closed."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            self._retiring.add(task)
            task.add_done_callback(self._retiring.discard)
        return task

    async def _stop_runner(self) -> None:
        """Cancel the runner and wait until its socket is closed.

        asyncio.wait() (not `await task` under suppress(CancelledError)) so that OUR
        cancellation still propagates - swallowing it would turn a cancelled waiter into
        a zombie - and a cancellation of the caller does not hit the runner a second
        time, which would cut its graceful TLS close short."""
        task = self._retire_runner()
        if task is not None and not task.done():
            await asyncio.wait({task})

    def restart(self) -> None:
        """Apply new settings now: drop the current socket and skip any backoff sleep.
        Safe to call any time and repeatedly; the new runner waits for the old socket to
        close. Does nothing (but reset the failure streak) when nobody needs the camera."""
        self._fail_streak = 0
        self._auth_suspect = False
        if self._closed:
            return
        self._retire_runner()
        if self._leases:
            self._ensure_running()

    async def close(self) -> None:
        """Alias of aclose(): the old camera.Camera exposed close(), so Engine.close()
        keeps working when engine.camera is swapped for this class."""
        await self.aclose()

    async def aclose(self) -> None:
        """App shutdown: close the socket, end all streams, refuse new demand."""
        self._closed = True
        self._cancel_idle()
        self._leases.clear()
        await self._stop_runner()
        # An idle close or restart() may have left a runner still closing its socket.
        retiring = {t for t in self._retiring if not t.done()}
        if retiring:
            await asyncio.wait(retiring, timeout=5.0)
        self._notify()  # wake waiters so generators see _closed and end

    # ------------------------------------------------------------------ connection loop

    async def _run(self, prev: set[asyncio.Task[None]]) -> None:
        """Connect, stream, and reconnect with exponential backoff until cancelled.

        `prev` are runners being torn down: we wait for them so the printer never sees two
        of our sockets at once (an overlapping socket strands frames)."""
        prev = {t for t in prev if not t.done()}
        if prev:
            # asyncio.wait, not wait_for(shield(prev)) under suppress(CancelledError): that
            # swallowed OUR cancellation too, so a runner restarted twice quickly kept
            # going as an orphan with its own socket next to the new one.
            await asyncio.wait(prev, timeout=10.0)
        backoff = self.backoff_min
        try:
            while not self._closed:
                try:
                    self._reap()
                except Exception:  # noqa: BLE001 - reaping is best effort
                    log.exception("camera: lease reaper failed")
                host, code = self._settings()
                if not host or not code:
                    what = "printer IP" if not host else "LAN access code"
                    self._record_failure(KIND_CONFIG, f"Camera not configured ({what} missing)")
                    await self._backoff_sleep(self.backoff_max)
                    continue
                self._state = "connecting"
                self._attempt_at = time.monotonic()
                frames_before = self._frames_total
                try:
                    await self._session(host, code)
                    err = _SessionEnd(KIND_STREAM, "Printer closed the camera connection")
                except _SessionEnd as ex:
                    err = ex
                except Exception as ex:  # noqa: BLE001 - a bug here must not end reconnecting
                    log.exception("camera: unexpected session error")
                    err = _SessionEnd(KIND_STREAM, f"Camera error: {type(ex).__name__}")
                finally:
                    if self._is_current():
                        self._connected = False
                got_frames = self._frames_total > frames_before
                if got_frames:
                    backoff = self.backoff_min
                    self._fail_streak = 0
                self._record_failure(err.kind, err.message)
                delay = backoff
                if err.kind == KIND_AUTH and self._fail_streak >= 2:
                    # Repeated rejected logins: slow down hard, hammering the printer with a
                    # wrong code is pointless and floods its log.
                    delay = max(delay, self.auth_backoff)
                await self._backoff_sleep(delay)
                backoff = min(self.backoff_max, max(self.backoff_min, backoff * 2))
        finally:
            if self._is_current():
                self._connected = False
                self._state = "idle"
                self._backoff_until = 0.0

    async def _backoff_sleep(self, delay: float) -> None:
        """Sleep before the next attempt; _wake (set by a new consumer, see
        _fail_fast_reason) cuts it short."""
        self._state = "backoff"
        self._backoff_until = time.monotonic() + delay
        self._wake.clear()
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
        finally:
            self._backoff_until = 0.0

    async def _session(self, host: str, code: str) -> None:
        """One connection: TLS connect, login, then read frames until something breaks.
        Always raises _SessionEnd (or returns on clean EOF between frames)."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, self.port, ssl=self._ssl), timeout=self.connect_timeout
            )
        except TimeoutError:
            raise _SessionEnd(KIND_CONNECT, f"Camera unreachable ({host}:{self.port}): connect timeout") from None
        except ssl.SSLError as ex:
            raise _SessionEnd(KIND_CONNECT, f"Camera TLS handshake failed: {ex.reason or type(ex).__name__}") from None
        except OSError as ex:
            raise _SessionEnd(
                KIND_CONNECT, f"Camera unreachable ({host}:{self.port}): {ex.strerror or type(ex).__name__}"
            ) from None

        self._connects += 1
        frames_here = 0
        try:
            try:
                writer.write(build_auth_packet(code))
                await asyncio.wait_for(writer.drain(), timeout=self.connect_timeout)
            except (OSError, TimeoutError) as ex:
                raise _SessionEnd(KIND_CONNECT, f"Camera login could not be sent: {type(ex).__name__}") from None
            log.info("camera: connected to %s:%d", host, self.port)
            while True:
                timeout = self.read_timeout if frames_here else self.first_frame_timeout
                try:
                    header = await asyncio.wait_for(reader.readexactly(HEADER_SIZE), timeout=timeout)
                    size = struct.unpack_from("<I", header, 0)[0]
                    if size == 0 or size > MAX_FRAME_BYTES:
                        raise _SessionEnd(KIND_PROTOCOL, f"Camera stream out of sync (frame size {size})")
                    payload = await asyncio.wait_for(reader.readexactly(size), timeout=self.read_timeout)
                except asyncio.IncompleteReadError:
                    if frames_here == 0:
                        raise _SessionEnd(
                            KIND_AUTH,
                            "Printer closed the camera connection right after login "
                            "(wrong LAN access code, or camera busy/disabled?)",
                        ) from None
                    raise _SessionEnd(KIND_STREAM, "Printer closed the camera connection") from None
                except TimeoutError:
                    if frames_here == 0:
                        raise _SessionEnd(
                            KIND_AUTH,
                            f"No camera frame within {timeout:.0f}s after login "
                            "(wrong LAN access code, or camera disabled?)",
                        ) from None
                    raise _SessionEnd(KIND_STREAM, f"Camera stalled: no frame for {timeout:.0f}s") from None
                except (ssl.SSLError, OSError) as ex:
                    raise _SessionEnd(KIND_STREAM, f"Camera connection error: {type(ex).__name__}") from None
                if not payload.startswith(JPEG_SOI):
                    self._bad_frames += 1  # keep the socket: framing is still intact
                    continue
                if frames_here == 0:
                    self._connected = True
                    self._state = "streaming"
                    self._auth_suspect = False
                    if self._last_error:
                        log.info("camera: streaming again")
                    self._last_error = self._last_error_kind = ""
                frames_here += 1
                self._on_frame(payload)
        finally:
            # Graceful TLS close, but never wait on a dead peer: abort after 2 s.
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
            except asyncio.CancelledError:
                writer.transport.abort()
                raise
            except Exception:  # noqa: BLE001 - timeout / reset during close: just drop it
                writer.transport.abort()

    def _on_frame(self, jpeg: bytes) -> None:
        now = time.monotonic()
        self._frame, self._frame_at, self._frame_wall = jpeg, now, time.time()
        self._seq += 1
        self._frames_total += 1
        self._stamps.append(now)
        self._notify()
        # Reap abandoned stream leases here (once per frame) rather than in the session
        # loop, so every frame source - and every test fake - drives the reaper.
        try:
            self._reap()
        except Exception:  # noqa: BLE001 - reaping is best effort
            log.exception("camera: lease reaper failed")

    def _record_failure(self, kind: str, message: str) -> None:
        if message != self._last_error:
            # Log on change only: a camera that is off would otherwise log every backoff.
            log.warning("camera: %s", message)
        self._last_error, self._last_error_kind, self._last_error_at = message, kind, time.time()
        self._auth_suspect = kind == KIND_AUTH
        self._fail_streak += 1
        self._fail_seq += 1
        self._notify()

    def _notify(self) -> None:
        ev, self._changed = self._changed, asyncio.Event()
        ev.set()

    # ------------------------------------------------------------------ consumers

    async def wait_frame(self, after_seq: int, timeout: float, *, fail_fast: bool = False) -> bool:
        """Wait until a frame newer than `after_seq` exists. With fail_fast, also give up
        as soon as a connection attempt fails (so a snapshot does not sit out a backoff)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        fails0 = self._fail_seq
        while self._seq <= after_seq:
            if self._closed or (fail_fast and self._fail_seq != fails0):
                return False
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=remaining)
            except TimeoutError:
                return False
        return True

    def _fail_fast_reason(self) -> str | None:
        """Decide what a new consumer does while the runner sleeps in a backoff.

        Returns the error to report immediately, or None to go ahead and wait for a
        frame. Waiting is right when the next attempt is imminent (e.g. 1 s after the
        printer dropped a healthy stream - failing then made a timelapse frame miss on
        every routine reconnect). For errors that can heal by themselves (printer was
        off, stream dropped) a new consumer cuts a long backoff short, rate-limited by
        `demand_retry_min`, so a viewer opening the page right after the printer powered
        on does not stare at "unreachable" for up to backoff_max. Config and auth errors
        fail fast: they only change via settings + restart()."""
        task = self._task
        if task is None or task.done() or self._state != "backoff":
            return None
        now = time.monotonic()
        if self._backoff_until - now <= self.imminent_retry:
            return None
        if (
            self._last_error_kind in (KIND_CONNECT, KIND_STREAM, KIND_PROTOCOL)
            and now - self._attempt_at >= self.demand_retry_min
        ):
            self._wake.set()
            return None
        return self._last_error or "Camera not connected"

    def latest(self) -> tuple[bytes | None, float | None]:
        """(latest JPEG, its age in seconds) without creating demand."""
        if self._frame is None:
            return None, None
        return self._frame, time.monotonic() - self._frame_at

    async def snapshot(self, max_age: float = 3.0, timeout: float | None = None) -> tuple[bytes | None, str]:
        """Latest JPEG not older than `max_age`; connects on demand when needed.
        Returns (jpeg, "") or (None, error). The idle grace keeps the socket open for a
        while, so periodic snapshots (timelapse) do not reconnect every time."""
        frame, age = self.latest()
        if frame is not None and age is not None and age <= max_age:
            return frame, ""
        if self._closed:
            return None, "Camera client closed"
        if (reason := self._fail_fast_reason()) is not None:
            return None, reason
        lid = self.want("snapshot")
        try:
            wait = timeout if timeout is not None else self.connect_timeout + self.first_frame_timeout
            ok = await self.wait_frame(self._seq, wait, fail_fast=True)
        finally:
            self.release(lid)
        if ok and self._frame is not None:
            return self._frame, ""
        return None, self._last_error or "No camera frame received in time"

    async def open_stream(
        self, fps: float = 5.0, *, reason: str = "viewer", max_stall: float | None = 30.0
    ) -> tuple[AsyncIterator[bytes] | None, str, str]:
        """MJPEG for a StreamingResponse: (chunk iterator, content type, error).

        Same contract as camera.Camera.open_stream, so the route
        can return 503 with a readable reason instead of an endless blank stream."""
        if self._closed:
            return None, "", "Camera client closed"
        frame, age = self.latest()
        fresh = frame is not None and age is not None and age <= max(5.0, 3.0 / max(fps, 0.1))
        if not fresh and (why := self._fail_fast_reason()) is not None:
            return None, "", why
        lid = self.want(reason, ttl=self.stream_lease_ttl)
        try:
            if not fresh:
                ok = await self.wait_frame(self._seq, self.connect_timeout + self.first_frame_timeout, fail_fast=True)
                if not ok:
                    self.release(lid)
                    return None, "", self._last_error or "No camera frame received in time"
        except BaseException:
            # The HTTP request was cancelled (client gone) while we waited for the first
            # frame: give the lease back now instead of leaving it to the TTL reaper.
            self.release(lid)
            raise
        return self.mjpeg(fps, max_stall=max_stall, lease_id=lid), MJPEG_CONTENT_TYPE, ""

    async def mjpeg(
        self,
        fps: float = 5.0,
        *,
        reason: str = "viewer",
        max_stall: float | None = 30.0,
        lease_id: int | None = None,
    ) -> AsyncIterator[bytes]:
        """Async generator of multipart parts (boundary "frame").

        Demand is taken on the first iteration (or `lease_id` is adopted, as open_stream
        does) and released when the generator ends, is closed or cancelled - i.e. when
        the HTTP client disconnects. If the web server abandons the generator without
        closing it, the lease TTL reaper releases it. Rate-limits to `fps` by skipping to
        the newest frame; ends after `max_stall` s without frames so the browser retries."""
        lid = lease_id if lease_id is not None else self.want(reason, ttl=self.stream_lease_ttl)
        interval = 1.0 / fps if fps > 0 else 0.2
        seq = 0
        next_at = 0.0
        try:
            # Start with the buffered frame (if reasonably fresh) for an instant picture.
            frame, age = self.latest()
            if frame is not None and age is not None and age < 5.0:
                seq = self._seq
                if not self._park(lid):
                    return
                yield mjpeg_part(frame)
                next_at = time.monotonic() + interval
            while not self._closed:
                lease = self._leases.get(lid)
                if lease is None:
                    return  # reaped or camera closed
                lease.parked_since = None
                delay = next_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                ok = await self.wait_frame(seq, max_stall if max_stall else 3600.0)
                if not ok:
                    if self._closed or max_stall:
                        return
                    continue
                seq, frame = self._seq, self._frame
                if frame is None:
                    continue
                if not self._park(lid):
                    return
                yield mjpeg_part(frame)
                next_at = time.monotonic() + interval
        finally:
            self.release(lid)

    def _park(self, lid: int) -> bool:
        """Mark the lease as parked at a yield (reapable after its TTL) right before
        handing a chunk to the web server; False if the lease is already gone."""
        lease = self._leases.get(lid)
        if lease is None:
            return False
        lease.parked_since = time.monotonic()
        return True

    # ------------------------------------------------------------------ status

    def fps(self) -> float:
        """Measured frame rate over the last ~5 s (0 when stalled)."""
        now = time.monotonic()
        recent = [t for t in self._stamps if now - t <= 5.0]
        if len(recent) < 2 or now - recent[-1] > 5.0:
            return 0.0
        span = recent[-1] - recent[0]
        return round((len(recent) - 1) / span, 2) if span > 0 else 0.0

    def status(self) -> dict[str, Any]:
        """Snapshot of the client state for the UI / diagnostics (no secrets)."""
        demand: dict[str, int] = {}
        for ls in self._leases.values():
            demand[ls.reason] = demand.get(ls.reason, 0) + 1
        _, age = self.latest()
        return {
            "configured": all(self._settings()),
            "state": self._state,
            "connected": self._connected,
            "fps": self.fps() if self._connected else 0.0,
            "last_frame_age": round(age, 2) if age is not None else None,
            "last_frame_at": self._frame_wall or None,
            "last_error": self._last_error,
            "last_error_kind": self._last_error_kind,
            "last_error_at": self._last_error_at or None,
            "auth_suspect": self._auth_suspect,
            "demand": demand,
            "connects": self._connects,
            "frames": self._frames_total,
            "bad_frames": self._bad_frames,
        }

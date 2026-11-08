"""Direct implicit-FTPS access to the P1S SD card.

Why this module looks the way it does:

- The printer runs vsFTPd on port 990 with *implicit* TLS (the socket is TLS from
  the first byte, there is no AUTH TLS). User is ``bblp``, password is the LAN access
  code, the certificate is self-signed so it is not verified.
- vsFTPd insists that the TLS data channel resumes the control channel's TLS session
  ("session reuse required"), which stock ``ftplib.FTP_TLS`` does not do. We pin TLS
  1.2 by default because resumption is synchronous there (Bambu printers only speak
  1.2 on :990 anyway).
- The printer serves ONE transfer at a time, slowly (~100 KB/s, worse while printing)
  and answers overlapping connections with 425/421 or silence. So every operation in
  this process goes through one global priority queue: interactive requests (user
  opened a file, pressed delete) jump ahead of background ones (cache warm-up,
  timelapse sync), and the queue slot is only released once the worker *thread* has
  really finished - an executor thread cannot be cancelled, and releasing early would
  put two transfers on the printer.
- vsFTPd often ends a finished transfer badly: the data channel closes without a TLS
  close_notify, or the final "226" on the control channel never arrives (SSL EOF) /
  arrives as "426". The bytes are still complete. We therefore judge a transfer by
  size (SIZE before RETR, SIZE after STOR) instead of by the final reply.
- Nothing here raises into the caller's loop: every public coroutine returns an
  ``FtpResult`` (``ok``/``data``/``message``/``kind``), like ``printer_mqtt.SendResult``.
  Only ``asyncio.CancelledError`` propagates, as asyncio requires.

The access code is never logged and never put into a message.
"""

from __future__ import annotations

import asyncio
import ftplib  # nosec B402 - FTPS to the printer on the LAN, there is no alternative protocol
import heapq
import itertools
import logging
import os
import random
import socket
import ssl
import threading
import time
import weakref
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Generic, TypeVar

log = logging.getLogger("printer_ftp")

T = TypeVar("T")

FTP_PORT = 990
FTP_USER = "bblp"

PRIORITY_INTERACTIVE = 0
PRIORITY_BACKGROUND = 10

DEFAULT_SOCKET_TIMEOUT = 30.0
CHUNK_SIZE = 64 * 1024

# Whole-transfer deadlines follow the file size at a deliberately pessimistic rate:
# a slow-but-alive link is caught by the socket (inactivity) timeout instead, so the
# deadline is only a backstop and must never kill a healthy big transfer.
FLOOR_BYTES_PER_SEC = 25 * 1024
MIN_OP_DEADLINE = 120.0

# STOR's final reply can take a long time while the printer flushes to the SD card.
STOR_REPLY_TIMEOUT = 60.0
# Closing a TLS data channel politely must not cost a full socket timeout.
DATA_CLOSE_TIMEOUT = 5.0
# After a TLS handshake failure background work stays away from the printer for a
# while (printers were seen answering :990 in cleartext for long stretches; hammering
# them every few seconds does not help). User-initiated work ignores this.
TLS_COOLOFF_S = 120.0
# Progress callbacks are hopped onto the event loop; more than a few per second is noise.
PROGRESS_MIN_INTERVAL = 0.25

# Reply codes that mean "printer is busy / data channel could not be opened" -> retry.
_BUSY_CODES = {"421", "425", "426", "450", "451"}
_STORAGE_CODES = {"452", "552", "553"}


# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class FtpResult(Generic[T]):
    """Outcome of one public operation. ``kind`` is machine-readable, ``message`` is for humans.

    kinds: not_found, auth, config, invalid, timeout, busy, storage, tls, data_tls,
    network, size_mismatch, cancelled, local, cooloff, error.
    """

    ok: bool
    data: T | None = None
    message: str = ""
    kind: str = ""
    code: str | None = None
    attempts: int = 1


class FtpOpError(Exception):
    """Internal failure carrier from the worker thread; turned into an FtpResult."""

    def __init__(self, kind: str, message: str, *, code: str | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.code = code
        self.retryable = retryable


def _reply_code(exc: BaseException) -> str | None:
    head = str(exc)[:3]
    return head if head.isdigit() else None


def _short(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text.splitlines()[0][:200]


def classify_error(exc: BaseException, *, phase: str, path: str = "") -> FtpOpError:
    """Map an ftplib/ssl/socket exception to a kind, a clear message and retryability.

    ``phase`` is "connect" (TCP+TLS+login), "data" (opening the data channel) or "op".
    The phase matters because an SSL error during connect is a handshake problem
    (cool-off worthy), while one on the data channel is the session-reuse / PROT quirk.
    """
    if isinstance(exc, FtpOpError):
        return exc
    where = f": {path}" if path else ""
    code = _reply_code(exc)
    if isinstance(exc, ftplib.error_perm):
        if code == "550":
            return FtpOpError("not_found", f"550 not on printer{where}", code=code)
        if code in ("530", "331", "332"):
            return FtpOpError("auth", f"{code} login rejected - check the printer's LAN access code", code=code)
        if code in _STORAGE_CODES:
            return FtpOpError("storage", f"{code} printer refused to store the file (SD card missing/full?){where}", code=code)
        return FtpOpError("error", f"printer refused: {_short(exc)}{where}", code=code)
    if isinstance(exc, ftplib.error_temp):
        if code in _STORAGE_CODES:
            return FtpOpError("storage", f"{code} printer out of storage{where}", code=code)
        if code in _BUSY_CODES:
            return FtpOpError("busy", f"printer busy ({_short(exc)}){where}", code=code, retryable=True)
        return FtpOpError("error", f"temporary error: {_short(exc)}{where}", code=code, retryable=True)
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return FtpOpError("timeout", f"timeout during {phase}{where}", retryable=True)
    if isinstance(exc, ssl.SSLError):
        if phase == "connect":
            return FtpOpError("tls", f"TLS handshake failed ({getattr(exc, 'reason', None) or _short(exc)})", retryable=True)
        if phase == "data":
            return FtpOpError("data_tls", f"TLS on data channel failed ({getattr(exc, 'reason', None) or _short(exc)})", retryable=True)
        return FtpOpError("network", f"TLS connection lost ({_short(exc)}){where}", retryable=True)
    if isinstance(exc, ConnectionRefusedError):
        return FtpOpError("network", "connection refused - printer offline or FTP disabled", retryable=True)
    if isinstance(exc, EOFError):
        return FtpOpError("network", f"printer closed the connection during {phase}{where}", retryable=True)
    if isinstance(exc, ftplib.Error):  # error_reply / error_proto: desynced or odd server
        return FtpOpError("network", f"unexpected FTP reply: {_short(exc)}{where}", code=code, retryable=True)
    if isinstance(exc, OSError):
        return FtpOpError("network", f"network error during {phase}: {_short(exc)}", retryable=True)
    return FtpOpError("error", f"{type(exc).__name__}: {_short(exc)}")


# --------------------------------------------------------------------------- the queue


class TransferQueue:
    """One-holder lock with priority ordering (lower number first, FIFO within a priority).

    A plain asyncio.Lock is FIFO only; a user waiting for a preview must not sit behind
    twenty background cache downloads. Cancelled waiters are skipped lazily.
    """

    def __init__(self) -> None:
        self._busy = False
        self._heap: list[tuple[int, int, asyncio.Future[None], str]] = []
        self._seq = itertools.count()
        self.current: str | None = None

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def waiting(self) -> int:
        return sum(1 for _, _, f, _ in self._heap if not f.done())

    def status(self) -> dict[str, Any]:
        return {"busy": self._busy, "current": self.current, "waiting": self.waiting}

    async def acquire(self, priority: int, label: str = "") -> None:
        if not self._busy and self.waiting == 0:
            self._busy = True
            self.current = label
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (priority, next(self._seq), fut, label))
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                # Slot was handed to us in the same tick we got cancelled: pass it on.
                self.release()
            raise

    def release(self) -> None:
        while self._heap:
            _, _, fut, label = heapq.heappop(self._heap)
            if not fut.done():
                self.current = label
                fut.set_result(None)  # ownership moves directly, _busy stays True
                return
        self._busy = False
        self.current = None


_queues: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, TransferQueue] = weakref.WeakKeyDictionary()


def get_transfer_queue() -> TransferQueue:
    """The process-wide FTP queue (per event loop, so tests on fresh loops stay isolated)."""
    loop = asyncio.get_running_loop()
    q = _queues.get(loop)
    if q is None:
        q = TransferQueue()
        _queues[loop] = q
    return q


_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    """Dedicated tiny pool: FTP threads can block for minutes and must not starve to_thread users."""
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="printer-ftp")
        return _executor


# --------------------------------------------------------------------------- ftplib glue


class ImplicitFTPS(ftplib.FTP_TLS):
    """FTP_TLS for implicit TLS (:990) that resumes the control TLS session on data sockets."""

    def __init__(self, *, context: ssl.SSLContext, timeout: float, protect_data: bool = True) -> None:
        super().__init__(context=context, timeout=timeout)
        self.protect_data = protect_data

    def connect(self, host: str = "", port: int = 0, timeout: float = -999, source_address: Any = None) -> str:  # type: ignore[override]
        if host:
            self.host = host
        if port > 0:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        raw = socket.create_connection((self.host, self.port), self.timeout)
        try:
            self.sock = self.context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise
        self.af = self.sock.family
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd: str, rest: int | str | None = None) -> tuple[socket.socket, int | None]:
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            try:
                conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
            except BaseException:
                conn.close()
                raise
        return conn, size


def make_ssl_context(cap_tls12: bool = True) -> ssl.SSLContext:
    """No verification (self-signed printer cert), TLS 1.2 floor, optionally pinned to 1.2.

    A bare PROTOCOL_TLS_CLIENT context instead of create_default_context(): the latter
    loads the whole system CA store for every session, which is pure waste with
    CERT_NONE on a small server that opens one session per file.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if cap_tls12:
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _close_data(conn: socket.socket, *, polite: bool) -> None:
    """Close a data socket; ``polite`` sends close_notify first (bounded, errors ignored).

    RETR: ftplib unwraps too, and vsFTPd answers it. STOR: a plain
    close is what works across printers, and SIZE verification covers the rest.
    """
    if polite and isinstance(conn, ssl.SSLSocket):
        try:
            conn.settimeout(DATA_CLOSE_TIMEOUT)
            conn.unwrap()
        except (OSError, ValueError):
            pass
    try:
        conn.close()
    except OSError:
        pass


def _close_session(ftp: ImplicitFTPS | None, *, clean: bool) -> None:
    if ftp is None:
        return
    if clean:
        try:
            ftp.quit()
            return
        except (OSError, EOFError, ftplib.Error):
            pass
    try:
        ftp.close()
    except OSError:
        pass


def parse_list_line(line: str, now: datetime | None = None) -> dict[str, Any] | None:
    """Parse one Unix ``LIST`` line (vsFTPd format) into {name, size, mtime, is_dir}.

    mtime is a naive ISO string in the printer's clock (vsFTPd reports UTC by default);
    "recent" entries carry no year, so the year is inferred like ``ls`` does.
    """
    line = line.rstrip("\r\n")
    if not line or line.startswith("total "):
        return None
    parts = line.split(None, 8)
    if len(parts) < 9 or len(parts[0]) < 10:
        return None
    perms, name = parts[0], parts[8]
    kind = perms[0]
    if kind == "l" and " -> " in name:
        name = name.split(" -> ", 1)[0]
    if name in (".", ".."):
        return None
    is_dir = kind == "d"
    try:
        size = int(parts[4])
    except ValueError:
        size = 0
    mtime: str | None = None
    now = now or datetime.now()
    month, day, tail = parts[5], parts[6], parts[7]
    try:
        if ":" in tail:
            dt = datetime.strptime(f"{month} {day} {now.year} {tail}", "%b %d %Y %H:%M")
            if dt > now + timedelta(days=1):
                dt = datetime.strptime(f"{month} {day} {now.year - 1} {tail}", "%b %d %Y %H:%M")
        else:
            dt = datetime.strptime(f"{month} {day} {tail}", "%b %d %Y")
        mtime = dt.isoformat()
    except ValueError:
        mtime = None
    # "is_directory" is the listing shape sdcard.py and
    # compat.py already consume, so a listing from here is a drop-in replacement.
    return {"name": name, "size": 0 if is_dir else size, "mtime": mtime, "is_dir": is_dir, "is_directory": is_dir}


def _check_remote(path: str) -> str:
    """Reject paths that could smuggle extra FTP commands onto the control channel."""
    if not path or any(c in path for c in "\r\n\0"):
        raise FtpOpError("invalid", "invalid remote path")
    return path


# --------------------------------------------------------------------------- worker context


_progress_tasks: set[asyncio.Future[Any]] = set()


def _progress_task_done(task: asyncio.Future[Any]) -> None:
    _progress_tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.debug("async progress callback failed: %s", task.exception())


@dataclass
class _Ctx:
    """What the worker thread shares with the event loop for one attempt."""

    loop: asyncio.AbstractEventLoop
    cancel: threading.Event = field(default_factory=threading.Event)
    deadline: float = 0.0
    activity: float = field(default_factory=time.monotonic)
    progress_cb: Callable[[int, int | None], Any] | None = None
    _last_progress: float = 0.0

    def touch(self) -> None:
        """Mark liveness before a blocking phase (connect, login, final reply, SIZE).

        The watchdog in ``_watch`` judges a thread by its last activity. Without this,
        only data chunks count, and a healthy upload that sends its last chunk and then
        waits 60 s for 226, reconnects and asks SIZE could be declared stuck - the slot
        would be released while the thread still holds a printer session.
        """
        self.activity = time.monotonic()

    def check(self) -> None:
        """Called per chunk: the only way to stop a blocking ftplib transfer from outside."""
        self.activity = time.monotonic()
        if self.cancel.is_set():
            raise FtpOpError("cancelled", "cancelled")
        if self.deadline and self.activity > self.deadline:
            raise FtpOpError("timeout", "transfer deadline exceeded (link too slow)", retryable=False)

    def extend_for_size(self, size: int | None, base: float) -> None:
        if size and size > 0:
            self.deadline = max(self.deadline, time.monotonic() + max(base, size / FLOOR_BYTES_PER_SEC))

    def progress(self, done: int, total: int | None, *, final: bool = False) -> None:
        cb = self.progress_cb
        if cb is None:
            return
        now = time.monotonic()
        if not final and now - self._last_progress < PROGRESS_MIN_INTERVAL:
            return
        self._last_progress = now

        def _call() -> None:
            try:
                res = cb(done, total)
                if asyncio.iscoroutine(res):
                    # The loop only keeps weak references to tasks: hold one until done,
                    # or an async callback can be garbage-collected half way through.
                    task = asyncio.ensure_future(res)
                    _progress_tasks.add(task)
                    task.add_done_callback(_progress_task_done)
            except Exception as e:  # a UI callback must never break a transfer
                log.debug("progress callback failed: %s", e)

        try:
            self.loop.call_soon_threadsafe(_call)
        except RuntimeError:
            pass  # loop closed


# --------------------------------------------------------------------------- client


class PrinterFTP:
    """Async client for the printer's SD card over implicit FTPS.

    ``access_code`` may be a callable so a code changed in settings applies without
    rebuilding the client (e.g. ``lambda: settings.secret("printer_access_code")``).
    """

    def __init__(
        self,
        host: str,
        access_code: str | Callable[[], str],
        *,
        port: int = FTP_PORT,
        user: str = FTP_USER,
        socket_timeout: float = DEFAULT_SOCKET_TIMEOUT,
        retries: int = 2,
        backoff: float = 2.0,
        backoff_max: float = 20.0,
        cap_tls12: bool = True,
        protect_data: bool = True,
        allow_prot_c_fallback: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self._access_code = access_code
        self.socket_timeout = socket_timeout
        self.retries = max(0, retries)
        self.backoff = backoff
        self.backoff_max = backoff_max
        self.cap_tls12 = cap_tls12
        # P1S needs PROT P. PROT C fallback (used for the A1 data-channel
        # trouble) is opt-in: on a P1S it would only turn one error into another.
        self.protect_data = protect_data
        self.allow_prot_c_fallback = allow_prot_c_fallback
        self._tls_cooloff_until = 0.0
        self.last_error: str = ""

    def __repr__(self) -> str:  # never show the access code
        return f"PrinterFTP({self.host}:{self.port})"

    # ---- public API

    def status(self) -> dict[str, Any]:
        """Queue + last error for a UI badge. Must be called from the event loop."""
        st = get_transfer_queue().status()
        st["last_error"] = self.last_error
        st["cooloff_s"] = max(0, round(self._tls_cooloff_until - time.monotonic()))
        return st

    async def list_dir(self, path: str = "/", *, priority: int = PRIORITY_INTERACTIVE) -> FtpResult[list[dict[str, Any]]]:
        """[{name, size, mtime (ISO or None), is_dir}] of one directory."""

        def work(ctx: _Ctx, protect: bool) -> list[dict[str, Any]]:
            _check_remote(path)
            ctx.deadline = time.monotonic() + max(MIN_OP_DEADLINE, self.socket_timeout * 3)
            ftp = self._open(protect, ctx)
            ok = False
            try:
                entries = self._list(ftp, path, ctx)
                ok = True
                return entries
            finally:
                _close_session(ftp, clean=ok)

        return await self._run(f"list {path}", work, priority)

    async def size(self, remote: str, *, priority: int = PRIORITY_INTERACTIVE) -> FtpResult[int]:
        """Size in bytes of a remote file (SIZE command)."""

        def work(ctx: _Ctx, protect: bool) -> int:
            _check_remote(remote)
            ctx.deadline = time.monotonic() + max(MIN_OP_DEADLINE, self.socket_timeout * 3)
            ftp = self._open(protect, ctx)
            ok = False
            try:
                n = self._size(ftp, remote)
                if n is None:
                    raise FtpOpError("error", f"printer did not report a size: {remote}")
                ok = True
                return n
            finally:
                _close_session(ftp, clean=ok)

        return await self._run(f"size {remote}", work, priority)

    async def download(
        self,
        remote: str,
        local_path: str | os.PathLike[str],
        progress_cb: Callable[[int, int | None], Any] | None = None,
        *,
        expected_size: int | None = None,
        priority: int = PRIORITY_INTERACTIVE,
    ) -> FtpResult[int]:
        """Fetch ``remote`` into ``local_path`` (via a .part file, replaced only when complete).

        Success means the byte count equals the printer's SIZE (or ``expected_size`` when
        SIZE is unavailable). A transfer whose final reply is lost to an SSL EOF / 426
        still counts when the sizes match - that is how vsFTPd on Bambu ends many RETRs.
        ``progress_cb(done, total)`` runs on the event loop (sync or async).
        """
        local = Path(local_path)

        def work(ctx: _Ctx, protect: bool) -> int:
            _check_remote(remote)
            return self._download(ctx, protect, remote, local, expected_size)

        return await self._run(f"download {remote}", work, priority, progress_cb=progress_cb)

    async def upload(
        self,
        local_path: str | os.PathLike[str],
        remote: str,
        progress_cb: Callable[[int, int | None], Any] | None = None,
        *,
        priority: int = PRIORITY_INTERACTIVE,
    ) -> FtpResult[int]:
        """Store a local file on the printer, then verify the remote SIZE matches.

        A size mismatch removes the partial remote file (best effort) so a print can
        never be started from a truncated 3MF.
        """
        local = Path(local_path)

        def work(ctx: _Ctx, protect: bool) -> int:
            _check_remote(remote)
            return self._upload(ctx, protect, local, remote)

        return await self._run(f"upload {remote}", work, priority, progress_cb=progress_cb)

    async def delete(self, remote: str, *, missing_ok: bool = False, priority: int = PRIORITY_INTERACTIVE) -> FtpResult[bool]:
        """Delete a remote file. data=True if deleted, False if it was already gone (missing_ok)."""

        def work(ctx: _Ctx, protect: bool) -> bool:
            _check_remote(remote)
            ctx.deadline = time.monotonic() + max(MIN_OP_DEADLINE, self.socket_timeout * 3)
            ftp = self._open(protect, ctx)
            ok = False
            try:
                try:
                    ftp.delete(remote)
                except ftplib.error_perm as e:
                    if _reply_code(e) == "550" and missing_ok:
                        ok = True
                        return False
                    raise classify_error(e, phase="op", path=remote) from None
                except (OSError, EOFError, ftplib.Error) as e:
                    raise classify_error(e, phase="op", path=remote) from None
                ok = True
                return True
            finally:
                _close_session(ftp, clean=ok)

        return await self._run(f"delete {remote}", work, priority)

    # ---- orchestration (event loop side)

    async def _run(
        self,
        label: str,
        work: Callable[[_Ctx, bool], T],
        priority: int,
        progress_cb: Callable[[int, int | None], Any] | None = None,
    ) -> FtpResult[T]:
        """Retry loop. The queue slot is released between attempts so interactive work can cut in."""
        attempts = self.retries + 1
        protect = self.protect_data
        res: FtpResult[T] = FtpResult(False, message="not attempted", kind="error")
        for attempt in range(1, attempts + 1):
            if priority >= PRIORITY_BACKGROUND and time.monotonic() < self._tls_cooloff_until:
                left = self._tls_cooloff_until - time.monotonic()
                return FtpResult(False, message=f"FTP paused for {left:.0f}s after a TLS handshake failure", kind="cooloff", attempts=attempt - 1)
            res, retryable = await self._attempt(label, work, priority, protect, progress_cb)
            res = FtpResult(res.ok, res.data, res.message, res.kind, res.code, attempt)
            if res.ok:
                if protect != self.protect_data:
                    log.info("FTP data channel works with PROT C on %s; keeping it", self.host)
                    self.protect_data = protect
                self.last_error = ""
                return res
            self.last_error = res.message
            if res.kind == "tls":
                self._tls_cooloff_until = time.monotonic() + TLS_COOLOFF_S
                if priority >= PRIORITY_BACKGROUND:
                    # The next attempt would only hit the cool-off we just opened and
                    # report "cooloff", hiding the real handshake error from the caller.
                    break
            if res.kind == "data_tls" and protect and self.allow_prot_c_fallback:
                log.info("FTP %s: TLS data channel failed, trying PROT C", label)
                protect = False
            if not retryable or attempt == attempts:
                break
            delay = min(self.backoff_max, self.backoff * (2 ** (attempt - 1))) * random.uniform(0.8, 1.2)
            log.info("FTP %s failed (%s), retry %d/%d in %.1fs", label, res.message, attempt, attempts - 1, delay)
            await asyncio.sleep(delay)
        log.warning("FTP %s failed: %s", label, res.message)
        return res

    async def _attempt(
        self,
        label: str,
        work: Callable[[_Ctx, bool], T],
        priority: int,
        protect: bool,
        progress_cb: Callable[[int, int | None], Any] | None,
    ) -> tuple[FtpResult[T], bool]:
        loop = asyncio.get_running_loop()
        queue = get_transfer_queue()
        await queue.acquire(priority, label)
        ctx = _Ctx(loop=loop, progress_cb=progress_cb)
        released = False

        def release(_: Any = None) -> None:
            nonlocal released
            if not released:
                released = True
                queue.release()

        try:
            fut = loop.run_in_executor(_get_executor(), self._guarded, work, ctx, protect)
        except BaseException:
            release()
            raise
        # Release only when the thread is really done, never when the awaiting task is.
        fut.add_done_callback(release)
        try:
            return await self._watch(label, fut, ctx, release)
        except asyncio.CancelledError:
            ctx.cancel.set()  # the worker stops at its next chunk and cleans up
            raise

    async def _watch(
        self,
        label: str,
        fut: asyncio.Future[tuple[FtpResult[T], bool]],
        ctx: _Ctx,
        release: Callable[[], None],
    ) -> tuple[FtpResult[T], bool]:
        """Wait for the worker; a thread with no activity for far too long is abandoned.

        Socket timeouts bound every blocking call, so this should never fire; it exists
        so one wedged thread cannot hold the printer queue forever.
        """
        hang_after = self.socket_timeout * 3 + STOR_REPLY_TIMEOUT
        while True:
            done, _ = await asyncio.wait({fut}, timeout=min(5.0, hang_after))
            if done:
                return fut.result()
            if time.monotonic() - ctx.activity > hang_after:
                ctx.cancel.set()
                log.error("FTP %s: worker thread stuck for %.0fs, abandoning it", label, hang_after)
                release()
                return FtpResult(False, message=f"{label}: printer stopped responding", kind="timeout"), False

    def _guarded(self, work: Callable[[_Ctx, bool], T], ctx: _Ctx, protect: bool) -> tuple[FtpResult[T], bool]:
        """Thread entry: never raises, converts every failure to (FtpResult, retryable)."""
        try:
            return FtpResult(True, work(ctx, protect)), False
        except FtpOpError as e:
            return FtpResult(False, message=e.message, kind=e.kind, code=e.code), e.retryable
        except Exception as e:  # pragma: no cover - defensive
            err = classify_error(e, phase="op")
            log.exception("FTP worker crashed")
            return FtpResult(False, message=err.message, kind=err.kind, code=err.code), err.retryable

    # ---- blocking work (worker thread only)

    def _code(self) -> str:
        try:
            code = self._access_code() if callable(self._access_code) else self._access_code
        except Exception:
            code = ""
        return (code or "").strip()

    def _open(self, protect: bool, ctx: _Ctx | None = None) -> ImplicitFTPS:
        code = self._code()
        if not code:
            raise FtpOpError("config", "printer LAN access code is not configured")
        if ctx is not None:
            ctx.touch()
        ftp = ImplicitFTPS(context=make_ssl_context(self.cap_tls12), timeout=self.socket_timeout, protect_data=protect)
        try:
            ftp.connect(self.host, self.port)
            if ctx is not None:
                ctx.touch()
            ftp.login(self.user, code)
            if ctx is not None:
                ctx.touch()
            if protect:
                ftp.prot_p()
            else:
                ftp.prot_c()
            ftp.voidcmd("TYPE I")  # SIZE and byte counts are only meaningful in binary mode
        except BaseException as e:
            _close_session(ftp, clean=False)
            if isinstance(e, (OSError, EOFError, ftplib.Error)):
                raise classify_error(e, phase="connect") from None
            raise
        self._tls_cooloff_until = 0.0
        return ftp

    def _size(self, ftp: ImplicitFTPS, remote: str) -> int | None:
        """SIZE, or None if unsupported. 550 propagates as not_found (file is not there)."""
        try:
            n = ftp.size(remote)
        except ftplib.error_perm as e:
            if _reply_code(e) == "550":
                raise classify_error(e, phase="op", path=remote) from None
            return None
        except (OSError, EOFError, ftplib.Error) as e:
            raise classify_error(e, phase="op", path=remote) from None
        return n if isinstance(n, int) and n >= 0 else None

    def _transfer_cmd(self, ftp: ImplicitFTPS, cmd: str, path: str) -> socket.socket:
        try:
            return ftp.transfercmd(cmd)
        except ssl.SSLError as e:
            raise classify_error(e, phase="data", path=path) from None
        except (OSError, EOFError, ftplib.Error) as e:
            raise classify_error(e, phase="data" if not isinstance(e, ftplib.Error) else "op", path=path) from None

    def _receive(
        self, ftp: ImplicitFTPS, cmd: str, path: str, sink: Callable[[bytes], Any], ctx: _Ctx, total: int | None
    ) -> tuple[int, bool, BaseException | None]:
        """Run a receiving transfer. Returns (bytes, clean_end, final_reply_error).

        A ragged TLS end on the data channel or a missing/odd final reply is reported,
        not raised, so the caller can decide by size whether the transfer was complete.
        """
        conn = self._transfer_cmd(ftp, cmd, path)
        received = 0
        clean = True
        try:
            conn.settimeout(self.socket_timeout)
            while True:
                ctx.check()
                try:
                    chunk = conn.recv(CHUNK_SIZE)
                except ssl.SSLError as e:  # e.g. UNEXPECTED_EOF_WHILE_READING: no close_notify
                    log.debug("FTP %s: data channel ended uncleanly after %d bytes: %s", path, received, e)
                    clean = False
                    break
                except (ConnectionResetError, BrokenPipeError) as e:
                    log.debug("FTP %s: data channel reset after %d bytes: %s", path, received, e)
                    clean = False
                    break
                if not chunk:
                    break
                sink(chunk)
                received += len(chunk)
                ctx.progress(received, total)
        except FtpOpError:
            _close_data(conn, polite=False)
            raise
        except (OSError, ValueError) as e:  # timeout etc. - truly broken
            _close_data(conn, polite=False)
            raise classify_error(e, phase="op", path=path) from None
        _close_data(conn, polite=clean)
        reply_err: BaseException | None = None
        ctx.touch()
        try:
            ftp.voidresp()
        except (OSError, EOFError, ftplib.Error) as e:
            reply_err = e
        return received, clean, reply_err

    def _list(self, ftp: ImplicitFTPS, path: str, ctx: _Ctx) -> list[dict[str, Any]]:
        try:
            ftp.cwd(path)
        except (OSError, EOFError, ftplib.Error) as e:
            err = classify_error(e, phase="op", path=path)
            if err.kind == "not_found":
                err = FtpOpError("not_found", f"550 directory not on printer: {path}", code="550")
            raise err from None
        buf = bytearray()
        _, clean, reply_err = self._receive(ftp, "LIST", path, buf.extend, ctx, None)
        if reply_err is not None and (not clean or isinstance(reply_err, ftplib.error_perm)):
            raise classify_error(reply_err, phase="op", path=path)
        if reply_err is not None:
            # Listing arrived intact (clean TLS end) but the 226 got lost: accept it.
            log.debug("FTP list %s: final reply lost (%s), listing accepted", path, _short(reply_err))
        now = datetime.now()
        text = buf.decode(ftp.encoding, errors="replace")
        entries = [e for e in (parse_list_line(ln, now) for ln in text.splitlines()) if e is not None]
        base = path.rstrip("/")
        for e in entries:
            e["path"] = f"{base}/{e['name']}"  # listing field, used by compat.py callers
        return entries

    def _download(self, ctx: _Ctx, protect: bool, remote: str, local: Path, expected_size: int | None) -> int:
        base = max(MIN_OP_DEADLINE, self.socket_timeout * 3)
        ctx.deadline = time.monotonic() + base
        try:
            local.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise FtpOpError("local", f"cannot create {local.parent}: {_short(e)}") from None
        part = local.with_name(local.name + ".part")
        ftp = self._open(protect, ctx)
        clean_session = False
        try:
            server_size = self._size(ftp, remote)
            total = server_size if server_size is not None else expected_size
            ctx.extend_for_size(total, base)
            try:
                fh = open(part, "wb")
            except OSError as e:
                raise FtpOpError("local", f"cannot write {part}: {_short(e)}") from None
            def sink(chunk: bytes) -> None:
                # A full disk is our problem, not the printer's: never report it as a network error.
                try:
                    fh.write(chunk)
                except OSError as e:
                    raise FtpOpError("local", f"cannot write {part}: {_short(e)}") from None

            with fh:
                received, clean, reply_err = self._receive(ftp, f"RETR {remote}", remote, sink, ctx, total)
            ctx.progress(received, total, final=True)
            if total is not None:
                if received != total:
                    if reply_err is not None and isinstance(reply_err, ftplib.error_perm):
                        raise classify_error(reply_err, phase="op", path=remote)
                    raise FtpOpError(
                        "size_mismatch", f"short download of {remote}: got {received} of {total} bytes", retryable=True
                    )
                if reply_err is not None or not clean:
                    log.info(
                        "FTP download %s: %s after all %d bytes - size matches, accepted",
                        remote,
                        _short(reply_err) if reply_err else "unclean TLS end",
                        received,
                    )
            elif received == 0:
                # With no size to compare, an empty result is far more likely
                # a transfer that never really started than a genuinely empty file.
                raise FtpOpError("network", f"download of {remote} returned 0 bytes and printer reports no size", retryable=True)
            elif reply_err is not None or not clean:
                # No size to check against and no confirmation: cannot tell complete from cut.
                err = classify_error(reply_err, phase="op", path=remote) if reply_err else None
                raise FtpOpError(
                    "network",
                    f"download of {remote} unconfirmed ({err.message if err else 'unclean end'}) and printer reports no size",
                    retryable=True,
                )
            clean_session = reply_err is None
            try:
                os.replace(part, local)
            except OSError as e:
                raise FtpOpError("local", f"cannot move download into place: {_short(e)}") from None
            log.info("FTP downloaded %s (%d bytes)", remote, received)
            return received
        finally:
            _close_session(ftp, clean=clean_session)
            if part.exists():
                try:
                    part.unlink()
                except OSError:
                    pass

    def _upload(self, ctx: _Ctx, protect: bool, local: Path, remote: str) -> int:
        try:
            size = local.stat().st_size
            fh = open(local, "rb")
        except OSError as e:
            raise FtpOpError("local", f"cannot read {local}: {_short(e)}") from None
        base = max(MIN_OP_DEADLINE, self.socket_timeout * 3)
        ctx.deadline = time.monotonic()
        ctx.extend_for_size(max(size, 1), base)
        ftp: ImplicitFTPS | None = None
        control_ok = False
        try:
            ftp = self._open(protect, ctx)
            conn = self._transfer_cmd(ftp, f"STOR {remote}", remote)
            sent = 0
            send_err: FtpOpError | None = None
            try:
                conn.settimeout(self.socket_timeout)
                with fh:
                    while True:
                        ctx.check()
                        chunk = fh.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        conn.sendall(chunk)
                        sent += len(chunk)
                        ctx.progress(sent, size)
            except FtpOpError as e:
                send_err = e
            except OSError as e:
                send_err = classify_error(e, phase="op", path=remote)
            finally:
                _close_data(conn, polite=False)
            reply_err: BaseException | None = None
            ctx.touch()
            try:
                # A healthy STOR's 226 can take long while the printer flushes to SD; an
                # aborted one is answered (426/451) right away, so don't wait 60 s for it.
                ftp.sock.settimeout(STOR_REPLY_TIMEOUT if send_err is None else self.socket_timeout)
                ftp.voidresp()
                control_ok = True
            except ftplib.Error as e:
                reply_err = e
                control_ok = True  # a reply arrived, the channel is still in sync
            except (OSError, EOFError) as e:
                reply_err = e
            finally:
                try:
                    ftp.sock.settimeout(self.socket_timeout)
                except (OSError, AttributeError):
                    pass
            ctx.touch()
            if send_err is not None:
                if not control_ok:
                    # Close the dead session BEFORE _remove_partial opens a new one: two
                    # sessions at once is exactly what the printer answers with 421/425.
                    _close_session(ftp, clean=False)
                    ftp = None
                self._remove_partial(ftp, protect, remote, ctx)
                raise send_err
            ctx.progress(sent, size, final=True)
            # Verify on the printer. The control channel may be dead after a lost reply;
            # then use a fresh session (still inside our queue slot, so still one-at-a-time).
            if not control_ok:
                _close_session(ftp, clean=False)
                ftp = self._open(protect, ctx)
                control_ok = True
            ctx.touch()
            try:
                remote_size = self._size(ftp, remote)
            except FtpOpError as e:
                if e.kind == "not_found":
                    remote_size = -1
                else:
                    raise
            if remote_size == size:
                if reply_err is not None:
                    log.info("FTP upload %s: %s but size matches (%d bytes), accepted", remote, _short(reply_err), size)
                log.info("FTP uploaded %s (%d bytes)", remote, size)
                return size
            if remote_size is None and reply_err is None:
                log.info("FTP uploaded %s (%d bytes, 226 confirmed, SIZE unsupported)", remote, size)
                return size
            self._remove_partial(ftp, protect, remote, ctx)
            if isinstance(reply_err, ftplib.error_perm):
                raise classify_error(reply_err, phase="op", path=remote)
            got = "missing" if remote_size in (None, -1) else f"{remote_size} bytes"
            raise FtpOpError("size_mismatch", f"upload of {remote} incomplete: printer has {got}, expected {size}", retryable=True)
        finally:
            if not fh.closed:
                fh.close()
            _close_session(ftp, clean=control_ok)

    def _remove_partial(self, ftp: ImplicitFTPS | None, protect: bool, remote: str, ctx: _Ctx | None = None) -> None:
        """Best effort: a truncated file left on the SD card is worse than none.

        With ``ftp=None`` it opens its own session; the caller must have closed any
        previous one first (one printer session at a time).
        """
        opened = False
        if ctx is not None:
            ctx.touch()
        try:
            if ftp is None:
                ftp = self._open(protect, ctx)
                opened = True
            ftp.delete(remote)
            log.info("FTP removed partial upload %s", remote)
        except FtpOpError as e:
            log.info("FTP could not remove partial %s: %s", remote, e.message)
        except (OSError, EOFError, ftplib.Error) as e:
            if _reply_code(e) != "550":
                log.info("FTP could not remove partial %s: %s", remote, _short(e))
        finally:
            if opened:
                _close_session(ftp, clean=True)


__all__ = [
    "FTP_PORT",
    "FTP_USER",
    "PRIORITY_BACKGROUND",
    "PRIORITY_INTERACTIVE",
    "FtpResult",
    "ImplicitFTPS",
    "PrinterFTP",
    "TransferQueue",
    "classify_error",
    "get_transfer_queue",
    "make_ssl_context",
    "parse_list_line",
]

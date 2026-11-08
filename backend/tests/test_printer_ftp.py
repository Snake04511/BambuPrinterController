"""Tests for backend/app/printer_ftp.py against a local *implicit* FTPS server.

pyftpdlib only speaks explicit TLS (AUTH TLS); the handler below starts TLS on accept
instead, which is exactly what the P1S does on :990. Everything binds 127.0.0.1 and
is torn down per test. Quirks of the printer (SSL EOF instead of 226, 425 busy, 426
after STOR, wrong SIZE) are injected by handler class attributes.

Test-only deps: pytest, pyftpdlib, pyOpenSSL. Cert via the openssl CLI.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyftpdlib")
pytest.importorskip("OpenSSL")

from pyftpdlib.authorizers import DummyAuthorizer  # noqa: E402
from pyftpdlib.handlers import TLS_FTPHandler  # noqa: E402
from pyftpdlib.servers import FTPServer  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import printer_ftp as pf  # noqa: E402
from app.printer_ftp import (  # noqa: E402
    PRIORITY_BACKGROUND,
    PRIORITY_INTERACTIVE,
    PrinterFTP,
    TransferQueue,
    get_transfer_queue,
    parse_list_line,
)

logging.getLogger("pyftpdlib").setLevel(logging.ERROR)

CODE = "12345678"


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def certfile(tmp_path_factory: pytest.TempPathFactory) -> str:
    d = tmp_path_factory.mktemp("cert")
    pem = d / "server.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
            "-subj", "/CN=127.0.0.1", "-keyout", str(d / "key.pem"), "-out", str(d / "cert.pem"),
        ],
        check=True,
        capture_output=True,
    )
    pem.write_bytes((d / "cert.pem").read_bytes() + (d / "key.pem").read_bytes())
    return str(pem)


class Quirks:
    """Shared mutable server behaviour + counters, one instance per test server."""

    def __init__(self) -> None:
        self.eof_after_retr = False
        self.busy_retr = 0  # answer RETR with 425 this many times
        self.stor_426 = False
        self.stor_553 = False
        self.size_delta = 0
        self.size_unsupported = False
        self.logins = 0


def make_handler(certfile: str, root: str, quirks: Quirks) -> type:
    authorizer = DummyAuthorizer()
    authorizer.add_user("bblp", CODE, root, perm="elradfmwMT")

    class ImplicitTLSHandler(TLS_FTPHandler):
        """Implicit FTPS: TLS on accept, banner only once the handshake is done."""

        _banner_sent = False
        _pending: str | None = None

        def handle(self) -> None:
            self.secure_connection(self.ssl_context)

        def handle_ssl_established(self) -> None:
            if not self._banner_sent:
                self._banner_sent = True
                TLS_FTPHandler.handle(self)

        def on_login(self, username: str) -> None:
            quirks.logins += 1

        def ftp_RETR(self, file: str) -> Any:
            if quirks.busy_retr > 0:
                quirks.busy_retr -= 1
                self.respond("425 Can't open data connection.")
                return None
            self._pending = "RETR"
            return super().ftp_RETR(file)

        def ftp_STOR(self, file: str, mode: str = "w") -> Any:
            if quirks.stor_553:
                self.respond("553 Could not create file.")
                return None
            self._pending = "STOR"
            return super().ftp_STOR(file, mode)

        def ftp_SIZE(self, path: str) -> Any:
            if quirks.size_unsupported:
                self.respond("502 Command not implemented.")
                return None
            if quirks.size_delta:
                try:
                    n = os.path.getsize(path)
                except OSError:
                    return super().ftp_SIZE(path)
                self.respond(f"213 {n + quirks.size_delta}")
                return None
            return super().ftp_SIZE(path)

        def respond(self, resp: str, logfun: Any = logging.debug) -> None:
            pending, self._pending = self._pending, None if resp.startswith(("2", "4", "5")) else self._pending
            if pending == "RETR" and resp.startswith("226") and quirks.eof_after_retr:
                # The P1S way: transfer done, then the control channel just drops
                # (no 226, no TLS close_notify) -> client sees SSL EOF / EOFError.
                try:
                    self.socket.sock_shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                self.close()
                return
            if pending == "STOR" and resp.startswith("226") and quirks.stor_426:
                resp = "426 Failure reading network stream."
            super().respond(resp, logfun)

    ImplicitTLSHandler.certfile = certfile
    ImplicitTLSHandler.ssl_context = None  # fresh context per handler class
    # Like vsFTPd: keep a session cache so the data channel can resume the control session.
    ImplicitTLSHandler.get_ssl_context().set_session_id(b"printer-ftp-test")
    ImplicitTLSHandler.authorizer = authorizer
    ImplicitTLSHandler.passive_ports = None
    ImplicitTLSHandler.permit_foreign_addresses = False
    return ImplicitTLSHandler


class Server:
    def __init__(self, certfile: str, root: Path) -> None:
        self.root = root
        self.quirks = Quirks()
        self._srv = FTPServer(("127.0.0.1", 0), make_handler(certfile, str(root), self.quirks))
        self.port = self._srv.socket.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._srv.serve_forever(timeout=0.05, blocking=False, handle_exit=False)
        self._srv.close_all()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(5)


@pytest.fixture
def server(certfile: str, tmp_path: Path) -> Any:
    root = tmp_path / "sd"
    (root / "cache").mkdir(parents=True)
    (root / "timelapse").mkdir()
    (root / "cache" / "Benchy plate 1.gcode.3mf").write_bytes(os.urandom(300_000))
    (root / "small.txt").write_bytes(b"hello printer\n")
    srv = Server(certfile, root)
    yield srv
    srv.close()


def client(srv: Server, **kw: Any) -> PrinterFTP:
    kw.setdefault("socket_timeout", 5.0)
    kw.setdefault("backoff", 0.05)
    return PrinterFTP("127.0.0.1", kw.pop("code", CODE), port=srv.port, **kw)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --------------------------------------------------------------------------- pure units


def test_parse_list_line_variants() -> None:
    from datetime import datetime

    now = datetime(2026, 11, 7, 12, 0)
    f = parse_list_line("-rw-r--r--    1 0        0          1234 Nov 30 10:15 My File.gcode.3mf", now)
    assert f == {
        "name": "My File.gcode.3mf", "size": 1234, "mtime": "2025-11-30T10:15:00", "is_dir": False, "is_directory": False,
    }
    d = parse_list_line("drwxr-xr-x    2 0        0             0 Nov 05 09:00 timelapse", now)
    assert d and d["is_dir"] and d["size"] == 0 and d["mtime"] == "2026-11-05T09:00:00"
    old = parse_list_line("-rw-r--r--    1 0        0            10 Jan 02  2024 a.txt", now)
    assert old and old["mtime"] == "2024-01-02T00:00:00"
    link = parse_list_line("lrwxrwxrwx    1 0        0             4 Nov 05 09:00 x -> y", now)
    assert link and link["name"] == "x"
    assert parse_list_line("total 12", now) is None
    assert parse_list_line("drwxr-xr-x    2 0 0 0 Nov 05 09:00 .", now) is None
    assert parse_list_line("garbage", now) is None
    weird = parse_list_line("-rw-r--r--    1 0        0            10 Foo 99 99:99 b.txt", now)
    assert weird and weird["mtime"] is None


def test_classify_error_messages() -> None:
    import ftplib

    e = pf.classify_error(ftplib.error_perm("550 No such file."), phase="op", path="/cache/a.3mf")
    assert e.kind == "not_found" and e.message == "550 not on printer: /cache/a.3mf" and not e.retryable
    e = pf.classify_error(ftplib.error_perm("530 Login incorrect."), phase="connect")
    assert e.kind == "auth" and "access code" in e.message and not e.retryable
    e = pf.classify_error(ftplib.error_temp("425 Can't open data connection."), phase="data")
    assert e.kind == "busy" and e.retryable
    e = pf.classify_error(ftplib.error_temp("452 Insufficient storage."), phase="op")
    assert e.kind == "storage" and not e.retryable
    e = pf.classify_error(TimeoutError("timed out"), phase="connect")
    assert e.kind == "timeout" and e.retryable
    e = pf.classify_error(ssl.SSLEOFError(8, "EOF occurred"), phase="connect")
    assert e.kind == "tls"
    e = pf.classify_error(EOFError(), phase="op")
    assert e.kind == "network" and e.retryable


def test_transfer_queue_priority_and_cancel() -> None:
    async def main() -> list[str]:
        q = TransferQueue()
        order: list[str] = []
        await q.acquire(PRIORITY_BACKGROUND, "holder")

        async def worker(name: str, prio: int) -> None:
            await q.acquire(prio, name)
            order.append(name)
            await asyncio.sleep(0)
            q.release()

        b1 = asyncio.create_task(worker("bg1", PRIORITY_BACKGROUND))
        await asyncio.sleep(0)
        b2 = asyncio.create_task(worker("bg2", PRIORITY_BACKGROUND))
        await asyncio.sleep(0)
        gone = asyncio.create_task(worker("cancelled", PRIORITY_INTERACTIVE))
        await asyncio.sleep(0)
        i1 = asyncio.create_task(worker("ui", PRIORITY_INTERACTIVE))
        await asyncio.sleep(0)
        assert q.waiting == 4
        gone.cancel()
        await asyncio.sleep(0)
        assert q.waiting == 3
        q.release()
        await asyncio.gather(b1, b2, i1)
        assert not q.busy and q.current is None
        return order

    assert run(main()) == ["ui", "bg1", "bg2"]


def test_invalid_path_and_missing_code() -> None:
    async def main() -> None:
        c = PrinterFTP("127.0.0.1", "", port=1, socket_timeout=0.5, retries=0)
        r = await c.list_dir("/")
        assert not r.ok and r.kind == "config"
        c = PrinterFTP("127.0.0.1", CODE, port=1, socket_timeout=0.5, retries=0)
        r = await c.delete("/a\r\nDELE /b")
        assert not r.ok and r.kind == "invalid"
        assert CODE not in repr(c)

    run(main())


# --------------------------------------------------------------------------- against the server


def test_list_dir(server: Server) -> None:
    r = run(client(server).list_dir("/"))
    assert r.ok, r.message
    names = {e["name"]: e for e in r.data}
    assert names["cache"]["is_dir"] and names["timelapse"]["is_dir"]
    assert names["small.txt"]["size"] == 14 and not names["small.txt"]["is_dir"]
    assert names["small.txt"]["mtime"] and "T" in names["small.txt"]["mtime"]

    r = run(client(server).list_dir("/cache"))
    assert r.ok and r.data[0]["name"] == "Benchy plate 1.gcode.3mf" and r.data[0]["size"] == 300_000

    r = run(client(server).list_dir("/nope"))
    assert not r.ok and r.kind == "not_found" and r.message.startswith("550") and r.attempts == 1


def test_download_with_progress(server: Server, tmp_path: Path) -> None:
    seen: list[tuple[int, int | None]] = []
    dest = tmp_path / "out" / "benchy.3mf"
    r = run(client(server).download("/cache/Benchy plate 1.gcode.3mf", dest, lambda d, t: seen.append((d, t))))
    assert r.ok, r.message
    assert r.data == 300_000
    assert dest.read_bytes() == (server.root / "cache" / "Benchy plate 1.gcode.3mf").read_bytes()
    assert not (tmp_path / "out" / "benchy.3mf.part").exists()
    assert seen and seen[-1] == (300_000, 300_000)


def test_data_channel_reuses_tls_session(server: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """vsFTPd on the printer rejects data connections that do not resume the control session."""
    reused: list[tuple[bool, str | None]] = []
    orig = pf.ImplicitFTPS.ntransfercmd

    def spy(self: Any, cmd: str, rest: Any = None) -> Any:
        conn, size = orig(self, cmd, rest)
        reused.append((conn.session_reused, conn.version()))
        return conn, size

    monkeypatch.setattr(pf.ImplicitFTPS, "ntransfercmd", spy)
    r = run(client(server).download("/small.txt", tmp_path / "s"))
    assert r.ok, r.message
    assert reused == [(True, "TLSv1.2")]


def test_download_missing_is_550(server: Server, tmp_path: Path) -> None:
    r = run(client(server).download("/cache/none.3mf", tmp_path / "x"))
    assert not r.ok and r.kind == "not_found" and r.message == "550 not on printer: /cache/none.3mf"
    assert r.attempts == 1  # never retried
    assert not (tmp_path / "x").exists() and not (tmp_path / "x.part").exists()


def test_download_ssl_eof_after_complete_transfer(server: Server, tmp_path: Path) -> None:
    server.quirks.eof_after_retr = True
    dest = tmp_path / "eof.3mf"
    r = run(client(server, retries=0).download("/cache/Benchy plate 1.gcode.3mf", dest))
    assert r.ok, r.message
    assert dest.stat().st_size == 300_000


def test_download_eof_without_size_needs_expected_size(server: Server, tmp_path: Path) -> None:
    server.quirks.eof_after_retr = True
    server.quirks.size_unsupported = True
    r = run(client(server, retries=0).download("/small.txt", tmp_path / "a"))
    assert not r.ok and "unconfirmed" in r.message and not (tmp_path / "a").exists()
    r = run(client(server, retries=0).download("/small.txt", tmp_path / "b", expected_size=14))
    assert r.ok and (tmp_path / "b").read_bytes() == b"hello printer\n"


def test_download_size_mismatch_retries_then_fails(server: Server, tmp_path: Path) -> None:
    server.quirks.size_delta = 100
    r = run(client(server, retries=1).download("/small.txt", tmp_path / "s"))
    assert not r.ok and r.kind == "size_mismatch" and r.attempts == 2
    assert "got 14 of 114" in r.message
    assert not (tmp_path / "s").exists() and not (tmp_path / "s.part").exists()


def test_busy_425_is_retried(server: Server, tmp_path: Path) -> None:
    server.quirks.busy_retr = 1
    r = run(client(server, retries=2).download("/small.txt", tmp_path / "b"))
    assert r.ok and r.attempts == 2


def test_wrong_access_code_not_retried(server: Server) -> None:
    r = run(client(server, code="wrong", retries=3).list_dir("/"))
    assert not r.ok and r.kind == "auth" and r.attempts == 1
    assert "wrong" not in r.message


def test_upload_and_verify(server: Server, tmp_path: Path) -> None:
    src = tmp_path / "job.gcode.3mf"
    src.write_bytes(os.urandom(200_000))
    seen: list[int] = []
    r = run(client(server).upload(src, "/cache/job.gcode.3mf", lambda d, t: seen.append(d)))
    assert r.ok, r.message
    assert r.data == 200_000
    assert (server.root / "cache" / "job.gcode.3mf").read_bytes() == src.read_bytes()
    assert seen[-1] == 200_000


def test_upload_426_but_size_matches_is_ok(server: Server, tmp_path: Path) -> None:
    server.quirks.stor_426 = True
    src = tmp_path / "j.3mf"
    src.write_bytes(os.urandom(50_000))
    r = run(client(server, retries=0).upload(src, "/cache/j.3mf"))
    assert r.ok, r.message


def test_upload_size_mismatch_removes_partial(server: Server, tmp_path: Path) -> None:
    server.quirks.size_delta = -1
    src = tmp_path / "k.3mf"
    src.write_bytes(os.urandom(10_000))
    r = run(client(server, retries=0).upload(src, "/cache/k.3mf"))
    assert not r.ok and r.kind == "size_mismatch"
    assert not (server.root / "cache" / "k.3mf").exists()


def test_upload_553_storage_not_retried(server: Server, tmp_path: Path) -> None:
    server.quirks.stor_553 = True
    src = tmp_path / "x.3mf"
    src.write_bytes(b"x" * 10)
    r = run(client(server, retries=3).upload(src, "/cache/x.3mf"))
    assert not r.ok and r.kind == "storage" and r.attempts == 1 and r.message.startswith("553")


def test_upload_missing_local_file(server: Server, tmp_path: Path) -> None:
    r = run(client(server).upload(tmp_path / "nope", "/cache/nope"))
    assert not r.ok and r.kind == "local"


def test_delete(server: Server) -> None:
    async def main() -> None:
        c = client(server)
        r = await c.delete("/small.txt")
        assert r.ok and r.data is True
        assert not (server.root / "small.txt").exists()
        r = await c.delete("/small.txt")
        assert not r.ok and r.kind == "not_found" and r.message == "550 not on printer: /small.txt"
        r = await c.delete("/small.txt", missing_ok=True)
        assert r.ok and r.data is False

    run(main())


def test_concurrent_calls_serialize(server: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Many concurrent operations -> never two printer sessions at once."""
    active = 0
    peak = 0
    lock = threading.Lock()
    orig_open = PrinterFTP._open
    orig_close = pf._close_session

    def tracked_open(self: PrinterFTP, protect: bool, ctx: Any = None) -> Any:
        nonlocal active, peak
        ftp = orig_open(self, protect, ctx)
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)  # widen any overlap window
        return ftp

    def tracked_close(ftp: Any, *, clean: bool) -> None:
        nonlocal active
        if ftp is not None:
            with lock:
                active -= 1
        orig_close(ftp, clean=clean)

    monkeypatch.setattr(PrinterFTP, "_open", tracked_open)
    monkeypatch.setattr(pf, "_close_session", tracked_close)

    async def main() -> list[Any]:
        c = client(server)
        c2 = client(server)  # a second client instance shares the same global queue
        return await asyncio.gather(
            c.list_dir("/"),
            c.download("/cache/Benchy plate 1.gcode.3mf", tmp_path / "1"),
            c2.download("/cache/Benchy plate 1.gcode.3mf", tmp_path / "2", priority=PRIORITY_BACKGROUND),
            c2.list_dir("/cache"),
            c.download("/small.txt", tmp_path / "3"),
        )

    results = run(main())
    assert all(r.ok for r in results), [r.message for r in results]
    assert peak == 1
    assert active == 0


def test_interactive_jumps_queue(server: Server) -> None:
    async def main() -> list[str]:
        c = client(server)
        q = get_transfer_queue()
        done: list[str] = []
        await q.acquire(PRIORITY_INTERACTIVE, "test holder")

        async def op(name: str, prio: int) -> None:
            r = await c.list_dir("/", priority=prio)
            assert r.ok
            done.append(name)

        tasks = [asyncio.create_task(op("bg1", PRIORITY_BACKGROUND)), asyncio.create_task(op("bg2", PRIORITY_BACKGROUND))]
        await asyncio.sleep(0.01)
        tasks.append(asyncio.create_task(op("ui", PRIORITY_INTERACTIVE)))
        await asyncio.sleep(0.01)
        assert q.status()["waiting"] == 3
        q.release()
        await asyncio.gather(*tasks)
        return done

    assert run(main()) == ["ui", "bg1", "bg2"]


def test_cancel_download_releases_slot_after_thread(server: Server, tmp_path: Path) -> None:
    big = server.root / "cache" / "big.3mf"
    big.write_bytes(os.urandom(4_000_000))
    dest = tmp_path / "big.3mf"

    async def main() -> None:
        c = client(server)
        task: asyncio.Task[Any] | None = None

        def cb(done: int, total: int | None) -> None:
            if task is not None and done > 0 and not task.done():
                task.cancel()

        task = asyncio.create_task(c.download("/cache/big.3mf", dest, cb))
        with pytest.raises(asyncio.CancelledError):
            await task
        q = get_transfer_queue()
        for _ in range(200):  # the worker notices at its next chunk
            if not q.busy:
                break
            await asyncio.sleep(0.02)
        assert not q.busy
        assert not dest.exists() and not (tmp_path / "big.3mf.part").exists()
        r = await c.list_dir("/cache")  # printer usable again
        assert r.ok

    run(main())


def test_connect_timeout(tmp_path: Path) -> None:
    """A port that accepts but never speaks TLS -> clean timeout error, no hang."""
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(4)
    port = lsock.getsockname()[1]
    try:
        c = PrinterFTP("127.0.0.1", CODE, port=port, socket_timeout=0.5, retries=0)
        t0 = time.monotonic()
        r = run(c.list_dir("/"))
        assert not r.ok and r.kind == "timeout" and time.monotonic() - t0 < 5
    finally:
        lsock.close()


def test_cleartext_reply_is_tls_error_and_background_cools_off() -> None:
    """Printer answering :990 in cleartext -> tls error, background pauses."""
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(4)
    port = lsock.getsockname()[1]
    accepted = 0
    stop = threading.Event()

    def serve() -> None:
        nonlocal accepted
        lsock.settimeout(0.1)
        while not stop.is_set():
            try:
                s, _ = lsock.accept()
            except OSError:
                continue
            accepted += 1
            s.sendall(b"421 There are too many connections from your internet address.\r\n")
            s.close()

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    try:
        async def main() -> None:
            c = PrinterFTP("127.0.0.1", CODE, port=port, socket_timeout=1.0, retries=0)
            r = await c.list_dir("/", priority=PRIORITY_BACKGROUND)
            assert not r.ok and r.kind == "tls" and "WRONG_VERSION_NUMBER" in r.message, r
            n = accepted
            r2 = await c.list_dir("/", priority=PRIORITY_BACKGROUND)
            assert r2.kind == "cooloff" and accepted == n  # nothing sent
            r3 = await c.list_dir("/", priority=PRIORITY_INTERACTIVE)
            assert not r3.ok and accepted == n + 1  # user action still tries

        run(main())
    finally:
        stop.set()
        th.join(2)
        lsock.close()


# --------------------------------------------------------------------------- review regressions


def _track_sessions(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count open printer sessions (via _open/_close_session) and remember the peak."""
    st = {"active": 0, "peak": 0, "opened": 0}
    lock = threading.Lock()
    orig_open = PrinterFTP._open
    orig_close = pf._close_session

    def tracked_open(self: PrinterFTP, protect: bool, ctx: Any = None) -> Any:
        ftp = orig_open(self, protect, ctx)
        with lock:
            st["active"] += 1
            st["opened"] += 1
            st["peak"] = max(st["peak"], st["active"])
        return ftp

    def tracked_close(ftp: Any, *, clean: bool) -> None:
        if ftp is not None:
            with lock:
                st["active"] -= 1
        orig_close(ftp, clean=clean)

    monkeypatch.setattr(PrinterFTP, "_open", tracked_open)
    monkeypatch.setattr(pf, "_close_session", tracked_close)
    return st


def test_background_tls_failure_reports_tls_not_cooloff() -> None:
    """With retries, a background op used to retry into its own cool-off and return
    kind="cooloff", hiding the handshake error. It must report "tls" after 1 attempt."""
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(4)
    port = lsock.getsockname()[1]
    accepted = 0
    stop = threading.Event()

    def serve() -> None:
        nonlocal accepted
        lsock.settimeout(0.1)
        while not stop.is_set():
            try:
                s, _ = lsock.accept()
            except OSError:
                continue
            accepted += 1
            s.sendall(b"421 Too many connections.\r\n")
            s.close()

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    try:
        async def main() -> None:
            c = PrinterFTP("127.0.0.1", CODE, port=port, socket_timeout=1.0, retries=3, backoff=0.01)
            r = await c.list_dir("/", priority=PRIORITY_BACKGROUND)
            assert not r.ok and r.kind == "tls" and r.attempts == 1, r
            assert accepted == 1
            assert c.status()["cooloff_s"] > 0 and c.status()["last_error"] == r.message

        run(main())
    finally:
        stop.set()
        th.join(2)
        lsock.close()


def test_list_entries_keep_listing_keys(server: Server) -> None:
    """sdcard.py/compat.py read is_directory and path: keep them present."""
    r = run(client(server).list_dir("/cache/"))
    assert r.ok, r.message
    e = r.data[0]
    assert e["path"] == "/cache/Benchy plate 1.gcode.3mf"
    assert e["is_directory"] is False and e["is_dir"] is False
    r = run(client(server).list_dir("/"))
    assert {x["path"] for x in r.data if x["is_directory"]} == {"/cache", "/timelapse"}


def test_async_progress_callback_runs(server: Server, tmp_path: Path) -> None:
    seen: list[tuple[int, int | None]] = []

    async def cb(done: int, total: int | None) -> None:
        await asyncio.sleep(0)
        seen.append((done, total))

    async def main() -> Any:
        r = await client(server).download("/cache/Benchy plate 1.gcode.3mf", tmp_path / "p", cb)
        for _ in range(20):  # let the last callback task finish
            await asyncio.sleep(0)
        return r

    r = run(main())
    assert r.ok, r.message
    assert seen and seen[-1] == (300_000, 300_000)
    assert not pf._progress_tasks  # references dropped once done


def test_zero_byte_download_without_size_is_error(server: Server, tmp_path: Path) -> None:
    (server.root / "empty.bin").write_bytes(b"")
    server.quirks.size_unsupported = True
    r = run(client(server, retries=0).download("/empty.bin", tmp_path / "e"))
    assert not r.ok and "0 bytes" in r.message and not (tmp_path / "e").exists()
    server.quirks.size_unsupported = False
    r = run(client(server, retries=0).download("/empty.bin", tmp_path / "e"))
    assert r.ok and r.data == 0 and (tmp_path / "e").read_bytes() == b""  # SIZE 0 is authoritative


def test_upload_failure_with_dead_control_never_overlaps_sessions(
    server: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Data channel dies mid-STOR and the control reply is lost: the cleanup session that
    deletes the partial file must open only AFTER the dead one is closed (peak == 1)."""
    st = _track_sessions(monkeypatch)
    calls = {"check": 0, "failed": False}
    orig_check = pf._Ctx.check
    orig_voidresp = pf.ImplicitFTPS.voidresp

    def flaky_check(self: Any) -> None:
        calls["check"] += 1
        if calls["check"] == 3:  # after two 64 KB chunks went out
            calls["failed"] = True
            raise pf.FtpOpError("network", "simulated data channel loss", retryable=True)
        orig_check(self)

    def lossy_voidresp(self: Any) -> str:
        if calls["failed"]:
            calls["failed"] = False
            raise EOFError()  # the reply never arrives: control channel is dead
        return orig_voidresp(self)

    monkeypatch.setattr(pf._Ctx, "check", flaky_check)
    monkeypatch.setattr(pf.ImplicitFTPS, "voidresp", lossy_voidresp)
    src = tmp_path / "big.3mf"
    src.write_bytes(os.urandom(400_000))
    r = run(client(server, retries=0).upload(src, "/cache/big.3mf"))
    assert not r.ok and r.kind == "network" and "simulated" in r.message
    assert st["opened"] == 2, st  # the upload session + one cleanup session
    assert st["peak"] == 1 and st["active"] == 0, st
    deadline = time.monotonic() + 2
    while (server.root / "cache" / "big.3mf").exists() and time.monotonic() < deadline:
        time.sleep(0.02)  # pyftpdlib finishes the aborted STOR asynchronously
    assert not (server.root / "cache" / "big.3mf").exists()


def test_upload_reply_timeout_restored_after_error(server: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After a 426 on STOR the same control session verifies SIZE; its socket timeout
    must be back to socket_timeout, not left at the long STOR reply timeout."""
    server.quirks.stor_426 = True
    seen: list[float | None] = []
    orig_size = PrinterFTP._size

    def spy(self: PrinterFTP, ftp: Any, remote: str) -> Any:
        seen.append(ftp.sock.gettimeout())
        return orig_size(self, ftp, remote)

    monkeypatch.setattr(PrinterFTP, "_size", spy)
    src = tmp_path / "t.3mf"
    src.write_bytes(os.urandom(20_000))
    r = run(client(server, retries=0, socket_timeout=4.0).upload(src, "/cache/t.3mf"))
    assert r.ok, r.message
    assert seen == [4.0]

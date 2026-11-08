"""Push notifications to Discord (webhook) and ntfy.

Why this shape:
- The engine loop must never stall or crash because a phone push failed, so every public
  coroutine returns a result value instead of raising, and ``fire()`` runs a send in the
  background (retries with backoff can take several seconds).
- Settings are read on every send through the getters the app already has
  (``settings.get`` / ``settings.secret``), so a change in the UI applies immediately
  without re-creating the notifier.
- The poller re-evaluates state every tick, so the same condition (HMS code, Obico score,
  offline printer) is seen many times; a per-event dedupe window plus a global rate limit
  keep a bug or a flapping sensor from flooding the phone / tripping Discord's rate limit.

Channels:
- Discord: one embed (colour by level, fields, footer, timestamp). With an image the request
  is multipart: ``payload_json`` + ``files[0]``, and the embed shows it via
  ``attachment://<filename>``.
- ntfy: plain-text POST to ``{server}/{topic}`` with ``Title``/``Priority``/``Tags``/``Click``
  headers. With an image: PUT with the image as the body, ``Filename`` header and the text in
  the ``Message`` header (newlines sent as a literal ``\\n``, which ntfy turns back into
  newlines). Non-ASCII header values are RFC 2047 encoded (``=?UTF-8?B?...?=``), which ntfy
  decodes; raw UTF-8 in headers is not portable through proxies.

Settings keys (merge ``SETTINGS_DEFAULTS`` / ``SETTINGS_RULES`` into settings.py):
    notify_discord_enabled, notify_ntfy_enabled, ntfy_server, ntfy_topic, notify_app_url,
    notify_on_<event> for every event in ``EVENTS``.
Secrets (``settings.secret`` names): ``discord_webhook_url``, ``ntfy_token``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import struct
import time
import zlib
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx

from .notify_texts import t as T

log = logging.getLogger("notify")

Level = Literal["info", "success", "warning", "error", "critical"]
FieldsArg = Mapping[str, Any] | Sequence[tuple[str, Any]] | None

# ---------------------------------------------------------------- catalogue


@dataclass(frozen=True)
class EventSpec:
    """One notification type: user toggle default, look and dedupe behaviour."""

    key: str
    label: str
    default_on: bool
    level: Level
    tag: str  # ntfy emoji short code shown in front of the title
    dedupe_s: float  # same event + dedupe key inside this window is dropped


EVENTS: dict[str, EventSpec] = {
    e.key: e
    for e in (
        EventSpec("print_started", "Print started", True, "info", "arrow_forward", 60),
        EventSpec("print_finished", "Print finished", True, "success", "white_check_mark", 60),
        EventSpec("plate_done", "Plate done, next one waits (multi-plate)", True, "success", "arrow_right_hook", 60),
        EventSpec("print_failed", "Print failed", True, "error", "x", 60),
        EventSpec("print_stopped", "Print stopped", True, "warning", "stop_sign", 60),
        EventSpec("print_paused", "Print paused", True, "warning", "pause_button", 300),
        EventSpec("obico_warning", "Obico warning", False, "warning", "eyes", 900),
        EventSpec("obico_failure", "Obico failure detected", True, "critical", "rotating_light", 600),
        EventSpec("filament_low", "Filament low", True, "warning", "thread", 6 * 3600),
        EventSpec("runout_forecast", "Filament runs out during print", True, "warning", "hourglass", 12 * 3600),
        EventSpec("usage_not_booked", "Filament usage not booked", True, "warning", "balance_scale", 12 * 3600),
        EventSpec("hms_error", "Printer error (HMS)", True, "error", "warning", 1800),
        EventSpec("printer_offline", "Printer offline", True, "error", "electric_plug", 1800),
        EventSpec("summary", "Daily / weekly summary", True, "info", "bar_chart", 3600),
        EventSpec("spool_wet", "Spool needs drying", True, "warning", "droplet", 24 * 3600),
    )
}

# Discord embed colours (the panel's neon palette) and ntfy priorities (1 min .. 5 max).
LEVEL_COLOR: dict[str, int] = {
    "info": 0x3BA7FF,
    "success": 0x38FF9C,
    "warning": 0xFFB020,
    "error": 0xFF4D5E,
    "critical": 0xFF2D95,
}
LEVEL_PRIORITY: dict[str, int] = {"info": 3, "success": 3, "warning": 4, "error": 4, "critical": 5}

SETTINGS_DEFAULTS: dict[str, Any] = {
    "notify_discord_enabled": False,
    "notify_ntfy_enabled": False,
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    "notify_app_url": "",  # e.g. http://192.168.1.120:8080 - click target when a send has no url
    **{f"notify_on_{k}": spec.default_on for k, spec in EVENTS.items()},
    # which channel gets which event (Settings > Notifications)
    **{f"notify_{ch}_on_{k}": spec.default_on for k, spec in EVENTS.items() for ch in ("discord", "ntfy")},
}
SETTINGS_RULES: dict[str, tuple] = {
    "notify_discord_enabled": (bool,),
    "notify_ntfy_enabled": (bool,),
    "ntfy_server": (str,),
    "ntfy_topic": (str,),
    "notify_app_url": (str,),
    **{f"notify_on_{k}": (bool,) for k in EVENTS},
    **{f"notify_{ch}_on_{k}": (bool,) for k in EVENTS for ch in ("discord", "ntfy")},
}
SECRET_NAMES = ("discord_webhook_url", "ntfy_token")

# Discord embed limits (https://discord.com/developers/docs/resources/message#embed-object-embed-limits)
_D_TITLE, _D_DESC, _D_FNAME, _D_FVALUE, _D_FIELDS, _D_TOTAL = 256, 4096, 256, 1024, 25, 6000
_MAX_IMAGE = 8 * 1024 * 1024  # under Discord's 10 MB and ntfy's default 15 MB
_TOPIC_RE = re.compile(r"^[-_A-Za-z0-9]{1,64}$")
_DISCORD_HOSTS = ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com")
# Users paste both https://discord.com/api/webhooks/... and the versioned /api/v10/webhooks/...
_DISCORD_PATH_RE = re.compile(r"^/api/(v\d+/)?webhooks/\d+/[^/]+/?$")
# ntfy turns a message body over its message-size-limit (default 4096 bytes) into a file
# attachment instead of a push text; headers are also limited by proxies (nginx: 8 KB/line).
_NTFY_MAX_TEXT = 3800
_NTFY_MAX_TITLE = 250
# A failed delivery re-arms its dedupe key after this many seconds (instead of the event's
# full window), so a print_finished lost to a Wi-Fi blip is not silenced for the whole window
# while a dead network still does not trigger a retry round on every poller tick.
_FAIL_REARM_S = 120.0

# ---------------------------------------------------------------- results


@dataclass
class ChannelResult:
    ok: bool
    detail: str
    status: int | None = None
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "detail": self.detail, "status": self.status, "attempts": self.attempts}


@dataclass
class SendResult:
    """What happened to one ``send``: ``skipped`` is set when nothing was attempted."""

    event: str
    delivered: bool = False
    skipped: str | None = None  # disabled / no_channel / duplicate / rate_limited / unknown_event / error
    channels: dict[str, ChannelResult] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "delivered": self.delivered,
            "skipped": self.skipped,
            "channels": {k: v.to_dict() for k, v in self.channels.items()},
        }


@dataclass
class Notification:
    """A ready-to-send message produced by the ``build_*`` helpers."""

    event: str
    title: str
    message: str
    level: Level
    fields: list[tuple[str, str]] = field(default_factory=list)
    image: bytes | None = None
    url: str | None = None
    dedupe_key: str | None = None
    thumb: bytes | None = None  # small picture (Discord thumbnail; ntfy uses it when there is no image)


# ---------------------------------------------------------------- notifier


class _Outcome:
    """One HTTP attempt: whether it worked, whether retrying makes sense, how long to wait."""

    __slots__ = ("ok", "retry", "status", "detail", "wait")

    def __init__(self, ok: bool, retry: bool, status: int | None, detail: str, wait: float | None = None) -> None:
        self.ok, self.retry, self.status, self.detail, self.wait = ok, retry, status, detail, wait


class Notifier:
    """Delivers notifications to every enabled channel; never raises into the caller.

    ``settings`` / ``secrets`` may be plain mappings (tests) or getters
    (``settings.get`` and ``settings.secret`` in the app) so changes apply live.
    """

    def __init__(
        self,
        settings: Mapping[str, Any] | Callable[[], Mapping[str, Any]],
        secrets: Mapping[str, str] | Callable[[str], str],
        *,
        client: httpx.AsyncClient | None = None,
        attempts: int = 3,
        backoff_s: float = 2.0,
        max_wait_s: float = 30.0,
        timeout_s: float = 10.0,
        rate_limit: tuple[int, float] = (20, 60.0),
        strict_discord_url: bool = True,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        username: str = "Printer Dashboard",
    ) -> None:
        self._settings = settings
        self._secrets = secrets
        self._client = client
        self._own_client = client is None
        self.attempts = max(1, attempts)
        self.backoff_s = backoff_s
        self.max_wait_s = max_wait_s
        self.timeout_s = timeout_s
        self.rate_limit = rate_limit
        self.strict_discord_url = strict_discord_url
        self._clock = clock
        self._wall_clock = wall_clock
        self.username = username
        self._last_sent: dict[str, float] = {}
        self._recent: deque[float] = deque()
        self._tasks: set[asyncio.Task] = set()

    @classmethod
    def from_app(cls, **kwargs: Any) -> Notifier:
        """Notifier wired to the app's settings store (lazy import: db opens /data on import)."""
        from . import settings as app_settings

        return cls(app_settings.get, app_settings.secret, **kwargs)

    # ------------------------------------------------------------ config access

    def _cfg(self) -> dict[str, Any]:
        try:
            raw = dict((self._settings() if callable(self._settings) else self._settings) or {})
            base = {k: v for k, v in SETTINGS_DEFAULTS.items() if not k.startswith(("notify_discord_on_", "notify_ntfy_on_"))}
            return {**base, **raw}
        except Exception:  # noqa: BLE001 - a broken settings store must not kill the caller
            log.exception("notify: reading settings failed")
            return dict(SETTINGS_DEFAULTS)

    def _secret(self, name: str) -> str:
        try:
            if callable(self._secrets):
                val = self._secrets(name)
            else:
                val = self._secrets.get(name, "")
            return (val or "").strip()
        except Exception:  # noqa: BLE001
            log.warning("notify: reading secret %s failed", name)
            return ""

    def event_enabled(self, event: str, cfg: Mapping[str, Any] | None = None, channel: str | None = None) -> bool:
        """Is ``event`` switched on (for ``channel``, or for any channel)?"""
        cfg = cfg if cfg is not None else self._cfg()
        spec = EVENTS.get(event)
        if not spec:
            return False
        chans = [channel] if channel else ["discord", "ntfy"]
        return any(bool(cfg.get(f"notify_{ch}_on_{event}", cfg.get(f"notify_on_{event}", spec.default_on))) for ch in chans)

    def channel_status(self) -> dict[str, dict[str, Any]]:
        """Per-channel 'enabled / configured / problem' for the settings UI (no secrets)."""
        cfg = self._cfg()
        d_err = self._discord_config_error()
        n_err = self._ntfy_config_error(cfg)
        return {
            "discord": {"enabled": bool(cfg["notify_discord_enabled"]), "configured": d_err is None, "error": d_err},
            "ntfy": {"enabled": bool(cfg["notify_ntfy_enabled"]), "configured": n_err is None, "error": n_err},
        }

    def _discord_config_error(self) -> str | None:
        url = self._secret("discord_webhook_url")
        if not url:
            return "Discord webhook URL is not set"
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return "Discord webhook URL is invalid"
        if self.strict_discord_url and (
            parts.scheme != "https" or parts.hostname not in _DISCORD_HOSTS or not _DISCORD_PATH_RE.match(parts.path)
        ):
            return "Not a Discord webhook URL (https://discord.com/api/webhooks/...)"
        return None

    def _ntfy_config_error(self, cfg: Mapping[str, Any]) -> str | None:
        server = str(cfg.get("ntfy_server") or "").strip()
        parts = urlsplit(server)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return "ntfy server must be an http(s) URL"
        if not _TOPIC_RE.match(str(cfg.get("ntfy_topic") or "").strip()):
            return "ntfy topic must be 1-64 letters, digits, '-' or '_'"
        return None

    # ------------------------------------------------------------ public API

    async def send(
        self,
        event: str,
        title: str,
        message: str,
        *,
        level: Level | None = None,
        fields: FieldsArg = None,
        image: bytes | None = None,
        url: str | None = None,
        dedupe_key: str | None = None,
        dedupe_s: float | None = None,
        thumb: bytes | None = None,
    ) -> SendResult:
        """Deliver to every enabled channel, honouring the event toggle, dedupe and rate limit.

        Dedupe is stamped when the send is attempted (not when it succeeds): a dead network
        must not turn every poller tick into a fresh round of retries. If nothing was
        delivered, the stamp is shortened so the same key may try again after
        ``_FAIL_REARM_S`` (a lost print_finished is worth one more try, not a full window).
        """
        res = SendResult(event)
        try:
            spec = EVENTS.get(event)
            if spec is None:
                log.warning("notify: unknown event %r dropped", event)
                res.skipped = "unknown_event"
                return res
            cfg = self._cfg()
            if not self.event_enabled(event, cfg):
                res.skipped = "disabled"
                return res
            channels = [ch for ch in self._enabled_channels(cfg) if self.event_enabled(event, cfg, ch)]
            if not channels:
                res.skipped = "no_channel"
                return res
            now = self._clock()
            key = f"{event}:{dedupe_key or ''}"
            window = spec.dedupe_s if dedupe_s is None else dedupe_s
            last = self._last_sent.get(key)
            if window > 0 and last is not None and now - last < window:
                res.skipped = "duplicate"
                return res
            if not self._take_rate_slot(now):
                log.warning("notify: rate limit hit, %s dropped", event)
                res.skipped = "rate_limited"
                return res
            self._last_sent[key] = now
            self._prune_dedupe(now)
            res.channels = await self._deliver(
                channels, cfg, event, title, message, level or spec.level, fields, image, url, thumb
            )
            res.delivered = any(c.ok for c in res.channels.values())
            if not res.delivered and window > _FAIL_REARM_S and self._last_sent.get(key) == now:
                self._last_sent[key] = now - (window - _FAIL_REARM_S)
            return res
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - never raise into the engine loop
            log.error("notify: send %s failed: %s", event, self._scrub(f"{type(e).__name__}: {e}"))
            res.skipped = "error"
            return res

    async def notify(self, n: Notification) -> SendResult:
        """Send a ``Notification`` built by one of the ``build_*`` helpers."""
        return await self.send(
            n.event, n.title, n.message, level=n.level, fields=n.fields, image=n.image, url=n.url, dedupe_key=n.dedupe_key,
            thumb=n.thumb,
        )

    def fire(self, n: Notification) -> asyncio.Task | None:
        """Fire-and-forget ``notify`` for the poller loop; the task is kept referenced until done."""
        try:
            task = asyncio.get_running_loop().create_task(self.notify(n))
        except RuntimeError:
            log.warning("notify: no running loop, %s dropped", n.event)
            return None
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def send_test(self, channel: str | None = None, *, with_image: bool = False) -> dict[str, ChannelResult]:
        """Test button: ``channel`` ('discord'/'ntfy') is tested even when switched off;
        without it every enabled channel is tested. Bypasses toggles, dedupe and rate limit."""
        try:
            cfg = self._cfg()
            if channel is None:
                channels = self._enabled_channels(cfg)
            elif channel in ("discord", "ntfy"):
                channels = [channel]
            else:
                return {str(channel): ChannelResult(False, "Unknown channel")}
            if not channels:
                # An empty dict renders as "nothing happened" in the UI; say why instead.
                return {"all": ChannelResult(False, "No notification channel is enabled")}
            image = _test_png() if with_image else None
            return await self._deliver(
                channels,
                cfg,
                "test",
                T("test.title"),
                T("test.message"),
                "success",
                [("Csatorna", ", ".join(channels))],
                image,
                None,
                swatch_png("#38FF9C", 75),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.error("notify: test failed: %s", self._scrub(f"{type(e).__name__}: {e}"))
            return {channel or "all": ChannelResult(False, "Internal error")}

    async def aclose(self, timeout_s: float = 5.0) -> None:
        """Let background sends finish (bounded), then close the HTTP client."""
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            _, still = await asyncio.wait(pending, timeout=timeout_s)
            for t in still:
                t.cancel()
            if still:
                # Let the cancelled sends unwind before their HTTP client is closed under them.
                await asyncio.gather(*still, return_exceptions=True)
        if self._client is not None and self._own_client:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    # ------------------------------------------------------------ internals

    def _enabled_channels(self, cfg: Mapping[str, Any]) -> list[str]:
        out = []
        if cfg.get("notify_discord_enabled"):
            out.append("discord")
        if cfg.get("notify_ntfy_enabled"):
            out.append("ntfy")
        return out

    def _take_rate_slot(self, now: float) -> bool:
        limit, window = self.rate_limit
        while self._recent and now - self._recent[0] >= window:
            self._recent.popleft()
        if len(self._recent) >= limit:
            return False
        self._recent.append(now)
        return True

    def _prune_dedupe(self, now: float) -> None:
        if len(self._last_sent) < 256:
            return
        longest = max(s.dedupe_s for s in EVENTS.values())
        for k in [k for k, t in self._last_sent.items() if now - t > longest]:
            del self._last_sent[k]

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=False)
            self._own_client = True
        return self._client

    def _scrub(self, text: str) -> str:
        """Removes secret values from error text (the webhook URL is itself a credential)."""
        for name in SECRET_NAMES:
            val = self._secret(name)
            if not val:
                continue
            text = text.replace(val, "***")
            if name == "discord_webhook_url":
                # Errors may echo only the path ("/api/webhooks/<id>/<token>"); the token
                # segment alone is enough to post as the webhook.
                token = urlsplit(val).path.rstrip("/").rsplit("/", 1)[-1]
                if len(token) >= 8:
                    text = text.replace(token, "***")
        return text

    async def _deliver(
        self,
        channels: list[str],
        cfg: Mapping[str, Any],
        event: str,
        title: str,
        message: str,
        level: str,
        fields: FieldsArg,
        image: bytes | None,
        url: str | None,
        thumb: bytes | None = None,
    ) -> dict[str, ChannelResult]:
        flds = _norm_fields(fields)
        img = _image_info(image)
        th = _image_info(thumb, "thumb")
        footer = T("footer", printer=str(cfg.get("printer_name") or "P1S"))
        if image and img is None:
            log.warning("notify: image for %s dropped (not JPEG/PNG or too large)", event)
        link = (url or str(cfg.get("notify_app_url") or "")).strip() or None
        if link and urlsplit(link).scheme not in ("http", "https"):
            link = None
        jobs = []
        for ch in channels:
            if ch == "discord":
                jobs.append(self._send_discord(event, title, message, level, flds, img, link, th, footer))
            else:
                jobs.append(self._send_ntfy(cfg, event, title, message, level, flds, img or th, link))
        results = await asyncio.gather(*jobs, return_exceptions=True)
        out: dict[str, ChannelResult] = {}
        for ch, r in zip(channels, results):
            if isinstance(r, BaseException):
                if isinstance(r, asyncio.CancelledError):
                    raise r
                r = ChannelResult(False, self._scrub(f"{type(r).__name__}: {r}"))
            out[ch] = r
            if r.ok:
                log.info("notify: %s -> %s ok", event, ch)
            else:
                log.warning("notify: %s -> %s failed: %s", event, ch, r.detail)
        return out

    async def _with_retries(self, attempt: Callable[[int], Any]) -> ChannelResult:
        """Runs ``attempt(n)`` until it succeeds or a non-retryable failure; 5xx, 429 and
        network errors are retried with exponential backoff (429 waits Retry-After)."""
        last = _Outcome(False, False, None, "not attempted")
        n = 0
        for n in range(1, self.attempts + 1):
            try:
                last = await attempt(n)
            except httpx.HTTPError as e:
                last = _Outcome(False, True, None, self._scrub(f"{type(e).__name__}: {e}") or type(e).__name__)
            if last.ok or not last.retry or n == self.attempts:
                break
            wait = last.wait if last.wait is not None else self.backoff_s * (2 ** (n - 1))
            await asyncio.sleep(min(max(wait, 0.0), self.max_wait_s))
        return ChannelResult(last.ok, last.detail, last.status, n)

    # ------------------------------------------------------------ Discord

    def discord_payload(
        self, title: str, message: str, level: str, fields: list[tuple[str, str]], image_name: str | None, url: str | None,
        thumb_name: str | None = None, footer: str = "",
    ) -> dict[str, Any]:
        """Webhook JSON body (public so tests and previews can see the exact shape)."""
        # Discord rejects empty strings in an embed (and an embed with no content at all),
        # so empty title/description are left out and a title is always present.
        embed: dict[str, Any] = {
            "title": _clip(_md_escape(title.strip()) if isinstance(title, str) else title, _D_TITLE) or "Printer notification",
            "color": LEVEL_COLOR.get(level, LEVEL_COLOR["info"]),
            "timestamp": self._wall_clock().astimezone(timezone.utc).isoformat(timespec="seconds"),
        }
        desc = _clip(message or "", _D_DESC)  # Markdown already: the builders escape the values
        if desc.strip():
            embed["description"] = desc
        if url:
            embed["url"] = url
        budget = _D_TOTAL - len(embed["title"]) - len(embed.get("description", ""))
        flds = []
        for name, value in fields[:_D_FIELDS]:
            n, v = _clip(name, _D_FNAME) or "-", _clip(_md_escape(value), _D_FVALUE) or "-"
            if len(n) + len(v) > budget:
                break
            budget -= len(n) + len(v)
            flds.append({"name": n, "value": v, "inline": len(v) <= 40 and "\n" not in v})
        if flds:
            embed["fields"] = flds
        payload: dict[str, Any] = {
            "username": self.username,
            "embeds": [embed],
            # File names / HMS texts must never ping anyone.
            "allowed_mentions": {"parse": []},
        }
        if footer:
            embed["footer"] = {"text": _clip(footer, 2048)}
        att = []
        if image_name:
            embed["image"] = {"url": f"attachment://{image_name}"}
            att.append({"id": 0, "filename": image_name})
        if thumb_name:
            embed["thumbnail"] = {"url": f"attachment://{thumb_name}"}
            att.append({"id": len(att), "filename": thumb_name})
        if att:
            payload["attachments"] = att
        return payload

    async def _send_discord(
        self,
        event: str,
        title: str,
        message: str,
        level: str,
        fields: list[tuple[str, str]],
        img: tuple[bytes, str, str] | None,
        url: str | None,
        thumb: tuple[bytes, str, str] | None = None,
        footer: str = "",
    ) -> ChannelResult:
        err = self._discord_config_error()
        if err:
            return ChannelResult(False, err)
        hook = self._secret("discord_webhook_url")
        client = self._get_client()
        state: dict[str, Any] = {"img": img, "thumb": thumb, "url": url}

        async def post_once() -> httpx.Response:
            pics = [p for p in (state["img"], state["thumb"]) if p]
            if pics:
                payload = self.discord_payload(title, message, level, fields, state["img"][1] if state["img"] else None,
                                               state["url"], state["thumb"][1] if state["thumb"] else None, footer)
                resp = await client.post(
                    hook,
                    data={"payload_json": json.dumps(payload)},
                    files={f"files[{k}]": (p[1], p[0], p[2]) for k, p in enumerate(pics)},
                )
                if resp.status_code != 413:
                    return resp
                # Image too big for this webhook: the text alone is better than nothing.
                log.warning("notify: Discord rejected the image (413), sending without it")
                state["img"] = state["thumb"] = None
            return await client.post(hook, json=self.discord_payload(title, message, level, fields, None, state["url"], None, footer))

        async def attempt(_n: int) -> _Outcome:
            resp = await post_once()
            if resp.status_code == 400 and state["url"] and "url" in _safe_text(resp).lower():
                # Discord refuses embed links it considers malformed (e.g. a LAN host without
                # a TLD, which is exactly what notify_app_url usually is). Drop the link only.
                log.warning("notify: Discord rejected the embed link, sending without it")
                state["url"] = None
                resp = await post_once()
            return self._outcome(resp, "Discord")

        return await self._with_retries(attempt)

    # ------------------------------------------------------------ ntfy

    def ntfy_request(
        self,
        cfg: Mapping[str, Any],
        event: str,
        title: str,
        message: str,
        level: str,
        fields: list[tuple[str, str]],
        img: tuple[bytes, str, str] | None,
        url: str | None,
    ) -> tuple[str, str, dict[str, str], bytes]:
        """(method, url, headers, body) of the ntfy publish (public for tests/previews)."""
        server = str(cfg.get("ntfy_server") or "").strip().rstrip("/")
        topic = str(cfg.get("ntfy_topic") or "").strip()
        text = message or ""
        if fields:
            text = (text + "\n\n" if text else "") + "\n".join(f"{k}: {v}" for k, v in fields)
        # Over ntfy's message-size-limit the body would arrive as "attachment.txt", not text.
        text = _clip_bytes(text, _NTFY_MAX_TEXT)
        spec = EVENTS.get(event)
        tags = [spec.tag if spec else "bell", event]
        headers: dict[str, str] = {
            "Markdown": "yes",
            "Title": _hdr(_clip(title, _NTFY_MAX_TITLE)),
            "Priority": str(LEVEL_PRIORITY.get(level, 3)),
            "Tags": ",".join(tags),
        }
        if url:
            # httpx refuses non-ASCII header values; a percent-encoded URL is equivalent.
            headers["Click"] = quote(url.replace("\r", "").replace("\n", ""), safe=":/?#[]@!$&'()*+,;=%~")
        token = self._secret("ntfy_token")
        if token:
            if ":" in token:  # "user:password" -> basic auth, ntfy accepts both
                headers["Authorization"] = "Basic " + base64.b64encode(token.encode()).decode()
            else:
                headers["Authorization"] = f"Bearer {token}"
        if img:
            headers["Filename"] = img[1]
            # Headers cannot carry newlines; ntfy converts a literal "\n" in Message back.
            headers["Message"] = _hdr(text.replace("\r", "").replace("\n", "\\n"))
            return "PUT", f"{server}/{topic}", headers, img[0]
        headers["Content-Type"] = "text/plain; charset=utf-8"
        return "POST", f"{server}/{topic}", headers, text.encode("utf-8")

    async def _send_ntfy(
        self,
        cfg: Mapping[str, Any],
        event: str,
        title: str,
        message: str,
        level: str,
        fields: list[tuple[str, str]],
        img: tuple[bytes, str, str] | None,
        url: str | None,
    ) -> ChannelResult:
        err = self._ntfy_config_error(cfg)
        if err:
            return ChannelResult(False, err)
        client = self._get_client()
        state = {"img": img}

        async def attempt(_n: int) -> _Outcome:
            method, target, headers, body = self.ntfy_request(cfg, event, title, message, level, fields, state["img"], url)
            resp = await client.request(method, target, headers=headers, content=body)
            if state["img"] and (resp.status_code == 413 or (resp.status_code == 400 and "attachment" in _safe_text(resp).lower())):
                # Attachments disabled / too large on this server (ntfy 40014 / 41301...) -> plain text.
                log.warning("notify: ntfy rejected the attachment (HTTP %s), sending as text", resp.status_code)
                state["img"] = None
                method, target, headers, body = self.ntfy_request(cfg, event, title, message, level, fields, None, url)
                resp = await client.request(method, target, headers=headers, content=body)
            return self._outcome(resp, "ntfy")

        return await self._with_retries(attempt)

    # ------------------------------------------------------------ helpers

    def _outcome(self, resp: httpx.Response, label: str) -> _Outcome:
        code = resp.status_code
        if 200 <= code < 300:
            return _Outcome(True, False, code, "sent")
        detail = f"{label} HTTP {code}"
        try:
            body = resp.text.strip().replace("\n", " ")[:160]
        except Exception:  # noqa: BLE001
            body = ""
        if body and "<html" not in body.lower():
            detail += f": {self._scrub(body)}"
        if code == 429:
            return _Outcome(False, True, code, detail, _retry_after(resp))
        # 408 (proxy/request timeout) is transient like a 5xx; other 4xx are our fault.
        return _Outcome(False, code >= 500 or code == 408, code, detail)


def _safe_text(resp: httpx.Response) -> str:
    try:
        return resp.text
    except Exception:  # noqa: BLE001 - undecodable body
        return ""


def _clip_bytes(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` UTF-8 bytes (ntfy limits are in bytes, not chars)."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[: limit - 3].decode("utf-8", "ignore") + "…"


def _retry_after(resp: httpx.Response) -> float | None:
    """Seconds to wait from a 429: header (seconds) or Discord's JSON ``retry_after``."""
    raw = resp.headers.get("Retry-After")
    try:
        if raw is not None:
            return float(raw)
    except ValueError:
        pass
    try:
        return float(resp.json().get("retry_after"))
    except Exception:  # noqa: BLE001
        return None


def _hdr(value: str) -> str:
    """Header-safe value: ASCII as is, anything else RFC 2047 base64 (ntfy decodes it)."""
    value = value.replace("\r", " ").replace("\n", " ")
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


_MD_CHARS = re.compile(r"([\\*_~`|])")


def _md_escape(text: str) -> str:
    """Discord renders markdown in descriptions/field values; file names like
    ``Benchy_PLA_0.2`` must show literally, so the text is escaped (messages are plain text)."""
    return _MD_CHARS.sub(r"\\\1", text)


def _clip(text: Any, limit: int) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _norm_fields(fields: FieldsArg) -> list[tuple[str, str]]:
    if not fields:
        return []
    items = fields.items() if isinstance(fields, Mapping) else fields
    out = []
    for item in items:
        try:
            k, v = item
        except (TypeError, ValueError):
            continue
        if v is None or v == "":
            continue
        out.append((str(k), str(v)))
    return out


def _image_info(image: bytes | None, name: str = "frame") -> tuple[bytes, str, str] | None:
    """(bytes, filename, mime) for JPEG/PNG; anything else is dropped rather than sent broken."""
    if not image or len(image) > _MAX_IMAGE:
        return None
    if image[:3] == b"\xff\xd8\xff":
        return image, f"{name}.jpg", "image/jpeg"
    if image[:8] == b"\x89PNG\r\n\x1a\n":
        return image, f"{name}.png", "image/png"
    return None


def _test_png() -> bytes:
    """Tiny neon-green PNG for the test button (stdlib only, no pillow needed)."""
    w, h = 64, 32
    row = b"\x00" + bytes((0x38, 0xFF, 0x9C)) * w
    raw = zlib.compress(row * h)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", raw)
        + chunk(b"IEND", b"")
    )



# ---------------------------------------------------------------- message builders
#
# Inputs use the app's own shapes: job = parse_status()["job"], filament lines like
# usage.record() items ({label, grams, spool_name, state}), HMS = {code?, text, severity}.
# parse_status() reports unknown numbers as -1 (layer, total_layers, remaining_min), so every
# builder treats negatives / None / garbage as "unknown" and never raises: they are called
# inline in the poller tick (``notifier.fire(build_...(...))``), outside send()'s safety net.


def _num(v: Any) -> float | None:
    """Float or None for missing / -1 / non-numeric values (bool is not a number here)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        try:
            v = float(v)  # numeric strings
        except (TypeError, ValueError):
            return None
    v = float(v)
    return v if v >= 0 and v == v else None  # also drops NaN


def _pos_int(v: Any) -> int | None:
    n = _num(v)
    return int(n) if n is not None and n > 0 else None


def _job(job: Any) -> str:
    return str(job).strip() if job else T("fallback_job")


def _e(v: Any) -> str:
    """A value going into a Markdown message (file names have _ and *)."""
    return _md_escape(str(v)) if v not in (None, "") else ""


def fmt_duration(seconds: float | int | None) -> str:
    s = _num(seconds)
    if s is None:
        return T("unknown")
    if s < 60:
        return T("dur.lt_min") if s > 0 else T("dur.min", m=0)
    m = int(round(s / 60))
    if m < 60:
        return T("dur.min", m=m)
    return T("dur.hour", h=m // 60, m=m % 60)


def _grams(g: Any) -> str:
    n = _num(g)
    return f"{n:.0f} g" if n is not None and n >= 100 else f"{n:.1f} g" if n is not None else T("unknown")


def _score(v: Any) -> str | None:
    n = _num(v)
    return f"{n:.2f}" if n is not None else None


def _filament_lines(filaments: Sequence[Mapping[str, Any]] | None) -> tuple[str, float]:
    lines, total = [], 0.0
    for f in filaments or []:
        if not isinstance(f, Mapping):
            continue
        g = _num(f.get("grams")) or 0.0
        if g <= 0:
            continue
        total += g
        name = str(f.get("spool_name") or f.get("name") or f.get("material") or f.get("type") or "").strip()
        label = str(f.get("label") or "").strip()
        dot = _color_dot(str(f.get("color") or ""))
        head = " ".join(p for p in (dot, label, name) if p) or "Filament"
        lines.append(f"{head}: {g:.1f} g")
    return "\n".join(lines), total


_DOTS = (("⚫", (0, 0, 0)), ("⚪", (255, 255, 255)), ("🔴", (220, 40, 40)), ("🟠", (255, 140, 0)),
         ("🟡", (250, 220, 40)), ("🟢", (40, 180, 70)), ("🔵", (40, 100, 230)), ("🟣", (140, 60, 200)),
         ("🟤", (130, 80, 40)))


def _color_dot(hex_color: str) -> str:
    """Nearest coloured circle emoji of a '#RRGGBB' (a hint of the colour in plain text)."""
    c = hex_color.lstrip("#")
    if len(c) < 6:
        return ""
    try:
        r, g, b = (int(c[k:k + 2], 16) for k in (0, 2, 4))
    except ValueError:
        return ""
    return min(_DOTS, key=lambda d: (d[1][0] - r) ** 2 + (d[1][1] - g) ** 2 + (d[1][2] - b) ** 2)[0]


def _layers(layer: Any, total: Any) -> str | None:
    lay, tot = _pos_int(layer), _pos_int(total)
    if tot:
        return f"{lay or 0}/{tot}"
    return str(lay) if lay else None


def _progress(p: Any) -> str | None:
    n = _num(p)
    return f"{n:.0f}%" if n is not None else None


def swatch_png(hex_color: str, pct: float | None = None, size: int = 160) -> bytes | None:
    """Filament colour as a round swatch (Discord thumbnail / ntfy picture); the remaining
    share as a ring around it. None when Pillow is missing or the colour is unknown."""
    c = (hex_color or "").lstrip("#")
    if len(c) < 6:
        return None
    try:
        import io

        from PIL import Image, ImageDraw

        rgb = tuple(int(c[k:k + 2], 16) for k in (0, 2, 4))
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        pad = size // 10
        d.ellipse((pad, pad, size - pad, size - pad), fill=rgb + (255,), outline=(255, 255, 255, 70), width=2)
        if pct is not None:
            ring = (4, 4, size - 4, size - 4)
            d.arc(ring, 0, 360, fill=(60, 60, 70, 255), width=6)
            col = (56, 255, 156, 255) if pct > 20 else (255, 176, 32, 255) if pct > 10 else (255, 77, 94, 255)
            d.arc(ring, -90, -90 + 360 * max(0.0, min(100.0, pct)) / 100, fill=col, width=6)
        out = io.BytesIO()
        img.save(out, "PNG")
        return out.getvalue()
    except Exception:  # noqa: BLE001 - the text goes without the picture
        return None


def build_print_started(
    job: str,
    *,
    estimate_s: float | None = None,
    filaments: Sequence[Mapping[str, Any]] | None = None,
    total_layers: int | None = None,
    image: bytes | None = None,
    url: str | None = None,
) -> Notification:
    """``estimate_s`` in SECONDS; ``filaments``: planned grams per filament (slicer estimate,
    each may carry ``label`` = the tray it comes from and ``color``)."""
    job = _job(job)
    fil, total = _filament_lines(filaments)
    est = _num(estimate_s)
    eta = None
    if est:
        end = datetime.now() + timedelta(seconds=est)
        eta = end.strftime("%H:%M") if end.date() == datetime.now().date() else end.strftime("%m.%d. %H:%M")
    fields = [(T("label.estimate"), fmt_duration(est) if est else None), (T("label.eta"), eta),
              (T("label.layers"), _pos_int(total_layers))]
    if fil:
        fields += [(T("label.filament"), fil), (T("label.total"), f"{total:.1f} g")]
    msg = T("print_started.message", job=_e(job), eta=eta or T("unknown"), duration=fmt_duration(est) if est else T("unknown"))
    return Notification(
        "print_started", T("print_started.title", job=job), msg, "info",
        _norm_fields(fields), image, url, dedupe_key=job,
    )


def build_print_finished(
    job: str,
    *,
    duration_s: float | None = None,
    filaments: Sequence[Mapping[str, Any]] | None = None,
    layer: int | None = None,
    total_layers: int | None = None,
    image: bytes | None = None,
    url: str | None = None,
    thumb: bytes | None = None,
) -> Notification:
    job = _job(job)
    fil, total = _filament_lines(filaments)
    # A finished print is on its last layer even when the final report says -1 / 0.
    lay = _pos_int(layer) or _pos_int(total_layers)
    dur = _num(duration_s)
    fields = [(T("label.time"), fmt_duration(dur) if dur is not None else None), (T("label.layers"), _layers(lay, total_layers))]
    if fil:
        fields += [(T("label.filament_used"), fil), (T("label.total"), f"{total:.1f} g")]
    msg = T("print_finished.message", job=_e(job), duration=fmt_duration(dur))
    return Notification("print_finished", T("print_finished.title", job=job), msg, "success", _norm_fields(fields), image, url,
                        dedupe_key=job, thumb=thumb)


def build_print_failed(
    job: str,
    *,
    stopped: bool = False,
    reason: str | None = None,
    progress: float | None = None,
    layer: int | None = None,
    total_layers: int | None = None,
    duration_s: float | None = None,
    filaments: Sequence[Mapping[str, Any]] | None = None,
    image: bytes | None = None,
    url: str | None = None,
) -> Notification:
    """``stopped=True``: cancelled by the user (print_stopped), else a failure (print_failed)."""
    job = _job(job)
    fil, total = _filament_lines(filaments)
    dur = _num(duration_s)
    fields = [
        (T("label.reason"), reason),
        (T("label.progress"), _progress(progress)),
        (T("label.layer"), _layers(layer, total_layers)),
        (T("label.time"), fmt_duration(dur) if dur is not None else None),
    ]
    if fil:
        fields += [(T("label.filament_used"), fil), (T("label.total"), f"{total:.1f} g")]
    if stopped:
        return Notification("print_stopped", T("print_stopped.title", job=job), T("print_stopped.message", job=_e(job)), "warning",
                            _norm_fields(fields), image, url, dedupe_key=job)
    msg = T("print_failed.message", job=_e(job), reason=_e(reason))
    return Notification("print_failed", T("print_failed.title", job=job), msg, "error", _norm_fields(fields), image, url,
                        dedupe_key=job)


def build_print_paused(
    job: str,
    *,
    reason: str | None = None,
    progress: float | None = None,
    layer: int | None = None,
    total_layers: int | None = None,
    image: bytes | None = None,
    url: str | None = None,
) -> Notification:
    """``reason``: the HMS text that explains the pause (runout, clog, Obico, ...), if any."""
    job = _job(job)
    fields = [(T("label.reason"), reason), (T("label.progress"), _progress(progress)), (T("label.layer"), _layers(layer, total_layers))]
    msg = T("print_paused.message", job=_e(job), reason=_e(reason))
    return Notification("print_paused", T("print_paused.title", job=job), msg, "warning", _norm_fields(fields), image, url,
                        dedupe_key=f"{job}:{reason or ''}")


def build_obico_warning(
    job: str, *, score: float | None, threshold: float | None = None, image: bytes | None = None,
    url: str | None = None,
) -> Notification:
    """``score`` / ``threshold`` as in parse_obico (score, low)."""
    job = _job(job)
    fields = [(T("label.score"), _score(score)), (T("label.warn_level"), _score(threshold))]
    return Notification("obico_warning", T("obico_warning.title", job=job), T("obico_warning.message", job=_e(job)),
                        "warning", _norm_fields(fields), image, url, dedupe_key=job)


def build_obico_failure(
    job: str,
    *,
    score: float | None,
    action: str | None = None,
    threshold: float | None = None,
    image: bytes | None = None,
    url: str | None = None,
) -> Notification:
    """``image``: the camera frame the detection fired on - the whole point of the alert.
    ``action``: 'notify' | 'pause' | 'pause_and_off'."""
    job = _job(job)
    a = (action or "").lower()
    act = T(f"obico_action.{'stop' if a == 'cancel' else a}") if a in ("pause", "pause_and_off", "stop", "cancel") else T("obico_action.none")
    fields = [(T("label.score"), _score(score)), (T("label.fail_level"), _score(threshold)), (T("label.action"), action or "notify")]
    return Notification("obico_failure", T("obico_failure.title", job=job), T("obico_failure.message", job=_e(job), action=act),
                        "critical", _norm_fields(fields), image, url, dedupe_key=job)


def build_filament_low(
    label: str, *, spool_name: str = "", remaining_g: float | None = None, spool_id: int | None = None,
    url: str | None = None, color: str = "", color_name: str = "", remaining_pct: float | None = None,
) -> Notification:
    """Spool in a tray is almost empty (independent of any print). ``color``: '#RRGGBB'."""
    rem_g, pct = _num(remaining_g), _num(remaining_pct)
    spool = str(spool_name or "").strip()
    col = " ".join(p for p in (_color_dot(color), color_name or "", color.upper() if color else "") if p)
    fields = [(T("label.tray"), label), (T("label.spool"), spool or None), (T("label.color"), col or None),
              (T("label.remaining"), " · ".join(p for p in (_grams(rem_g) if rem_g is not None else "",
                                                           f"{pct:.0f}%" if pct is not None else "") if p) or None)]
    msg = T("filament_low.message", tray=_e(label), spool=_e(spool), grams=_grams(rem_g),
            percent=f"{pct:.0f}%" if pct is not None else T("unknown"))
    return Notification("filament_low", T("filament_low.title", tray=label, spool=spool), msg, "warning",
                        _norm_fields(fields), None, url, dedupe_key=str(spool_id if spool_id is not None else label),
                        thumb=swatch_png(color, pct))


def build_runout_forecast(
    job: str, *, label: str, spool_name: str = "", remaining_g: float | None, needed_g: float | None,
    url: str | None = None, color: str = "", color_name: str = "",
) -> Notification:
    """The running/planned print needs more from a tray than the spool has left."""
    job = _job(job)
    need, have = _num(needed_g), _num(remaining_g)
    short = max(need - have, 0.0) if need is not None and have is not None else None
    col = " ".join(p for p in (_color_dot(color), color_name or "", color.upper() if color else "") if p)
    fields = [(T("label.tray"), label), (T("label.spool"), spool_name or None), (T("label.color"), col or None),
              (T("label.needed"), _grams(need)), (T("label.remaining"), _grams(have)),
              (T("label.short_by"), _grams(short) if short is not None else None)]
    pct = 100 * have / need if have is not None and need else None
    msg = T("runout_forecast.message", job=_e(job), tray=_e(label), spool=_e(spool_name), needed=_grams(need), grams=_grams(have))
    return Notification("runout_forecast", T("runout_forecast.title", job=job), msg,
                        "warning", _norm_fields(fields), None, url, dedupe_key=f"{job}:{label}", thumb=swatch_png(color, pct))


def build_usage_not_booked(
    job: str, *, items: Sequence[Mapping[str, Any]], url: str | None = None
) -> Notification:
    """``items``: usage.record() items. Only the ones in state 'missing' are listed (items
    without a ``state`` key are taken as missing), so the whole record can be passed."""
    job = _job(job)
    missing = [i for i in items or [] if isinstance(i, Mapping) and i.get("state", "missing") == "missing"]
    fil, total = _filament_lines(missing)
    fields = [(T("label.not_booked"), fil or None), (T("label.total"), f"{total:.1f} g" if total else None)]
    return Notification("usage_not_booked", T("usage_not_booked.title", job=job), T("usage_not_booked.message", job=_e(job)),
                        "warning", _norm_fields(fields), None, url, dedupe_key=job)


# Bambu HMS alert level = code >> 16 (Bambu Studio DevHMSItem,
# printer_mqtt.parse_hms): 1 error (task stopped), 2 warning (task paused), 3 notification,
# 0 invalid. Lower is worse.
_HMS_LEVEL: dict[int, Level] = {1: "critical", 2: "error", 3: "warning"}


def build_plate_done(
    file: str, *, done: int, total: int, next_plate: int | None, next_name: str = "", url: str | None = None,
    image: bytes | None = None, thumb: bytes | None = None,
) -> Notification:
    """Multi-plate run: a plate finished; the next one waits for the user's button."""
    name = _job(file)
    if next_plate is None:
        return Notification("plate_done", T("plate_all_done.title", total=total), T("plate_all_done.message", file=_e(name), total=total),
                            "success", [], image, url, dedupe_key=f"{name}:{done}")
    nxt = f"{next_plate}." + (f" - {next_name}" if next_name else "")
    return Notification(
        "plate_done", T("plate_done.title", done=done, total=total), T("plate_done.message", file=_e(name), done=done, total=total),
        "success", _norm_fields([(T("label.next"), nxt)]), image, url, dedupe_key=f"{name}:{done}", thumb=thumb,
    )


def build_hms_error(
    text: str, *, code: str | None = None, severity: int | None = None, job: str | None = None,
    image: bytes | None = None, url: str | None = None,
) -> Notification:
    """``code``: preferably the full 16-hex HMS code (stable dedupe key);
    ``severity``: the HMS alert level (see ``_HMS_LEVEL``)."""
    text = str(text or "").strip()
    sev_i = severity if isinstance(severity, int) else -1
    level = _HMS_LEVEL.get(sev_i, "warning")
    sev = T(f"hms.severity{sev_i}") if sev_i in _HMS_LEVEL else None
    fields = [(T("label.code"), code), (T("label.severity"), sev), (T("label.print"), job)]
    return Notification("hms_error", T("hms_error.title", text=_clip(text or code or T("unknown"), 120)),
                        T("hms_error.message", text=_e(text or code or T("unknown"))), level,
                        _norm_fields(fields), image, url, dedupe_key=code or text)


def build_summary(
    period: str, *, prints: int, ok: int, failed: int, stopped: int, hours: float, grams: float, cost: float | None,
    currency: str = "Ft", spools: Sequence[str] = (), low: Sequence[str] = (), maint: Sequence[str] = (),
    url: str | None = None,
) -> Notification:
    """Daily / weekly digest: ``period`` is the already formatted label (e.g. "ma" / "a héten")."""
    fields = [
        (T("label.prints"), f"{prints} ({T('summary.outcome', ok=ok, failed=failed, stopped=stopped)})"),
        (T("label.time"), fmt_duration(hours * 3600) if hours else "0"),
        (T("label.filament"), f"{grams:.0f} g"),
        (T("label.cost"), f"{cost:,.0f} {currency}".replace(",", " ") if cost else None),
        (T("label.spools_used"), "\n".join(spools) or None),
        (T("label.low_spools"), "\n".join(low) or None),
        (T("label.maintenance"), "\n".join(maint) or None),
    ]
    key = "summary.message" if prints else "summary.message_none"
    return Notification("summary", T("summary.title", period=period), T(key, period=period, prints=prints), "info",
                        _norm_fields(fields), None, url, dedupe_key=period)


def build_spool_wet(
    spool: str, *, hours: float, humidity: float | None, tray: str = "", color: str = "", spool_id: int | None = None,
    url: str | None = None,
) -> Notification:
    """A moisture-sensitive spool sat in humid air for a long time since it was last dried."""
    fields = [(T("label.tray"), tray or None), (T("label.humid_hours"), f"{hours:.0f}"),
              (T("label.humidity"), f"{humidity:.0f}%" if humidity is not None else None)]
    return Notification("spool_wet", T("spool_wet.title", spool=spool), T("spool_wet.message", spool=_e(spool), hours=f"{hours:.0f}"),
                        "warning", _norm_fields(fields), None, url, dedupe_key=str(spool_id or spool), thumb=swatch_png(color))


def build_printer_offline(
    *, offline_s: float | None = None, job: str | None = None, printing: bool = False, url: str | None = None
) -> Notification:
    """``printing``: it dropped off mid-print (power cut / Wi-Fi), which is worth a louder alert."""
    off = _num(offline_s)
    dur = fmt_duration(off) if off else T("unknown")
    if printing and job:
        msg = T("printer_offline.message_printing", duration=dur, job=_e(job))
    else:
        msg = T("printer_offline.message", duration=dur)
    fields = [(T("label.offline_for"), fmt_duration(off) if off else None), (T("label.print"), job if printing else None)]
    return Notification("printer_offline", T("printer_offline.title"), msg, "critical" if printing else "error",
                        _norm_fields(fields), None, url, dedupe_key=None)

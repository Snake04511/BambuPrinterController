"""Printer camera, straight from the printer (printer_camera.PrinterCamera).

Every caller (viewer, timelapse, Obico, push pictures) goes through here: one printer
socket shared by all of them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, AsyncIterator

from . import settings
from .printer_camera import PrinterCamera

if TYPE_CHECKING:
    from .poller import Engine

log = logging.getLogger("camera")


class Camera:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self.direct_cam = PrinterCamera(
            lambda: settings.get().get("printer_ip") or "",
            lambda: settings.secret("printer_access_code"),
        )
        self._print_lease: int | None = None

    @property
    def configured(self) -> bool:
        return bool(settings.get().get("printer_ip") and settings.secret("printer_access_code"))

    def keep_open(self, printing: bool) -> None:
        """During a print the timelapse / Obico take a frame every few seconds: keep the one
        printer socket open instead of reconnecting for each."""
        want = printing and self.configured
        if want and self._print_lease is None:
            self._print_lease = self.direct_cam.want("print")
        elif not want and self._print_lease is not None:
            self.direct_cam.release(self._print_lease)
            self._print_lease = None

    def restart(self) -> None:
        """Settings changed (IP / code): drop the printer socket, reconnect on demand."""
        self.direct_cam.restart()

    def status(self) -> dict:
        return {"direct": True, **(self.direct_cam.status() if self.configured else {})}

    async def close(self) -> None:
        await self.direct_cam.aclose()

    async def snapshot(self) -> tuple[bytes | None, str]:
        """Latest camera frame (JPEG)."""
        if not self.configured:
            return None, "Set the printer's IP and access code in Settings"
        return await self.direct_cam.snapshot()

    async def open_stream(self, fps: int) -> tuple[AsyncIterator[bytes] | None, str, str]:
        """MJPEG stream: (chunk iterator, content type, error)."""
        if not self.configured:
            return None, "", "Set the printer's IP and access code in Settings"
        return await self.direct_cam.open_stream(fps)

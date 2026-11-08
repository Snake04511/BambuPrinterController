"""Printer SD card: listing, file details (plates, filaments, previews), cache, delete.

Ported from the panel's SD worker (src/app/Backend.cpp):
- the card is listed over the printer's FTPS (slow, ONE data connection at a time);
- details come from, in this order: disk cache (name + size) -> the kept copy of the file ->
  Bambu Cloud history cover (list preview only) -> the file itself over FTPS;
- printer downloads never run in parallel and wait while printing (unless allowed);
- a file the user opens goes first.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import settings
from .printer_mqtt import parse_color
from .printer_ftp import PRIORITY_BACKGROUND, PRIORITY_INTERACTIVE
from .db import DATA_DIR

if TYPE_CHECKING:
    from .poller import Engine

log = logging.getLogger("sdcard")

CACHE = DATA_DIR / "cache" / "sd"
TMP = DATA_DIR / "tmp"
DL_DIR = TMP / "dl"  # files copied for a browser download (direct FTP), kept 30 min
FILES_DIR = DATA_DIR / "cache" / "files"  # copies of the printer's 3MF files (key.3mf), kept across restarts


def file_copy(key: str) -> Path:
    return FILES_DIR / f"{key}.3mf"


def keep_copy(key: str) -> Path | None:
    """The kept copy of a file, marked as just used (oldest-used go first when over the limit)."""
    p = file_copy(key)
    if p.is_file():
        try:
            os.utime(p)
        except OSError:
            pass
        return p
    return None


def prune_copies(limit_bytes: int) -> None:
    files = sorted(FILES_DIR.glob("*.3mf"), key=lambda p: p.stat().st_mtime) if FILES_DIR.is_dir() else []
    total = sum(p.stat().st_size for p in files)
    while files and total > limit_bytes:
        p = files.pop(0)
        total -= p.stat().st_size
        p.unlink(missing_ok=True)
        log.info("file copy %s removed (limit)", p.name)
CACHE.mkdir(parents=True, exist_ok=True)
TMP.mkdir(parents=True, exist_ok=True)

MAX_FILES = 100
LIST_IDLE_S = 600  # every listing is an FTP connection on the printer: rarely, plus when FILES opens
LIST_FRESH_S = 60
LIST_PRINTING_S = 300
LIST_RETRY_S = 120
FAILED_RETRY_S = 30 * 60
NEW_FILE_WAIT_S = 10 * 60  # a newly uploaded file is read in the background only after this
PRINTABLE = (".gcode.3mf", ".3mf", ".gcode")


def printable(name: str) -> bool:
    n = name.lower()
    return any(n.endswith(e) and len(n) > len(e) for e in PRINTABLE)


def stem(name: str) -> str:
    n = name.lower()
    for ext in (".gcode.3mf", ".3mf"):
        if n.endswith(ext):
            return n[: -len(ext)]
    return n


def same_job_name(a: str, b: str) -> bool:
    """Panel's sameJobName: extension stripped, alphanumerics only, case-insensitive."""
    na = re.sub(r"[^0-9a-z]", "", stem(a or ""))
    nb = re.sub(r"[^0-9a-z]", "", stem(b or ""))
    return bool(na) and na == nb


def cache_key(name: str, size: int) -> str:
    return hashlib.sha1(f"sd|{name}|{size}".encode()).hexdigest()[:24]


def detail_file(key: str) -> Path:
    return CACHE / f"{key}.json"


def png_file(key: str, plate: int | str) -> Path:
    return CACHE / f"{key}_{plate}.png"


def _num(v: Any, cast=float, default=None):
    try:
        return cast(v)
    except (TypeError, ValueError):
        return default


def parse_3mf(path: str) -> tuple[list[dict], dict[Any, bytes], str]:
    """(plates, {plate index: png}, error) from a Bambu 3MF (Metadata/slice_info.config)."""
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return [], {}, "Not a 3MF (ZIP) file"
    with zf:
        try:
            xml = zf.read("Metadata/slice_info.config")
        except KeyError:
            return [], {}, "No slice info in this file"
        except (zipfile.BadZipFile, OSError, RuntimeError):
            return [], {}, "Damaged 3MF entry"
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return [], {}, "Damaged 3MF entry"
        plates = []
        for pl in root.iter("plate"):
            meta = {m.get("key"): m.get("value") for m in pl.findall("metadata")}
            if "index" not in meta:
                continue
            obj = pl.find("object")
            objects = [
                {"id": _num(o.get("identify_id"), int, -1), "name": o.get("name") or ""}
                for o in pl.findall("object")
                if _num(o.get("identify_id"), int, -1) >= 0
            ]
            fils = []
            for i, f in enumerate(pl.findall("filament")):
                fils.append(
                    {
                        "slot": _num(f.get("id"), int, i + 1),
                        "color": parse_color(f.get("color")),
                        "type": f.get("type") or "",
                        "grams": _num(f.get("used_g"), float, 0.0),
                        "meters": _num(f.get("used_m"), float, 0.0),
                    }
                )
            plates.append(
                {
                    "index": _num(meta.get("index"), int, 1),
                    "name": (obj.get("name") if obj is not None else "") or "",
                    "print_sec": _num(meta.get("prediction"), int, -1),
                    "grams": _num(meta.get("weight"), float, -1.0),
                    "has_thumb": False,
                    "filaments": fils,
                    "objects": objects[:64],
                    "has_pick": False,
                }
            )
        pngs: dict[Any, bytes] = {}
        for pl in plates:
            try:
                pngs[pl["index"]] = zf.read(f"Metadata/plate_{pl['index']}.png")
                pl["has_thumb"] = True
            except (KeyError, zipfile.BadZipFile, OSError, RuntimeError):
                pass
            try:  # object shapes for skip objects (colour = identify_id)
                pngs[f"pick{pl['index']}"] = zf.read(f"Metadata/pick_{pl['index']}.png")
                pl["has_pick"] = True
            except (KeyError, zipfile.BadZipFile, OSError, RuntimeError):
                pass
        if not plates:
            return [], {}, "No sliced plate in this file"
        return plates, pngs, ""


class SdCard:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self.files: list[dict] = []  # name, size, date (newest first)
        self.error = ""
        self.listed = False
        self.list_loading = False
        self.version = 0
        self._next_list = 0.0
        self._empty_streak = 0
        self._failed: dict[str, tuple[float, str]] = {}  # key -> (when, error)
        self._seen_at: dict[str, float] = {}  # key -> when this process first listed the file
        self._busy_key = ""  # file being read from the printer right now
        self._prio: list[tuple[str, int, bool]] = []  # (name, size, bypass cache)
        self._wake = asyncio.Event()
        self._ftp = asyncio.Lock()
        self._dl: dict[str, dict] = {}  # browser downloads: id -> {state, name, size, token, message, t}
        self._covers_tried: set[str] = set()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------ public

    def start(self) -> None:
        for p in DL_DIR.glob("*") if DL_DIR.is_dir() else []:
            p.unlink(missing_ok=True)  # browser downloads of a previous run
        self._task = asyncio.create_task(self._run(), name="sdcard")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def reset(self) -> None:
        """Printer changed."""
        self.files, self.error, self.listed = [], "", False
        self._next_list = 0.0
        self._bump()

    def list_if_old(self) -> None:
        """FILES opened: a fresh list unless the last one is under a minute old."""
        if time.monotonic() - getattr(self, "_listed_at", 0.0) > LIST_FRESH_S:
            self._next_list = 0.0
            self._wake.set()

    def refresh(self) -> None:
        """Manual refresh: list again and retry failed files."""
        self._failed.clear()
        self._covers_tried.clear()
        self._next_list = 0.0
        self.list_loading = True
        self._bump()
        self._wake.set()

    # ------------------------------------------------------------ download of a file to the browser
    # The file is copied from the printer once (kept copy), then streamed to the browser.

    def start_download(self, name: str, size: int) -> str:
        now = time.monotonic()
        for k in [k for k, v in self._dl.items() if now - v["t"] > 1800]:
            if self._dl[k].get("path") and Path(self._dl[k]["path"]).parent == DL_DIR:
                Path(self._dl[k]["path"]).unlink(missing_ok=True)  # never a kept copy
            del self._dl[k]
        did = secrets.token_hex(8)
        self._dl[did] = {"state": "waiting", "name": name, "size": size, "token": "", "message": "", "t": now}
        asyncio.create_task(self._prepare_download(did))
        return did

    async def _prepare_download(self, did: str) -> None:
        d = self._dl[did]
        full = "/" + d["name"]
        key = cache_key(d["name"], d["size"])
        kept = keep_copy(key)
        if kept:
            d["state"], d["path"] = "ready", str(kept)
            log.info("download of '%s' ready for the browser (kept copy)", d["name"])
            return
        dest = file_copy(key)
        FILES_DIR.mkdir(parents=True, exist_ok=True)
        async with self._ftp:
            d["state"] = "preparing"
            r = await self.e.ftp.download(full, dest, expected_size=d["size"] or None, priority=PRIORITY_INTERACTIVE)
        if r.ok:
            self._prune_copies()
            d["state"], d["path"] = "ready", str(dest)
            log.info("download of '%s' ready for the browser", d["name"])
        else:
            d["state"], d["message"] = "failed", r.message

    def download_status(self, did: str) -> dict | None:
        d = self._dl.get(did)
        return {k: d[k] for k in ("state", "name", "message")} if d else None

    async def download_stream(self, did: str):
        """(chunk iterator, file name, size) of a prepared download; None when not ready."""
        d = self._dl.get(did)
        if not d or d["state"] != "ready":
            return None
        p = Path(d.get("path") or "")
        if not d.get("path") or not p.is_file():
            return None

        async def file_chunks():
            with p.open("rb") as f:
                while chunk := await asyncio.to_thread(f.read, 256 * 1024):
                    yield chunk

        return file_chunks(), d["name"], str(p.stat().st_size)

    def waiting(self) -> list[dict]:
        """New files not read yet because they arrived less than NEW_FILE_WAIT_S ago."""
        now = time.monotonic()
        out = []
        for f in self.files:
            key = cache_key(f["name"], f["size"])
            if detail_file(key).is_file() or key == self._busy_key:
                continue
            left = NEW_FILE_WAIT_S - (now - self._seen_at.get(key, 0.0))
            if left > 0:
                out.append({**f, "left": int(left)})
        return out

    def fetch_new(self) -> int:
        """"Load now": read the waiting new files right away (as if opened by hand)."""
        files = self.waiting()
        for f in reversed(files):  # newest first in the queue
            self._seen_at[cache_key(f["name"], f["size"])] = 0.0
            self.request(f["name"], f["size"])
        self._bump()
        return len(files)

    def request(self, name: str, size: int, force: bool = False) -> None:
        """A file the user opened: read it before anything else."""
        self._failed.pop(cache_key(name, size), None)
        if not any(p[0] == name and p[1] == size for p in self._prio):
            self._prio.insert(0, (name, size, force))
        self._wake.set()

    def detail(self, name: str, size: int) -> dict:
        key = cache_key(name, size)
        d = self._load_detail(key)
        if d:
            return {"state": "ready", "detail": d}
        if key in self._failed:
            return {"state": "error", "error": self._failed[key][1]}
        if self._busy_key == key:
            return {"state": "loading"}
        if self._busy_key:
            return {"state": "queued"}
        return {"state": "loading"}

    def job_file(self, job: str) -> tuple[dict, str, dict | None] | None:
        """The SD file of a printer job (same name) with its cached details, if any:
        (file, cache key, detail or None)."""
        for f in self.files:
            if same_job_name(f["name"], job):
                key = cache_key(f["name"], f["size"])
                return f, key, self._load_detail(key)
        # Not in the listing (e.g. restarted during a print: the SD card is not listed while
        # printing): this app's cache of the file still has its plates and objects.
        if job:
            best = None
            for p in CACHE.glob("*.json"):
                try:
                    d = json.loads(p.read_text())
                except (OSError, ValueError):
                    continue
                if same_job_name(str(d.get("name") or ""), job) and (best is None or p.stat().st_mtime > best[0]):
                    best = (p.stat().st_mtime, p.stem, d)
            if best:
                _, key, d = best
                return {"name": d["name"], "size": int(d.get("size") or 0), "date": ""}, key, d
        return None

    def job_plate(self, detail: dict, ids: list[int]) -> dict | None:
        """The plate being printed: the one the printer reports (gcode_file
        Metadata/plate_N.gcode), else the one whose objects match the printer's ids best;
        a single-plate file is that plate."""
        plates = detail.get("plates") or []
        if len(plates) == 1:
            return plates[0]
        idx = self.e.plate_index()
        if idx:
            hit = next((p for p in plates if p.get("index") == idx), None)
            if hit:
                return hit
        want = set(ids)
        best, score = None, 0
        for p in plates:
            n = len(want & {o["id"] for o in p.get("objects") or []})
            if n > score:
                best, score = p, n
        return best

    def png(self, key: str, what: Any) -> bytes | None:
        f = png_file(key, what)
        try:
            return f.read_bytes() if f.is_file() else None
        except OSError:
            return None

    def first_plate(self, name: str, size: int) -> int:
        d = self._load_detail(cache_key(name, size))
        return d["plates"][0]["index"] if d and d.get("plates") else 1

    def thumb(self, name: str, size: int, plate: int) -> Path | None:
        key = cache_key(name, size)
        for p in (png_file(key, plate), png_file(key, "cover")):
            if p.is_file():
                return p
        if plate != 1:
            p = png_file(key, 1)
            if p.is_file():
                return p
        return None

    def listing(self) -> dict:
        items = []
        for f in self.files:
            key = cache_key(f["name"], f["size"])
            d = self._load_detail(key)
            first = d["plates"][0] if d and d.get("plates") else None
            items.append(
                {
                    **f,
                    "print_sec": first["print_sec"] if first else -1,
                    "grams": first["grams"] if first else -1,
                    "plates": len(d["plates"]) if d else 0,
                    "thumb": bool(png_file(key, first["index"] if first else 1).is_file() or png_file(key, "cover").is_file()),
                    "thumb_plate": first["index"] if first else 1,
                    "failed": key in self._failed,
                }
            )
        return {
            "files": items,
            "error": self.error,
            "listed": self.listed,
            "loading": self.list_loading,
            "busy": self._busy_key != "",
            "version": self.version,
            "waiting": [{"name": w["name"], "left": w["left"]} for w in self.waiting()],
        }

    async def delete(self, name: str) -> dict:
        if not self.e.ftp_direct:
            return {"ok": False, "message": "Set the printer's IP and access code in Settings"}
        async with self._ftp:
            fr = await self.e.ftp.delete("/" + name, missing_ok=True)
        ok, msg = fr.ok, fr.message
        if ok:
            for f in self.files:
                if f["name"] == name:
                    file_copy(cache_key(f["name"], f["size"])).unlink(missing_ok=True)
            self.files = [f for f in self.files if f["name"] != name]
            self._bump()
            self._next_list = 0.0
            self._wake.set()
            self.e.notify(True, f"Deleted {name[:80]}")
            return {"ok": True, "message": "Deleted"}
        self.e.notify(False, f"Delete failed: {msg}")
        return {"ok": False, "message": msg}

    # ------------------------------------------------------------ internals

    def _prune_copies(self) -> None:
        prune_copies(int(settings.get().get("file_cache_gb") or 5) * 1024**3)

    def paths(self, key: str, plate: int) -> tuple[str, bytes | None]:
        """(state, gzip JSON) of a plate's wall paths for the 3D view: 'ready', 'none'
        (no G-code in the file) or 'missing' (no kept copy of the file to read them from)."""
        out = CACHE / f"{key}_paths{plate}.v3.json.gz"  # v3: [tool, object id, x, y, ...]
        if out.is_file():
            return "ready", out.read_bytes()
        src = keep_copy(key)
        if src is None:
            return "missing", None
        from . import gcodepaths

        with zipfile.ZipFile(src) as zf:
            data = gcodepaths.from_3mf(zf, plate)
        if data is None:
            return "none", None
        tmp = out.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(out)
        return "ready", data

    def _bump(self) -> None:
        self.version += 1
        self.e.bump()

    @staticmethod
    def _load_detail(key: str) -> dict | None:
        p = detail_file(key)
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _save(key: str, name: str, size: int, plates: list[dict], pngs: dict[Any, bytes], source: str) -> dict:
        for idx, data in pngs.items():
            tmp = png_file(key, idx).with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(png_file(key, idx))
        d = {"name": name, "size": size, "plates": plates, "source": source}
        tmp = detail_file(key).with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(detail_file(key))
        return d

    def _ready(self) -> bool:
        return self.e.link == "online" and self.e.ftp_direct

    def _downloads_deferred(self) -> bool:
        st = self.e.status or {}
        from .poller import is_active

        return is_active(st.get("state", "")) and not settings.get()["sd_while_printing"]

    async def _run(self) -> None:
        while True:
            try:
                did = await self._step()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("sd step failed")
                did = False
            if not did:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass

    async def _step(self) -> bool:
        if not self._ready():
            return False
        now = time.monotonic()
        if now >= self._next_list:
            await self._list()
            return True
        if self._prio:
            name, size, force = self._prio.pop(0)
            await self._read(name, size, allow_printer=True, use_cache=not force)
            return True
        if not settings.get()["file_previews"]:
            return False
        # Background: cheap sources for every file first, then the printer, newest first.
        deferred = self._downloads_deferred()
        for f in self.files:
            key = cache_key(f["name"], f["size"])
            if detail_file(key).is_file() or key in self._covers_tried:
                continue
            if key in self._failed and now - self._failed[key][0] < FAILED_RETRY_S:
                continue
            self._covers_tried.add(key)
            if await self._read(f["name"], f["size"], allow_printer=False, use_cache=False, quiet=True):
                return True
            if await self._cloud_cover(f, key):
                return True
        if deferred:
            return False
        for f in self.files:
            key = cache_key(f["name"], f["size"])
            if detail_file(key).is_file():
                continue
            if key in self._failed and now - self._failed[key][0] < FAILED_RETRY_S:
                continue
            # A file that just arrived is usually printed right away ("send and print"):
            # then the printer needs the FTP for it. Read it a bit later.
            if now - self._seen_at.get(key, 0.0) < NEW_FILE_WAIT_S:
                continue
            await self._read(f["name"], f["size"], allow_printer=True, use_cache=False)
            return True
        return False

    async def _list(self) -> None:
        first = not self.listed
        if first:
            self.list_loading = True
            self._bump()
        async with self._ftp:
            r = await self.e.ftp.list_dir("/", priority=PRIORITY_BACKGROUND)
        from .poller import is_active

        printing = is_active((self.e.status or {}).get("state", ""))
        self.list_loading = False
        if not r.ok:
            self._next_list = time.monotonic() + LIST_RETRY_S
            if not self.listed:
                self.error = r.message
            log.info("SD listing failed: %s", r.message)
            self._bump()
            return
        files = []
        for f in r.data or []:
            name = str(f.get("name") or "")
            if f.get("is_directory") or not printable(name):
                continue
            date = str(f.get("mtime") or "")[:16].replace("T", " ")
            files.append({"name": name, "size": int(f.get("size") or 0), "date": date})
        files.sort(key=lambda x: x["date"], reverse=True)
        files = files[:MAX_FILES]
        if not files and self.files:
            # The printer sometimes answers an empty listing: believe it the third time.
            self._empty_streak += 1
            if self._empty_streak < 3:
                self._next_list = time.monotonic() + LIST_RETRY_S
                self._bump()
                return
        self._empty_streak = 0
        self._next_list = time.monotonic() + (LIST_PRINTING_S if printing else LIST_IDLE_S)
        self._listed_at = time.monotonic()
        if not files:
            self._next_list = time.monotonic() + LIST_RETRY_S
        self.error = ""
        # Files there at start-up are old; later arrivals wait NEW_FILE_WAIT_S before a background read.
        now = time.monotonic()
        for f in files:
            self._seen_at.setdefault(cache_key(f["name"], f["size"]), now if self.listed else now - NEW_FILE_WAIT_S)
        self.listed = True
        if files != self.files:
            self.files = files
        self._bump()

    async def _read(self, name: str, size: int, *, allow_printer: bool, use_cache: bool, quiet: bool = False) -> bool:
        """Fills the cache for one file. Returns True when something was fetched."""
        key = cache_key(name, size)
        if use_cache and detail_file(key).is_file():
            self._bump()
            return False
        # Background pass without printer access: only files with a kept copy.
        if not keep_copy(key) and not allow_printer:
            return False
        # The kept copy, else the file itself from the printer (one at a time).
        async with self._ftp:
            self._busy_key = key
            self._bump()
            try:
                ok, err = await self._read_from_printer(name, size, key)
            finally:
                self._busy_key = ""
        if ok:
            self._failed.pop(key, None)
        else:
            self._failed[key] = (time.monotonic(), err)
            if not quiet:
                log.info("SD '%s': %s", name, err)
        self._bump()
        return True

    async def _read_from_printer(self, name: str, size: int, key: str) -> tuple[bool, str]:
        full = "/" + name
        kept = keep_copy(key)
        dest = kept or file_copy(key)
        FILES_DIR.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        if not kept:
            r = await self.e.ftp.download(full, dest, expected_size=size or None, priority=PRIORITY_BACKGROUND)
            if not r.ok:
                return False, r.message
            self._prune_copies()
        plates, pngs, err = await asyncio.to_thread(parse_3mf, str(dest))
        if err:
            dest.unlink(missing_ok=True)  # a broken copy must not stay
            return False, err
        self._save(key, name, size, plates, pngs, "file")
        log.info("SD '%s': read %s in %.0f s", name, "from the kept copy" if kept else "over FTP", time.monotonic() - t0)
        return True, ""

    async def _cloud_cover(self, f: dict, key: str) -> bool:
        """List preview from the Bambu Cloud history cover of a job with the same name."""
        if png_file(key, "cover").is_file():
            return False
        task = next((t for t in self.e.cloud_tasks if t.get("cover") and same_job_name(t["title"], f["name"])), None)
        if not task:
            return False
        data = await self.e.cloud_cover(task["id"])
        if not data:
            return False
        png_file(key, "cover").write_bytes(data)
        self._bump()
        return True

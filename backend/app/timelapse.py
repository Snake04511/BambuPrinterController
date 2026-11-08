"""Timelapse of every print, recorded by this app (camera frames + the Obico score of the
moment), watchable while the print runs; the player jumps to a frame from the score chart.

Frames come from the printer camera's snapshot (the shared connection, no FTP).
With Obico on: one frame per analysed Obico frame (~every 10 s), so each picture has its own
score. Otherwise one per layer change, at most every MIN_GAP_S, at least every MAX_GAP_S.
Frames are scaled down to FRAME_W wide to keep long prints small (~35 KB each).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import settings
from .db import DATA_DIR

if TYPE_CHECKING:
    from .poller import Engine

log = logging.getLogger("timelapse")

ROOT = DATA_DIR / "timelapse"
ROOT.mkdir(parents=True, exist_ok=True)
MIN_GAP_S = 10
MAX_GAP_S = 60
OBICO_MIN_GAP_S = 4
FRAME_W = 960
KEEP_COUNT = 30
KEEP_BYTES = 3 * 1024**3  # oldest finished recordings go first
MAX_FRAMES = 15000


def _draw_overlays(src: Path, dst: Path, meta: dict, n: int) -> None:
    """Frames with an info bar (time, layer, %, Obico score) and the score curve, for the MP4."""
    from datetime import datetime

    from PIL import Image, ImageDraw, ImageFont

    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True)
    frames = (meta.get("frames") or [])[:n]
    scores = [f[1] for f in frames]
    high = float(meta.get("high") or 0.78)
    low = float(meta.get("low") or 0.38)
    top = max([1.0, high] + [s for s in scores if s is not None]) * 1.05
    font = big = None
    for i, f in enumerate(frames):
        p = src / f"{i:05d}.jpg"
        if not p.is_file():
            continue
        img = Image.open(p).convert("RGB")
        w, h = img.size
        if font is None:
            font = ImageFont.load_default(size=max(14, h // 30))
            big = ImageFont.load_default(size=max(16, h // 24))
        bar_h = h // 7
        ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        d.rectangle((0, h - bar_h, w, h), fill=(8, 10, 14, 175))
        # score curve along the bar, frames so far bright, a cursor at "now"
        chart_top, chart_bot = h - bar_h + 6, h - 6
        def y(v: float) -> float:
            return chart_bot - (v / top) * (chart_bot - chart_top)
        for lim, col in ((low, (255, 176, 32, 140)), (high, (255, 77, 94, 140))):
            if lim > 0:
                d.line((0, y(lim), w, y(lim)), fill=col, width=1)
        pts = [(k * w / max(1, len(frames) - 1), y(s)) for k, s in enumerate(scores) if s is not None]
        if len(pts) > 1:
            d.line(pts, fill=(59, 167, 255, 110), width=2)
            done = [pt for pt in pts if pt[0] <= i * w / max(1, len(frames) - 1)]
            if len(done) > 1:
                d.line(done, fill=(59, 167, 255, 255), width=3)
        cx = i * w / max(1, len(frames) - 1)
        d.line((cx, chart_top, cx, chart_bot), fill=(56, 255, 156, 230), width=2)
        # text: time · layer · % · score, job name top-left
        t, score, _cls, layer, prog, _light = (list(f) + [None] * 6)[:6]
        txt = f"{datetime.fromtimestamp(t):%H:%M}" if t else ""
        if layer:
            txt += f"   layer {layer}"
        if prog is not None:
            txt += f"   {int(prog)}%"
        d.text((12, h - bar_h - big.size - 14), txt, font=big, fill=(255, 255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0, 200))
        if score is not None:
            col = (255, 77, 94, 255) if score >= high else (255, 176, 32, 255) if score >= low else (56, 255, 156, 255)
            s_txt = f"Obico {score:.2f}"
            tw = d.textlength(s_txt, font=big)
            d.text((w - tw - 12, h - bar_h - big.size - 14), s_txt, font=big, fill=col, stroke_width=2, stroke_fill=(0, 0, 0, 200))
        job = str(meta.get("job") or "")[:60]
        if job:
            d.text((12, 10), job, font=font, fill=(255, 255, 255, 230), stroke_width=2, stroke_fill=(0, 0, 0, 180))
        Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB").save(dst / f"{i:05d}.jpg", quality=85)


def dir_size(p: Path) -> int:
    n = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                n += os.stat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return n


def _slug(job: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", job).strip("_")[:40] or "print"


def _shrink(jpg: bytes) -> bytes:
    """Camera frame scaled to FRAME_W wide (JPEG q72); the original if it cannot be read."""
    from io import BytesIO

    from PIL import Image

    try:
        im = Image.open(BytesIO(jpg))
        if im.width > FRAME_W:
            im = im.convert("RGB").resize((FRAME_W, round(im.height * FRAME_W / im.width)), Image.LANCZOS)
        out = BytesIO()
        im.convert("RGB").save(out, "JPEG", quality=72, optimize=True)
        return out.getvalue()
    except Exception:  # noqa: BLE001
        return jpg


class Timelapse:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self.cur: dict | None = None  # meta of the recording in progress
        self._dir: Path | None = None
        self._last_t = 0.0
        self._last_layer = -1
        self._busy = False
        self._last_obico = -1  # Obico frame count at the last picture
        self.version = 0
        self._exports: dict[tuple[str, int], dict] = {}  # (id, fps) -> {state, message}
        self._export_lock = asyncio.Lock()

    # ------------------------------------------------------------ lifecycle (from printjob hooks)

    def on_boot(self, st: dict, active: bool) -> None:
        if not active:
            return
        # Restarted during a print: carry on with its recording (one recording per print,
        # pieces left by earlier restarts are joined first).
        self.merge_pieces(st["job"])
        for d in sorted(ROOT.iterdir(), reverse=True):
            m = self._load(d)
            if m and not m.get("end") and m.get("job") == st["job"]:
                self.cur, self._dir = m, d
                log.info("timelapse of '%s' resumed (%d frames)", m["job"], len(m["frames"]))
                return
        self._begin(st["job"])

    def merge_pieces(self, job: str) -> None:
        """Unfinished recordings of the same print (one per restart) -> the oldest one, frames in
        time order, renumbered."""
        pieces = []
        for d in sorted(p for p in ROOT.iterdir() if p.is_dir()):
            m = self._load(d)
            if m and m.get("job") == job and not m.get("end"):
                pieces.append((d, m))
        if len(pieces) < 2:
            return
        self.join(pieces)

    def join(self, pieces: list) -> None:
        """[(dir, meta), ...] oldest first -> one recording in the first dir; the end / result
        / archive of the last finished piece are kept."""
        base_dir, base = pieces[0]
        job = base.get("job", "")
        allf = []
        for d, m in pieces:
            for i, f in enumerate(m.get("frames") or []):
                src = d / f"{i:05d}.jpg"
                if src.is_file():
                    allf.append((f[0], src, f))
        allf.sort(key=lambda x: x[0])
        tmp = base_dir / "merge.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        for k, (_, src, _) in enumerate(allf):
            shutil.copy2(src, tmp / f"{k:05d}.jpg")
        for old in base_dir.glob("*.jpg"):
            old.unlink()
        for f in tmp.iterdir():
            f.replace(base_dir / f.name)
        tmp.rmdir()
        for v in base_dir.glob("video_*.mp4"):
            v.unlink()
        base["frames"] = [f for _, _, f in allf]
        base["start"] = min(m.get("start") or base["start"] for _, m in pieces)
        base["archive_id"] = next((m["archive_id"] for _, m in reversed(pieces) if (m.get("archive_id") or -1) >= 0), -1)
        last = pieces[-1][1]
        base["end"], base["result"] = last.get("end") or 0, last.get("result") or ""
        self._save(base_dir, base)
        for d, _ in pieces[1:]:
            shutil.rmtree(d, ignore_errors=True)
        log.info("timelapse of '%s': %d pieces joined (%d frames)", job, len(pieces), len(allf))

    def on_start(self, job: str) -> None:
        self._finish("")
        self._begin(job)

    def on_end(self, st: dict) -> None:
        self._finish(st.get("state", ""))

    def set_archive(self, job: str, archive_id: int) -> None:
        """Pairs the recording with the print's log entry (History shows it there)."""
        for d in sorted(ROOT.iterdir(), reverse=True)[:5]:
            m = self._load(d)
            if m and m.get("job") == job and m.get("archive_id", -1) < 0:
                m["archive_id"] = archive_id
                self._save(d, m)
                return

    def _begin(self, job: str) -> None:
        if not settings.get().get("record_timelapse", True):
            self.cur = self._dir = None
            return
        tid = f"{time.strftime('%Y%m%d-%H%M%S')}_{_slug(job)}"
        d = ROOT / tid
        d.mkdir(parents=True, exist_ok=True)
        o = self.e.obico or {}
        self.cur = {"id": tid, "job": job, "start": int(time.time()), "end": 0, "result": "", "archive_id": -1,
                    "low": o.get("low", 0), "high": o.get("high", 0), "frames": []}
        self._dir = d
        self._last_t, self._last_layer = 0.0, -1
        self._save(d, self.cur)
        self._prune()
        log.info("timelapse of '%s' started", job)

    def _finish(self, result: str) -> None:
        if self.cur and self._dir:
            self.cur["end"] = int(time.time())
            self.cur["result"] = result
            if not self.cur["frames"]:
                shutil.rmtree(self._dir, ignore_errors=True)
            else:
                self._save(self._dir, self.cur)
                log.info("timelapse of '%s' finished: %d frames", self.cur["job"], len(self.cur["frames"]))
        self.cur = self._dir = None
        self.version += 1

    # ------------------------------------------------------------ capture (printjob.tick)

    def tick(self, st: dict, active: bool) -> None:
        if not active or not self.cur or self._busy or st.get("state") not in ("RUNNING", "PAUSE"):
            return
        now = time.monotonic()
        layer = int(st.get("layer") or 0)
        gap = now - self._last_t
        o = self.e.obico or {}
        obico_on = bool(o.get("enabled") and o.get("running") and o.get("has_printer"))
        if obico_on and st.get("state") == "RUNNING" and gap < MAX_GAP_S:
            # A new analysed frame: the picture of that moment, with its score.
            if o.get("frames") == self._last_obico or gap < OBICO_MIN_GAP_S:
                return
        elif gap < MIN_GAP_S or (layer == self._last_layer and gap < MAX_GAP_S):
            return
        if st.get("state") == "PAUSE" and gap < MAX_GAP_S:
            return
        self._last_obico = o.get("frames", -1)
        if len(self.cur["frames"]) >= MAX_FRAMES:
            return
        self._busy = True
        self._last_t = now
        self._last_layer = layer
        asyncio.create_task(self._capture(st))

    async def _capture(self, st: dict) -> None:
        try:
            jpg, err = await self.e.camera.snapshot()
            cur, d = self.cur, self._dir
            if not jpg or not cur or not d:
                if err:
                    log.debug("timelapse frame skipped: %s", err)
                return
            n = len(cur["frames"])
            small = await asyncio.to_thread(_shrink, jpg)
            (d / f"{n:05d}.jpg").write_bytes(small)
            o = self.e.obico or {}
            score = float(o.get("score") or 0) if o.get("enabled") and o.get("cls") else None
            cur["frames"].append(
                [int(time.time()), score, o.get("cls") or "", int(st.get("layer") or 0), round(float(st.get("progress") or 0), 1),
                 bool(st.get("light"))]
            )
            if o.get("high"):
                cur["low"], cur["high"] = o.get("low", 0), o.get("high", 0)
            self._save(d, cur)
            self.version += 1
            self.e.bump()
        except OSError as ex:
            log.warning("timelapse frame not saved: %s", ex)
        finally:
            self._busy = False

    # ------------------------------------------------------------ storage

    @staticmethod
    def _load(d: Path) -> dict | None:
        try:
            return json.loads((d / "meta.json").read_text())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _save(d: Path, m: dict) -> None:
        tmp = d / "meta.tmp"
        tmp.write_text(json.dumps(m))
        tmp.replace(d / "meta.json")

    def _prune(self) -> None:
        """Oldest recordings go first: over the count / size of Settings > Storage, or while
        all of /data is over its limit (the volume itself has no quota on this host)."""
        cfg = settings.get()
        keep_count = int(cfg.get("timelapse_keep_count") or KEEP_COUNT)
        keep_bytes = int(cfg.get("timelapse_keep_gb") or KEEP_BYTES // 1024**3) * 1024**3
        dirs = sorted((p for p in ROOT.iterdir() if p.is_dir()), key=lambda p: p.name)
        sizes = {p: sum(f.stat().st_size for f in p.glob("*")) for p in dirs}
        total = sum(sizes.values())
        other = dir_size(DATA_DIR) - total  # everything that is not a timelapse
        limit = int(cfg.get("data_limit_gb") or 50) * 1024**3
        keep_bytes = min(keep_bytes, max(0, limit - other))
        while dirs and (len(dirs) > keep_count or total > keep_bytes):
            old = dirs.pop(0)
            if self._dir and old == self._dir:
                break
            total -= sizes[old]
            shutil.rmtree(old, ignore_errors=True)
            log.info("timelapse %s removed (space)", old.name)

    def path(self, tid: str) -> Path | None:
        if not re.fullmatch(r"[0-9A-Za-z_\-]+", tid or ""):
            return None
        d = ROOT / tid
        return d if d.is_dir() else None

    def meta(self, tid: str) -> dict | None:
        d = self.path(tid)
        m = self._load(d) if d else None
        if m:
            m["active"] = bool(self.cur and self.cur["id"] == tid)
        return m

    def frame(self, tid: str, n: int) -> Path | None:
        d = self.path(tid)
        f = d / f"{int(n):05d}.jpg" if d else None
        return f if f and f.is_file() else None

    def listing(self) -> list[dict]:
        out = []
        for d in sorted((p for p in ROOT.iterdir() if p.is_dir()), reverse=True):
            m = self._load(d)
            if not m:
                continue
            out.append({k: m.get(k) for k in ("id", "job", "start", "end", "result", "archive_id")} | {
                "frames": len(m.get("frames") or []),
                "active": bool(self.cur and self.cur["id"] == m["id"]),
            })
        return out

    # ------------------------------------------------------------ download as an MP4 video

    @staticmethod
    def _vname(fps: int, n: int, overlay: bool) -> str:
        return f"video_{fps}_{n}{'_o' if overlay else ''}.mp4"

    def video_file(self, tid: str, fps: int, overlay: bool = False) -> Path | None:
        """The finished MP4 of a recording (frames so far, for a running one); None if not made yet."""
        d = self.path(tid)
        m = self._load(d) if d else None
        if not m:
            return None
        f = d / self._vname(fps, len(m.get("frames") or []), overlay)
        return f if f.is_file() else None

    def export_status(self, tid: str, fps: int, overlay: bool = False) -> dict:
        job = self._exports.get((tid, fps, overlay))
        if self.video_file(tid, fps, overlay):
            return {"state": "ready", "message": ""}
        if job:
            return {"state": job["state"], "message": job["message"]}
        return {"state": "none", "message": ""}

    def start_export(self, tid: str, fps: int, overlay: bool = False) -> dict:
        d = self.path(tid)
        m = self._load(d) if d else None
        if not m or not m.get("frames"):
            return {"state": "failed", "message": "No frames"}
        if self.video_file(tid, fps, overlay):
            return {"state": "ready", "message": ""}
        job = self._exports.get((tid, fps, overlay))
        if job and job["state"] == "making":
            return {"state": "making", "message": ""}
        self._exports[(tid, fps, overlay)] = {"state": "making", "message": ""}
        asyncio.create_task(self._export(d, tid, fps, len(m["frames"]), overlay, m))
        return {"state": "making", "message": ""}

    async def _export(self, d: Path, tid: str, fps: int, n: int, overlay: bool = False, meta: dict | None = None) -> None:
        job = self._exports[(tid, fps, overlay)]
        out = d / self._vname(fps, n, overlay)
        tmp = out.with_suffix(".part.mp4")
        src = d
        if overlay:
            src = DATA_DIR / "tmp" / f"overlay_{tid}"
            try:
                await asyncio.to_thread(_draw_overlays, d, src, meta or {}, n)
            except Exception as e:  # noqa: BLE001
                shutil.rmtree(src, ignore_errors=True)
                job["state"], job["message"] = "failed", f"Overlay failed: {e}"
                return
        cmd = [
            "nice", "-n", "15", "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-framerate", str(fps), "-i", str(src / "%05d.jpg"), "-frames:v", str(n),
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-threads", "2", str(tmp),
        ]
        t0 = time.monotonic()
        if not shutil.which("ffmpeg"):
            job["state"], job["message"] = "failed", "ffmpeg is not installed on the server"
            return
        try:
            async with self._export_lock:  # one encode at a time on the small server
                proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                _, err = await proc.communicate()
            if proc.returncode != 0 or not tmp.is_file():
                raise RuntimeError((err or b"").decode(errors="replace").strip()[-200:] or f"ffmpeg exit {proc.returncode}")
            for old in d.glob(f"video_{fps}_*{'_o' if overlay else ''}.mp4"):
                if overlay == old.stem.endswith("_o"):
                    old.unlink(missing_ok=True)  # an older (shorter) video of a recording that went on
            tmp.replace(out)
            job["state"] = "ready"
            log.info("timelapse %s: %d frames -> MP4 at %d fps in %.0f s", tid, n, fps, time.monotonic() - t0)
        except FileNotFoundError:
            job["state"], job["message"] = "failed", "ffmpeg is not installed"
        except Exception as e:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            job["state"], job["message"] = "failed", str(e)
            log.warning("timelapse %s: video failed: %s", tid, e)
        finally:
            if overlay:
                shutil.rmtree(src, ignore_errors=True)

    def storage(self) -> dict:
        """Settings > Storage: what /data holds now."""
        dirs = [p for p in ROOT.iterdir() if p.is_dir()]
        tl = sum(dir_size(p) for p in dirs)
        st = os.statvfs(DATA_DIR)
        return {"data_bytes": dir_size(DATA_DIR), "timelapse_bytes": tl, "timelapse_count": len(dirs),
                "disk_free_bytes": st.f_bavail * st.f_frsize, "disk_total_bytes": st.f_blocks * st.f_frsize}

    def prune_now(self) -> None:
        self._prune()

    def current_id(self) -> str:
        return self.cur["id"] if self.cur and self.cur["frames"] else ""

    # ------------------------------------------------------------ import of the printer's own timelapse

    def import_avi(self, src: Path, archive: dict, rec: dict | None, start: float, end: float) -> dict:
        """The printer's timelapse (.avi, Motion-JPEG, one frame per layer) as a recording of a
        print: frames spread evenly over the print (the file has no times), and the Obico score
        of each moment from the print's Obico record (spread over the time it covers)."""
        data = src.read_bytes()
        jpgs = _split_mjpeg(data)
        if not jpgs:
            raise ValueError("No video frames found (expected a Bambu timelapse .avi)")
        job = str(archive.get("print_name") or archive.get("filename") or "print")
        tid = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(start))}_{_slug(job)}_printer"
        d = ROOT / tid
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        n = len(jpgs)
        span = max(1.0, end - start)
        # Obico record: points over [rs, re] (start unknown when it began mid-print: from its sample count).
        pts: list[int] = (rec.get("points") or []) if rec else []
        low = float(rec.get("low") or 0) if rec else 0.0
        high = float(rec.get("high") or 0) if rec else 0.0
        re_ = float(rec.get("end") or end) if rec else end
        rs = float(rec.get("start") or 0) if rec else 0.0
        if rec and not rs:
            rs = max(start, re_ - int(rec.get("samples") or 0) * OBICO_SAMPLE_S)
        frames = []
        for i, jpg in enumerate(jpgs):
            (d / f"{i:05d}.jpg").write_bytes(_shrink(jpg))
            t = start + (i + 0.5) / n * span
            score, cls = None, ""
            if pts and rs <= t <= re_ and re_ > rs:
                k = min(len(pts) - 1, int((t - rs) / (re_ - rs) * len(pts)))
                score = round(pts[k] / 200, 3)
                cls = "failure" if high and score >= high else "warning" if low and score >= low else "safe"
            frames.append([int(t), score, cls, i + 1, round(100 * (i + 1) / n, 1), True])
        m = {
            "id": tid,
            "job": job,
            "start": int(start),
            "end": int(end),
            "result": "FINISH" if archive.get("status") == "completed" else str(archive.get("status") or "").upper(),
            "archive_id": int(archive.get("id") or -1),
            "low": low,
            "high": high,
            "frames": frames,
            "imported": True,
            "approx": True,
        }
        self._save(d, m)
        self.version += 1
        self._prune()
        log.info("printer timelapse imported for '%s': %d frames, %d with an Obico score", job, n, sum(1 for f in frames if f[1] is not None))
        return m

    def relink(self, old_archive: int, new_archive: int) -> None:
        for d in ROOT.iterdir():
            m = self._load(d) if d.is_dir() else None
            if m and m.get("archive_id") == old_archive:
                m["archive_id"] = new_archive
                self._save(d, m)


OBICO_SAMPLE_S = 15  # Obico was polled every 15 s while those older records were made


def _split_mjpeg(data: bytes) -> list[bytes]:
    """JPEG frames of a Motion-JPEG stream (AVI chunks around them are skipped)."""
    out = []
    pos = data.find(b"\xff\xd8\xff")
    while pos >= 0:
        nxt = data.find(b"\xff\xd8\xff", pos + 3)
        chunk = data[pos : nxt if nxt >= 0 else len(data)]
        eoi = chunk.rfind(b"\xff\xd9")
        if eoi > 0:
            out.append(chunk[: eoi + 2])
        pos = nxt
    return out

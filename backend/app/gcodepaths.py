"""Outer-wall tool paths of a Bambu plate G-code, small enough to draw in the browser.

Used by the live 3D progress view: per layer, the outer (and overhang) wall polylines,
simplified (a point every ~TOL mm) and quantised to 0.1 mm. That is the silhouette of
every object, which is what the eye needs to see "how far is it"; infill and inner walls
would be most of the data and add little.

Bambu G-code: layers start with "; CHANGE_LAYER" + "; Z_HEIGHT: z", sections with
"; FEATURE: <type>", absolute XY (G90), relative E (M83), arcs as G2/G3 with I/J, tool
changes as "M620 S<n>A" / "T<n>".
"""

from __future__ import annotations

import gzip
import io
import json
import math
import re
from collections.abc import Iterable

KEEP = ("Outer wall", "Overhang wall")
TOL = 0.6  # mm between kept points
MAX_POINTS = 350_000
_NUM = re.compile(r"([XYZEIJ])(-?\d*\.?\d+)")


def _arc(x0: float, y0: float, x1: float, y1: float, i: float, j: float, cw: bool, step: float) -> list[tuple[float, float]]:
    cx, cy = x0 + i, y0 + j
    r = math.hypot(i, j)
    a0, a1 = math.atan2(y0 - cy, x0 - cx), math.atan2(y1 - cy, x1 - cx)
    if cw:
        if a1 >= a0:
            a1 -= 2 * math.pi
    elif a1 <= a0:
        a1 += 2 * math.pi
    n = max(1, int(abs(a1 - a0) * r / step))
    return [(cx + r * math.cos(a0 + (a1 - a0) * k / n), cy + r * math.sin(a0 + (a1 - a0) * k / n)) for k in range(1, n + 1)]


def extract(lines: Iterable[str], tol: float = TOL) -> dict:
    layers: list[dict] = []
    cur: dict | None = None
    path: list[float] | None = None
    keep = False
    tool = 0
    obj = -1  # the printer's object id (the one skip objects uses), -1 outside objects
    x = y = 0.0
    lx = ly = None
    bbox = [1e9, 1e9, -1e9, -1e9]
    z_next = None
    points = 0

    def close() -> None:
        nonlocal path
        if path and cur is not None and len(path) >= 6:  # tool, object + at least 2 points
            cur["p"].append(path)
        path = None

    def add(px: float, py: float, force: bool = False) -> None:
        nonlocal lx, ly, points
        if lx is not None and not force and math.hypot(px - lx, py - ly) < tol:
            return
        path.extend((int(round(px * 10)), int(round(py * 10))))
        lx, ly = px, py
        points += 1
        bbox[0], bbox[1] = min(bbox[0], px), min(bbox[1], py)
        bbox[2], bbox[3] = max(bbox[2], px), max(bbox[3], py)

    for raw in lines:
        if raw.startswith(";"):
            if raw.startswith("; CHANGE_LAYER"):
                close()
                cur = {"n": len(layers) + 1, "z": 0.0, "p": []}  # n = the printer's layer number
                layers.append(cur)
            elif raw.startswith("; Z_HEIGHT:") and cur is not None:
                try:
                    cur["z"] = round(float(raw.split(":", 1)[1]), 3)
                except ValueError:
                    pass
            elif raw.startswith("; FEATURE:"):
                close()
                keep = raw[10:].strip() in KEEP
            elif raw.startswith("; start printing object, unique label id:"):
                close()
                try:
                    obj = int(raw.rsplit(":", 1)[1])
                except ValueError:
                    obj = -1
            elif raw.startswith("; stop printing object"):
                close()
                obj = -1
            continue
        c = raw[:3]
        if c in ("G1 ", "G0 ", "G2 ", "G3 "):
            v = dict(_NUM.findall(raw.split(";", 1)[0]))
            nx = float(v["X"]) if "X" in v else x
            ny = float(v["Y"]) if "Y" in v else y
            e = float(v.get("E", 0) or 0)
            if keep and e > 0 and cur is not None and (nx != x or ny != y):
                if path is None:
                    path = [tool, obj]
                    lx = ly = None
                    add(x, y, True)
                if c in ("G2 ", "G3 ") and ("I" in v or "J" in v):
                    pts = _arc(x, y, nx, ny, float(v.get("I", 0)), float(v.get("J", 0)), c == "G2 ", max(tol, 0.3))
                    for k, (px, py) in enumerate(pts):
                        add(px, py, k == len(pts) - 1)
                else:
                    add(nx, ny)
            elif path is not None and (nx != x or ny != y):
                close()  # travel move ends the polyline
            x, y = nx, ny
        elif raw.startswith("M620 S") and "A" in raw:
            m = re.match(r"M620 S(\d+)A", raw)
            if m and int(m.group(1)) < 16:
                close()
                tool = int(m.group(1))
        elif raw[:1] == "T" and raw[1:3].strip().isdigit():
            n = int(raw[1:].split()[0])
            if n < 16:
                close()
                tool = n
    close()
    all_layers = layers
    layers = [l for l in layers if l["p"]]
    if points > MAX_POINTS and tol < 3:
        raise _TooBig(tol * 2)
    return {"v": 3, "layers_total": len(all_layers), "bbox": [round(b, 1) for b in bbox] if layers else [0, 0, 0, 0],
            "zmax": layers[-1]["z"] if layers else 0, "layers": layers}


class _TooBig(Exception):
    def __init__(self, tol: float) -> None:
        self.tol = tol


def from_3mf(zf, plate: int) -> bytes | None:
    """gzip-ed JSON of the plate's paths, or None (no G-code in this 3MF)."""
    name = f"Metadata/plate_{plate}.gcode"
    if name not in zf.namelist():
        return None
    tol = TOL
    while True:
        with zf.open(name) as f:
            try:
                data = extract(io.TextIOWrapper(f, encoding="utf-8", errors="replace"), tol)
                break
            except _TooBig as e:
                tol = e.tol
    return gzip.compress(json.dumps(data, separators=(",", ":")).encode(), 6)

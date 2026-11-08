"""AMS auto mapping + filament forecast on the server (same rules as frontend/src/ams.ts).

For thin clients (the ESP32 panel): the file's filaments (SD plates or a cloud task) ->
tray per filament slot + per tray need / now / after / state.
"""

from __future__ import annotations

from typing import Any

MAP_UNUSED = -1
MAP_MISSING = -2
MAP_EXTERNAL = 254

TEXT = {
    "missing": "Not loaded",
    "no_usage": "Filament usage not available",
    "no_spool": "Spool weight unknown",
    "short": "NOT ENOUGH",
    "low": "Low remaining",
    "enough": "Enough",
}


def tray_label(tid: int) -> str:
    if tid == MAP_EXTERNAL:
        return "Ext"
    if tid >= 128:
        return f"HT{tid - 127}"
    if tid >= 0:
        return f"{chr(65 + tid // 4)}{tid % 4 + 1}"
    return "-"


def loaded_trays(st: dict) -> list[tuple[int, dict]]:
    out = []
    for unit in st.get("ams") or []:
        for slot, t in enumerate(unit.get("trays") or []):
            if t and t.get("has_filament"):
                out.append((unit["id"] if unit["id"] >= 128 else unit["id"] * 4 + slot, t))
    ext = (st.get("ext") or [None])[0]
    if ext and ext.get("has_filament"):
        out.append((MAP_EXTERNAL, ext))
    return out


def _rgb(c: str) -> tuple[int, int, int]:
    h = (c or "#000000").lstrip("#")
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return 0, 0, 0


def _dist(a: str, b: str) -> int:
    (r1, g1, b1), (r2, g2, b2) = _rgb(a), _rgb(b)
    return (r1 - r2) ** 2 + (g1 - g2) ** 2 + (b1 - b2) ** 2


def _same_type(a: str, b: str) -> bool:
    return bool(a) and bool(b) and a.lower() == b.lower()


def merge_plates(plates: list[dict]) -> list[dict]:
    """Filaments of several plates of one file (a multi-plate run): one row per slot, grams summed."""
    by_slot: dict[int, dict] = {}
    for p in plates:
        for f in p.get("filaments") or []:
            cur = by_slot.get(f["slot"])
            if cur is None:
                by_slot[f["slot"]] = dict(f)
            elif cur["grams"] > 0 and f["grams"] > 0:
                cur["grams"] = round(cur["grams"] + f["grams"], 2)
    return [by_slot[k] for k in sorted(by_slot)]


def auto_map(st: dict, filaments: list[dict]) -> list[int]:
    """Exact type + colour, then type + closest colour; each AMS tray once; the external
    spool when nothing in the AMS matches. Index = filament slot - 1."""
    count = max([f["slot"] for f in filaments] + [0])
    out = [MAP_UNUSED] * min(count, 16)
    cands = [{"id": tid, "t": t, "used": False} for tid, t in loaded_trays(st) if tid != MAP_EXTERNAL]
    ext = (st.get("ext") or [None])[0]
    for f in filaments:
        if not 1 <= f["slot"] <= 16:
            continue
        color = (f.get("color") or "").upper()
        best = next((c for c in cands if not c["used"] and _same_type(c["t"].get("type", ""), f["type"])
                     and (c["t"].get("color") or "").upper() == color), None)
        if best is None:
            d = None
            for c in cands:
                if c["used"] or not _same_type(c["t"].get("type", ""), f["type"]):
                    continue
                dist = _dist(c["t"].get("color", ""), f.get("color", ""))
                if d is None or dist < d:
                    d, best = dist, c
        if best is not None:
            best["used"] = True
            out[f["slot"] - 1] = best["id"]
        elif ext and ext.get("has_filament") and _same_type(ext.get("type", ""), f["type"]):
            out[f["slot"] - 1] = MAP_EXTERNAL
        else:
            out[f["slot"] - 1] = MAP_MISSING
    return out


def _spool_for(assignments: list[dict], tid: int) -> dict | None:
    if tid == MAP_EXTERNAL:
        ams, tray = 255, 0
    elif tid >= 128:
        ams, tray = tid, 0
    else:
        ams, tray = tid // 4, tid % 4
    a = next((x for x in assignments if x["ams_id"] == ams and x["tray_id"] == tray), None)
    return a["spool"] if a else None


def forecast(st: dict, assignments: list[dict], filaments: list[dict], mapping: list[int],
             live: dict[str, float] | None = None) -> list[dict]:
    """Per tray: need = grams of every file filament mapped to it, now = label - used - live."""
    live = live or {}
    trays = dict(loaded_trays(st))
    rows: dict[int, dict] = {}
    missing = []
    for f in filaments:
        i = f["slot"] - 1
        tid = mapping[i] if 0 <= i < len(mapping) else MAP_UNUSED
        if tid == MAP_UNUSED:
            continue
        if tid == MAP_MISSING:
            missing.append({"tray": MAP_MISSING, "label": f"#{f['slot']}", "slots": [f["slot"]], "type": f["type"],
                            "color": f.get("color", ""), "need": f["grams"] if f["grams"] > 0 else None,
                            "now": None, "after": None, "state": "missing"})
            continue
        r = rows.get(tid)
        if r is None:
            sp = _spool_for(assignments, tid)
            now = None
            if sp and (sp.get("label_weight") or 0) > 0:
                now = round(max(0.0, sp["label_weight"] - (sp.get("weight_used") or 0) - live.get(str(tid), 0.0)), 1)
            t = trays.get(tid) or {}
            r = rows[tid] = {"tray": tid, "label": tray_label(tid), "slots": [], "type": t.get("type", f["type"]),
                             "color": t.get("color", f.get("color", "")), "spool_id": sp["id"] if sp else None,
                             "label_weight": (sp or {}).get("label_weight") or 0, "need": 0.0, "now": now,
                             "after": None, "state": "enough", "_loaded": tid in trays}
        r["slots"].append(f["slot"])
        if f["grams"] > 0 and r["need"] is not None:
            r["need"] = round(r["need"] + f["grams"], 1)
        else:
            r["need"] = None
    for r in rows.values():
        if not r.pop("_loaded"):
            r["state"] = "missing"
        elif not r["need"]:
            r["state"] = "no_usage"
        elif r["now"] is None:
            r["state"] = "no_spool"
        else:
            r["after"] = round(r["now"] - r["need"], 1)
            lw = r["label_weight"]
            r["state"] = "short" if r["after"] < 0 else "low" if r["after"] < 50 or (lw > 0 and r["after"] < lw * 0.1) else "enough"
    out = list(rows.values()) + missing
    for r in out:
        r.pop("label_weight", None)
        r["text"] = TEXT[r["state"]]
    return out


def build(st: dict, assignments: list[dict], filaments: list[dict], overrides: list[int] | None,
          live: dict[str, float] | None) -> dict:
    """overrides[slot-1]: a tray id, or -1 / missing = automatic."""
    auto = auto_map(st, filaments)
    mapping = list(auto)
    for i, t in enumerate(overrides or []):
        if i < len(mapping) and t != MAP_UNUSED:
            mapping[i] = t
    rows = forecast(st, assignments, filaments, mapping, live)
    short = [r for r in rows if r["state"] in ("short", "missing")]
    warn = "; ".join(
        f"{r['label']}: not loaded" if r["state"] == "missing"
        else f"{r['label']}: need {r['need']:.0f} g, have {r['now']:.0f} g"
        for r in short
    )
    return {
        "map": mapping,
        "auto": auto,
        "filaments": [{k: f.get(k) for k in ("slot", "type", "color", "grams")} for f in filaments],
        "rows": rows,
        "ok": not short,
        "missing": any(t == MAP_MISSING for t in mapping),
        "warning": warn,
    }


def cloud_filaments(detail: dict) -> list[dict]:
    """Rows of a cloud task detail -> the file filament shape."""
    out = []
    for i, r in enumerate(detail.get("rows") or []):
        c = str(r.get("source_color") or "").lstrip("#")[:6]
        out.append({"slot": i + 1, "color": f"#{c.upper()}" if c else "", "type": str(r.get("filament_type") or ""),
                    "grams": float(r.get("weight") or 0), "meters": 0})
    return out


def as_int_list(v: Any) -> list[int] | None:
    if not isinstance(v, list) or len(v) > 16 or not all(isinstance(x, int) and not isinstance(x, bool) for x in v):
        return None
    return v

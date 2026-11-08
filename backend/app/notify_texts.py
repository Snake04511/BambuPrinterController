"""Texts of the phone notifications (Discord / ntfy) - Hungarian by default, editable.

DEFAULTS below are the built-in texts. The user's changes are kept in
DATA_DIR/notify_texts.json (only the keys that differ) and can be edited on the Settings
page. Placeholders in {braces} are filled by notify.py; an unknown placeholder stays empty.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("notify_texts")

DEFAULTS: dict[str, str] = {
    # ---- general
    "footer": "PrinterDash · {printer}",
    "fallback_job": "Nyomtatás",
    "unknown": "?",
    # ---- durations
    "dur.lt_min": "<1 perc",
    "dur.min": "{m} perc",
    "dur.hour": "{h} óra {m:02d} perc",
    # ---- field labels
    "label.estimate": "⏱️ Becsült idő",
    "label.eta": "🏁 Kész kb.",
    "label.time": "⏱️ Nyomtatási idő",
    "label.layers": "🧱 Rétegek",
    "label.layer": "🧱 Réteg",
    "label.progress": "📊 Haladás",
    "label.filament": "🧵 Filament",
    "label.filament_used": "🧵 Felhasznált filament",
    "label.total": "⚖️ Összesen",
    "label.reason": "❗ Ok",
    "label.score": "📈 Pontszám",
    "label.warn_level": "Figyelmeztetési szint",
    "label.fail_level": "Hibaszint",
    "label.action": "Teendő",
    "label.tray": "📦 Tálca",
    "label.spool": "🧵 Spula",
    "label.color": "🎨 Szín",
    "label.remaining": "⚖️ Maradék",
    "label.needed": "📐 Szükséges",
    "label.short_by": "❗ Hiányzik",
    "label.not_booked": "Nincs lekönyvelve",
    "label.code": "Kód",
    "label.severity": "Súlyosság",
    "label.print": "🖨️ Nyomtatás",
    "label.offline_for": "⏱️ Offline ennyi ideje",
    "label.next": "➡️ Következő",
    # ---- events
    "print_started.title": "▶️ Nyomtatás elindult: {job}",
    "print_started.message": "Becsült befejezés: **{eta}** ({duration}).",
    "print_finished.title": "✅ Kész: {job}",
    "print_finished.message": "Elkészült {duration} alatt. Leveheted a tálcáról.",
    "print_failed.title": "❌ Hibával leállt: {job}",
    "print_failed.message": "A nyomtatás hibával leállt. {reason}",
    "print_stopped.title": "⏹️ Leállítva: {job}",
    "print_stopped.message": "A nyomtatást leállították.",
    "print_paused.title": "⏸️ Szünetel: {job}",
    "print_paused.message": "A nyomtatás szünetel. {reason} Nézd meg a nyomtatót.",
    "obico_warning.title": "👀 Gyanús nyomtatás: {job}",
    "obico_warning.message": "Az Obico gyanús dolgot lát. Nézz rá a kamerára.",
    "obico_failure.title": "🚨 Hibás nyomtatás észlelve: {job}",
    "obico_failure.message": "Az Obico hibát (spagetti) észlelt. {action}",
    "obico_action.pause": "A nyomtatás szüneteltetve.",
    "obico_action.pause_and_off": "A nyomtatás szüneteltetve, a konnektor lekapcsolva.",
    "obico_action.stop": "A nyomtatás leállítva.",
    "obico_action.none": "Nem történt beavatkozás - nézd meg most!",
    "filament_low.title": "🧵 Fogy a filament: {tray} {spool}",
    "filament_low.message": "A(z) **{tray}** tálcán lévő {spool} kifogyóban: **{grams}** ({percent}) maradt.",
    "runout_forecast.title": "⚠️ Kifogy a filament: {job}",
    "runout_forecast.message": "**{needed}** kell a(z) {tray} tálcáról ({spool}), de csak **{grams}** van rajta. Készíts elő egy új spulát.",
    "usage_not_booked.title": "⚖️ Nincs lekönyvelve: {job}",
    "usage_not_booked.message": "A filament-fogyás nincs lekönyvelve a spulákon. Könyveld le az appban.",
    "plate_done.title": "🍽️ {done}/{total}. tálca kész",
    "plate_done.message": "A(z) **{file}** {done}/{total}. tálcája elkészült. Vedd le a tálcát, majd indítsd a következőt az appban.",
    "plate_all_done.title": "🏁 Minden tálca kész",
    "plate_all_done.message": "A(z) **{file}** mind a {total} tálcája elkészült.",
    "hms_error.title": "🛠️ Nyomtatóhiba: {text}",
    "hms_error.message": "{text}",
    "hms.severity1": "Hiba (a nyomtatás leállt)",
    "hms.severity2": "Figyelmeztetés (a nyomtatás szünetel)",
    "hms.severity3": "Értesítés",
    "printer_offline.title": "🔌 A nyomtató nem elérhető",
    "printer_offline.message": "A nyomtató {duration} óta nem elérhető.",
    "printer_offline.message_printing": "A nyomtató {duration} óta nem elérhető - éppen a(z) **{job}** nyomtatása futott!",
    "label.prints": "🖨️ Nyomtatások",
    "label.cost": "💰 Költség",
    "label.spools_used": "🧵 Fogyott spulák",
    "label.low_spools": "⚠️ Kifogyóban",
    "label.maintenance": "🛠️ Karbantartás",
    "label.humid_hours": "💧 Órák párás levegőn",
    "label.humidity": "💧 Páratartalom most",
    "summary.title": "📊 Összefoglaló - {period}",
    "summary.message": "{period}: {prints} nyomtatás.",
    "summary.message_none": "{period} nem volt nyomtatás.",
    "summary.outcome": "{ok} kész, {failed} hibás, {stopped} leállítva",
    "summary.today": "ma",
    "summary.week": "az elmúlt hét",
    "spool_wet.title": "💧 Szárítani kellene: {spool}",
    "spool_wet.message": "A(z) **{spool}** az utolsó szárítás óta {hours} órát töltött párás levegőn. Szárítsd ki, mielőtt nyomtatsz vele, majd jelöld szárítottnak a Spools oldalon.",
    "test.title": "🧪 Teszt értesítés",
    "test.message": "Ha ezt olvasod, a PrinterDash értesítései működnek.",
}

_cache: dict[str, str] | None = None


def _file():
    from .db import DATA_DIR  # lazily: importing db opens /data

    return DATA_DIR / "notify_texts.json"


def overrides() -> dict[str, str]:
    try:
        d = json.loads(_file().read_text())
        return {k: str(v) for k, v in d.items() if k in DEFAULTS} if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}
    except Exception:  # noqa: BLE001 - no data dir (tests): built-in texts
        return {}


def texts() -> dict[str, str]:
    global _cache
    if _cache is None:
        _cache = {**DEFAULTS, **overrides()}
    return _cache


def save(changes: dict[str, Any]) -> dict[str, str]:
    """Keeps only keys that differ from the defaults; an empty value restores the default."""
    global _cache
    cur = overrides()
    for k, v in changes.items():
        if k not in DEFAULTS:
            continue
        v = str(v or "")
        if not v.strip() or v == DEFAULTS[k]:
            cur.pop(k, None)
        else:
            cur[k] = v[:1000]
    f = _file()
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=1))
    tmp.replace(f)
    _cache = None
    return cur


class _Safe(dict):
    def __missing__(self, key: str) -> str:
        return ""


def t(key: str, **kw: Any) -> str:
    tpl = texts().get(key, DEFAULTS.get(key, key))
    try:
        return " ".join(tpl.format_map(_Safe(kw)).split(" ")).replace("  ", " ").strip()
    except (ValueError, IndexError, KeyError) as e:  # a broken user template: the default
        log.warning("notify text %s is broken (%s), using the default", key, e)
        return DEFAULTS.get(key, key).format_map(_Safe(kw)).strip()

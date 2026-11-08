"""Tests for app.inventory: CRUD, tray assignment rules and idempotent booking.

Uses a throw-away DATA_DIR (set before app.db is imported), so nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

if "app.db" not in sys.modules:
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="inv-test-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app import db, inventory  # noqa: E402

@pytest.fixture(autouse=True)
def clean_tables():
    # The fixture wipes inv_* tables: refuse to run against anything but a throw-away DB
    # (e.g. another test module imported app.db first with a real DATA_DIR).
    tmp = Path(tempfile.gettempdir()).resolve()
    if tmp not in Path(db.DB_PATH).resolve().parents:
        pytest.skip(f"app.db is not in a temp dir ({db.DB_PATH}); refusing to wipe it")
    inventory._ensure()
    for t in ("inv_usage", "inv_assignment", "inv_spool"):
        db.execute(f"DELETE FROM {t}")
    db.execute("DELETE FROM sqlite_sequence WHERE name LIKE 'inv_%'")
    yield


def run(coro):
    return asyncio.run(coro)


def _mk(**kw) -> dict:
    r = inventory.create_spool({"material": "PLA", **kw})
    assert r["ok"], r
    return r["spool"]


# ---------------------------------------------------------------------------- CRUD


def test_create_and_shape():
    sp = _mk(subtype="Matte", brand="Bambu Lab", color_name="Black", color="#1a1a1a", label_weight=1000,
             core_weight=250, cost_per_kg=25, temp_min=200, temp_max=230, note="n")
    base = {"id": sp["id"], "material": "PLA", "subtype": "Matte", "brand": "Bambu Lab", "color_name": "Black",
            "color": "#1A1A1A", "label_weight": 1000, "weight_used": 0.0, "temp_min": 200, "temp_max": 230}
    assert {k: sp[k] for k in base} == base
    assert sp["core_weight"] == 250 and sp["cost_per_kg"] == 25 and sp["remaining"] == 1000
    assert sp["archived"] is False and sp["created_at"] > 0 and sp["origin"] == "local"
    assert inventory.get_spool(sp["id"]) == sp


def test_create_validation():
    assert not inventory.create_spool({})["ok"]
    assert not inventory.create_spool({"material": "PLA", "color": "red"})["ok"]
    assert not inventory.create_spool({"material": "PLA", "label_weight": -5})["ok"]
    assert not inventory.create_spool({"material": "PLA", "temp_min": 250, "temp_max": 200})["ok"]
    assert not inventory.create_spool({"material": "x" * 51})["ok"]
    assert not inventory.create_spool("nope")["ok"]  # type: ignore[arg-type]
    assert inventory.list_spools() == []


def test_patch_only_given_fields():
    sp = _mk(brand="A", color="#FF0000", temp_min=190, temp_max=220)
    r = inventory.patch_spool(sp["id"], {"weight_used": 12.345, "id": 999, "remaining": 1, "bogus": 1})
    assert r["ok"]
    after = r["spool"]
    assert after["weight_used"] == 12.35 and after["id"] == sp["id"]
    assert after["brand"] == "A" and after["color"] == "#FF0000" and after["temp_min"] == 190
    assert inventory.patch_spool(sp["id"], {"rgba": "00FF00FF"})["spool"]["color"] == "#00FF00"
    assert not inventory.patch_spool(sp["id"], {"temp_min": 250})["ok"]  # above stored max
    assert inventory.patch_spool(sp["id"], {"temp_min": 0, "temp_max": 0})["spool"]["temp_min"] == 0
    assert inventory.patch_spool(sp["id"], {"cost_per_kg": None})["spool"]["cost_per_kg"] is None
    assert not inventory.patch_spool(sp["id"], {"material": ""})["ok"]
    assert inventory.patch_spool(sp["id"], {})["message"] == "Nothing changed"
    assert not inventory.patch_spool(12345, {"brand": "x"})["ok"]


def test_archive_hides_and_unassigns():
    a, b = _mk(), _mk(material="PETG")
    assert inventory.assign(a["id"], 0, 0)["ok"]
    assert inventory.archive_spool(a["id"])["ok"]
    assert [s["id"] for s in inventory.list_spools()] == [b["id"]]
    assert {s["id"] for s in inventory.list_spools(include_archived=True)} == {a["id"], b["id"]}
    assert inventory.get_spool(a["id"])["archived"] is True
    assert inventory.assignments() == []
    assert not inventory.assign(a["id"], 0, 0)["ok"]  # archived spool cannot be loaded
    assert inventory.archive_spool(a["id"], archived=False)["ok"]
    assert inventory.get_spool(a["id"])["archived_at"] is None
    assert not inventory.archive_spool(999)["ok"]


# ---------------------------------------------------------------------------- assignments


def test_assignment_unique_per_tray_and_per_spool():
    a, b = _mk(), _mk(material="PETG")
    assert inventory.assign(a["id"], 0, 1)["ok"]
    assert inventory.assign(b["id"], 0, 1)["ok"]  # replaces a in the same tray
    got = inventory.assignments()
    assert len(got) == 1 and got[0]["spool"]["id"] == b["id"]

    r = inventory.assign(b["id"], 1, 2)  # spool moves: never in two trays
    assert r["ok"] and r["moved_from"] == {"ams_id": 0, "tray_id": 1}
    assert [(x["ams_id"], x["tray_id"]) for x in inventory.assignments()] == [(1, 2)]
    assert inventory.assign(b["id"], 1, 2)["message"] == "Already assigned"

    assert inventory.assign(a["id"], 255, 0)["ok"]
    assert inventory.assign(a["id"], 128, 0)["ok"]  # moves from external to AMS-HT
    shape = inventory.assignments()
    assert [(x["ams_id"], x["tray_id"]) for x in shape] == [(1, 2), (128, 0)]
    assert {"ams_id", "tray_id", "spool_id", "spool"} <= set(shape[0]) and {"id", "material", "color"} <= set(shape[0]["spool"])


def test_assignment_validation_and_unassign():
    a = _mk()
    for ams, tray in ((0, 4), (4, 0), (128, 1), (255, 2), (-1, 0), (254, 0)):
        assert not inventory.assign(a["id"], ams, tray)["ok"], (ams, tray)
    assert not inventory.assign(999, 0, 0)["ok"]
    assert not inventory.assign("x", 0, 0)["ok"]  # type: ignore[arg-type]
    assert inventory.assign(a["id"], 3, 3)["ok"]
    assert inventory.unassign(3, 3)["message"] == "Spool unassigned"
    assert inventory.unassign(3, 3)["ok"]  # already empty: still fine
    assert inventory.assignments() == []


# ---------------------------------------------------------------------------- booking


def test_book_usage_is_idempotent_and_atomic():
    a, b = _mk(cost_per_kg=20), _mk(material="PETG")
    items = [{"spool_id": a["id"], "grams": 10.0, "tray": 0}, {"spool_id": b["id"], "grams": 5.5},
             {"spool_id": a["id"], "grams": 2.0, "tray": 1}]  # same spool twice -> summed
    rev = inventory.revision()
    r1 = inventory.book_usage(items, job="Benchy", ref="usage-123", source="print")
    assert r1["ok"] and {x["spool_id"]: x["grams"] for x in r1["booked"]} == {a["id"]: 12.0, b["id"]: 5.5}
    assert inventory.revision() > rev
    assert inventory.get_spool(a["id"])["weight_used"] == 12.0
    assert inventory.get_spool(a["id"])["last_used"] is not None

    r2 = inventory.book_usage(items, job="Benchy", ref="usage-123", source="sync")  # pressed again
    assert r2["ok"] and r2["booked"] == [] and len(r2["skipped"]) == 2 and r2["message"] == "Already booked"
    assert inventory.get_spool(a["id"])["weight_used"] == 12.0
    assert inventory.get_spool(b["id"])["weight_used"] == 5.5

    # Explicit key wins over ref; a different key books again.
    assert inventory.book_usage([{"spool_id": a["id"], "grams": 1}], ref="usage-123", key="k2")["booked"]
    assert inventory.get_spool(a["id"])["weight_used"] == 13.0

    hist = inventory.usage_history(spool_id=a["id"])
    assert [h["grams"] for h in hist] == [1.0, 12.0]
    assert hist[1]["cost"] == 0.24 and hist[1]["job"] == "Benchy" and hist[1]["key"] == "usage-123"
    assert hist[1]["spool_name"] == "PLA" and hist[1]["source"] == "print"
    assert len(inventory.usage_history()) == 3
    assert len(inventory.usage_history(limit=1)) == 1
    assert len(inventory.usage_history(key="usage-123")) == 2


def test_book_usage_bad_items():
    a = _mk()
    r = inventory.book_usage([{"spool_id": a["id"], "grams": 3}, {"spool_id": 999, "grams": 1},
                              {"spool_id": a["id"], "grams": -1}, {"grams": 1}, "junk"], ref="r")
    assert not r["ok"] and len(r["errors"]) == 4
    assert [x["spool_id"] for x in r["booked"]] == [a["id"]]  # valid line still booked
    assert inventory.get_spool(a["id"])["weight_used"] == 3.0
    assert inventory.book_usage(None, ref="x")["message"] == "Nothing to book"  # type: ignore[arg-type]
    assert not inventory.book_usage(5, ref="x")["ok"]  # type: ignore[arg-type]


def test_book_without_key_always_books_and_archived_still_books():
    a = _mk()
    inventory.archive_spool(a["id"])
    inventory.book_usage([{"spool_id": a["id"], "grams": 1}], source="manual")
    inventory.book_usage([{"spool_id": a["id"], "grams": 1}], source="manual")
    assert inventory.get_spool(a["id"])["weight_used"] == 2.0


def test_transaction_rolls_back_on_error():
    a = _mk()
    with pytest.raises(RuntimeError):
        with inventory._tx() as conn:
            conn.execute("UPDATE inv_spool SET weight_used = 50 WHERE id=?", (a["id"],))
            raise RuntimeError("crash mid-transaction")
    assert inventory.get_spool(a["id"])["weight_used"] == 0.0  # rolled back
    assert inventory.book_usage([{"spool_id": a["id"], "grams": 1}], ref="after")["ok"]  # lock released


# ----------------------------------------------------------------------------


def test_bad_ids_return_values_never_raise():
    """int('x') / ints above 2**63 used to raise ValueError / OverflowError into the caller."""
    huge = 10**30
    assert inventory.get_spool("x") is None and inventory.get_spool(huge) is None  # type: ignore[arg-type]
    assert inventory.get_spool(True) is None  # type: ignore[arg-type]
    assert not inventory.patch_spool(huge, {"brand": "a"})["ok"]
    assert not inventory.patch_spool("x", {"brand": "a"})["ok"]  # type: ignore[arg-type]
    assert not inventory.archive_spool("x")["ok"]  # type: ignore[arg-type]
    assert not inventory.archive_spool(huge)["ok"]
    assert not inventory.assign(huge, 0, 0)["ok"]
    assert not inventory.assign(1, huge, 0)["ok"]
    assert not inventory.unassign(huge, 0)["ok"]
    assert not inventory.unassign("x", 0)["ok"]  # type: ignore[arg-type]
    assert not inventory.unassign(9, 9)["ok"]  # not a tray
    r = inventory.book_usage([{"spool_id": 1e20, "grams": 1}, {"spool_id": huge, "grams": 1}], ref="a")
    assert not r["ok"] and len(r["errors"]) == 2 and r["booked"] == []
    assert inventory.usage_history(limit="x") == []  # type: ignore[arg-type]
    assert inventory.usage_history(spool_id="x") == []  # type: ignore[arg-type]
    assert not inventory.book_usage("abc", ref="s")["ok"]  # type: ignore[arg-type]
    # Numeric strings (form / query values) still work.
    sp = _mk()
    assert inventory.get_spool(str(sp["id"]))["id"] == sp["id"]  # type: ignore[arg-type]
    assert inventory.assign(str(sp["id"]), "0", "2")["ok"]  # type: ignore[arg-type]


def test_database_errors_are_values(monkeypatch):
    """A broken DB must not propagate exceptions into the engine loop."""
    sp = _mk()

    def boom() -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(inventory, "_ensure", boom)
    assert inventory.list_spools() == [] and inventory.assignments() == []
    assert inventory.get_spool(sp["id"]) is None and inventory.usage_history() == []
    for r in (inventory.create_spool({"material": "PLA"}), inventory.patch_spool(sp["id"], {"brand": "x"}),
              inventory.archive_spool(sp["id"]), inventory.assign(sp["id"], 0, 0), inventory.unassign(0, 0),
              inventory.book_usage([{"spool_id": sp["id"], "grams": 1}], ref="r")):
        assert r["ok"] is False and r["message"] == "Database error", r
    monkeypatch.undo()
    assert inventory.get_spool(sp["id"])["weight_used"] == 0.0  # nothing was half-written


def test_failed_commit_does_not_wedge_the_connection(monkeypatch):
    """A COMMIT that fails used to leave the transaction open: every later BEGIN then failed."""
    sp = _mk()
    real = db._conn

    class FailingCommit:
        def __init__(self, conn):
            self._c = conn

        def execute(self, sql, *a):
            if sql == "COMMIT":
                raise sqlite3.OperationalError("database is locked")
            return self._c.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self._c, name)

    monkeypatch.setattr(db, "_conn", FailingCommit(real))
    r = inventory.book_usage([{"spool_id": sp["id"], "grams": 5}], ref="c1")
    assert not r["ok"] and r["message"] == "Database error"
    monkeypatch.setattr(db, "_conn", real)
    assert not real.in_transaction
    assert inventory.get_spool(sp["id"])["weight_used"] == 0.0  # rolled back
    assert inventory.book_usage([{"spool_id": sp["id"], "grams": 5}], ref="c1")["booked"]  # key not burnt
    assert inventory.get_spool(sp["id"])["weight_used"] == 5.0


def test_archive_twice_keeps_original_timestamp(monkeypatch):
    sp = _mk()
    monkeypatch.setattr(inventory, "_now", lambda: 1_700_000_000)
    assert inventory.archive_spool(sp["id"])["spool"]["archived_at"] == 1_700_000_000
    monkeypatch.setattr(inventory, "_now", lambda: 1_800_000_000)
    assert inventory.archive_spool(sp["id"])["spool"]["archived_at"] == 1_700_000_000
    assert inventory.archive_spool(sp["id"], archived=False)["spool"]["archived"] is False


def test_book_usage_default_cost_and_dust():
    a, b, c = _mk(cost_per_kg=30), _mk(), _mk(cost_per_kg=0)
    r = inventory.book_usage(
        [{"spool_id": a["id"], "grams": 100}, {"spool_id": b["id"], "grams": 100},
         {"spool_id": c["id"], "grams": 100}, {"spool_id": b["id"], "grams": 0.001}],
        ref="cost", default_cost_per_kg=20,
    )
    cost = {x["spool_id"]: x["cost"] for x in r["booked"]}
    # Spool rate wins; default only when the spool has none; an explicit 0 means "free".
    assert cost == {a["id"]: 3.0, b["id"]: 2.0, c["id"]: None}
    dust = inventory.book_usage([{"spool_id": b["id"], "grams": 0.001}], ref="dust")
    assert dust["booked"] == [] and inventory.usage_history(key="dust") == []


def test_revision_only_moves_on_real_changes():
    a = _mk()
    assert inventory.assign(a["id"], 0, 0)["ok"]
    rev = inventory.revision()
    assert inventory.assign(a["id"], 0, 0)["message"] == "Already assigned"
    assert not inventory.archive_spool(999)["ok"]
    assert inventory.unassign(1, 1)["message"] == "Tray was not assigned"
    inventory.book_usage([{"spool_id": a["id"], "grams": 1}], ref="r1")
    rev2 = inventory.revision()
    assert rev2 == rev + 1
    inventory.book_usage([{"spool_id": a["id"], "grams": 1}], ref="r1")  # repeated sync
    assert inventory.revision() == rev2


def test_zero_gram_lines_are_not_errors_and_string_zero_temp():
    a, b = _mk(), _mk()
    r = inventory.book_usage([{"spool_id": a["id"], "grams": 4}, {"spool_id": b["id"], "grams": 0}], ref="z")
    assert r["ok"] and r["errors"] == [] and [x["spool_id"] for x in r["booked"]] == [a["id"]]
    assert inventory.get_spool(b["id"])["weight_used"] == 0.0
    sp = _mk(temp_min=190, temp_max=220)
    r = inventory.patch_spool(sp["id"], {"temp_min": "0", "temp_max": "0"})
    assert r["ok"] and r["spool"]["temp_min"] == 0 and r["spool"]["temp_max"] == 0


def test_patch_reports_db_error_not_missing_spool(monkeypatch):
    sp = _mk()

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(inventory, "_rows", boom)
    r = inventory.patch_spool(sp["id"], {"brand": "x"})
    assert r == {"ok": False, "message": "Database error"}


def test_unexpected_exceptions_become_values(monkeypatch):
    def bug(*a, **k):
        raise OverflowError("boom")

    monkeypatch.setattr(inventory, "_rows", bug)
    assert inventory.list_spools() == [] and inventory.usage_history() == []

from app import forecast as fc
from app.printer_hms import pause_info


def tray(t, c, has=True):
    return {"type": t, "color": c, "has_filament": has}


ST = {
    "state": "IDLE",
    "ams": [{"id": 0, "trays": [tray("PLA", "#FF0000"), tray("PLA", "#00FF00"), tray("PETG", "#000000"), None]}],
    "ext": [tray("TPU", "#FFFFFF")],
}
ASSIGN = [{"ams_id": 0, "tray_id": 0, "spool": {"id": 7, "label_weight": 1000, "weight_used": 900}}]


def fil(slot, t, c, g):
    return {"slot": slot, "type": t, "color": c, "grams": g, "meters": 0}


def test_auto_map_exact_closest_ext_missing():
    fils = [fil(1, "PLA", "#00ff00", 10), fil(2, "PLA", "#FE0101", 10), fil(3, "TPU", "#123456", 5), fil(4, "ABS", "#000000", 5)]
    assert fc.auto_map(ST, fils) == [1, 0, 254, fc.MAP_MISSING]


def test_forecast_short_with_live_use_and_overrides():
    r = fc.build(ST, ASSIGN, [fil(1, "PLA", "#FF0000", 80)], None, {"0": 30})
    row = r["rows"][0]
    assert (row["label"], row["now"], row["after"], row["state"]) == ("A1", 70.0, -10.0, "short")
    assert not r["ok"] and "need 80 g, have 70 g" in r["warning"]
    r = fc.build(ST, ASSIGN, [fil(1, "PLA", "#FF0000", 80)], [1], None)
    assert r["map"] == [1] and r["rows"][0]["state"] == "no_spool" and r["ok"]


def test_merge_plates_sums_grams():
    m = fc.merge_plates([{"filaments": [fil(1, "PLA", "#FF0000", 10)]}, {"filaments": [fil(1, "PLA", "#FF0000", 15), fil(2, "PLA", "#00FF00", 3)]}])
    assert [(f["slot"], f["grams"]) for f in m] == [(1, 25), (2, 3)]


def test_cloud_filaments():
    assert fc.cloud_filaments({"rows": [{"source_color": "ff0000ff", "filament_type": "PLA", "weight": 12.5}]}) == [
        fil(1, "PLA", "#FF0000", 12.5)
    ]


def test_pause_info():
    assert pause_info({"state": "RUNNING"}) is None
    assert pause_info({"state": "PAUSE", "hms": []})["reason"] == "user"
    p = pause_info({"state": "PAUSE", "hms": [{"severity": 2, "text": "AMS filament ran out."}]})
    assert p["reason"] == "runout" and p["message"] == "AMS filament ran out."

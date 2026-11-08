"""Tests for backend/app/printer_mqtt.py.

Two layers:
  * pure unit tests: canned P1S reports (full pushall + partial deltas) fed through
    ``ingest``; command payloads checked through a fake paho client's publish().
  * integration tests: a tiny MQTT 3.1.1 broker over TLS on 127.0.0.1 (self-signed cert
    from the openssl CLI) - enough of the protocol (CONNECT/SUBSCRIBE/PUBLISH qos0+1/
    PING/DISCONNECT) to exercise connect, auth refusal, pushall-on-connect, reconnect
    after a dropped link and the stale-session rebuild with the real paho client.

Nothing talks to the real printer. Test-only dep: pytest.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# The normalized status / tray keys the rest of the app (and the panel API) reads.
STATUS_KEYS = {
    "ams", "awaiting_plate_clear", "bed", "bed_target", "chamber", "chamber_heater", "chamber_target", "connected",
    "door_open", "ext", "fan_aux", "fan_chamber", "fan_part", "has_chamber", "hms", "hms_severity", "job", "layer",
    "light", "nozzle", "nozzle_target", "progress", "remaining_min", "speed_level", "state", "timelapse",
    "total_layers", "tray_now", "wifi_signal",
}
TRAY_KEYS = {"color", "has_filament", "id", "info_idx", "k", "remain", "rfid", "state", "sub_brands", "temp_max", "temp_min", "type"}
from app import printer_mqtt as pm  # noqa: E402
from app.printer_mqtt import PrinterMqtt  # noqa: E402

SERIAL = "01P00TEST000001"
CODE = "87654321"

# --------------------------------------------------------------------------- canned reports


def tray(tid: int, ttype: str, color: str, remain: int, **kw: Any) -> dict:
    t = {
        "id": str(tid),
        "tray_type": ttype,
        "tray_sub_brands": kw.pop("sub", ""),
        "tray_color": color,
        "tray_info_idx": kw.pop("idx", "GFA00"),
        "tray_id_name": "",
        "tag_uid": kw.pop("uid", "0000000000000000"),
        "tray_uuid": "00000000000000000000000000000000",
        "remain": remain,
        "k": 0.02,
        "n": 1,
        "cali_idx": -1,
        "nozzle_temp_min": "190",
        "nozzle_temp_max": "230",
        "tray_weight": "1000",
        "state": 11,
    }
    t.update(kw)
    return t


def full_report() -> dict:
    """A P1S push_status as answered to pushall (the fields status() reads)."""
    return {
        "print": {
            "command": "push_status",
            "msg": 0,
            "sequence_id": "2021",
            "gcode_state": "RUNNING",
            "mc_percent": 42,
            "mc_remaining_time": 37,
            "mc_print_stage": "2",
            "mc_print_sub_stage": 0,
            "layer_num": 55,
            "total_layer_num": 150,
            "nozzle_temper": 219.8,
            "nozzle_target_temper": 220.0,
            "bed_temper": 59.9,
            "bed_target_temper": 60.0,
            "chamber_temper": 5,
            "lights_report": [{"node": "chamber_light", "mode": "on"}, {"node": "work_light", "mode": "flashing"}],
            "ipcam": {"ipcam_dev": "1", "ipcam_record": "enable", "timelapse": "disable", "resolution": "1080p"},
            "xcam": {"allow_skip_parts": False, "buildplate_marker_detector": True},
            "spd_lvl": 2,
            "spd_mag": 100,
            "cooling_fan_speed": "15",
            "big_fan1_speed": "10",
            "big_fan2_speed": "0",
            "heatbreak_fan_speed": "15",
            "wifi_signal": "-52dBm",
            "stat": "46258008",
            "home_flag": 0x1D8,
            "sdcard": True,
            "nozzle_diameter": "0.4",
            "nozzle_type": "stainless_steel",
            "print_type": "local",
            "subtask_name": "Benchy",
            "subtask_id": "123456",
            "task_id": "123456",
            "gcode_file": "/data/Metadata/plate_1.gcode",
            "stg_cur": 0,
            "s_obj": [],
            "hms": [],
            "print_error": 0,
            "ams": {
                "ams": [
                    {
                        "id": "0",
                        "humidity": "4",
                        "humidity_raw": "23",
                        "temp": "24.5",
                        "tray": [
                            tray(0, "PLA", "FF0000FF", 80, sub="PLA Basic", uid="A1B2C3D4E5F60708"),
                            tray(1, "PETG", "00FF00FF", 55),
                            tray(2, "PLA", "0000FFFF", -1),
                            {"id": "3", "state": 0, "tray_type": "", "remain": 0},
                        ],
                    }
                ],
                "ams_exist_bits": "1",
                "tray_exist_bits": "7",
                "tray_is_bbl_bits": "1",
                "tray_now": "1",
                "tray_pre": "1",
                "tray_tar": "1",
                "version": 5,
                "power_on_flag": True,
                "insert_flag": True,
            },
            "vt_tray": {
                "id": "254",
                "tray_type": "TPU",
                "tray_color": "FFFFFFFF",
                "tray_info_idx": "GFU99",
                "tag_uid": "0000000000000000",
                "remain": 0,
                "k": 0.02,
                "nozzle_temp_min": "200",
                "nozzle_temp_max": "250",
            },
        }
    }


def delta(**fields: Any) -> dict:
    return {"print": {"command": "push_status", "msg": 1, "sequence_id": "2022", **fields}}


def fresh(**kw: Any) -> PrinterMqtt:
    p = PrinterMqtt("127.0.0.1", SERIAL, CODE, **kw)
    p.ingest(full_report())
    return p


# --------------------------------------------------------------------------- status shape / full report


def test_status_keys() -> None:
    got = PrinterMqtt().status()
    assert set(got) == STATUS_KEYS
    # Same semantics for an empty state: no AMS, no faults, unknown tray.
    assert got["ams"] == [] and got["ext"] == [] and got["hms"] == []
    assert got["tray_now"] == 255 and got["speed_level"] == 2


def test_full_report_status() -> None:
    p = fresh()
    st = p.status()
    tray_keys = TRAY_KEYS
    assert st["connected"] is False  # no link in a unit test
    assert st["state"] == "RUNNING"
    assert st["job"] == "Benchy"
    assert st["progress"] == 42.0 and st["remaining_min"] == 37
    assert st["layer"] == 55 and st["total_layers"] == 150
    assert (st["nozzle"], st["nozzle_target"], st["bed"], st["bed_target"]) == (219.8, 220.0, 59.9, 60.0)
    # P1S: no chamber sensor -> chamber_temper is dropped.
    assert st["has_chamber"] is False and st["chamber"] == 0.0 and st["chamber_target"] == 0.0
    assert st["chamber_heater"] is False
    assert st["light"] is True and st["timelapse"] is False
    assert st["speed_level"] == 2
    assert (st["fan_part"], st["fan_aux"], st["fan_chamber"]) == (100, 67, 0)
    assert st["wifi_signal"] == "-52"
    assert st["door_open"] is False and st["awaiting_plate_clear"] is False
    assert st["tray_now"] == 1
    assert st["hms"] == [] and st["hms_severity"] == 0

    assert len(st["ams"]) == 1
    a = st["ams"][0]
    assert a["id"] == 0 and a["humidity"] == 23 and a["temp"] == 24.5 and a["is_ht"] is False
    t0, t1, t2, t3 = a["trays"]
    assert set(t0) == tray_keys
    assert t0 == {
        "id": 0, "type": "PLA", "sub_brands": "PLA Basic", "info_idx": "GFA00", "has_filament": True,
        "color": "#FF0000", "remain": 80, "state": 11, "temp_min": 190, "temp_max": 230, "k": 0.02, "rfid": True,
    }  # fmt: skip
    assert t1["type"] == "PETG" and t1["color"] == "#00FF00" and t1["rfid"] is False
    assert t2["remain"] == -1  # unknown remain stays -1 (not RFID)
    assert t3["type"] == "" and t3["has_filament"] is False

    assert len(st["ext"]) == 1
    e = st["ext"][0]
    assert set(e) == tray_keys
    assert e["id"] == 254 and e["type"] == "TPU" and e["color"] == "#FFFFFF" and e["temp_max"] == 250


def test_job_falls_back_to_gcode_file() -> None:
    p = PrinterMqtt()
    p.ingest(delta(gcode_state="IDLE", gcode_file="cube.gcode.3mf"))
    assert p.status()["job"] == "cube.gcode.3mf"
    assert p.status()["state"] == "IDLE"


# --------------------------------------------------------------------------- delta merging


def test_partial_delta_keeps_everything_else() -> None:
    p = fresh()
    before = p.status()
    assert p.ingest(delta(mc_percent=43, mc_remaining_time=36, nozzle_temper=220.1)) is True
    st = p.status()
    assert st["progress"] == 43.0 and st["remaining_min"] == 36 and st["nozzle"] == 220.1
    for k in ("job", "layer", "total_layers", "bed", "ams", "ext", "light", "fan_part", "tray_now"):
        assert st[k] == before[k], k


def test_identical_delta_reports_no_change() -> None:
    calls: list[int] = []
    p = fresh(on_change=lambda: calls.append(1))
    calls.clear()
    assert p.ingest(delta(mc_percent=42)) is False
    assert calls == []
    assert p.ingest(delta(mc_percent=44)) is True
    assert calls == [1]


def test_ams_tray_delta_merges_by_id() -> None:
    p = fresh()
    p.ingest(delta(ams={"ams": [{"id": "0", "humidity_raw": "30", "tray": [{"id": "1", "remain": 40}]}], "tray_now": "2"}))
    a = p.status()["ams"][0]
    assert a["humidity"] == 30 and a["temp"] == 24.5
    assert a["trays"][1]["remain"] == 40 and a["trays"][1]["type"] == "PETG"
    assert a["trays"][0]["type"] == "PLA" and a["trays"][2]["type"] == "PLA"
    assert p.status()["tray_now"] == 2


def test_tray_placeholders_do_not_wipe_known_data() -> None:
    p = fresh()
    # Placeholders of non-"always update" fields are ignored (tray_color / sub_brands
    # are on the always-update list, so they are not sent here).
    p.ingest(delta(ams={"ams": [{"id": "0", "tray": [{"id": "0", "tag_uid": "0000000000000000", "tray_weight": "", "state": 11}]}]}))
    t0 = p.status()["ams"][0]["trays"][0]
    assert t0["rfid"] is True and t0["color"] == "#FF0000" and t0["sub_brands"] == "PLA Basic"
    assert p._ams_units[0]["tray"][0]["tray_weight"] == "1000"


def test_tray_explicit_empty_type_clears_slot() -> None:
    p = fresh()
    p.ingest(delta(ams={"ams": [{"id": "0", "tray": [{"id": "1", "tray_type": "", "tray_color": "", "state": 0}]}]}))
    t1 = p.status()["ams"][0]["trays"][1]
    assert t1["type"] == "" and t1["has_filament"] is False and t1["color"] == ""


def test_tray_exist_bits_clear_removed_spool() -> None:
    p = fresh()
    p.ingest(delta(ams={"tray_exist_bits": "5"}))  # slot 1 bit dropped
    trays = p.status()["ams"][0]["trays"]
    assert trays[1]["type"] == "" and trays[1]["state"] == 9 and trays[1]["remain"] == 0
    assert trays[0]["type"] == "PLA" and trays[2]["type"] == "PLA"


def test_tray_exist_bits_power_off_pattern_ignored() -> None:
    p = fresh()
    p.ingest(delta(ams={"tray_exist_bits": "0", "ams_exist_bits": "0", "power_on_flag": False}))
    st = p.status()
    assert len(st["ams"]) == 1 and st["ams"][0]["trays"][0]["type"] == "PLA"


def test_ams_unit_detached_is_dropped() -> None:
    p = fresh()
    p.ingest(delta(ams={"ams": [], "ams_exist_bits": "0", "power_on_flag": True}))
    assert p.status()["ams"] == []


def test_vt_tray_partial_delta() -> None:
    p = fresh()
    p.ingest(delta(vt_tray={"id": "254", "remain": 80}))
    e = p.status()["ext"][0]
    assert e["remain"] == 80 and e["type"] == "TPU"


def test_lists_replace_and_timelapse_sources() -> None:
    p = fresh()
    p.ingest(delta(lights_report=[{"node": "chamber_light", "mode": "off"}]))
    assert p.status()["light"] is False
    p.ingest(delta(ipcam={"timelapse": "enable"}))
    st = p.status()
    assert st["timelapse"] is True
    assert p.details()["ipcam_record"] is True  # nested dict merged, not replaced


def test_fans_speed_wifi_door() -> None:
    p = fresh()
    p.ingest(delta(cooling_fan_speed="0", big_fan1_speed="255", big_fan2_speed="7", spd_lvl=4, wifi_signal="-70dBm", stat="46A58008"))
    st = p.status()
    assert (st["fan_part"], st["fan_aux"], st["fan_chamber"]) == (0, 100, 47)
    assert st["speed_level"] == 4 and st["wifi_signal"] == "-70" and st["door_open"] is True


def test_hms_and_print_error_text_and_severity() -> None:
    p = fresh()
    p.ingest(delta(hms=[{"attr": 0x07000100, "code": 0x00020002}], print_error=0x03008001))
    st = p.status()
    assert st["hms"] == [
        {"severity": 2, "text": "The AMS A assist motor is overloaded. The filament may be tangled or stuck."},
        {"severity": 2, "text": "Printing paused due to the pause command added to the printing file."},
    ]
    assert st["hms_severity"] == 2
    faults = p.faults()
    assert faults[0]["full_code"] == "0700010000020002" and faults[0]["short_code"] == "0700_0002"
    assert faults[1]["full_code"] == "03008001"


def test_hms_unknown_code_falls_back_to_full_code_and_worst_severity() -> None:
    p = fresh()
    p.ingest(delta(hms=[{"attr": 0x0C00FF00, "code": 0x0003FFFE}, {"attr": 0x07000100, "code": 0x00010001}]))
    st = p.status()
    assert st["hms"][0] == {"severity": 3, "text": "0C00FF000003FFFE"}
    assert st["hms_severity"] == 1


def test_hms_filters_status_codes_cancel_echo_and_duplicates() -> None:
    p = fresh()
    p.ingest(
        delta(
            hms=[{"attr": 0x03000100, "code": 0x00010003}, {"attr": 0x05000100, "code": 0x0003400E}],
            print_error=0x03000003,  # < 0x4000: phase indicator, not a fault
        )
    )
    assert [h["severity"] for h in p.status()["hms"]] == [1]
    p.ingest(delta(hms=[{"attr": 0x03000100, "code": 0x00028001}], print_error=0x03008001))  # same MMMM_EEEE
    assert len(p.status()["hms"]) == 1
    p.ingest(delta(hms=[], print_error=0x0300400C))  # user cancel echo
    assert p.status()["hms"] == []


def test_finish_keeps_total_layers_new_print_resets_job_state() -> None:
    fake = FakeClient()
    p = fresh()
    attach(p, fake)
    p.ingest(delta(gcode_state="FINISH", mc_percent=100, layer_num=150, total_layer_num=0,
                   hms=[{"attr": 0x07000100, "code": 0x00020002}]))  # fmt: skip
    st = p.status()
    assert st["state"] == "FINISH" and st["total_layers"] == 150 and len(st["hms"]) == 1
    p.ingest(delta(gcode_state="PREPARE", mc_percent=0, layer_num=0))
    st = p.status()
    assert st["state"] == "PREPARE" and st["total_layers"] == 0 and st["hms"] == []
    assert fake.payloads()[-1] == {"pushing": {"command": "pushall", "sequence_id": "0"}}


def test_command_ack_is_not_state() -> None:
    p = fresh()
    p.ingest({"print": {"command": "project_file", "sequence_id": "20000", "result": "FAIL", "reason": "busy",
                        "timelapse": True, "gcode_state": "IDLE"}})  # fmt: skip
    st = p.status()
    assert st["timelapse"] is False and st["state"] == "RUNNING"
    assert p.last_ack("project_file")["result"] == "FAIL"


def test_get_version_firmware() -> None:
    p = fresh()
    p.ingest({"info": {"command": "get_version", "module": [{"name": "ota", "sw_ver": "01.08.02.00"}, {"name": "ams/0", "sw_ver": "00.00.06.49"}]}})
    assert p.details()["firmware"] == {"ota": "01.08.02.00", "ams/0": "00.00.06.49"}


def test_malformed_frames_never_raise() -> None:
    p = fresh()
    for bad in (
        {"print": "x"},
        {"print": {"ams": "nope", "vt_tray": 5, "hms": "x", "lights_report": None, "mc_percent": "abc"}},
        {"print": {"ams": {"ams": [None, {"id": None}, {"id": "0", "tray": [None, {"id": None}, "x"]}]}}},
        {"print": {"layer_num": "zz", "total_layer_num": None, "spd_lvl": True, "wifi_signal": "weak"}},
        {"info": {"command": "get_version", "module": "x"}},
        [],
    ):
        p.ingest(bad)  # type: ignore[arg-type]
        st = p.status()
        assert set(st) == STATUS_KEYS
    assert p.status()["progress"] == 0.0  # "abc" -> default


def test_async_on_change_is_scheduled() -> None:
    async def main() -> None:
        hits: list[str] = []

        async def cb() -> None:
            hits.append("x")

        p = PrinterMqtt(on_change=cb)
        p._loop = asyncio.get_running_loop()
        p.ingest(full_report())
        await asyncio.sleep(0)
        assert hits == ["x"]

    asyncio.run(main())


def test_on_change_exception_is_swallowed() -> None:
    def boom() -> None:
        raise RuntimeError("x")

    p = PrinterMqtt(on_change=boom)
    assert p.ingest(full_report()) is True


# --------------------------------------------------------------------------- commands (mocked publish)


class _Info:
    def __init__(self, rc: int) -> None:
        self.rc = rc


class FakeClient:
    def __init__(self, rc: int = 0) -> None:
        self.rc = rc
        self.sent: list[tuple[str, str, int]] = []

    def publish(self, topic: str, payload: str, qos: int = 0) -> _Info:
        self.sent.append((topic, payload, qos))
        return _Info(self.rc)

    def payloads(self) -> list[dict]:
        return [json.loads(s[1]) for s in self.sent]


def attach(p: PrinterMqtt, fake: FakeClient) -> None:
    p._client = fake  # type: ignore[assignment]
    p._connected = True


def connected(**kw: Any) -> tuple[PrinterMqtt, FakeClient]:
    p = fresh(**kw)
    fake = FakeClient()
    attach(p, fake)
    return p, fake


def last(fake: FakeClient) -> dict:
    return fake.payloads()[-1]


def test_not_connected_returns_error() -> None:
    p = fresh()
    r = p.pause()
    assert r.ok is False and "not connected" in r.message


def test_publish_failure_returns_error() -> None:
    p = fresh()
    attach(p, FakeClient(rc=pm.mqtt.MQTT_ERR_NO_CONN))
    r = p.resume()
    assert r.ok is False and "failed" in r.message


def test_topic_and_qos() -> None:
    p, fake = connected()
    assert p.pause().ok
    assert fake.sent[-1][0] == f"device/{SERIAL}/request" and fake.sent[-1][2] == 1


def test_print_control_payloads() -> None:
    p, fake = connected()
    p.pause()
    assert last(fake) == {"print": {"command": "pause", "sequence_id": "0"}}
    p.resume()
    assert last(fake) == {"print": {"command": "resume", "sequence_id": "0"}}
    p.stop_print()
    assert last(fake) == {"print": {"command": "stop", "sequence_id": "0"}}
    p.request_pushall()
    assert last(fake) == {"pushing": {"command": "pushall", "sequence_id": "0"}}


def test_clear_hms_clears_locally() -> None:
    p, fake = connected()
    p.ingest(delta(hms=[{"attr": 0x07000100, "code": 0x00020002}], print_error=0x03008001))
    assert p.clear_hms().ok
    assert last(fake) == {"print": {"command": "clean_print_error", "sequence_id": "0"}}
    assert p.status()["hms"] == []


def test_light_switches_both_nodes() -> None:
    p, fake = connected()
    assert p.set_light(False).ok
    a, b = fake.payloads()[-2:]
    for msg, node in ((a, "chamber_light"), (b, "chamber_light2")):
        s = msg["system"]
        seq = s.pop("sequence_id")
        assert seq.isdigit()
        assert s == {"command": "ledctrl", "led_node": node, "led_mode": "off", "led_on_time": 500,
                     "led_off_time": 500, "loop_times": 0, "interval_time": 0}  # fmt: skip
    p.set_light(True)
    assert last(fake)["system"]["led_mode"] == "on"


def gcode_of(msg: dict) -> str:
    pr = msg["print"]
    assert pr["command"] == "gcode_line" and pr["sequence_id"].isdigit()
    # every gcode_line must end in a newline, or the firmware drops its last line
    assert pr["param"].endswith("\n") and not pr["param"].endswith("\n\n")
    return pr["param"][:-1]


def test_temperature_commands() -> None:
    p, fake = connected()
    assert p.set_nozzle_temp(215).ok and gcode_of(last(fake)) == "M104 S215"
    assert p.set_bed_temp(65).ok and gcode_of(last(fake)) == "M140 S65"
    n = len(fake.sent)
    assert not p.set_nozzle_temp(400).ok and not p.set_bed_temp(-1).ok
    r = p.set_chamber_temp(40)  # P1S has no chamber heater
    assert not r.ok and "chamber heater" in r.message
    assert len(fake.sent) == n
    h2, f2 = connected(model="H2D")
    assert h2.set_chamber_temp(40).ok and gcode_of(last(f2)) == "M141 S40"


def test_fan_commands() -> None:
    p, fake = connected()
    assert p.set_fan("part", 100).ok and gcode_of(last(fake)) == "M106 P1 S255"
    assert p.set_fan("aux", 50).ok and gcode_of(last(fake)) == "M106 P2 S128"
    assert p.set_fan("chamber", 0).ok and gcode_of(last(fake)) == "M106 P3 S0"
    assert not p.set_fan("hotend", 10).ok and not p.set_fan("part", 101).ok


def test_speed_level() -> None:
    p, fake = connected()
    assert p.set_speed_level(3).ok
    assert last(fake) == {"print": {"command": "print_speed", "param": "3", "sequence_id": "0"}}
    assert not p.set_speed_level(5).ok and not p.set_speed_level(0).ok


def test_motion_commands() -> None:
    p, fake = connected()
    assert p.home().ok and gcode_of(last(fake)) == "G28"
    assert p.jog_xy(10, 0).ok and gcode_of(last(fake)) == "G91\nG1 X10.00 F6000\nG90"
    assert p.jog_xy(-1.5, 2).ok and gcode_of(last(fake)) == "G91\nG1 X-1.50 Y2.00 F6000\nG90"
    assert p.jog_z(-10).ok and gcode_of(last(fake)) == "G91\nG1 Z-10.00 F600\nG90"
    assert p.extrude(10).ok and gcode_of(last(fake)) == "M83\nG1 E10.00 F300\nM82"
    assert p.extrude(-5).ok and gcode_of(last(fake)) == "M83\nG1 E-5.00 F300\nM82"
    assert not p.jog_xy(0, 0).ok and not p.jog_z(0).ok and not p.extrude(500).ok


def test_sequence_ids_increase() -> None:
    p, fake = connected()
    p.home()
    p.home()
    a, b = (int(m["print"]["sequence_id"]) for m in fake.payloads()[-2:])
    assert b == a + 1


def test_ams_load_unload() -> None:
    p, fake = connected()
    assert p.ams_load(6).ok
    pr = last(fake)["print"]
    pr.pop("sequence_id")
    assert pr == {"command": "ams_change_filament", "ams_id": 1, "slot_id": 2, "target": 6, "curr_temp": -1, "tar_temp": -1}
    p.ams_load(254)
    pr = last(fake)["print"]
    assert (pr["ams_id"], pr["slot_id"], pr["target"]) == (255, 254, 254)
    p.ams_load(128)
    pr = last(fake)["print"]
    assert (pr["ams_id"], pr["slot_id"], pr["target"]) == (128, 0, 128)
    assert not p.ams_load(20).ok

    # tray_now 1 (AMS 0), nozzle 219.8 -> unload at 219
    assert p.ams_unload().ok
    pr = last(fake)["print"]
    pr.pop("sequence_id")
    assert pr == {"command": "ams_change_filament", "ams_id": 0, "slot_id": 255, "target": 255, "curr_temp": 219, "tar_temp": 219}
    p.ingest(delta(nozzle_temper=30.0, ams={"tray_now": "254"}))
    p.ams_unload()
    pr = last(fake)["print"]
    assert pr["ams_id"] == 255 and pr["curr_temp"] == 210 and pr["tar_temp"] == 210


def test_skip_objects() -> None:
    p, fake = connected()
    assert p.skip_objects([12, "34"]).ok  # type: ignore[list-item]
    msg = last(fake)["print"]
    assert msg["command"] == "skip_objects" and msg["obj_list"] == [12, 34] and msg["sequence_id"].isdigit()
    assert not p.skip_objects([]).ok and not p.skip_objects(["x"]).ok  # type: ignore[list-item]
    p.ingest(delta(gcode_state="IDLE"))
    assert not p.skip_objects([1]).ok


def test_start_sd_print_payload() -> None:
    p, fake = connected()
    r = p.start_sd_print("Benchy.gcode.3mf")
    assert not r.ok and "busy" in r.message  # still RUNNING
    p.ingest(delta(gcode_state="FINISH"))
    r = p.start_sd_print("/Benchy.gcode.3mf", 2, [1, -1, 254], timelapse=True, bed_levelling=True, flow_cali="auto")
    assert r.ok
    pr = last(fake)["print"]
    sid = pr.pop("project_id")
    assert sid.isdigit() and 0 < int(sid) < 2**31 and pr.pop("subtask_id") == sid and pr.pop("task_id") == sid
    assert pr == {
        "sequence_id": "20000",
        "command": "project_file",
        "param": "Metadata/plate_2.gcode",
        "url": "ftp://Benchy.gcode.3mf",
        "file": "Benchy.gcode.3mf",
        "md5": "",
        "bed_type": "auto",
        "timelapse": True,
        "bed_leveling": True,
        "auto_bed_leveling": 1,
        "flow_cali": False,
        "vibration_cali": False,
        "layer_inspect": False,
        "use_ams": True,
        "extrude_cali_flag": 2,
        "extrude_cali_manual_mode": 0,
        "nozzle_offset_cali": 0,
        "subtask_name": "Benchy",
        "profile_id": "0",
        "ams_mapping": [1, -1, -1],
        "ams_mapping2": [{"ams_id": 0, "slot_id": 1}, {"ams_id": 255, "slot_id": 255}, {"ams_id": 255, "slot_id": 0}],
    }


def test_start_sd_print_use_ams_rules() -> None:
    p, fake = connected()
    p.ingest(delta(gcode_state="IDLE"))
    p.start_sd_print("a.3mf", 1, [254], use_ams=True)  # external only -> use_ams off
    pr = last(fake)["print"]
    assert pr["use_ams"] is False and pr["ams_mapping"] == [-1] and pr["subtask_name"] == "a"
    p.start_sd_print("a.3mf", 1, [5], use_ams=False)  # real tray forces it on
    assert last(fake)["print"]["use_ams"] is True
    p.start_sd_print("a.3mf", 1, None, bed_levelling=False, flow_cali=True)
    pr = last(fake)["print"]
    assert "ams_mapping" not in pr and pr["use_ams"] is True
    assert (pr["bed_leveling"], pr["auto_bed_leveling"], pr["flow_cali"], pr["extrude_cali_flag"]) == (False, 0, True, 1)
    assert not p.start_sd_print("", 1).ok and not p.start_sd_print("a.3mf", 0).ok


# --------------------------------------------------------------------------- fake TLS MQTT broker


@pytest.fixture(scope="session")
def cert(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    d = tmp_path_factory.mktemp("mqttcert")
    crt, key = d / "c.pem", d / "k.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-subj", "/CN=01P00TEST000001", "-keyout", str(key), "-out", str(crt)],
        check=True, capture_output=True,
    )  # fmt: skip
    return str(crt), str(key)


def _enc_str(s: str) -> bytes:
    b = s.encode()
    return struct.pack("!H", len(b)) + b


def _packet(first: int, body: bytes) -> bytes:
    n, out = len(body), bytearray()
    while True:
        byte, n = n % 128, n // 128
        out.append(byte | (0x80 if n else 0))
        if not n:
            break
    return bytes([first]) + bytes(out) + body


class FakeBroker:
    """Just enough MQTT 3.1.1 to look like the printer's broker."""

    def __init__(self, cert: tuple[str, str], password: str = CODE) -> None:
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(*cert)
        self.password = password
        self.connects: list[tuple[str, str]] = []  # (username, password)
        self.subs: list[str] = []
        self.received: list[tuple[str, dict]] = []
        self.writers: list[asyncio.StreamWriter] = []
        self.on_publish: Callable[[str, dict], Awaitable[None]] | None = None
        self.server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=self.ctx)
        self.port = self.server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        self.drop_all()
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    def drop_all(self) -> None:
        for w in self.writers:
            w.close()
        self.writers.clear()

    async def send_report(self, payload: dict) -> None:
        body = _enc_str(f"device/{SERIAL}/report") + json.dumps(payload).encode()
        for w in list(self.writers):
            try:
                w.write(_packet(0x30, body))
                await w.drain()
            except (ConnectionError, RuntimeError):
                pass

    def published(self) -> list[dict]:
        return [p for _, p in self.received]

    async def _read_packet(self, r: asyncio.StreamReader) -> tuple[int, bytes]:
        first = (await r.readexactly(1))[0]
        mult, n = 1, 0
        while True:
            b = (await r.readexactly(1))[0]
            n += (b & 0x7F) * mult
            mult *= 128
            if not b & 0x80:
                break
        return first, await r.readexactly(n)

    async def _handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            while True:
                first, body = await self._read_packet(r)
                kind = first >> 4
                if kind == 1:  # CONNECT
                    i = 2 + struct.unpack("!H", body[:2])[0]
                    flags = body[i + 1]
                    i += 4
                    fields = []
                    while i < len(body):
                        ln = struct.unpack("!H", body[i : i + 2])[0]
                        fields.append(body[i + 2 : i + 2 + ln].decode())
                        i += 2 + ln
                    user = fields[1] if flags & 0x80 else ""
                    pw = fields[2] if flags & 0x40 else ""
                    self.connects.append((user, pw))
                    ok = user == "bblp" and pw == self.password
                    w.write(_packet(0x20, bytes([0, 0 if ok else 5])))
                    await w.drain()
                    if not ok:
                        w.close()
                        return
                    self.writers.append(w)
                elif kind == 8:  # SUBSCRIBE
                    pid = body[:2]
                    i, granted = 2, b""
                    while i < len(body):
                        ln = struct.unpack("!H", body[i : i + 2])[0]
                        self.subs.append(body[i + 2 : i + 2 + ln].decode())
                        i += 3 + ln
                        granted += b"\x00"
                    w.write(_packet(0x90, pid + granted))
                    await w.drain()
                elif kind == 3:  # PUBLISH
                    qos = (first >> 1) & 3
                    ln = struct.unpack("!H", body[:2])[0]
                    topic = body[2 : 2 + ln].decode()
                    i = 2 + ln
                    if qos:
                        w.write(_packet(0x40, body[i : i + 2]))
                        await w.drain()
                        i += 2
                    msg = json.loads(body[i:])
                    self.received.append((topic, msg))
                    if self.on_publish:
                        await self.on_publish(topic, msg)
                elif kind == 12:  # PINGREQ
                    w.write(b"\xd0\x00")
                    await w.drain()
                elif kind == 14:  # DISCONNECT
                    break
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError, OSError):
            pass
        finally:
            if w in self.writers:
                self.writers.remove(w)
            w.close()


async def wait_for(cond: Callable[[], bool], timeout: float = 8.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def run_with_broker(cert: tuple[str, str], body: Callable[[FakeBroker], Awaitable[None]], **kw: Any) -> None:
    async def main() -> None:
        broker = FakeBroker(cert, **kw)
        await broker.start()
        try:
            await body(broker)
        finally:
            await broker.close()

    asyncio.run(asyncio.wait_for(main(), 30))


def client_for(broker: FakeBroker, code: str = CODE, **kw: Any) -> PrinterMqtt:
    kw.setdefault("tick_s", 0.05)
    kw.setdefault("paho_delay", (1, 1))
    return PrinterMqtt("127.0.0.1", SERIAL, code, port=broker.port, **kw)


def test_connect_pushall_report_and_command(cert: tuple[str, str], caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    async def body(broker: FakeBroker) -> None:
        changes: list[int] = []

        async def answer(topic: str, msg: dict) -> None:
            if msg.get("pushing", {}).get("command") == "pushall":
                await broker.send_report(full_report())

        broker.on_publish = answer
        p = client_for(broker, on_change=lambda: changes.append(1))
        await p.start()
        try:
            await wait_for(lambda: p.status()["state"] == "RUNNING")
            assert p.connected and p.link == "online" and p.last_error == ""
            assert broker.connects == [("bblp", CODE)]
            assert broker.subs == [f"device/{SERIAL}/report"]
            assert {"pushing": {"command": "pushall", "sequence_id": "0"}} in broker.published()
            assert any(m.get("info", {}).get("command") == "get_version" for m in broker.published())
            st = p.status()
            assert st["connected"] is True and st["progress"] == 42.0 and st["ams"][0]["trays"][1]["type"] == "PETG"
            assert changes

            await broker.send_report(delta(mc_percent=50))
            await wait_for(lambda: p.status()["progress"] == 50.0)

            assert p.pause().ok
            await wait_for(lambda: {"print": {"command": "pause", "sequence_id": "0"}} in broker.published())
            assert all(t == f"device/{SERIAL}/request" for t, _ in broker.received)
            conn = p.connection()
            assert conn["link"] == "online" and CODE not in json.dumps(conn)
        finally:
            await p.stop()
        assert p.link == "stopped" and not p.connected

    run_with_broker(cert, body)
    assert CODE not in caplog.text


def test_wrong_access_code(cert: tuple[str, str], caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    async def body(broker: FakeBroker) -> None:
        p = client_for(broker, code="00000000")
        await p.start()
        try:
            await wait_for(lambda: p.link == "auth_error")
            assert not p.connected and "access code" in p.last_error
            assert "00000000" not in p.last_error
            assert not p.pause().ok
        finally:
            await p.stop()

    run_with_broker(cert, body)
    assert "00000000" not in caplog.text


def test_reconnect_after_drop(cert: tuple[str, str]) -> None:
    async def body(broker: FakeBroker) -> None:
        p = client_for(broker)
        await p.start()
        try:
            await wait_for(lambda: p.connected)
            broker.drop_all()
            await wait_for(lambda: not p.connected)
            assert p.link == "offline" and p.last_error
            await wait_for(lambda: p.connected and len(broker.connects) == 2)
            pushalls = [m for m in broker.published() if "pushing" in m]
            assert len(pushalls) >= 2  # pushall again on every (re)connect
        finally:
            await p.stop()

    run_with_broker(cert, body)


def test_silent_broker_nudges_then_rebuilds(cert: tuple[str, str]) -> None:
    async def body(broker: FakeBroker) -> None:
        p = client_for(broker, nudge_s=0.3, stale_s=0.8, retry_min_s=0.1)
        await p.start()
        try:
            await wait_for(lambda: p.connected)
            n0 = len([m for m in broker.published() if "pushing" in m])
            await wait_for(lambda: len([m for m in broker.published() if "pushing" in m]) > n0)  # nudge
            await wait_for(lambda: len(broker.connects) >= 2)  # stale -> new session
            await wait_for(lambda: p.connected)
        finally:
            await p.stop()

    run_with_broker(cert, body)


def test_periodic_pushall(cert: tuple[str, str]) -> None:
    async def body(broker: FakeBroker) -> None:
        p = client_for(broker, pushall_s=0.3)
        await p.start()
        try:
            await wait_for(lambda: p.connected)
            for _ in range(10):  # keep the link fresh so only the periodic timer fires
                await broker.send_report(delta(mc_percent=1))
                await asyncio.sleep(0.1)
            assert len([m for m in broker.published() if "pushing" in m]) >= 3
        finally:
            await p.stop()

    run_with_broker(cert, body)


def test_not_configured_and_unreachable() -> None:
    async def main() -> None:
        p = PrinterMqtt(tick_s=0.05)
        await p.start()
        await wait_for(lambda: p.link == "not_configured")
        # Closed local port: connection fails, app keeps running, error is reported.
        import socket

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        p.port = port
        p.configure("127.0.0.1", SERIAL, CODE)
        await wait_for(lambda: p.link == "offline" and p.last_error != "")
        assert CODE not in p.last_error
        await p.stop()

    asyncio.run(asyncio.wait_for(main(), 20))

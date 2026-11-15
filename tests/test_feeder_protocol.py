"""Tests for the WOPET Camera Pet Feeder schedule, feed and clock messages.

Records are synthetic but follow the layout decoded from app captures on
firmware 40.0.14.3 (see docs/PROTOCOL.md).
"""

from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
WOPET_DIR = ROOT / "wopet_bridge/wopet"


def _load(name: str):
    if str(WOPET_DIR) not in sys.path:
        sys.path.insert(0, str(WOPET_DIR))
    spec = importlib.util.spec_from_file_location(name, WOPET_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record(slot: int, hour: int, minute: int, portions: int, *, enabled=True, sound=0x0A,
           grid=1, name=b"Meal") -> bytes:
    rec = bytearray(56)
    rec[0:4] = b"\xff\xff\x02\x0a"
    rec[6] = 0x7F
    rec[7], rec[8], rec[9] = hour, minute, portions * 10
    rec[11] = 0x11 if enabled else 0x10
    rec[12], rec[13], rec[14] = slot, sound, grid
    rec[16 : 16 + len(name)] = name
    rec[16 + len(name) + 1 :] = b"\xaa" * (56 - 17 - len(name))  # junk after the NUL
    return bytes(rec)


def plan_reply(records: list[bytes], set_flag: int = 0) -> bytes:
    out = struct.pack("<III", 0, set_flag, len(records))
    for slot, rec in enumerate(records, start=1):
        out += struct.pack("<I", slot) + rec
    return out


SIX = [record(i, 6 * (i - 1) % 24, 0, 2, name=f"Meal {i}".encode()) for i in range(1, 7)]


def test_parse_plan_reads_decoded_fields() -> None:
    fp = _load("feeder_protocol")
    recs = list(SIX)
    recs[0] = record(1, 6, 1, 5, sound=1, grid=0, name=b"Breakfast test")
    recs[1] = record(2, 12, 0, 2, enabled=False)
    meals = fp.parse_plan(plan_reply(recs))
    assert len(meals) == 6
    m = meals[0].as_dict()
    assert (m["slot"], m["name"], m["hour"], m["minute"], m["portions"]) == (
        1, "Breakfast test", 6, 1, 5
    )
    assert (m["enabled"], m["audio"], m["sound"], m["grid"]) == (True, True, 1, "big")
    assert meals[1].enabled is False
    assert meals[1].sound is None and meals[1].audio is False


def test_parse_plan_rejects_bad_replies() -> None:
    fp = _load("feeder_protocol")
    with pytest.raises(fp.ProtocolError):
        fp.parse_plan(b"\0" * 8)
    with pytest.raises(fp.ProtocolError):
        fp.parse_plan(struct.pack("<III", 5, 0, 6) + b"\0" * 360)   # result != 0
    with pytest.raises(fp.ProtocolError):
        fp.parse_plan(struct.pack("<III", 0, 0, 0))                 # count 0 reply
    broken = bytearray(plan_reply(SIX))
    broken[16] = 0
    with pytest.raises(fp.ProtocolError):
        fp.parse_plan(bytes(broken))                                  # no ff ff marker


def test_apply_changes_touches_only_known_bytes() -> None:
    fp = _load("feeder_protocol")
    meal = fp.parse_plan(plan_reply(SIX))[2]
    before = bytes(meal.record)
    fp.apply_changes(meal, {"portions": 3, "hour": 7, "minute": 45, "grid": "big",
                            "enabled": False, "sound": 4, "name": "Snack"})
    after = bytes(meal.record)
    changed = {i for i in range(56) if before[i] != after[i]}
    assert changed <= {7, 8, 9, 11, 13, 14} | set(range(16, 48))
    assert (after[7], after[8], after[9], after[11], after[13], after[14]) == (
        7, 45, 30, 0x10, 4, 0
    )
    assert meal.name == "Snack"
    assert after[4:7] == before[4:7]                 # unknown bytes preserved


def test_audio_switch_keeps_or_defaults_the_clip() -> None:
    fp = _load("feeder_protocol")
    meal = fp.parse_plan(plan_reply([record(1, 6, 0, 2, sound=3)]))[0]
    fp.apply_changes(meal, {"audio": True})
    assert meal.sound == 3                           # already on: clip kept
    fp.apply_changes(meal, {"audio": False})
    assert meal.record[13] == fp.AUDIO_OFF
    fp.apply_changes(meal, {"audio": True})
    assert meal.sound == fp.DEFAULT_SOUND


@pytest.mark.parametrize(
    "changes",
    [
        {"portions": 0}, {"portions": 11}, {"portions": True}, {"portions": 1.5},
        {"hour": 24}, {"minute": 60}, {"grid": "huge"}, {"enabled": 1},
        {"sound": 5}, {"sound": False}, {"name": ""}, {"name": "x" * 32}, {"colour": 1},
    ],
)
def test_apply_changes_rejects_bad_values(changes) -> None:
    fp = _load("feeder_protocol")
    meal = fp.parse_plan(plan_reply(SIX))[0]
    with pytest.raises(ValueError):
        fp.apply_changes(meal, changes)


def test_plan_set_marks_requests_and_the_edited_slot() -> None:
    fp = _load("feeder_protocol")
    meals = fp.parse_plan(plan_reply(SIX))
    body = fp.build_plan_set(meals, 4)
    assert struct.unpack_from("<II", body) == (1, 6)
    for i in range(6):
        off = 8 + i * 60
        slot = struct.unpack_from("<I", body, off)[0]
        rec = body[off + 4 : off + 60]
        assert rec[2] == 0x01
        assert rec[15] == (1 if slot == 4 else 0)
    stored = fp.parse_plan(plan_reply([bytes(m.record) for m in meals], set_flag=1))
    assert fp.plans_match(meals, stored)
    fp.apply_changes(stored[0], {"portions": 9})
    assert not fp.plans_match(meals, stored)


def test_manual_feed_payload_matches_capture_shape() -> None:
    import time

    fp = _load("feeder_protocol")
    when = time.strptime("2026-11-15 01:16", "%Y-%m-%d %H:%M")
    body = fp.build_feed(1, when)
    assert len(body) == 68
    assert body[:4] == bytes.fromhex("01000e00")
    assert body[4:20] == bytes.fromhex("ffff010a420b0f0110" "0a" "000000" "0a" "0000")
    assert fp.build_feed(4, when)[13] == 40
    for bad in (0, 5, True, 2.0):
        with pytest.raises(ValueError):
            fp.build_feed(bad, when)


def test_clock_set_payload() -> None:
    fp = _load("feeder_protocol")
    assert fp.build_set_time(0x6AF9C635, -300) == bytes.fromhex("35c6f96ad4fe0000")
    with pytest.raises(ValueError):
        fp.build_set_time(0, 15 * 60)


def test_feed_event_codes() -> None:
    fp = _load("feeder_protocol")
    assert fp.parse_event(b"\0" * 16 + struct.pack("<I", 0x36)) == 0x36
    assert fp.parse_event(b"\0" * 10) is None
    assert "bowl full" in fp.EVENT_CODES[0x36]
    assert fp.parse_feed_reply(struct.pack("<I", 0)) == 0

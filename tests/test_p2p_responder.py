"""Tests for the local TUTK master/region responder.

Golden replies are the 2026-11-15 captured server replies with the UID and the
public address replaced by test values; the real code reproduced all 41 captured
replies byte-for-byte on the wire.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "wopet_bridge/wopet/p2p_responder.py"
UID = b"ABCDEFGHIJKLMNOPQRST"
SEEN = ("203.0.113.7", 0x6B4E)
SEEN_HEX = "02006b4ecb007107"


def _load():
    spec = importlib.util.spec_from_file_location("p2p_responder", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["p2p_responder"] = module
    spec.loader.exec_module(module)
    return module


def _master_query() -> bytes:
    header = bytes.fromhex("04021902260000000710180000000000")
    return header + UID + b"\0" * 16 + bytes.fromhex("0601")


def test_master_referral_matches_capture() -> None:
    r = _load()
    want = bytes.fromhex(
        "04021d006e0000000810830000000000"
        + UID.hex()
        + "00" * 12
        + "00000000" + SEEN_HEX + "00000000"
        + "00000000" + "05000300" + "02002000"
        + "020027112ffc0d010000000000000000"
        + "020027112ffe4ddf0000000000000000"
        + "0300040030000000" + "0100060089a101001100"
    )
    kind, got = r.build_reply(_master_query(), SEEN, UID)
    assert kind == "master"
    assert got == want


def test_probe_reply_matches_capture() -> None:
    r = _load()
    req = bytes.fromhex("040219020800000003803f000000000001000000fb2b0100")
    want = bytes.fromhex(
        "04021b001800000004804f0000000000" + SEEN_HEX + "00" * 8 + "01000000fb2b0000"
    )
    assert r.build_reply(req, SEEN, UID) == ("probe", want)


def test_login_reply_echoes_with_seen_address() -> None:
    r = _load()
    req = bytearray(288)
    req[:16] = bytes.fromhex("04021902100100000501140000000000")
    req[16:36] = UID
    req[36:44] = bytes.fromhex("0000b5dd0a050a17")
    req[236:244] = bytes.fromhex("01000000fb2b2800")
    kind, got = r.build_reply(bytes(req), SEEN, UID)
    assert kind == "login"
    want = bytearray(req)
    want[:16] = bytes.fromhex("04021b00100100000601410000000000")
    want[36:44] = bytes.fromhex(SEEN_HEX)
    assert got == bytes(want)


def test_foreign_uid_and_unknown_frames_are_ignored() -> None:
    r = _load()
    other = bytearray(_master_query())
    other[16] = ord("Z")
    assert r.build_reply(bytes(other), SEEN, UID) is None
    assert r.build_reply(b"\x8f\xc2\xca\xd9lie is the", SEEN, UID) is None


def test_replies_survive_the_wire_transcode() -> None:
    r = _load()
    for req in (_master_query(), bytes.fromhex("040219020800000003803f000000000001000000fb2b0100")):
        _kind, reply = r.build_reply(req, SEEN, UID)
        assert r.inv_transcode(r.transcode(reply)) == reply


def test_referral_can_name_this_host_as_region_server() -> None:
    """Default 'auto': the feeder's UDP 10001 comes straight here, no redirect needed."""
    r = _load()
    regions = r.region_servers("192.0.2.100", "192.0.2.23")
    assert regions == (("192.0.2.100", 10001), ("192.0.2.100", 10001))
    _kind, reply = r.build_reply(_master_query(), SEEN, UID, regions)
    assert reply[76:84] == bytes.fromhex("02002711c0000264")
    assert reply[92:100] == bytes.fromhex("02002711c0000264")
    assert len(reply) == 126


def test_region_server_settings() -> None:
    r = _load()
    assert r.region_servers("captured", "192.0.2.23") == r.REGION_SERVERS
    host = r.region_servers("auto", "127.0.0.1")
    assert host == (("127.0.0.1", 10001), ("127.0.0.1", 10001))
    import pytest

    with pytest.raises(OSError):
        r.region_servers("not-an-ip", "192.0.2.23")

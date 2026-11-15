"""Tests for the bridge control API handlers (no camera, no network)."""

from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
WOPET_DIR = ROOT / "wopet_bridge/wopet"
sys.path.insert(0, str(Path(__file__).parent))
from test_feeder_protocol import SIX, plan_reply  # noqa: E402


def _load_control():
    if str(WOPET_DIR) not in sys.path:
        sys.path.insert(0, str(WOPET_DIR))
    spec = importlib.util.spec_from_file_location("wopet_control_t", WOPET_DIR / "wopet_control.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSession:
    """Answers IOCTLs from a script; records every call and its send options."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def ioctl_during_stream(self, io_type, payload, *, resp_type, timeout, retransmit=True,
                            interval=0.4, match=None):
        self.calls.append({"io": io_type, "payload": payload, "retransmit": retransmit})
        while self.replies:
            body = self.replies.pop(0)
            if match is None or match(body):
                return resp_type, body
        raise TimeoutError("no matching reply")


def test_feed_is_sent_once_and_reports_bowl_full(monkeypatch) -> None:
    ctl = _load_control()
    monkeypatch.setattr(ctl, "EVENT_WAIT", 0.05)
    session = FakeSession([struct.pack("<I", 0) + b"\0" * 64])
    result = ctl.feed(session, {"portions": 2})
    assert len(session.calls) == 1
    assert session.calls[0]["io"] == 0x207E
    assert session.calls[0]["retransmit"] is False
    assert result == {"ok": True, "portions": 2, "code": None}

    session = FakeSession([struct.pack("<I", 0) + b"\0" * 64])
    import time

    ctl._events.append((time.time() + 1, 0x36))
    result = ctl.feed(session, {"portions": 1})
    assert result["ok"] is False and result["code"] == 0x36


def test_feed_rejects_bad_portions_without_sending() -> None:
    ctl = _load_control()
    session = FakeSession([])
    with pytest.raises(ValueError):
        ctl.feed(session, {"portions": 9})
    assert session.calls == []


def test_set_meal_is_read_modify_write_and_skips_stale_replies() -> None:
    ctl = _load_control()
    fp = ctl.fp
    edited = [bytes(r) for r in SIX]
    meal = fp.parse_plan(plan_reply(SIX))[1]
    fp.apply_changes(meal, {"portions": 4})
    edited[1] = bytes(meal.record)
    session = FakeSession([
        plan_reply(SIX),                     # read
        plan_reply(SIX),                     # late duplicate of the read: must be skipped
        struct.pack("<III", 0, 1, 0),        # count-0 reply: must be skipped
        plan_reply(edited, set_flag=1),      # the real ack
    ])
    result = ctl.set_meal(session, {"slot": 2, "portions": 4})
    assert result["ok"] is True
    assert result["meals"][1]["portions"] == 4
    assert [c["io"] for c in session.calls] == [0x208C, 0x208C]
    sent = session.calls[1]["payload"]
    assert struct.unpack_from("<II", sent) == (1, 6)


def test_set_meal_fails_when_feeder_does_not_store_it() -> None:
    ctl = _load_control()
    session = FakeSession([plan_reply(SIX), plan_reply(SIX, set_flag=1), plan_reply(SIX)])
    with pytest.raises(RuntimeError):
        ctl.set_meal(session, {"slot": 1, "portions": 7})


def test_set_meal_validates_request() -> None:
    ctl = _load_control()
    with pytest.raises(ValueError):
        ctl.set_meal(FakeSession([]), {"portions": 2})
    with pytest.raises(ValueError):
        ctl.set_meal(FakeSession([]), {"slot": 1})
    with pytest.raises(ValueError):
        ctl.set_meal(FakeSession([plan_reply(SIX)]), {"slot": 9, "portions": 2})


def test_set_time_sends_local_offset(monkeypatch) -> None:
    ctl = _load_control()
    session = FakeSession([b"\0" * 8])
    info = ctl.set_time(session)
    assert info["ok"] is True
    payload = session.calls[0]["payload"]
    assert session.calls[0]["io"] == 0x2052
    epoch, tz = struct.unpack("<Ih2x", payload)
    assert epoch == info["epoch"] and tz == info["tz_minutes"]
    with pytest.raises(RuntimeError):
        ctl.set_time(FakeSession([struct.pack("<I", 1) + b"\0" * 4]))


def test_control_health_uses_wrapped_transport_reader() -> None:
    ctl = _load_control()

    class Reader:
        def is_alive(self):
            return True

    class Transport:
        _av_reader_thread = Reader()

    class Session:
        _inner = Transport()

    assert ctl._stream_is_ready(Session()) is True
    assert ctl._stream_is_ready(object()) is False

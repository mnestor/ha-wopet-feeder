"""Two-way talk frames, checked against the WOpet app capture (2026-11-15)."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "wopet_bridge/vendor/cuboai_tutk"
WOPET = ROOT / "wopet_bridge/wopet"
for p in (VENDOR, WOPET):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import wopet_talk as wt  # noqa: E402

# Bytes [28:] of the app's frames, as captured.
APP_GRANT = bytes.fromhex(
    "00210b000c6dd16b00000000000000002400000057b01b5900000000000100010000000004000000"
    "fb071f0000000000000000000000030000000000")[:60]
APP_AUDIO_HDR = bytes.fromhex("03050b00030000000301010001001000300300000100000002000000")


def test_grant_matches_the_app() -> None:
    plain = wt.talk_grant_plain(0x8194, 1, 7, bytes.fromhex("57b01b59"))
    assert len(plain) == 88 and plain[14] == 1
    assert plain[28:88] == APP_GRANT


def test_audio_frame_matches_the_app() -> None:
    pcm = bytes(800)
    plain = wt.talk_audio_plain(0x8194, 1, 9, relseq=3, index=1, pcm=pcm, ts_ms=0xE105DA15)
    assert len(plain) == 872
    assert plain[28:56] == APP_AUDIO_HDR
    assert plain[56:856] == pcm
    assert plain[-16:] == bytes.fromhex("8c00020000000000000000001" "5da05e1")


class _Sock:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendto(self, data, _addr) -> None:
        self.sent.append(data)


class _Sess:
    _R, _cam, _seq, _relseq, _frmno = 0x8194, ("x", 1), 0, 0, 0


def test_talk_flow_start_grant_stream_stop(monkeypatch) -> None:
    from cuboai_pure import inv_transcode

    clock = [100.0]
    monkeypatch.setattr(wt.time, "monotonic", lambda: clock[0])
    talk, sess, sock = wt.WopetTalk(), _Sess(), _Sock()
    talk.feed(bytes(1600))                       # two frames
    talk.pump(sess, sock)
    assert talk.state == "starting"
    start = inv_transcode(sock.sent[-1])
    assert struct.unpack_from("<I", start, 64)[0] == wt.SPEAKER_START
    login = bytearray(598)
    login[14], login[28], login[29] = 1, 0x00, 0x20
    login[48:52] = b"\x01\x02\x03\x04"
    talk.rx(sess, sock, bytes(login))
    assert talk.state == "granted"
    assert inv_transcode(sock.sent[-2])[48:52] == b"\x01\x02\x03\x04"
    clock[0] += 0.12
    talk.pump(sess, sock)
    assert talk.frames_sent == 2
    clock[0] += wt.IDLE_STOP_SECONDS + 0.1
    talk.pump(sess, sock)
    assert talk.state == "idle"
    assert struct.unpack_from("<I", inv_transcode(sock.sent[-1]), 64)[0] == wt.SPEAKER_STOP


def test_alaw_decoding() -> None:
    import wopet_control as wc

    assert wc.alaw_to_pcm16(b"\xd5") == (8).to_bytes(2, "little", signed=True)
    assert wc.alaw_to_pcm16(b"\x55") == (-8).to_bytes(2, "little", signed=True)
    assert wc.alaw_to_pcm16(b"\xaa") == (32256).to_bytes(2, "little", signed=True)

"""Voice-clip upload, checked against the WOpet app captures (2026-11-15)."""

from __future__ import annotations

import io
import struct
import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "wopet_bridge/vendor/cuboai_tutk"
WOPET = ROOT / "wopet_bridge/wopet"
for p in (VENDOR, WOPET):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import feeder_protocol as fp  # noqa: E402
import wopet_clip as wc  # noqa: E402

# RDT headers ([28:48]) of the app's frames, as captured.
APP_PING = bytes.fromhex("5a97c2f101050000000000000000000000010000")
APP_SIZE = bytes.fromhex("5a97c2f102058000000000000000000001010000")
APP_ACK1 = bytes.fromhex("5a97c2f146050000010000000000000001010000")
APP_CHUNK1 = bytes.fromhex("5a97c2f102050005010000000000000001010000")
APP_FIN_ACK = bytes.fromhex("5a97c2f143050000ffffffffffffffff01010000")


def _wav(seconds: float, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(int(rate * seconds) * 2))
    return buf.getvalue()


def _entry(name: bytes, label: bytes, seconds: int, stamp: int) -> bytes:
    return (name.ljust(32, b"\0") + label.ljust(32, b"\0") + bytes(32)
            + struct.pack("<II", seconds, stamp))


def test_rdt_headers_match_the_app() -> None:
    assert wc.rdt_header(wc.PING, conn=0) == APP_PING
    assert wc.rdt_header(wc.DATA, 128, 0) == APP_SIZE
    assert wc.rdt_header(wc.ACK, seq=1) == APP_ACK1
    assert wc.rdt_header(wc.DATA, 1280, 1) == APP_CHUNK1
    assert wc.rdt_header(wc.FIN_ACK, fin=True) == APP_FIN_ACK
    assert wc.message(wc.MSG_SIZE, b"73274")[:6] == b"\x0273274"


def test_clip_list_and_upload_payload() -> None:
    body = struct.pack("<II", 0, 5) + _entry(b"sound0.wav", b"Audio0", 5, 0x6AF949BE)
    for k in range(1, 5):
        body += _entry(f"sound{k}.wav".encode(), b"invalid", 0, 0)
    clips = fp.parse_clips(body)
    assert clips[0] == {"clip": 0, "file": "sound0.wav", "label": "Audio0", "seconds": 5,
                        "time": 0x6AF949BE, "empty": False}
    assert clips[3]["empty"] and clips[3]["label"] is None

    payload = fp.build_clip_upload(1, "Audio1", 10, 0x6AF9C659)
    assert len(payload) == 272
    assert payload[0:4] == bytes(4)
    assert payload[4:14] == b"sound1.wav" and payload[36:42] == b"Audio1"
    # the app's 0x208a: +100 seconds, +104 Unix time
    assert payload[100:108] == bytes.fromhex("0a00000059c6f96a")
    assert fp.build_clip_play(0, "Audio0", 5, 1)[:4] == b"\x02\0\0\0"
    with pytest.raises(ValueError):
        fp.build_clip_upload(5, "x", 1, 0)


def test_wav_validation() -> None:
    assert wc.wav_info(_wav(4.5)) == pytest.approx(4.5)
    with pytest.raises(ValueError):
        wc.wav_info(_wav(1, rate=16000))
    with pytest.raises(ValueError):
        wc.wav_info(_wav(11))
    with pytest.raises(ValueError):
        wc.wav_info(b"not a wav file at all" * 4)


class _Sess:
    _R, _cam, _seq = 0x451A, ("x", 1), 0


class _FakeFeeder:
    """Plays the feeder's side of a capture, frame for frame."""

    def __init__(self) -> None:
        from cuboai_pure import inv_transcode

        self.decode = inv_transcode
        self.inbox: list[bytes] = []
        self.received = bytearray()
        self.size = None
        self.seq = 0

    def sendto(self, data, _addr) -> None:
        self.inbox.append(self.decode(data))

    def frame(self, kind, seq=0, data=b"", conn=1, fin=False) -> bytes:
        p = bytearray(48 + len(data))
        p[14] = 3
        p[28:48] = wc.rdt_header(kind, len(data), seq, conn, fin)
        p[48:] = data
        return bytes(p)

    def data(self, kind, text=b"") -> bytes:
        f = self.frame(wc.DATA, self.seq, wc.message(kind, text))
        self.seq += 1
        return f


def test_upload_flow_against_a_fake_feeder() -> None:
    wav = _wav(2.0)
    up, sess, feeder = wc.WopetClipUpload(), _Sess(), _FakeFeeder()
    up.begin(wav, "sound2.wav")
    up.pump(sess, feeder)
    assert wc.parse_rdt(feeder.inbox[-1])[0] == wc.PING
    up.rx(sess, feeder, feeder.frame(wc.PONG, conn=0))
    up.rx(sess, feeder, feeder.frame(wc.PING, conn=0))
    assert wc.parse_rdt(feeder.inbox[-1])[0] == wc.PONG
    up.rx(sess, feeder, feeder.data(wc.MSG_NAME, b"sound2.wav"))
    kinds = [wc.parse_rdt(f) for f in feeder.inbox[-2:]]
    assert kinds[0][0] == wc.ACK
    assert kinds[1][0] == wc.DATA and kinds[1][2][:1] == b"\x02"
    assert kinds[1][2][1:].rstrip(b"\0") == str(len(wav)).encode()
    up.rx(sess, feeder, feeder.frame(wc.ACK, 0))
    up.rx(sess, feeder, feeder.data(wc.MSG_START, b"Start"))

    chunks = {}
    for _ in range(50):
        feeder.inbox.clear()
        up.pump(sess, feeder)
        for f in feeder.inbox:
            kind, seq, data = wc.parse_rdt(f)
            if kind == wc.DATA and seq >= 1:
                chunks[seq] = data
                up.rx(sess, feeder, feeder.frame(wc.ACK, seq))
        if b"".join(chunks[k] for k in sorted(chunks)) == wav:
            break
    assert b"".join(chunks[k] for k in sorted(chunks)) == wav
    assert max(len(c) for c in chunks.values()) == wc.CHUNK

    up.rx(sess, feeder, feeder.data(wc.MSG_STOP, b"stop"))
    up.wait(timeout=0.1)                       # stored: no error
    up.rx(sess, feeder, feeder.frame(wc.FIN, fin=True))
    assert [wc.parse_rdt(f)[0] for f in feeder.inbox[-2:]] == [wc.CLOSE, wc.FIN_ACK]
    up.rx(sess, feeder, feeder.frame(wc.CLOSE, conn=0))
    assert up.state == "idle"


def test_frames_use_the_connection_id_from_the_feeders_ping() -> None:
    up, sess, feeder = wc.WopetClipUpload(), _Sess(), _FakeFeeder()
    up.begin(_wav(1), "sound4.wav")
    up.pump(sess, feeder)                      # our PING goes out with id 1
    ping = bytearray(feeder.frame(wc.PING, conn=0))
    ping[45] = 8                               # the feeder's id after several uploads
    up.rx(sess, feeder, bytes(ping))
    assert [f[45] for f in feeder.inbox] == [1, 8]
    up.rx(sess, feeder, feeder.data(wc.MSG_NAME, b"sound4.wav"))
    assert all(f[45] == 8 for f in feeder.inbox[1:])


def test_upload_times_out(monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(wc.time, "monotonic", lambda: clock[0])
    up, sess, feeder = wc.WopetClipUpload(), _Sess(), _FakeFeeder()
    up.begin(_wav(1), "sound0.wav")
    clock[0] += wc.UPLOAD_TIMEOUT + 1
    up.pump(sess, feeder)
    with pytest.raises(RuntimeError, match="timed out"):
        up.wait(timeout=0.1)

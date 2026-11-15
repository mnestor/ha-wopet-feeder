"""Voice-clip upload to the WOPET feeder over TUTK RDT, run on the AV reader thread.

Decoded from two WOpet app uploads (2026-11-15). After IOCTL 0x208a -> 0x208b (sent by the
control thread), the file goes over IOTC channel 3 as RDT frames: a 20-byte header at [28:48]
(magic 5a97c2f1, u8 type, u8 0x05, u16 data length, u32 sequence, u32 0, u8 0|1, u8 connection
id, 2 zero bytes) and data from [48:]. The connection id is the feeder's: its PING carries it (1
after a reboot, higher later), its PONG just echoes ours, and it ignores frames with another id.
The app's capture had 1 on both sides, which is why a fixed 1 worked once.

    host -> PING (01)                 feeder -> PONG (41); feeder -> PING, host -> PONG
    feeder -> DATA "\\x01soundN.wav"   host -> ACK (46, same seq)
    host -> DATA "\\x02<size>"         (ASCII decimal byte count; 128-byte messages)
    feeder -> DATA "\\x04Start"
    host -> DATA seq 1..N              the file, 1280 bytes per frame, each ACKed
    feeder -> DATA "\\x07stop"         after it has stored the file
    feeder -> FIN (03)                host -> CLOSE (04) + FIN-ACK (43); then CLOSE both ways

Each direction numbers its own DATA frames from 0.
"""

from __future__ import annotations

import struct
import threading
import time

from cuboai_pure import transcode
from wopet_talk import _header

CLIP_CHANNEL = 3
CHUNK = 1280
WINDOW = 16                      # unacknowledged file frames in flight
RESEND_AFTER = 0.4
PING_EVERY = 0.5
UPLOAD_TIMEOUT = 30.0
CLOSE_GRACE = 3.0

MAGIC = bytes.fromhex("5a97c2f1")
PING, PONG, DATA, ACK, FIN, FIN_ACK, CLOSE = 0x01, 0x41, 0x02, 0x46, 0x03, 0x43, 0x04
MSG_NAME, MSG_SIZE, MSG_START, MSG_STOP = 0x01, 0x02, 0x04, 0x07

MAX_SECONDS = 10                 # the app's limit
SAMPLE_RATE = 8000


def rdt_header(kind, length=0, seq=0, conn=1, fin=False, cid=1):
    h = bytearray(20)
    h[0:4] = MAGIC
    h[4] = kind
    h[5] = 0x05
    struct.pack_into("<H", h, 6, length)
    struct.pack_into("<I", h, 8, 0xFFFFFFFF if fin else seq & 0xFFFFFFFF)
    struct.pack_into("<I", h, 12, 0xFFFFFFFF if fin else 0)
    h[16] = conn
    h[17] = cid & 0xFF
    return bytes(h)


def message(kind, text=b""):
    body = bytes([kind]) + text
    return body + bytes(128 - len(body))


def parse_rdt(frame):
    """(type, seq, data) of an RDT frame on the clip channel, or None."""
    if len(frame) < 48 or frame[28:32] != MAGIC:
        return None
    length = struct.unpack_from("<H", frame, 34)[0]
    seq = struct.unpack_from("<I", frame, 36)[0]
    return frame[32], seq, bytes(frame[48:48 + length])


def wav_info(data: bytes) -> float:
    """Validate a feeder clip (RIFF WAV, PCM, mono, 16-bit, 8 kHz); return its length in seconds."""
    if len(data) < 44 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("clip must be a WAV file")
    pos, fmt, frames = 12, None, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        if cid == b"fmt ":
            fmt = struct.unpack_from("<HHIIHH", data, pos + 8)
        elif cid == b"data":
            frames = min(size, len(data) - pos - 8) // 2
            break
        pos += 8 + size + (size & 1)
    if fmt is None or frames is None:
        raise ValueError("WAV has no fmt or data chunk")
    tag, channels, rate, _byte_rate, _align, bits = fmt
    if (tag, channels, rate, bits) != (1, 1, SAMPLE_RATE, 16):
        raise ValueError("clip must be PCM, mono, 16-bit, 8 kHz")
    seconds = frames / SAMPLE_RATE
    if seconds > MAX_SECONDS + 0.05:
        raise ValueError(f"clip is {seconds:.1f} s; the feeder takes at most {MAX_SECONDS} s")
    return seconds


class WopetClipUpload:
    """One upload at a time. begin() from any thread, then wait(); pump()/rx() on the reader."""

    def __init__(self, channel=CLIP_CHANNEL, log=None):
        self.channel = channel
        self._log = log or (lambda _m: None)
        self._done = threading.Event()
        self._done.set()
        self.state = "idle"
        self.error = None

    @property
    def active(self) -> bool:
        return self.state != "idle"

    # ── control thread ──
    def begin(self, wav: bytes, name: str) -> None:
        if self.active:
            raise RuntimeError("an upload is already running")
        self._wav = bytes(wav)
        self._name = name
        self._cid = 1                          # replaced by the id in the feeder's PING
        self._chunks = [self._wav[i:i + CHUNK] for i in range(0, len(self._wav), CHUNK)]
        self._next_chunk = 0                   # index into _chunks of the next new frame
        self._unacked = {}                     # our DATA seq -> (payload, last sent)
        self._seen = set()                     # feeder DATA seqs already handled
        self._stop_seen = False
        self._closed_at = None
        self._last_ping = 0.0
        self._deadline = time.monotonic() + UPLOAD_TIMEOUT
        self.error = None
        self.resends = 0
        self._trace_left = 4                   # log the first few feeder frames of each upload
        self._done.clear()
        self.state = "hello"                   # set last: the reader starts acting on it

    def wait(self, timeout=UPLOAD_TIMEOUT + CLOSE_GRACE + 2) -> None:
        if not self._done.wait(timeout):
            self.error = self.error or "upload did not finish"
            self._finish()
        if self.error:
            raise RuntimeError(self.error)

    # ── reader thread ──
    def _send(self, sess, s, rdt, data=b""):
        rdt = rdt[:17] + bytes([self._cid & 0xFF]) + rdt[18:]
        total = 48 + len(data)
        # The session-wide frame counter, as talk uses: the feeder drops channel-3 frames
        # whose counter it has already seen, so a per-upload counter from 0 broke every
        # upload after the first.
        p = _header(total, sess._seq, sess._R, self.channel)
        p[28:48] = rdt
        p[48:] = data
        s.sendto(transcode(bytes(p)), sess._cam)
        sess._seq += 1

    def _send_data(self, sess, s, seq, payload):
        self._send(sess, s, rdt_header(DATA, len(payload), seq), payload)
        self._unacked[seq] = (payload, time.monotonic())

    def _finish(self, error=None):
        if error and not self.error:
            self.error = error
        self.state = "idle"
        self._done.set()

    def pump(self, sess, s) -> None:
        if self.state == "idle":
            return
        now = time.monotonic()
        if self.state == "closing":
            if now - self._closed_at > CLOSE_GRACE:
                self._log(f"[clip] {self._name}: closed (stop={self._stop_seen})")
                self._finish(None if self._stop_seen else "feeder closed before storing the clip")
            return
        if now > self._deadline:
            self._log(f"[clip] {self._name}: timed out in state {self.state}")
            self._finish(f"upload timed out ({self.state})")
            return
        if self.state == "hello" and now - self._last_ping > PING_EVERY:
            self._send(sess, s, rdt_header(PING, conn=0))
            self._last_ping = now
        for seq, (payload, sent) in list(self._unacked.items()):
            if now - sent > RESEND_AFTER:
                self._send_data(sess, s, seq, payload)
                self.resends += 1
        if self.state == "sending":
            while len(self._unacked) < WINDOW and self._next_chunk < len(self._chunks):
                self._send_data(sess, s, self._next_chunk + 1, self._chunks[self._next_chunk])
                self._next_chunk += 1

    def rx(self, sess, s, frame) -> None:
        parsed = parse_rdt(frame)
        if parsed is None or self.state == "idle":
            return
        kind, seq, data = parsed
        if kind != ACK and self._trace_left > 0:
            self._trace_left -= 1
            self._log(f"[clip] rx {kind:#04x} seq={seq} rdt={bytes(frame[28:48]).hex()} "
                      f"{data[:12].hex()} ({self.state})")
        if kind == PING:
            self._cid = frame[45]
            self._send(sess, s, rdt_header(PONG, seq=seq, conn=0))
        elif kind == PONG:
            if self.state == "hello":
                self.state = "await_name"
        elif kind == ACK:
            self._unacked.pop(seq, None)
        elif kind == DATA:
            self._send(sess, s, rdt_header(ACK, seq=seq))
            if seq in self._seen or not data:
                return
            self._seen.add(seq)
            self._on_message(sess, s, data[0], data[1:].split(b"\0")[0])
        elif kind == FIN:
            self._send(sess, s, rdt_header(CLOSE, conn=0))
            self._send(sess, s, rdt_header(FIN_ACK, fin=True))
            self._start_close()
        elif kind == CLOSE:
            if self.state == "closing":
                self._send(sess, s, rdt_header(CLOSE, conn=0))
                self._finish(None if self._stop_seen else "feeder closed before storing the clip")

    def _on_message(self, sess, s, kind, text) -> None:
        if kind == MSG_NAME:
            if self.state in ("hello", "await_name"):
                self.state = "await_start"
                self._send_data(sess, s, 0, message(MSG_SIZE, str(len(self._wav)).encode()))
        elif kind == MSG_START:
            if self.state == "await_start":
                self.state = "sending"
                self._log(f"[clip] {self._name}: feeder ready, sending {len(self._wav)} bytes")
        elif kind == MSG_STOP:
            self._stop_seen = True
            if self._unacked or self._next_chunk < len(self._chunks):
                self.error = "feeder stopped the transfer early"
            self._log(f"[clip] {self._name}: feeder stored the clip "
                      f"({len(self._chunks)} frames, {self.resends} resent)")
            self._start_close()
        else:
            self._log(f"[clip] {self._name}: feeder message {kind:#04x} {text!r}")

    def _start_close(self) -> None:
        if self.state != "closing":
            self.state = "closing"
            self._closed_at = time.monotonic()
            if self._stop_seen:
                self._done.set()               # the clip is stored; closing is best effort

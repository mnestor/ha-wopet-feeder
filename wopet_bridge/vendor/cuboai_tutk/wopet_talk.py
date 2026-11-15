"""Two-way talk for the WOPET feeder, run on the AV reader thread (the sole socket sender).

Decoded from a WOpet app capture (2026-11-15):

1. Host sends IOCTL 0x0350 (SPEAKERSTART) on channel 0, payload u32 channel (1) + 4 zero bytes.
2. The feeder logs in to us on that channel (598-byte frames, [28]=0x00, [29]=0x00 then 0x20;
   the 0x20 frame carries a session token at [48:52]).
3. Host grants (88 bytes, [29]=0x21, token echoed) and sends one 64-byte [29]=0x10 frame.
4. Host streams audio frames: [28]=0x03, 16-bit PCM 8 kHz mono, 50 ms (800 bytes) each, followed
   by the feeder's own 16-byte audio trailer (codec 0x8C, flags 0x02, u32 ms timestamp at +12).
   The feeder acknowledges with [28]=0x09 frames: C at [36:38], count of missing frames at [42:44],
   the missing offsets from C at [50:].
5. Host sends IOCTL 0x0351 (SPEAKERSTOP), payload u32 channel.
"""

from __future__ import annotations

import collections
import struct
import threading
import time

from cuboai_pure import _CLIENT_FINGERPRINT, build_ioctl_data, transcode

TALK_CHANNEL = 1
FRAME_BYTES = 800                 # 50 ms of 16-bit PCM at 8 kHz
FRAME_SECONDS = 0.05
IDLE_STOP_SECONDS = 1.5           # stop the speaker this long after the last audio arrives
LOGIN_TIMEOUT_SECONDS = 5.0
SPEAKER_START = 0x0350
SPEAKER_STOP = 0x0351
# Grant capability word the app sent (not the feeder's own); kept as captured.
_GRANT_CAP = bytes.fromhex("0c6dd16b")
_GRANT_TAIL = bytes.fromhex("00000000000000000000030000000000")   # grant [72:88]
_SETUP_BODY = bytes.fromhex("00100b000000000000100000010000000800000000000000000000004000000000010000")


def _header(total, seq, R, channel):
    p = bytearray(total)
    p[0:4] = b"\x04\x02\x1a\x0a"
    struct.pack_into("<H", p, 4, total - 16)
    struct.pack_into("<H", p, 6, seq & 0xFFFF)
    p[8:12] = b"\x07\x04\x21\x00"
    struct.pack_into("<H", p, 12, R & 0xFFFF)
    p[14] = channel & 0xFF
    p[16:20] = b"\x0c\x00\x00\x00"
    struct.pack_into("<H", p, 20, R & 0xFFFF)
    p[22:28] = _CLIENT_FINGERPRINT
    return p


def talk_grant_plain(R, channel, seq, token):
    p = _header(88, seq, R, channel)
    p[28:32] = b"\x00\x21\x0b\x00"
    p[32:36] = _GRANT_CAP
    struct.pack_into("<I", p, 44, 0x24)
    p[48:52] = token
    p[56:60] = b"\x00\x01\x00\x01"
    p[64:68] = b"\x04\x00\x00\x00"
    p[68:72] = b"\xfb\x07\x1f\x00"
    p[72:88] = _GRANT_TAIL
    return bytes(p)


def talk_setup_plain(R, channel, seq):
    p = _header(64, seq, R, channel)
    p[28:64] = _SETUP_BODY
    return bytes(p)


def audio_trailer(ts_ms):
    t = bytearray(16)
    t[0] = 0x8C
    t[2] = 0x02                     # 16-bit; rate index 0 = 8 kHz; mono
    struct.pack_into("<I", t, 12, ts_ms & 0xFFFFFFFF)
    return bytes(t)


def talk_audio_plain(R, channel, seq, relseq, index, pcm, ts_ms, flush=True):
    """One audio frame; `index` is the per-talk message index (0, 1, 2, ...)."""
    au = bytes(pcm) + audio_trailer(ts_ms)
    p = _header(56 + len(au), seq, R, channel)
    p[28] = 0x03
    p[29] = 0x05 if flush else 0x01  # 0x04: last frame of a burst (the feeder acks it)
    p[30] = 0x0B
    struct.pack_into("<I", p, 32, relseq & 0xFFFFFFFF)
    struct.pack_into("<HH", p, 36, 0x0103, index & 0xFFFF)
    struct.pack_into("<HH", p, 40, 0x0001, 0x0010)
    struct.pack_into("<I", p, 44, len(au))
    struct.pack_into("<II", p, 48, index & 0xFFFFFFFF, (index + 1) & 0xFFFFFFFF)
    p[56:] = au
    return bytes(p)


class WopetTalk:
    """Thread-safe talk queue; pump()/rx() run on the reader thread, feed() anywhere."""

    def __init__(self, channel=TALK_CHANNEL, log=None):
        self.channel = channel
        self._log = log or (lambda _m: None)
        self._lock = threading.Lock()
        self._pcm = bytearray()
        self._last_feed = 0.0
        self._reset()

    def _reset(self):
        self.state = "idle"           # idle -> starting -> granted -> idle
        self._started = 0.0
        self._token = None
        self._next_due = None
        self._last_sent = 0.0
        self._index = 0
        self._relseq = 2              # the app's first audio frame used 2 (0 = setup, 1 = an ack)
        self._sent = collections.OrderedDict()
        self.frames_sent = 0
        self.resends = 0
        self.acked = -1                # highest frame index the feeder confirmed ([38:40] of 0x09)

    # ── any thread ──
    def feed(self, pcm: bytes) -> None:
        """Queue 16-bit little-endian PCM, 8 kHz mono."""
        with self._lock:
            self._pcm += pcm
            self._last_feed = time.monotonic()
            if len(self._pcm) > FRAME_BYTES * 200:          # cap ~10 s of backlog
                del self._pcm[: len(self._pcm) - FRAME_BYTES * 200]

    @property
    def active(self) -> bool:
        return self.state != "idle"

    # ── reader thread ──
    def _send(self, sess, s, plain):
        s.sendto(transcode(plain), sess._cam)
        sess._seq += 1

    def _ioctl(self, sess, s, io_type, payload):
        s.sendto(build_ioctl_data(sess._R, sess._seq, sess._relseq, sess._frmno, io_type, payload),
                 sess._cam)
        sess._seq += 1
        sess._relseq += 1
        sess._frmno += 1

    def pump(self, sess, s) -> None:
        now = time.monotonic()
        with self._lock:
            pending = len(self._pcm)
            idle_for = now - self._last_feed
        if self.state == "idle":
            if pending >= FRAME_BYTES:
                self._ioctl(sess, s, SPEAKER_START, struct.pack("<I", self.channel) + bytes(4))
                self.state = "starting"
                self._started = now
                self._log("[talk] speaker start sent")
            return
        if self.state == "starting":
            if now - self._started > LOGIN_TIMEOUT_SECONDS:
                self._log("[talk] feeder did not log in to the talk channel; giving up")
                self.stop(sess, s)
            return
        # granted: send every frame that is due on a 50 ms grid
        if self._next_due is None:
            self._next_due = now
        while now >= self._next_due:
            with self._lock:
                if len(self._pcm) < FRAME_BYTES:
                    break
                pcm = bytes(self._pcm[:FRAME_BYTES])
                del self._pcm[:FRAME_BYTES]
            plain = talk_audio_plain(sess._R, self.channel, sess._seq, self._relseq, self._index,
                                     pcm, int(time.time() * 1000))
            self._send(sess, s, plain)
            self._sent[self._index & 0xFFFF] = pcm
            while len(self._sent) > 128:
                self._sent.popitem(last=False)
            self._relseq += 1
            self._index += 1
            self.frames_sent += 1
            self._last_sent = now
            self._next_due += FRAME_SECONDS
            if now - self._next_due > 8 * FRAME_SECONDS:     # fell behind: resync, no burst
                self._next_due = now + FRAME_SECONDS
        with self._lock:
            empty = len(self._pcm) < FRAME_BYTES
        # the feeder buffers about half a second, so wait after the last frame *sent* too
        if empty and min(idle_for, now - self._last_sent) > IDLE_STOP_SECONDS:
            self.stop(sess, s)

    def rx(self, sess, s, dec) -> None:
        """A decoded frame on the talk channel."""
        sub = dec[28]
        if sub == 0x00 and len(dec) >= 300 and self.state == "starting":
            if dec[29] == 0x20:
                self._token = bytes(dec[48:52])
                self._send(sess, s, talk_grant_plain(sess._R, self.channel, sess._seq, self._token))
                self._send(sess, s, talk_setup_plain(sess._R, self.channel, sess._seq))
                self.state = "granted"
                self._next_due = time.monotonic()   # frames are due from the grant on
                self._log("[talk] feeder logged in; streaming audio")
            return
        if sub == 0x09 and self.state == "granted" and len(dec) >= 44:
            base, top = struct.unpack_from("<HH", dec, 36)
            count = struct.unpack_from("<H", dec, 42)[0]
            if top != 0xFFFF:
                self.acked = max(self.acked, top)
            if base == 0xFFFF or not 0 < count < 64:
                return
            for k in range(min(count, (len(dec) - 50) // 2)):
                idx = (base + struct.unpack_from("<H", dec, 50 + 2 * k)[0]) & 0xFFFF
                pcm = self._sent.get(idx)
                if pcm is None:
                    continue
                self._send(sess, s, talk_audio_plain(sess._R, self.channel, sess._seq, self._relseq,
                                                     idx, pcm, int(time.time() * 1000)))
                self._relseq += 1
                self.resends += 1

    def stop(self, sess, s) -> None:
        if self.state != "idle":
            try:
                self._ioctl(sess, s, SPEAKER_STOP, struct.pack("<I", self.channel))
            except OSError:
                pass
            self._log(f"[talk] speaker stop ({self.frames_sent} frames sent, feeder confirmed up to "
                      f"frame {self.acked}, {self.resends} resent)")
        with self._lock:
            self._pcm.clear()
        self._reset()

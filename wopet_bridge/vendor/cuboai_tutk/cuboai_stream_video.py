#!/usr/bin/env python3
"""
cuboai_stream_video.py — HEVC video stream for go2rtc (CuboAI camera, pure Python).

Connects to a CuboAI camera and writes a continuous media stream to stdout, designed to be used
as an 'exec' source in go2rtc.

DEFAULT (production): MPEG-TS carrying per-AU PTS from the camera FRAMEINFO, with the FRAMEINFO
trailer stripped, selective-repeat loss recovery, and clean-GOP gating — the proven stack that
plays in MSE/HLS/WebRTC. `--raw` reverts to the original byte-for-byte HEVC Annex-B passthrough
(the byte-identical regression anchor). `--output-format annexb` keeps Annex-B but still strips
the trailer. Audio (AAC) is available, gated behind CUBOAI_MUX_AUDIO (default off).

STATUS: Working at ~10–12 fps. The pure-Python transport's background reader keeps the camera's
send-window open even if go2rtc's pipe back-pressures, so the stream does not stall (session-14
C/D windowed-ACK fix).

Usage in go2rtc config (go2rtc.yaml):
    streams:
      cuboai_video:
        - exec:python3 /path/to/cuboai_stream_video.py#killsignal=15#killtimeout=8

Environment variables (required):
    CUBOAI_UID        Device UID (license_id from the REST API)
    CUBOAI_ACCOUNT    dev_admin_id  (e.g. admin@YOUR_DEVICE_HEX)
    CUBOAI_PASSWORD   dev_admin_pwd
    CUBOAI_CAMERA_IP  LAN IP of camera (optional but recommended for LAN)
    CUBOAI_LIB        Path to libIOTCAPIs_ALL.so (optional, auto-detected)

Why exec source?
    go2rtc's 'exec' source runs a subprocess and reads its stdout as a
    media stream. This avoids the need for a full RTSP server and lets
    go2rtc handle all the WebRTC/HLS/RTSP re-streaming to Home Assistant.

Video format:
    Raw HEVC (H.265) Annex B bytestream, no container.
    Each frame starts with 00 00 00 01 (start code).
    Keyframes start with 00 00 00 01 40 (VPS NAL unit, type 32).
    Camera streams ~10-12 fps with a keyframe roughly every 3 s (P-frames between).
    Resolution: 2560x1440 on the test camera (depends on firmware/setting).

Known limitations:
    - Resolution cannot be changed via IOCTL (camera ignores it).
    - Frame rate is fixed by camera firmware.
    - Audio (AAC, interleaved on the same channel) is muxed into the TS only when
      CUBOAI_MUX_AUDIO=1; go2rtc transcodes it to Opus for the WebRTC leg.

See also:
    cuboai_stream_audio.py — standalone audio stream (legacy)
    LIBRARY_SETUP.md       — native library notes (the deployment runs pure Python)
"""

import argparse
import os
import struct
import sys

# ── Locate our modules ────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'wopet'))

from cuboai_session import get_session   # auto: PureSession (no --lib) or TUTKSession
from wopet_control import start_control_server


def is_annexb_keyframe(au):
    """Return whether an Annex-B AU can safely open a new HEVC stream.

    go2rtc probes an exec source from its first NAL and requires VPS/SPS/PPS or
    an IRAP frame. A reconnect may begin mid-GOP, so forwarding an ordinary
    P-frame first makes go2rtc reject the source and immediately respawn it.
    """
    return (len(au) >= 5 and au[:4] == b'\x00\x00\x00\x01'
            and ((au[4] >> 1) & 0x3f) in (32, 33, 34, 19, 20, 21))


def is_safe_hevc_annexb_au(au):
    """Reject malformed AUs before go2rtc's HEVC packetizer sees them.

    A damaged/reassembled frame can contain a one-byte NAL between Annex-B
    delimiters. go2rtc 1.9.14 assumes both HEVC header bytes exist and panics on
    that input, restarting the whole app. Valid HEVC NALs have two header bytes,
    a clear forbidden bit, and a non-zero temporal_id_plus1 field.
    """
    starts = []
    i = 0
    while i <= len(au) - 3:
        if au[i:i + 4] == b'\x00\x00\x00\x01':
            starts.append((i, 4))
            i += 4
        elif au[i:i + 3] == b'\x00\x00\x01':
            starts.append((i, 3))
            i += 3
        else:
            i += 1

    if not starts or starts[0][0] != 0:
        return False

    for index, (offset, marker_len) in enumerate(starts):
        nalu_start = offset + marker_len
        nalu_end = starts[index + 1][0] if index + 1 < len(starts) else len(au)
        if nalu_end - nalu_start < 2:
            return False
        high, low = au[nalu_start], au[nalu_start + 1]
        if high & 0x80 or not low & 0x07:
            return False
    return True


# ── Codec dispatch (Wopet feeder fork) ─────────────────────────────────────
# Upstream's D100 camera sends HEVC; the WOPET Camera Pet Feeder sends H.264. Read
# with the HEVC rules above, H.264 slice data fails the temporal_id test a few
# frames into every GOP, which dropped all but ~4 frames per 2-second GOP (about
# 2 fps of a 15 fps stream). CUBOAI_VIDEO_CODEC selects the rules; the default stays
# HEVC for upstream behaviour.

# Exit status when the feeder stops sending video; wopet_stream.py reconnects quickly.
VIDEO_STALL_EXIT = 3


def pacing_interval(arrivals, backlog):
    """Seconds to wait after writing a frame: the recent mean frame interval, clamped,
    shortened while frames are queued so latency cannot grow without bound."""
    if len(arrivals) >= 2 and arrivals[-1] > arrivals[0]:
        interval = (arrivals[-1] - arrivals[0]) / (len(arrivals) - 1)
    else:
        interval = 1 / 15
    interval = min(max(interval, 0.025), 0.125)
    if backlog > 15:
        return 0.005
    if backlog > 4:
        return interval / 2
    return interval


def video_codec():
    codec = os.environ.get('CUBOAI_VIDEO_CODEC', 'hevc').strip().lower()
    return 'h264' if codec in ('h264', 'avc') else 'hevc'


def _h264_nal_spans(au):
    """(start, end) of each NAL payload in an Annex-B AU, or None if it does not start at 0."""
    starts = []
    i = 0
    while i <= len(au) - 3:
        if au[i:i + 4] == b'\x00\x00\x00\x01':
            starts.append((i, 4))
            i += 4
        elif au[i:i + 3] == b'\x00\x00\x01':
            starts.append((i, 3))
            i += 3
        else:
            i += 1
    if not starts or starts[0][0] != 0:
        return None
    return [
        (off + n, starts[k + 1][0] if k + 1 < len(starts) else len(au))
        for k, (off, n) in enumerate(starts)
    ]


def is_h264_annexb_keyframe(au):
    """An H.264 AU that can open a stream: it carries an SPS (7) or an IDR slice (5)."""
    spans = _h264_nal_spans(au)
    return bool(spans) and any(e > s and au[s] & 0x1f in (5, 7) for s, e in spans)


def is_safe_h264_annexb_au(au):
    """Every NAL has its header byte and a clear forbidden bit."""
    spans = _h264_nal_spans(au)
    return bool(spans) and all(e > s and not au[s] & 0x80 for s, e in spans)


H264_FRAMEINFO_LEN = 16


def strip_h264_frameinfo(au):
    """Drop the feeder's 16-byte TUTK FRAMEINFO trailer from an H.264 access unit.

    The feeder appends the older 16-byte struct (codec_id u16 = 0x4e/H.264, flags,
    cam_index, onlineNum, reserve1[3], reserve2 u32, timestamp u32 ms), not the 24-byte
    one the upstream strip recognises. Left in, its timestamp bytes sometimes form an
    Annex-B start code and go2rtc cuts fake NAL units out of it ("slice type 32 too
    large" in decoders). Only strip when codec, cam_index and reserve2 all match, so
    slice data is never cut.
    """
    n = H264_FRAMEINFO_LEN
    if len(au) < n + 5:
        return au
    tail = au[-n:]
    if tail[0] == 0x4E and tail[1] == 0x00 and tail[3] == 0x00 and tail[9:12] == b'\x00\x00\x00':
        return au[:-n]
    return au


def h264_frameinfo(au):
    """Strip the feeder's 16-byte trailer and return (au, frameinfo or None).

    The frameinfo carries the trailer's millisecond timestamp (offset 12), so the
    MPEG-TS path can stamp frames with the camera clock instead of arrival time.
    """
    stripped = strip_h264_frameinfo(au)
    if stripped is au:
        return au, None
    kf = is_h264_annexb_keyframe(stripped)
    return stripped, {
        'codec_id': 0x4E,
        'codec': 'h264',
        'timestamp_ms': struct.unpack_from('<I', au, len(au) - 4)[0],
        'ts_valid': True,
        'keyframe': kf,
        'is_keyframe': kf,
    }


# Audio and video trailer clocks further apart than this are not the same clock.
AUDIO_TS_MAX_SKEW_MS = 3000


def opens_annexb_stream(au):
    if video_codec() == 'h264':
        return is_h264_annexb_keyframe(au)
    return is_annexb_keyframe(au)


def is_safe_annexb_au(au):
    if video_codec() == 'h264':
        return is_safe_h264_annexb_au(au)
    return is_safe_hevc_annexb_au(au)


# ── Production env profile ─────────────────────────────────────────────────
# The proven streaming stack — MPEG-TS container + FRAMEINFO strip + selective-repeat loss
# recovery + clean-GOP — is the default so the engine works out of the box. Applied via
# os.environ.setdefault() BEFORE get_session() (the engine reads these at construction); every
# value stays overridable by an explicit env var, and --raw forces the passthrough profile.
#
# IMPORTANT: cuboai_pure.py keeps every one of these gates OFF by default — it is a neutral
# library shared by many tools (cli, validate, snapshot). The PRODUCTION profile is a property
# of THIS streaming entry point, not of the engine. So "no flags here" == production, while the
# engine on its own stays vanilla.  (CUBOAI_SELECTIVE_ACK / CUBOAI_GRACE_SCALE /
# CUBOAI_ECHO_CAMCLOCK already default ON inside the engine, so they are not repeated here.)
PRODUCTION_ENV = {
    'CUBOAI_OUTPUT_FORMAT':    'mpegts',   # MPEG-TS w/ per-AU PTS (MSE/HLS play along currentTime)
    'CUBOAI_STRIP_FRAMEINFO':  '1',        # drop the 24B trailer (HW decoders choke on it → black)
    'CUBOAI_NODROP':           '1',        # in-order never-skip seal (no POC-gap on a refs=1 stream)
    'CUBOAI_LONE_HOLE':        '1',        # pad a count-1 SACK to count≥2 so the camera resends it
    'CUBOAI_TRUNCATE_PARTIAL': '1',        # emit a clean prefix slice, never a bridged garbage AU
    'CUBOAI_GAP_DEPTH_CAP':    '200',      # let una hold past a ~69-frag keyframe burst before jumping
    'CUBOAI_LONE_SKIP_ROUNDS': '20',       # padded-request rounds before giving up on a lone hole
    'CUBOAI_RECOVERY_HOLD':    '24',       # hold a present-incomplete AU ~1s to catch a late resend
    'CUBOAI_CLEAN_GOP':        '1',        # mpegts path: emit only complete AUs, resync at IDR
    'CUBOAI_MUX_AUDIO':        '1',        # mux interleaved AAC into the TS (combined A/V by default)
}

# --raw / --passthrough: the historical byte-for-byte Annex-B passthrough. Forces the OUTPUT
# transform fully OFF (annexb container, no FRAMEINFO strip, no recovery/seal gates) so the
# emitted bytes equal the original pre-recovery engine output. This is the re-anchored
# byte-identical baseline — the regression guard for the core transport.
RAW_ENV = {
    'CUBOAI_OUTPUT_FORMAT':    'annexb',
    'CUBOAI_STRIP_FRAMEINFO':  '0',
    'CUBOAI_NODROP':           '0',
    'CUBOAI_LONE_HOLE':        '0',
    'CUBOAI_TRUNCATE_PARTIAL': '0',
    'CUBOAI_KF_GRACE':         '0',
    'CUBOAI_GRACE_SCALE':      '0',
    'CUBOAI_SELECTIVE_ACK':    '0',
    'CUBOAI_MUX_AUDIO':        '0',
}


def _env_float(name: str, default: float) -> float:
    """Parse a CUBOAI_* env var as float, falling back to `default` on empty/non-numeric input
    (B-8/R3 — the previous bare float(os.environ[...]) raised an uncaught ValueError on garbage)."""
    try:
        raw = os.environ.get(name, '')
        return float(raw) if raw else float(default)
    except (TypeError, ValueError):
        return float(default)


def _clamp_env_knobs():
    """Range-clamp the numeric CUBOAI_* tuning env vars (startup only, stderr only) so a zero/absurd
    override can't divide-by-zero or break recovery. Non-numeric -> default; out-of-range -> clamp.

    Lives in the shared streamer module (B-8) so BOTH the production streamer (apply_env_profile)
    and the validate CLI sanitise identically — the streamer used to skip clamping entirely, so a
    bad CUBOAI_* only got caught under `cuboai_validate`, never on the go2rtc exec path."""
    KNOBS = [  # (env, lo, hi, default-or-None, is_float)
        ('CUBOAI_GAP_DEPTH_CAP', 1, 10000, 200, False),
        ('CUBOAI_RECOVERY_HOLD', 0, 2000, 24, False),
        ('CUBOAI_LONE_SKIP_ROUNDS', 0, 2000, 20, False),
        ('CUBOAI_KF_HOLD', 0, 2000, 40, False),
        ('CUBOAI_GAP_HOLD_MS', 1, 600000, None, False),
        ('CUBOAI_VERBOSE_INTERVAL', 0.1, 3600, 5.0, True),
        ('CUBOAI_VERBOSE_CAMERA_STATS_INTERVAL', 1, 86400, None, False),
        ('CUBOAI_LIST_PACE_S', 0, 3600, 0.5, True),   # B-8/R3: also parsed with a bare float()
    ]
    for env, lo, hi, dflt, isf in KNOBS:
        raw = os.environ.get(env)
        if not raw:
            continue
        try:
            v = float(raw) if isf else int(raw)
        except ValueError:
            if dflt is not None:
                os.environ[env] = str(dflt)
                print(f"Warning: {env}={raw!r} is not a number; using default {dflt}.", file=sys.stderr)
            continue
        cv = min(hi, max(lo, v))
        if cv != v:
            os.environ[env] = str(cv if isf else int(cv))
            print(f"Warning: {env}={raw} out of range [{lo},{hi}]; clamped to {cv}.", file=sys.stderr)


def apply_env_profile(raw: bool) -> str:
    """Install the env profile and return the resolved output format.

    raw=True  → hard-force the passthrough profile (a stray production env in the shell can NOT
                defeat --raw; the byte-identical anchor must be reproducible).
    raw=False → setdefault the production profile (each value still overridable by an explicit
                env var). The engine reads these at construction, so this MUST run before
                get_session().
    """
    _clamp_env_knobs()          # B-8: sanitise numeric CUBOAI_* on the streamer path too (was
                                # validate-only) — a bad knob must never reach get_session().
    if raw:
        for k, v in RAW_ENV.items():
            os.environ[k] = v
        return 'annexb'
    for k, v in PRODUCTION_ENV.items():
        os.environ.setdefault(k, v)
    return (os.environ.get('CUBOAI_OUTPUT_FORMAT') or 'mpegts').lower()


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _linear_to_alaw(sample):
    """Encode one signed 16-bit PCM sample as ITU G.711 A-law."""
    sample = max(-32768, min(32767, sample)) >> 3
    mask = 0xD5 if sample >= 0 else 0x55
    if sample < 0:
        sample = -sample - 1
    boundaries = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)
    segment = next((i for i, bound in enumerate(boundaries) if sample <= bound), 8)
    if segment >= 8:
        return 0x7F ^ mask
    value = segment << 4
    value |= ((sample >> 1) if segment < 2 else (sample >> segment)) & 0x0F
    return value ^ mask


def pcm_s16le_to_alaw(data):
    """Convert mono little-endian PCM16 to browser/WebRTC-compatible PCMA."""
    usable = len(data) - (len(data) % 2)
    return bytes(
        _linear_to_alaw(int.from_bytes(data[i:i + 2], "little", signed=True))
        for i in range(0, usable, 2)
    )


def _verbose_loop(sess, interval, camera_stats, stop):
    """Periodic stream-health to STDERR ONLY (stdout is the media pipe — never touched).

    Runs on a daemon thread reading the engine's read-only get_stats() snapshot (lock-free,
    no socket I/O) and pairing successive snapshots through cuboai_pure.stats_delta for the
    interval fps/bitrate/loss/recovery — the same Task-1 metric set the benchmark prints.
    With camera_stats it also folds the camera 0x0934 session-stats at a slower cadence
    (injected on the reader thread via get_during_stream, so it never races the AV socket).
    Decoupled from the engine's own verbose (_vlog prints to stdout) so media stays clean.
    """
    import time as _t
    import cuboai_pure as cp
    if not hasattr(sess, 'get_stats'):
        _stderr("[health] verbose stats need the pure-Python backend — disabled.")
        return
    prev = None
    t0 = _t.time()
    tick = 0
    while not stop.wait(interval):          # first line after `interval` s; exits when stopped
        try:
            cur = sess.get_stats()
        except Exception:
            continue
        d = cp.stats_delta(prev, cur)
        prev = cur
        tsv = cur['ts_valid'] + cur['ts_garbage']
        gpct = (100.0 * cur['ts_garbage'] / tsv) if tsv else 0.0
        line = (f"[health t={_t.time() - t0:.0f}s] fps {d['fps']:.1f} "
                f"{d['bitrate_kbps'] / 1000.0:.1f}Mbps | loss {d['loss_pct']:.1f}% "
                f"recov {cur['recovery_pct']:.0f}% (req {d['resend_req']} rec {d['recovery_events']}) | "
                f"gap {cur['gap_now']} (max {cur['gap_max']}, capjmp {cur['gap_cap_jumps']}) | "
                f"audio {d['au_audio'] / d['interval_s']:.1f}fps | "
                f"incAU {d['au_incomplete']} kf {d['kf_incomplete']}/{d['kf_total']} | "
                f"ts garbage {gpct:.0f}% regress {cur['ts_regress']}")
        # SILENT-RECOVERY VISIBILITY (audit 2026-07-23). ioctl()'s auto-reconnect masked the
        # ghost-conn bug for weeks on Linux — a path that quietly recovers must never read as
        # healthy. Print the send side + the recoveries ONLY when non-zero, so a clean session's
        # line is unchanged and any occurrence stands out. stderr only (stdout is the media pipe).
        _rec = (cur.get('reconnects', 0), cur.get('ioctl_retries', 0),
                cur.get('tx_ioctl_retx', 0), cur.get('tx_err', 0), cur.get('keepalive_err', 0))
        if any(_rec):
            line += (f" | RECOVERED reconn {_rec[0]} (fail {cur.get('reconnect_fail', 0)}, "
                     f"{cur.get('reconnect_s', 0)}s) ioctl-retry {_rec[1]} ioctl-retx {_rec[2]} "
                     f"txerr {_rec[3]} kaerr {_rec[4]}")
        if camera_stats and tick % 6 == 0:          # slow cadence (~6× the interval)
            try:
                ss = sess.get_during_stream('get_session_stats', timeout=1.5) or {}
                vs = ss.get('video') or {}
                if ss.get('mode'):
                    line += f" | cam {ss['mode']}"
                    if vs.get('resendBufferUsage'):
                        line += f" rbuf {vs['resendBufferUsage']}"
                    if vs.get('send_err_count'):
                        line += f" serr {vs['send_err_count']}"
            except Exception:
                pass
        tick += 1
        _stderr(line)


def mux_timed_stream(frames_timed, emit, *, clean_gop=True, mux_audio=False, log=_stderr,
                     tap=None, audio_tap=None):
    """Mux a (kind, data, frameinfo) access-unit stream into MPEG-TS, writing each AU's TS bytes via
    emit(bytes). Single source of truth for the live mpegts path AND the validator — replaying a
    recorded fixture through it tests the real muxer, not a copy.

    Video PTS comes from the camera FRAMEINFO through PTSClock (interpolated when a trailer is
    absent); clean_gop drops incomplete VIDEO AUs until the next clean IDR so MSE/HLS never sees a
    broken GOP.

    mux_audio (CUBOAI_MUX_AUDIO): when True, add a second AAC ES — audio AUs get a PTS from the
    camera audio ts via AudioTimeline through a PTSClock that SHARES video's base (A/V sync; NOT a
    free-running cadence), and are interleaved on the audio PID in arrival (≈PTS) order. clean_gop
    never gates audio (audio has no GOP). When False the output is video-only and BYTE-IDENTICAL to
    the pre-audio muxer (one-ES PMT, no audio PID, the original now_ms PSI cadence).

    tap/audio_tap, if given, are called per muxed video/audio AU with (pts_90k, keyframe, pts_ms);
    the live path passes None so a 24/7 stream retains no per-AU state. Returns the PTSClock stats.
    """
    from cuboai_pts import AVTimeline
    from cuboai_mpegts import TSMuxer

    def _nal_kf(au):
        return opens_annexb_stream(au)

    # AVTimeline is the single source of truth for shared-base A/V PTS (also used by
    # cuboai_pure.record_video) so the live stream and a saved .mp4 stay in lockstep. Its audio
    # clock is created but only fed when mux_audio → the audio-off path is byte-identical.
    wopet_pcm = os.environ.get("CUBOAI_WOPET_AUDIOSTART") == "1"
    avc = AVTimeline(audio_nominal_ms=(50.0 if wopet_pcm else 64.0))
    mux = None; _warned = False
    audio_primed = False
    # Wopet feeder (H.264): strip the 16-byte trailer here too, take PTS from its
    # timestamp, and drop damaged AUs until the next keyframe (as the Annex-B path does).
    h264 = video_codec() == 'h264'
    h264_waiting = True
    last_video_ts = None
    audio_ts_mode = None
    # CUBOAI_AV_DEBUG=<file>: trace kind, camera timestamp and PTS of the first 20000 AUs.
    _dbg_path = os.environ.get('CUBOAI_AV_DEBUG')
    _dbg = open(_dbg_path, 'w', buffering=1) if _dbg_path else None
    _dbg_n = [0]

    def _trace(kind, ts, pts_ms):
        if _dbg is not None and _dbg_n[0] < 20000:
            _dbg_n[0] += 1
            import time as _t
            _dbg.write(f"{_t.time():.3f} {kind} {ts} {pts_ms:.1f}\n")
    synced = not clean_gop; _cg_drop = 0
    psi_now = [0]                                # monotonic PSI cadence clock (audio path only)

    if clean_gop:
        log("[clean_gop] ON — emitting only complete AUs, resync at IDR after any hole")
    if mux_audio:
        audio_name = "PCMA (from Wopet PCM)" if wopet_pcm else "AAC"
        log(f"[mux_audio] ON — interleaving {audio_name} audio (shared-base PTS) on a second TS PID")
    for kind, data, fi in frames_timed:
        if kind == 'audio':
            if not mux_audio or mux is None:         # need the muxer (built on first video AU)
                continue
            if (fi or {}).get('codec') == 'pcm_s16le':
                data = pcm_s16le_to_alaw(data)
            ats = (fi or {}).get('timestamp_ms') if h264 else None
            if ats is not None and last_video_ts is not None:
                use_ts = abs(ats - last_video_ts) <= AUDIO_TS_MAX_SKEW_MS
                if use_ts != audio_ts_mode:
                    audio_ts_mode = use_ts
                    log("[mux_audio] audio PTS from the " +
                        ("camera clock" if use_ts else "frame cadence (trailer clock disagrees with video)"))
                ta = avc.audio_ms(ats) if use_ts else avc.audio(None)
            else:
                ta = avc.audio(fi)                   # shared-base; lost trailer → AAC-cadence interp
            _trace('a', ats, ta['pts_ms'])
            now = max(psi_now[0], int(ta['pts_ms'])); psi_now[0] = now
            emit(mux.mux_audio_au(data, ta['pts_90k'], now_ms=now))
            if audio_tap is not None:
                audio_tap(ta['pts_90k'], ta['keyframe'], ta['pts_ms'])
            continue
        if kind != 'video':
            continue
        if h264:
            if fi is None:
                data, fi = h264_frameinfo(data)
            if h264_waiting:
                if not opens_annexb_stream(data):
                    continue
                h264_waiting = False
            elif not is_safe_annexb_au(data):
                h264_waiting = True
                continue
            if fi is not None:
                last_video_ts = fi['timestamp_ms']
        if mux is None:
            codec = (fi or {}).get('codec') or video_codec()
            audio_codec = ('pcma' if wopet_pcm else 'aac') if mux_audio else None
            mux = TSMuxer(codec=codec, audio_codec=audio_codec)
            log(f"[mpegts] muxing {codec}{'+' + audio_codec if audio_codec else ''} → MPEG-TS with FRAMEINFO PTS "
                f"(stream_type=0x{mux.stream_type:02x})")
        if clean_gop:
            if fi is None:                           # incomplete AU → poison the GOP tail
                if synced:
                    log("[clean_gop] hole → desync, awaiting IDR")
                synced = False; _cg_drop += 1
                continue
            if not synced:
                # Some Wopet firmware (including 40.23.6.5) leaves the FRAMEINFO
                # keyframe flag clear even though the HEVC access unit contains an IDR.
                # Trust either signal, matching the proven Annex-B output path below.
                if fi.get('is_keyframe') or _nal_kf(data):
                    synced = True
                    log(f"[clean_gop] resync at IDR (dropped {_cg_drop} AUs)")
                else:
                    _cg_drop += 1
                    continue
        if fi is None and not _warned:
            log("[mpegts] AU without FRAMEINFO (strip off/incomplete) — interpolating PTS")
            _warned = True
        # only a VALID ts seeds the shared base (the ts_valid gate lives inside AVTimeline) →
        # audio-off byte-identical with the pre-refactor inline PTSClock.
        t = avc.video(fi, nal_keyframe=_nal_kf(data))
        # video-only path keeps the ORIGINAL now_ms (byte-identical); audio path uses a monotonic
        # PSI clock so interleaved A/V now_ms can't make the PAT/PMT cadence regress.
        now = int(t['pts_ms'])
        if mux_audio:
            now = max(psi_now[0], now); psi_now[0] = now
        # Firmware 40.23.6.5 can take more than go2rtc's probe window to
        # deliver its first listen frame. Emit one 50 ms PCMA silence frame
        # with the opening video timestamp so the audio PID is discovered on
        # every producer reconnect; real camera audio replaces it as soon as
        # it arrives.
        if mux_audio and wopet_pcm and not audio_primed:
            emit(mux.mux_audio_au(b'\xd5' * 400, t['pts_90k'], now_ms=now))
            audio_primed = True
        _trace('v', (fi or {}).get('timestamp_ms'), t['pts_ms'])
        emit(mux.mux_au(data, t['pts_90k'], keyframe=t['keyframe'], now_ms=now))
        if tap is not None:
            tap(t['pts_90k'], t['keyframe'], t['pts_ms'])
    return avc.stats()


def main() -> None:
    # ── Configuration from CLI args (falling back to environment) ─────────
    ap = argparse.ArgumentParser(description="CuboAI raw HEVC video stream → stdout")
    ap.add_argument('--uid');       ap.add_argument('--account')
    ap.add_argument('--password');  ap.add_argument('--camera-ip')
    ap.add_argument('--lib', help='Path to libIOTCAPIs_ALL.so (omit → pure Python, the default)')
    ap.add_argument('--defer-start', action='store_true',
                    help='Re-enable the ~5s native startup defer (wire-fidelity). Default now starts '
                         'the stream immediately (the production behaviour).')
    ap.add_argument('--no-defer-start', action='store_true', help=argparse.SUPPRESS)  # back-compat no-op
    ap.add_argument('--output-format', choices=('annexb', 'mpegts'),
                    help='Output container. mpegts (default) = MPEG-TS carrying per-frame PTS from '
                         'the camera FRAMEINFO (MSE/HLS play along currentTime; auto-strips the '
                         'FRAMEINFO trailer). annexb = raw HEVC Annex-B (no timestamps).')
    ap.add_argument('--raw', '--passthrough', dest='raw', action='store_true',
                    help='Historical byte-for-byte Annex-B passthrough: forces --output-format '
                         'annexb and turns every FRAMEINFO-strip / loss-recovery / seal gate OFF. '
                         'The byte-identical regression anchor (overrides all of the above).')
    ap.add_argument('-v', '--verbose', action='store_true',
                    help='Print periodic stream-health metrics (loss%%, recovery, fps, bitrate, gaps, '
                         'PTS health) to STDERR only — stdout stays the media stream. Also CUBOAI_VERBOSE=1.')
    ap.add_argument('--verbose-interval', type=float, default=None, metavar='SECS',
                    help='Seconds between verbose health lines (default 5; or CUBOAI_VERBOSE_INTERVAL).')
    ap.add_argument('--verbose-camera-stats', action='store_true',
                    help='Also fold the camera 0x0934 session-stats into verbose output at a slower '
                         'cadence (injects a read onto the reader thread). Also CUBOAI_VERBOSE_CAMERA_STATS=1.')
    args = ap.parse_args()

    uid       = args.uid       or os.environ.get('CUBOAI_UID')
    account   = args.account   or os.environ.get('CUBOAI_ACCOUNT')
    password  = args.password  or os.environ.get('CUBOAI_PASSWORD')
    camera_ip = args.camera_ip or os.environ.get('CUBOAI_CAMERA_IP')
    lib_path  = args.lib       or os.environ.get('CUBOAI_LIB')

    # ── defer-start ───────────────────────────────────────────────────────
    # Default: start the stream immediately (the deployed/production behaviour — the wrapper always
    # passed --no-defer-start). --defer-start (or CUBOAI_DEFER_START) re-enables the ~5s native
    # startup defer for wire-fidelity. _defer=False starts fast; _defer=None follows full_fidelity
    # (~5s defer). --no-defer-start stays an accepted no-op so the existing wrapper keeps working.
    defer  = args.defer_start or os.environ.get('CUBOAI_DEFER_START', '0') != '0'
    _defer = None if defer else False

    # ── env profile (production setdefaults, or --raw passthrough) ─────────
    # Must run before get_session(): the engine reads its gate env vars at construction. An
    # explicit --output-format wins over the production default; --raw overrides everything.
    if args.raw and args.output_format and args.output_format != 'annexb':
        print("Error: --raw forces --output-format annexb (drop the conflicting --output-format).",
              file=sys.stderr)
        sys.exit(1)
    if args.output_format and not args.raw:
        os.environ['CUBOAI_OUTPUT_FORMAT'] = args.output_format
    output_format = apply_env_profile(args.raw)
    if args.raw:
        print("[raw] passthrough — annexb, FRAMEINFO strip + all recovery/seal gates OFF "
              "(byte-identical anchor)", file=sys.stderr, flush=True)

    if not all([uid, account, password]):
        print(
            "Error: --uid/--account/--password (or CUBOAI_UID/ACCOUNT/PASSWORD) required.",
            file=sys.stderr
        )
        sys.exit(1)

    # ── Connect (pure Python unless --lib/CUBOAI_LIB given) ───────────────
    # _defer (computed above): False = start immediately (default); None = follow full_fidelity
    # (~5s native defer). get_session forwards both kwargs; the native backend ignores them.
    # auto_discover_lib=False: pure is the GUARANTEED default for the deployment — only an EXPLICIT
    # --lib/CUBOAI_LIB selects native; a stray lib in ~ or a standard path can't override it.
    sess = get_session(uid, account, password, lib_path=lib_path, camera_ip=camera_ip,
                       defer_stream_start=_defer, defer_video_start_late=_defer,
                       auto_discover_lib=False)

    try:
        sess.connect()
    except Exception as e:
        print(f"Connection failed: {e}", file=sys.stderr)
        sys.exit(1)

    control_server = start_control_server(sess)

    # ── verbose health (STDERR only — stdout stays the media pipe) ────────
    # Decoupled from the engine's own connect/stream trace (which prints to stdout): this
    # reads the read-only get_stats() snapshot on a daemon thread and writes only to stderr.
    verbose = args.verbose or os.environ.get('CUBOAI_VERBOSE', '0') != '0'
    _v_stop = None
    if verbose:
        import threading as _threading
        v_interval = args.verbose_interval or _env_float('CUBOAI_VERBOSE_INTERVAL', 5.0)
        v_camera = (args.verbose_camera_stats
                    or os.environ.get('CUBOAI_VERBOSE_CAMERA_STATS', '0') != '0')
        _v_stop = _threading.Event()
        _threading.Thread(target=_verbose_loop, args=(sess, v_interval, v_camera, _v_stop),
                          daemon=True).start()
        _stderr(f"[health] verbose ON — metrics to stderr every {v_interval:g}s"
                + (" + camera session-stats" if v_camera else ""))

    # ── Stream video frames to stdout ────────────────────────────────────
    # go2rtc reads from stdout and handles the re-streaming.
    # We write raw HEVC Annex B frames — go2rtc detects the format from
    # the 00 00 00 01 start codes.
    import time as _time
    stdout = sys.stdout.buffer
    # Optional per-video-AU emit-timestamp trace (overnight #2 latency/jitter harness). Gated;
    # when CUBOAI_EMIT_TS_FILE is unset this is a no-op and the stream is byte-identical.
    _etsf = None
    _ets = os.environ.get('CUBOAI_EMIT_TS_FILE')
    if _ets:
        _etsf = open(_ets, 'w', buffering=1)

    # Stalled-consumer watchdog (WOPET feeder build): when go2rtc gives up on this
    # producer it can stop reading stdout without killing us. The write then blocks
    # forever, the session stops servicing camera replies, yet the control port stays
    # bound and reports healthy. Exit if a single write has been stuck too long.
    import queue as _queue_mod
    import threading as _threading
    _stall_limit = float(os.environ.get('WOPET_STDOUT_STALL_SECS', '15'))
    _write_started = [0.0]

    # Wopet feeder fork: a session the feeder stops serving (seen right after a restart,
    # while the feeder still held the previous sessions) stays open with no video and
    # never recovers. With go2rtc's preload nothing else restarts it, so exit with
    # VIDEO_STALL_EXIT and let the wrapper reconnect.
    _video_limit = float(os.environ.get('WOPET_VIDEO_STALL_SECS', '20'))
    _last_video = [_time.time()]

    # Wopet feeder fork: the feeder can stop sending listen audio while video keeps
    # flowing (seen 2026-11-15: no audio for a day on a preloaded session). The RTSP
    # stream still lists the audio track, so ffmpeg viewers that map it (Homebridge
    # live view and HKSV recording) wait forever and HKSV ends with reason 6 and zero
    # fragments. The feeder sends silence frames between sounds, so a gap in audio
    # means the audio leg is dead: exit with VIDEO_STALL_EXIT and reconnect.
    _audio_limit = float(os.environ.get('WOPET_AUDIO_STALL_SECS', '15'))
    _audio_watch = (output_format == 'mpegts'
                    and os.environ.get('CUBOAI_MUX_AUDIO', '0') != '0'
                    and _audio_limit > 0)
    _last_audio = [_time.time()]

    def _stall_watchdog():
        while True:
            _time.sleep(1.0)
            began = _write_started[0]
            if began and _time.time() - began > _stall_limit:
                print(f"stdout stalled {_stall_limit:.0f}s (no consumer); exiting",
                      file=sys.stderr, flush=True)
                os._exit(0)
            if _video_limit > 0 and _time.time() - _last_video[0] > _video_limit:
                print(f"no video from the feeder for {_video_limit:.0f}s; exiting to reconnect",
                      file=sys.stderr, flush=True)
                os._exit(VIDEO_STALL_EXIT)
            if _audio_watch and _time.time() - _last_audio[0] > _audio_limit:
                print(f"no audio from the feeder for {_audio_limit:.0f}s while video flows; "
                      "exiting to reconnect", file=sys.stderr, flush=True)
                os._exit(VIDEO_STALL_EXIT)

    _threading.Thread(target=_stall_watchdog, name="stdout-watchdog", daemon=True).start()

    def _write(data):
        if _etsf is not None:
            _etsf.write(f"{_time.time():.6f}\n")
        _write_started[0] = _time.time()
        stdout.write(data)
        stdout.flush()
        _write_started[0] = 0.0

    # Output pacing (Wopet feeder fork, CUBOAI_PACE_OUTPUT=1). go2rtc's raw Annex-B
    # reader stamps each frame with the time it reads it. Loss recovery releases frames
    # in bursts, so several frames got the same timestamp and RTSP clients merged them
    # into one picture (decode errors, dropped frames). The pacer writes frames at the
    # measured frame rate and catches up when a backlog builds; it never drops a frame.
    _pace = os.environ.get('CUBOAI_PACE_OUTPUT', '0') != '0'
    if _pace:
        import collections as _collections
        _pace_q = _queue_mod.Queue()

        def _pacer():
            arrivals = _collections.deque(maxlen=30)
            while True:
                t_in, data = _pace_q.get()
                arrivals.append(t_in)
                started = _time.time()
                try:
                    _write(data)
                except (BrokenPipeError, OSError):
                    os._exit(0)          # go2rtc closed the pipe
                interval = pacing_interval(list(arrivals), _pace_q.qsize())
                rest = interval - (_time.time() - started)
                if rest > 0:
                    _time.sleep(rest)

        _threading.Thread(target=_pacer, name="output-pacer", daemon=True).start()

    def _emit(data):
        _last_video[0] = _time.time()
        if _pace:
            _pace_q.put((_last_video[0], data))
        else:
            _write(data)

    # ── SIGTERM → clean teardown (B-1) ────────────────────────────────────
    # go2rtc stops an exec: source with SIGTERM (see the recipe above, killsignal=15; go2rtc needs the number).
    # Python's DEFAULT SIGTERM action terminates the process WITHOUT unwinding, so the
    # `finally: sess.disconnect()` below — the 3x build_close session-stop burst — is skipped
    # on every normal stream stop. Route SIGTERM into the SAME path SIGINT already takes:
    # raise KeyboardInterrupt in the main thread so the `except (BrokenPipeError,
    # KeyboardInterrupt)` + `finally` below run disconnect() exactly as they do for Ctrl-C.
    #
    # SIGINT is DELIBERATELY LEFT ALONE — Python's default handler already raises
    # KeyboardInterrupt and unwinds cleanly (offline-proven: 3x build_close, ~30 ms), so
    # touching it could only regress the working path. This is why the fix is just a SIGTERM
    # alias, with none of the SIGALRM-watchdog / os._exit machinery that hung SIGINT in the
    # first attempt. The handler is one-shot (restores SIG_DFL before raising) so a second,
    # impatient SIGTERM hard-kills instead of re-entering teardown.
    import signal as _signal

    def _term_handler(_signum, _frame):
        _signal.signal(_signal.SIGTERM, _signal.SIG_DFL)   # 2nd SIGTERM → hard kill
        raise KeyboardInterrupt                             # → except/finally → disconnect()

    try:
        _signal.signal(_signal.SIGTERM, _term_handler)
    except (ValueError, OSError):
        pass   # not the main thread / unsupported platform — leave the default (best-effort)

    try:
        if output_format == 'mpegts':
            # MPEG-TS path (Part C): carry per-frame PTS from the camera FRAMEINFO so MSE/HLS play
            # along currentTime without underrun. PACE is not used here — the PTS, not arrival
            # timing, drives the timeline. The muxing loop lives in mux_timed_stream() (shared with
            # the validator). clean-GOP (default on) drops incomplete AUs until the next clean IDR.
            clean_gop = os.environ.get('CUBOAI_CLEAN_GOP', '1') != '0'
            mux_audio = os.environ.get('CUBOAI_MUX_AUDIO', '0') != '0'
            # Unpaced (the PTS carries the timing); only video feeds the no-video watchdog.
            mux_timed_stream(sess.av_frames_timed(), _write, clean_gop=clean_gop, mux_audio=mux_audio,
                             tap=lambda *_a: _last_video.__setitem__(0, _time.time()),
                             audio_tap=lambda *_a: _last_audio.__setitem__(0, _time.time()))
        else:
            waiting_for_keyframe = True
            strip_trailer = video_codec() == 'h264'
            for frame_type, data in sess.av_frames():
                if frame_type == 'video':
                    if strip_trailer:
                        data = strip_h264_frameinfo(data)
                    if waiting_for_keyframe:
                        if not opens_annexb_stream(data):
                            continue
                        waiting_for_keyframe = False
                    elif not is_safe_annexb_au(data):
                        waiting_for_keyframe = True
                        continue
                    _emit(data)
    except (BrokenPipeError, KeyboardInterrupt):
        # go2rtc closed the pipe (stream stopped) — clean exit
        pass
    except Exception as e:
        print(f"Stream error: {e}", file=sys.stderr)
    finally:
        if control_server is not None:
            control_server.shutdown()
            control_server.server_close()
        if _v_stop is not None:
            _v_stop.set()
        if _etsf is not None:        # M3: close the gated emit-trace file (was leaked for process life)
            _etsf.close()
        sess.disconnect()


if __name__ == '__main__':
    main()

"""Tests for the Wopet-specific TUTK handshake additions."""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "wopet_bridge/vendor/cuboai_tutk/cuboai_pure.py"


def _load_protocol_module():
    spec = importlib.util.spec_from_file_location("wopet_cuboai_pure", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wopet_x2043_carries_ack_and_av_session_fields() -> None:
    """The registration packet must bind the discovery and AV handshakes."""
    protocol = _load_protocol_module()

    ack_decoded = bytearray(88)
    ack_decoded[16:32] = bytes(range(0x20, 0x30))
    av_decoded = bytearray(598)
    av_decoded[16:21] = bytes.fromhex("a1a2a3a4a5")
    av_decoded[24] = 0xC0
    av_decoded[28] = 0x78
    av_decoded[29] = 0x01
    av_decoded[31] = 0x13

    wire = protocol.build_wopet_x2043(
        protocol.xor_frame(bytes(ack_decoded)),
        protocol.xor_frame(bytes(av_decoded)),
    )
    decoded = protocol.xor_frame(wire)

    assert len(decoded) == 52
    assert decoded[:16] == bytes.fromhex("20431020000000001040a02140020002")
    assert decoded[16:32] == ack_decoded[16:32]
    assert decoded[32:37] == av_decoded[16:21]
    assert decoded[40] == 0xD3
    assert decoded[44:46] == bytes.fromhex("7841")
    assert decoded[47] == 0x13
    assert decoded[48:52] == bytes.fromhex("0b5fcde3")


def test_verbose_protocol_logging_uses_stderr() -> None:
    """Debug traces must never be mixed into go2rtc's stdout media stream."""
    protocol = _load_protocol_module()
    source = inspect.getsource(protocol.TUTKDirectSession._vlog)
    assert "file=sys.stderr" in source


def test_wopet_wrapper_defaults_to_native_hevc_and_can_enable_audio() -> None:
    """Keep proven Annex-B video while allowing opt-in MPEG-TS AAC audio."""
    source = (ROOT / "wopet_bridge/wopet/wopet_stream.py").read_text(encoding="utf-8")
    assert '"mpegts" if audio_enabled else "annexb"' in source
    assert '"1" if audio_enabled else "0"' in source


def test_pure_session_forwards_live_control_ioctl() -> None:
    source = (
        ROOT / "wopet_bridge/vendor/cuboai_tutk/cuboai_transport_py.py"
    ).read_text(encoding="utf-8")
    assert "def ioctl_during_stream(" in source
    assert "return self._inner.ioctl_during_stream(" in source


def test_wopet_audio_uses_captured_four_byte_start_payload(monkeypatch) -> None:
    vendor = ROOT / "wopet_bridge/vendor/cuboai_tutk"
    sys.path.insert(0, str(vendor))
    try:
        import cuboai_pure

        session = object.__new__(cuboai_pure.TUTKDirectSession)
        monkeypatch.setenv("CUBOAI_WOPET_AUDIOSTART", "1")
        assert session._stream_start_mid_command() == (0x0300, bytes(4))
        monkeypatch.setenv("CUBOAI_WOPET_AUDIOSTART", "0")
        assert session._stream_start_mid_command() == session._VIDEO_START_MID
    finally:
        sys.path.remove(str(vendor))

    wrapper = (
        ROOT / "wopet_bridge/wopet/wopet_stream.py"
    ).read_text(encoding="utf-8")
    assert 'os.environ["CUBOAI_WOPET_AUDIOSTART"]' in wrapper

    transport = MODULE_PATH.read_text(encoding="utf-8")
    wopet_branch = transport.split("if wopet_audio_start:", 1)[-1].split(
        "elif not self._defer_stream_start:", 1
    )[0]
    assert "start.append(self._VIDEO_START_LATE)" in wopet_branch
    assert "_stream_start_mid_command()" not in wopet_branch


def test_wopet_pcm_audio_is_detected_and_converted_to_pcma(monkeypatch) -> None:
    protocol = _load_protocol_module()
    frame = (b"\x00\x00" * 400) + bytes.fromhex(
        "8c000200000000000000000000000000"
    )
    info = protocol._parse_wopet_pcm_frame(frame)
    assert info is not None
    assert info["codec"] == "pcm_s16le"
    assert info["sample_rate"] == 8000
    assert info["channels"] == 1

    stream_path = ROOT / "wopet_bridge/vendor/cuboai_tutk/cuboai_stream_video.py"
    spec = importlib.util.spec_from_file_location("wopet_stream_audio", stream_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.syspath_prepend(str(stream_path.parent))
    spec.loader.exec_module(module)
    assert module.pcm_s16le_to_alaw(b"\x00\x00" * 400) == b"\xd5" * 400


def test_wopet_wrapper_supervises_the_stream_with_bounded_backoff() -> None:
    """A failed camera handshake must not become a go2rtc respawn storm."""
    wrapper_path = ROOT / "wopet_bridge/wopet/wopet_stream.py"
    spec = importlib.util.spec_from_file_location("wopet_stream_wrapper", wrapper_path)
    assert spec is not None and spec.loader is not None
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)

    assert [wrapper._retry_delay(attempt) for attempt in range(1, 7)] == [
        60,
        120,
        240,
        480,
        600,
        600,
    ]

    source = inspect.getsource(wrapper.main)
    assert "subprocess.Popen" in source
    assert "proc.terminate()" in source
    assert "proc.kill()" in source


def test_wopet_wrapper_persists_backoff_across_go2rtc_respawns(tmp_path: Path) -> None:
    """A new exec wrapper must retain the camera cooldown from its predecessor."""
    wrapper_path = ROOT / "wopet_bridge/wopet/wopet_stream.py"
    spec = importlib.util.spec_from_file_location("wopet_stream_state", wrapper_path)
    assert spec is not None and spec.loader is not None
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)

    state_path = tmp_path / "retry.json"
    wrapper._save_retry_state(state_path, 3, 1234.5)

    assert wrapper._load_retry_state(state_path) == (3, 1234.5)
    wrapper._clear_retry_state(state_path)
    assert wrapper._load_retry_state(state_path) == (0, 0.0)


def test_normal_consumer_disconnect_does_not_create_a_cooldown() -> None:
    """go2rtc stopping its on-demand producer must not poison the next view."""
    source = (ROOT / "wopet_bridge/wopet/wopet_stream.py").read_text(
        encoding="utf-8"
    )
    stopping_branch = source.split("if stopping:", 2)[-1].split(
        "consecutive_failures += 1", 1
    )[0]
    assert "_clear_retry_state(retry_state_path)" in stopping_branch
    assert "_save_retry_state" not in stopping_branch


def test_annexb_stream_waits_for_a_safe_hevc_opening_frame() -> None:
    """A reconnect that begins on a P-frame must wait for VPS/IRAP before go2rtc sees it."""
    module_path = ROOT / "wopet_bridge/vendor/cuboai_tutk/cuboai_stream_video.py"
    spec = importlib.util.spec_from_file_location("wopet_cuboai_stream_video", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    p_frame = bytes.fromhex("000000010201d04c")
    vps_frame = bytes.fromhex("0000000140010c01")
    idr_frame = bytes.fromhex("000000012601af09")

    assert not module.is_annexb_keyframe(p_frame)
    assert module.is_annexb_keyframe(vps_frame)
    assert module.is_annexb_keyframe(idr_frame)

    source = inspect.getsource(module.main)
    assert "waiting_for_keyframe = True" in source
    assert "if not opens_annexb_stream(data)" in source


def test_annexb_stream_rejects_short_or_invalid_hevc_nalus() -> None:
    """Malformed NAL units must not reach go2rtc's panic-prone HEVC packetizer."""
    module_path = ROOT / "wopet_bridge/vendor/cuboai_tutk/cuboai_stream_video.py"
    spec = importlib.util.spec_from_file_location("wopet_cuboai_stream_safety", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    valid = bytes.fromhex("0000000140010c01000000012601af09")
    one_byte_nalu = bytes.fromhex("0000000140010c010000000102")
    invalid_tid = bytes.fromhex("0000000140000c01")

    assert module.is_safe_hevc_annexb_au(valid)
    assert not module.is_safe_hevc_annexb_au(one_byte_nalu)
    assert not module.is_safe_hevc_annexb_au(invalid_tid)

    source = inspect.getsource(module.main)
    assert "elif not is_safe_annexb_au(data)" in source


def test_wrapper_detects_a_closed_feeder_port() -> None:
    """Port-unreachable means the feeder's P2P service is restarting: wait, don't back off."""
    import socket

    wrapper_path = ROOT / "wopet_bridge/wopet/wopet_stream.py"
    spec = importlib.util.spec_from_file_location("wopet_stream_probe", wrapper_path)
    assert spec is not None and spec.loader is not None
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
        listener.bind(("127.0.0.1", 0))
        open_port = listener.getsockname()[1]
        assert wrapper._feeder_listening("127.0.0.1", open_port) is True
    assert wrapper._feeder_listening("127.0.0.1", open_port) is False


def test_pure_session_supports_send_once_and_reply_matching() -> None:
    """Feeds must never be retransmitted; plan acks are matched by content."""
    source = (ROOT / "wopet_bridge/vendor/cuboai_tutk/cuboai_pure.py").read_text(encoding="utf-8")
    assert "gi.retransmit and now2 - gi.last_tx > gi.interval" in source
    assert "if gi.match is None or gi.match(body):" in source


def test_streamer_exits_when_go2rtc_stops_reading() -> None:
    """An orphaned producer must not keep the control port while serving nobody."""
    source = (
        ROOT / "wopet_bridge/vendor/cuboai_tutk/cuboai_stream_video.py"
    ).read_text(encoding="utf-8")
    assert "WOPET_STDOUT_STALL_SECS" in source
    assert "os._exit(0)" in source


def _load_stream_video(name: str):
    module_path = ROOT / "wopet_bridge/vendor/cuboai_tutk/cuboai_stream_video.py"
    spec = importlib.util.spec_from_file_location(name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_h264_aus_are_not_judged_by_hevc_rules(monkeypatch) -> None:
    """The feeder's H.264 P-slices must pass; HEVC rules dropped most of each GOP."""
    module = _load_stream_video("wopet_cuboai_stream_h264")
    sps_pps_idr = bytes.fromhex("00000001274d001fe7" "0000000128ee3c80" "0000000125888040ff")
    # P-slice whose second byte has the low three bits clear: an "invalid HEVC
    # temporal_id", but ordinary H.264 slice-header bits.
    p_slice = bytes.fromhex("00000001219800c0ffee")
    forbidden = bytes.fromhex("00000001a19a00")
    empty_nal = bytes.fromhex("0000000121" "00000001")
    mid_gop = p_slice

    assert not module.is_safe_hevc_annexb_au(p_slice)   # the old drop

    monkeypatch.setenv("CUBOAI_VIDEO_CODEC", "h264")
    assert module.opens_annexb_stream(sps_pps_idr)
    assert not module.opens_annexb_stream(mid_gop)
    assert module.is_safe_annexb_au(sps_pps_idr)
    assert module.is_safe_annexb_au(p_slice)
    assert not module.is_safe_annexb_au(forbidden)
    assert not module.is_safe_annexb_au(empty_nal)
    assert not module.is_safe_annexb_au(b"\x21\x98")  # no start code

    monkeypatch.setenv("CUBOAI_VIDEO_CODEC", "hevc")
    assert not module.is_safe_annexb_au(p_slice)


def test_bridge_selects_h264_rules() -> None:
    source = (ROOT / "wopet_bridge/wopet/wopet_stream.py").read_text(encoding="utf-8")
    assert 'os.environ["CUBOAI_VIDEO_CODEC"] = "h264"' in source


def test_go2rtc_kill_signal_is_numeric() -> None:
    """go2rtc parses killsignal with Atoi; a name becomes signal 0 and never stops the producer."""
    run = (ROOT / "wopet_bridge/rootfs/etc/services.d/wopet/run").read_text(encoding="utf-8")
    match = re.search(r"exec:python3 /app/wopet/wopet_stream\.py#killsignal=(\w+)", run)
    assert match is not None
    assert match.group(1) == "15"
    assert "#killtimeout=" in run


def test_stream_preload_is_on_by_default() -> None:
    import yaml

    config = yaml.safe_load((ROOT / "wopet_bridge/config.yaml").read_text(encoding="utf-8"))
    assert config["options"]["preload_stream"] is True
    assert config["schema"]["preload_stream"] == "bool?"
    run = (ROOT / "wopet_bridge/rootfs/etc/services.d/wopet/run").read_text(encoding="utf-8")
    assert "preload:" in run and ">> /data/go2rtc.yaml" in run


def test_video_stall_exit_reconnects_quickly() -> None:
    """A session that stops delivering video exits with a code the wrapper retries soon."""
    streamer = _load_stream_video("wopet_cuboai_stream_stall")
    wrapper_src = (ROOT / "wopet_bridge/wopet/wopet_stream.py").read_text(encoding="utf-8")
    match = re.search(r"^VIDEO_STALL_EXIT = (\d+)$", wrapper_src, re.MULTILINE)
    assert match is not None
    assert int(match.group(1)) == streamer.VIDEO_STALL_EXIT
    assert "return_code == VIDEO_STALL_EXIT" in wrapper_src
    source = inspect.getsource(streamer.main)
    assert "os._exit(VIDEO_STALL_EXIT)" in source
    assert "_last_video[0] = _time.time()" in source


def test_output_pacing_interval() -> None:
    module = _load_stream_video("wopet_cuboai_stream_pace")
    steady = [i / 15 for i in range(30)]
    assert abs(module.pacing_interval(steady, 0) - 1 / 15) < 1e-9
    burst = [0.0, 0.0, 0.0, 0.0, 2.0]          # 5 frames in 2 s, four at once
    assert abs(module.pacing_interval(burst, 0) - 0.125) < 1e-9   # clamped
    assert module.pacing_interval([0.0], 0) == 1 / 15             # no history yet
    assert module.pacing_interval(steady, 5) == (1 / 15) / 2       # catching up
    assert module.pacing_interval(steady, 16) == 0.005
    assert module.pacing_interval([0.0, 0.001], 0) == 0.025        # floor


def test_bridge_paces_output() -> None:
    source = (ROOT / "wopet_bridge/wopet/wopet_stream.py").read_text(encoding="utf-8")
    assert 'os.environ["CUBOAI_PACE_OUTPUT"] = "1"' in source


def test_feeder_frameinfo_trailer_is_stripped() -> None:
    module = _load_stream_video("wopet_cuboai_stream_trailer")
    frame = bytes.fromhex("0000000141e2ffee0011")
    trailer = bytes.fromhex("4e000000030100000300000096f392df")   # as captured, 16 bytes
    assert module.strip_h264_frameinfo(frame + trailer) == frame
    assert module.strip_h264_frameinfo(frame) == frame                 # nothing to strip
    lookalike = bytes.fromhex("4e000000030100000301000096f392df")  # reserve2 not small
    assert module.strip_h264_frameinfo(frame + lookalike) == frame + lookalike
    source = inspect.getsource(module.main)
    assert "data = strip_h264_frameinfo(data)" in source


def test_audio_path_strips_trailer_and_uses_camera_clock(monkeypatch) -> None:
    """With audio on (MPEG-TS), the H.264 trailer must go and PTS follow its timestamp."""
    monkeypatch.setenv("CUBOAI_VIDEO_CODEC", "h264")
    monkeypatch.setenv("CUBOAI_WOPET_AUDIOSTART", "1")
    module = _load_stream_video("wopet_cuboai_stream_ts_audio")

    def trailer(ts_ms: int) -> bytes:
        return bytes.fromhex("4e0000000301000003000000") + ts_ms.to_bytes(4, "little")

    idr = bytes.fromhex("00000001274d001fe7" "0000000128ee3c80" "0000000125888040ff")
    p_slice = bytes.fromhex("0000000141e2ffee0011")
    broken = bytes.fromhex("0000000141e2") + b"\x00\x00\x01\x80"   # forbidden bit set
    pcm = {"codec": "pcm_s16le", "ts_valid": False, "timestamp_ms": 1_000_030}
    frames = [
        ("video", p_slice + trailer(999_900), None),       # before the first keyframe: dropped
        ("video", idr + trailer(1_000_000), None),
        ("audio", b"\x00\x00" * 400, pcm),
        ("video", p_slice + trailer(1_000_066), None),
        ("video", broken + trailer(1_000_133), None),       # damaged: dropped, wait for IDR
        ("video", p_slice + trailer(1_000_200), None),      # still waiting: dropped
        ("video", idr + trailer(1_000_266), None),
    ]
    out: list[bytes] = []
    video_pts: list[float] = []
    audio_pts: list[float] = []
    module.mux_timed_stream(
        iter(frames), out.append, clean_gop=False, mux_audio=True, log=lambda _m: None,
        tap=lambda _p, _k, ms: video_pts.append(ms),
        audio_tap=lambda _p, _k, ms: audio_pts.append(ms),
    )
    assert video_pts == [0.0, 66.0, 266.0]
    assert audio_pts == [30.0]
    assert trailer(1_000_066) not in b"".join(out)


def test_h264_frameinfo_reads_trailer_timestamp() -> None:
    module = _load_stream_video("wopet_cuboai_stream_fi")
    frame = bytes.fromhex("0000000141e2ffee0011")
    au, fi = module.h264_frameinfo(frame + bytes.fromhex("4e000000030100000300000096f392df"))
    assert au == frame
    assert fi is not None and fi["timestamp_ms"] == 0xDF92F396 and fi["ts_valid"]
    assert module.h264_frameinfo(frame) == (frame, None)

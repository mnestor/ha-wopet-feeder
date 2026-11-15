"""The HomeKit motion helper parses ffmpeg's scene scores and builds a light command."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "wopet_motion", ROOT / "tools/homekit/wopet_motion.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scores_parse_metadata_print_output() -> None:
    module = _load()
    lines = [
        "frame:0    pts:0       pts_time:0\n",
        "lavfi.scene_score=0.000000\n",
        "frame:1    pts:22500   pts_time:0.25\n",
        "lavfi.scene_score=0.031250\n",
        "lavfi.scene_score=garbage\n",
    ]
    assert list(module.scores(lines)) == [0.0, 0.03125]


def test_ffmpeg_command_reads_rtsp_over_tcp_and_downscales(monkeypatch) -> None:
    module = _load()
    monkeypatch.setattr(module, "RTSP", "rtsp://192.0.2.100:8554/wopet")
    cmd = module.ffmpeg_command()
    assert cmd[cmd.index("-i") + 1] == "rtsp://192.0.2.100:8554/wopet"
    assert cmd[cmd.index("-rtsp_transport") + 1] == "tcp"
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale=320:-2" in vf and "lavfi.scene_score" in vf


def test_detector_needs_consecutive_frames_and_respects_retrigger() -> None:
    module = _load()
    d = module.Detector(threshold=0.02, frames=2, retrigger=10)
    assert not d.feed(0.5, 0.0)          # one glitch frame
    assert not d.feed(0.0, 0.25)
    assert not d.feed(0.03, 0.5)
    assert d.feed(0.03, 0.75)            # second consecutive frame
    assert not d.feed(0.04, 1.0)         # within retrigger window
    assert d.feed(0.04, 11.0)

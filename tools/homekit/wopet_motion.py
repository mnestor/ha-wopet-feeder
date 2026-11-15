#!/usr/bin/env python3
"""Software motion trigger for HomeKit Secure Video via homebridge-camera-ffmpeg.

The feeder reports no motion on the LAN, and HomeKit Secure Video only records
(and runs its person/animal/vehicle classification) while the camera's motion
sensor is active. This script watches the bridge's RTSP stream with ffmpeg's
scene-change score and calls the plugin's HTTP motion endpoint when the picture
changes. False triggers only cost a short recording: set the Home app to notify
on "Animal" and Apple's classifier does the filtering.

Configuration (environment):
  WOPET_RTSP        rtsp://<ha-host>:8554/wopet
  HB_MOTION_URL     http://localhost:<porthttp>/motion?<camera name, URL-encoded>
                    (localhost, not 127.0.0.1: the plugin listens on ::1 only)
  MOTION_THRESHOLD  scene score that counts as motion (default 0.02)
  MOTION_RETRIGGER  minimum seconds between triggers (default 10)
  MOTION_FPS        frames per second analysed (default 4)
  MOTION_FRAMES     consecutive frames at or above the threshold (default 2)
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request

RTSP = os.environ.get("WOPET_RTSP", "")
MOTION_URL = os.environ.get("HB_MOTION_URL", "")
THRESHOLD = float(os.environ.get("MOTION_THRESHOLD", "0.02"))
RETRIGGER = float(os.environ.get("MOTION_RETRIGGER", "10"))
FPS = float(os.environ.get("MOTION_FPS", "4"))
FRAMES = max(1, int(os.environ.get("MOTION_FRAMES", "2")))
STATS_SECONDS = 600


def log(msg: str) -> None:
    print(f"[wopet-motion] {msg}", file=sys.stderr, flush=True)


def ffmpeg_command() -> list[str]:
    # Small grey frames are enough for a change score and keep the CPU cost low.
    vf = (f"fps={FPS},scale=320:-2,format=gray,"
          "select='gte(scene,0)',metadata=print:key=lavfi.scene_score:file=-")
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
            "-i", RTSP, "-an", "-vf", vf, "-f", "null", "-"]


def scores(lines):
    """Yield scene scores from ffmpeg's metadata=print output."""
    for line in lines:
        if line.startswith("lavfi.scene_score="):
            try:
                yield float(line.split("=", 1)[1])
            except ValueError:
                continue


class Detector:
    """Motion when FRAMES consecutive scores reach THRESHOLD (one-frame glitches ignored)."""

    def __init__(self, threshold: float = THRESHOLD, frames: int = FRAMES,
                 retrigger: float = RETRIGGER) -> None:
        self.threshold, self.frames, self.retrigger = threshold, frames, retrigger
        self.run = 0
        self.last = float("-inf")

    def feed(self, score: float, now: float) -> bool:
        self.run = self.run + 1 if score >= self.threshold else 0
        if self.run >= self.frames and now - self.last >= self.retrigger:
            self.last = now
            return True
        return False


def trigger() -> bool:
    try:
        with urllib.request.urlopen(MOTION_URL, timeout=5) as resp:
            return 200 <= resp.status < 300
    except OSError as exc:
        log(f"motion call failed: {exc}")
        return False


def run_once() -> None:
    proc = subprocess.Popen(ffmpeg_command(), stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    detector = Detector()
    frames = triggers = 0
    peak = 0.0
    next_stats = time.monotonic() + STATS_SECONDS
    try:
        for score in scores(proc.stdout):
            frames += 1
            peak = max(peak, score)
            now = time.monotonic()
            if detector.feed(score, now):
                if trigger():
                    triggers += 1
                    log(f"motion (scene score {score:.3f})")
            if now >= next_stats:
                next_stats = now + STATS_SECONDS
                log(f"last 10 min: {frames} frames, peak score {peak:.3f}, "
                    f"{triggers} trigger(s)")
                frames = triggers = 0
                peak = 0.0
    finally:
        proc.kill()
        proc.wait()


def main() -> int:
    if not RTSP or not MOTION_URL:
        log("WOPET_RTSP and HB_MOTION_URL are required")
        return 2
    log(f"watching the stream at {FPS:g} fps, threshold {THRESHOLD} for {FRAMES} "
        f"frame(s), retrigger {RETRIGGER:g}s")
    while True:
        run_once()
        log("stream ended; reconnecting in 5 s")
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())

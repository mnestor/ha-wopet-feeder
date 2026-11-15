#!/usr/bin/env python3
"""Read HA app options and run the vendored pure-Python TUTK streamer."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

INITIAL_RETRY_DELAY = 60
MAX_RETRY_DELAY = 600
HEALTHY_RUN_SECONDS = 60
# cuboai_stream_video.py exits with this when the feeder stops sending video on an
# established session; reconnect after a short pause instead of the full backoff.
VIDEO_STALL_EXIT = 3
VIDEO_STALL_RETRY_SECONDS = 5
DEFAULT_RETRY_STATE_FILE = "/data/wopet_retry_state.json"
PID_FILE = Path("/data/wopet_stream.pid")
CHILD_KILL_GRACE = 3.0
# TUTK LAN port. With its internet blocked, firmware 40.0.14.3 restarts its P2P
# service every 10-60 minutes and the port is closed for minutes at a time.
FEEDER_PORT = 32761
PORT_POLL_SECONDS = 3.0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_wopet_producer(pid: int) -> bool:
    """Guard against PID reuse: only stop a process that is really this script."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"wopet_stream.py" in cmdline


def _children(ppid: int) -> list[int]:
    kids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
            if int(stat.rsplit(")", 1)[1].split()[1]) == ppid:
                kids.append(int(entry.name))
        except (OSError, ValueError, IndexError):
            continue
    return kids


def _take_over_previous_instance() -> None:
    """Stop an older producer go2rtc left running, so only one camera session exists.

    Orphaned producers kept their (dead) camera session and the control port, so
    controls went to a session the feeder no longer answered.
    """
    try:
        old = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        old = 0
    if old and old != os.getpid() and _pid_alive(old) and _is_wopet_producer(old):
        print(f"Stopping previous Wopet producer (pid {old})", file=sys.stderr, flush=True)
        try:
            os.kill(old, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + CHILD_KILL_GRACE + 3
        while time.monotonic() < deadline and _pid_alive(old):
            time.sleep(0.1)
        if _pid_alive(old):
            for pid in [*_children(old), old]:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def _feeder_listening(ip: str, port: int = FEEDER_PORT) -> bool:
    """False only when the feeder answers ICMP port-unreachable (service down).

    An empty datagram is not a session attempt, so polling this does not count
    against the feeder's session rate limit. No answer means listening (or a
    lost ICMP), which falls through to a real connect attempt.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.7)
            sock.connect((ip, port))
            for _ in range(2):
                sock.send(b"")
                try:
                    sock.recv(64)
                except TimeoutError:
                    continue
            return True
    except ConnectionRefusedError:
        return False
    except OSError:
        return True


def _required_string(options: dict[str, object], name: str) -> str:
    value = options.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Required app option is missing: {name}")
    return value.strip()


def _retry_delay(consecutive_failures: int) -> int:
    """Return a camera-friendly exponential reconnect delay."""
    exponent = max(0, consecutive_failures - 1)
    return min(INITIAL_RETRY_DELAY * (2**exponent), MAX_RETRY_DELAY)


def _load_retry_state(path: Path) -> tuple[int, float]:
    """Load a cross-process cooldown, tolerating old or damaged state files."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        failures = max(0, int(state.get("consecutive_failures", 0)))
        next_attempt = max(0.0, float(state.get("next_attempt", 0)))
        return failures, next_attempt
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return 0, 0.0


def _save_retry_state(path: Path, failures: int, next_attempt: float) -> None:
    """Atomically persist retry history so a go2rtc respawn cannot erase it."""
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(
            {"consecutive_failures": failures, "next_attempt": next_attempt},
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def _clear_retry_state(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def main() -> None:
    """Load private options without putting credentials in arguments or logs."""
    options_path = Path(os.environ.get("WOPET_OPTIONS_FILE", "/data/options.json"))
    options = json.loads(options_path.read_text(encoding="utf-8"))

    os.environ["CUBOAI_CAMERA_IP"] = _required_string(options, "camera_ip")
    os.environ["CUBOAI_UID"] = _required_string(options, "device_uid")
    os.environ["CUBOAI_ACCOUNT"] = _required_string(options, "device_username")
    os.environ["CUBOAI_PASSWORD"] = _required_string(options, "device_password")
    audio_enabled = bool(options.get("enable_audio", False))
    os.environ["CUBOAI_MUX_AUDIO"] = "1" if audio_enabled else "0"
    os.environ["CUBOAI_WOPET_AUDIOSTART"] = "1" if audio_enabled else "0"
    os.environ["CUBOAI_OUTPUT_FORMAT"] = "mpegts" if audio_enabled else "annexb"
    # Firmware 40.23.6.5 does not reliably mark a complete IDR at the assembled
    # access-unit boundary. Do not hold back the MPEG-TS PAT/PMT while waiting
    # for that hint; downstream demuxers can lock immediately and the decoder
    # will begin displaying at the next usable keyframe.
    os.environ["CUBOAI_CLEAN_GOP"] = "0" if audio_enabled else "1"
    os.environ["CUBOAI_WOPET_X2043"] = "1"
    # The feeder sends H.264 (upstream's D100 sends HEVC); pick the matching AU checks.
    os.environ["CUBOAI_VIDEO_CODEC"] = "h264"
    # Write frames at the frame rate: go2rtc timestamps raw H.264 on arrival.
    os.environ["CUBOAI_PACE_OUTPUT"] = "1"
    os.environ["WOPET_CONTROL_PORT"] = str(options.get("control_port", 1986))
    os.environ["WOPET_CONTROL_TOKEN"] = str(options.get("control_token", "")).strip()
    os.environ["CUBOAI_VERBOSE"] = (
        "1" if str(options.get("log_level", "info")).lower() == "debug" else "0"
    )

    vendor = Path(__file__).parents[1] / "vendor" / "cuboai_tutk"
    streamer = vendor / "cuboai_stream_video.py"
    retry_state_path = Path(
        os.environ.get("WOPET_RETRY_STATE_FILE", DEFAULT_RETRY_STATE_FILE)
    )
    stopping = False
    child: subprocess.Popen[bytes] | None = None

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        proc = child
        if proc is not None and proc.poll() is None:
            proc.terminate()

            def _force() -> None:
                time.sleep(CHILD_KILL_GRACE)
                if proc.poll() is None:
                    proc.kill()

            threading.Thread(target=_force, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    _take_over_previous_instance()
    consecutive_failures, next_attempt = _load_retry_state(retry_state_path)
    camera_ip = os.environ["CUBOAI_CAMERA_IP"]

    def wait_for_feeder() -> bool:
        """Block while the feeder's P2P service is down. True if it was down."""
        was_down = False
        while not stopping and not _feeder_listening(camera_ip):
            if not was_down:
                print(
                    "Feeder P2P service is down (port closed); waiting for it",
                    file=sys.stderr,
                    flush=True,
                )
                was_down = True
            time.sleep(PORT_POLL_SECONDS)
        if was_down and not stopping:
            print("Feeder P2P service is back; connecting", file=sys.stderr, flush=True)
        return was_down

    while not stopping:
        last_probe = time.monotonic()
        while not stopping and time.time() < next_attempt:
            time.sleep(min(0.5, next_attempt - time.time()))
            if time.monotonic() - last_probe >= PORT_POLL_SECONDS:
                last_probe = time.monotonic()
                if not _feeder_listening(camera_ip):
                    break
        if stopping:
            return
        if wait_for_feeder():
            # The service restarted; the earlier failures were the outage, not
            # the session rate limit, so connect immediately.
            consecutive_failures = 0
            next_attempt = 0.0
            _clear_retry_state(retry_state_path)
        if stopping:
            return

        started = time.monotonic()
        child = subprocess.Popen([sys.executable, str(streamer)], env=os.environ.copy())
        return_code = child.wait()
        runtime = time.monotonic() - started
        child = None

        if runtime >= HEALTHY_RUN_SECONDS:
            consecutive_failures = 0
            next_attempt = 0.0
            _clear_retry_state(retry_state_path)

        if stopping:
            # go2rtc stops an on-demand exec producer when its last consumer
            # disconnects. That is a normal lifecycle event, not a camera
            # failure; carrying a cooldown into the next viewer would make the
            # producer emit no bytes and break the next RTSP request.
            _clear_retry_state(retry_state_path)
            return

        if not _feeder_listening(camera_ip):
            # Dropped because the feeder restarted its P2P service: wait for the
            # port instead of backing off (handled at the top of the loop).
            print(
                f"Wopet stream process ended (exit {return_code}, ran {runtime:.1f}s); "
                "feeder service went down",
                file=sys.stderr,
                flush=True,
            )
            next_attempt = 0.0
            continue

        if return_code == VIDEO_STALL_EXIT and runtime >= HEALTHY_RUN_SECONDS:
            print(
                f"Wopet stream lost video after {runtime:.0f}s; "
                f"reconnecting in {VIDEO_STALL_RETRY_SECONDS}s",
                file=sys.stderr,
                flush=True,
            )
            next_attempt = time.time() + VIDEO_STALL_RETRY_SECONDS
            continue

        consecutive_failures += 1
        delay = _retry_delay(consecutive_failures)
        next_attempt = time.time() + delay
        _save_retry_state(retry_state_path, consecutive_failures, next_attempt)
        print(
            "Wopet stream process ended "
            f"(exit {return_code}, ran {runtime:.1f}s); retrying in {delay}s",
            file=sys.stderr,
            flush=True,
        )

        while not stopping and time.time() < next_attempt:
            time.sleep(min(0.5, next_attempt - time.time()))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Authenticated local HTTP controls for the WOPET feeder, over the live stream session.

Endpoints (all need "Authorization: Bearer <control_token>"):
  GET  /health            200 when the stream session is up
  GET  /plan              the six feeding-schedule slots
  POST /plan              {"slot": n, <field>: value, ...}  edit one slot, returns the new plan
  POST /feed              {"portions": n}  dispense now (sent once, never retried)
  POST /time              {}  set the feeder clock to this host's time and timezone

The clock is also set automatically a few seconds after every new camera session:
offline, the feeder has no working time source and loses time across a power cut.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import feeder_protocol as fp

PLAN_TIMEOUT = 6.0
FEED_TIMEOUT = 20.0  # the feeder replies only after the motor has run (~7 s seen)
EVENT_WAIT = 6.0     # the status event followed the reply by ~3 s
TIME_TIMEOUT = 6.0
AUTO_TIME_DELAY = 3.0

_lock = threading.Lock()
_events: list[tuple[float, int | None]] = []
_events_cv = threading.Condition()


def _stream_is_ready(session) -> bool:
    """Return whether the underlying transport's AV reader is running."""
    transport = getattr(session, "_inner", session)
    reader = getattr(transport, "_av_reader_thread", None)
    return reader is not None and reader.is_alive()


def _on_control_frame(ts: float, io_type: int, head: bytes) -> None:
    if io_type != fp.EVENT:
        return
    code = fp.parse_event(head[68:])
    with _events_cv:
        _events.append((ts, code))
        del _events[:-20]
        _events_cv.notify_all()


def _ioctl(session, io_type, payload, resp_type, timeout, retransmit=True, match=None) -> bytes:
    actual, body = session.ioctl_during_stream(
        io_type,
        payload,
        resp_type=resp_type,
        timeout=timeout,
        retransmit=retransmit,
        interval=1.5,
        match=match,
    )
    if actual != resp_type or body is None:
        raise RuntimeError("camera returned an unexpected response type")
    return body


def read_plan(session) -> list[fp.Meal]:
    body = _ioctl(
        session, fp.PLAN_REQ, fp.build_plan_get(), fp.PLAN_RESP, PLAN_TIMEOUT,
        match=lambda b: fp.plan_set_flag(b) == 0 and bool(fp.plan_count(b)),
    )
    return fp.parse_plan(body)


def get_plan(session) -> dict[str, object]:
    with _lock:
        meals = read_plan(session)
    return {"ok": True, "meals": [m.as_dict() for m in meals]}


def set_meal(session, request: dict[str, object]) -> dict[str, object]:
    slot = request.get("slot")
    if not isinstance(slot, int) or isinstance(slot, bool):
        raise ValueError("slot is required")
    changes = {k: v for k, v in request.items() if k != "slot"}
    if not changes:
        raise ValueError("no fields to change")
    with _lock:
        meals = read_plan(session)
        target = next((m for m in meals if m.slot == slot), None)
        if target is None:
            raise ValueError(f"no meal in slot {slot}")
        fp.apply_changes(target, changes)
        body = _ioctl(
            session,
            fp.PLAN_REQ,
            fp.build_plan_set(meals, slot),
            fp.PLAN_RESP,
            PLAN_TIMEOUT,
            match=lambda b: fp.plan_set_flag(b) == 1 and fp.plan_count(b) == len(meals),
        )
        try:
            stored = fp.parse_plan(body)
            acked = fp.plans_match(meals, stored)
        except fp.ProtocolError as exc:
            print(f"Wopet schedule ack not parsed: {exc}", file=sys.stderr, flush=True)
            acked = False
        if not acked:
            # Confirm with a fresh read; the stored plan is what matters.
            stored = read_plan(session)
            if not fp.plans_match(meals, stored):
                raise RuntimeError("feeder did not store the requested schedule")
    print(f"Wopet schedule slot {slot} updated: {sorted(changes)}", file=sys.stderr, flush=True)
    return {"ok": True, "meals": [m.as_dict() for m in stored]}


def feed(session, request: dict[str, object]) -> dict[str, object]:
    portions = request.get("portions")
    payload = fp.build_feed(portions)  # validates 1..MAX_FEED_PORTIONS
    with _lock:
        sent_at = time.time()
        body = _ioctl(
            session, fp.FEED_REQ, payload, fp.FEED_RESP, FEED_TIMEOUT, retransmit=False
        )
        result = fp.parse_feed_reply(body)
        if result != 0:
            raise RuntimeError(f"feeder rejected the feed (result {result})")
        code = None
        deadline = time.time() + EVENT_WAIT
        with _events_cv:
            while True:
                fresh = [c for ts, c in _events if ts >= sent_at]
                if fresh:
                    code = fresh[-1]
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                _events_cv.wait(remaining)
    print(f"Wopet feed {portions} portion(s): event code {code}", file=sys.stderr, flush=True)
    if code in fp.EVENT_CODES:
        return {"ok": False, "error": fp.EVENT_CODES[code], "code": code}
    return {"ok": True, "portions": portions, "code": code}


def set_time(session, _request: dict[str, object] | None = None) -> dict[str, object]:
    now = time.time()
    tz_minutes = time.localtime(now).tm_gmtoff // 60
    with _lock:
        payload = fp.build_set_time(int(now), tz_minutes)
        body = _ioctl(session, fp.TIME_REQ, payload, fp.TIME_RESP, TIME_TIMEOUT)
    result = int.from_bytes(body[:4], "little") if len(body) >= 4 else -1
    if result != 0:
        raise RuntimeError(f"feeder returned clock result {result}")
    return {"ok": True, "epoch": int(now), "tz_minutes": tz_minutes}


def _auto_set_time(session) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not _stream_is_ready(session):
        time.sleep(0.5)
    if not _stream_is_ready(session):
        return
    time.sleep(AUTO_TIME_DELAY)
    try:
        info = set_time(session)
        print(f"Wopet clock set (tz {info['tz_minutes']} min)", file=sys.stderr, flush=True)
    except Exception as exc:
        print(f"Wopet clock set failed: {exc!r}", file=sys.stderr, flush=True)


def start_control_server(session):
    """Start the local control API when a token and port are configured."""
    token = os.environ.get("WOPET_CONTROL_TOKEN", "")
    port_text = os.environ.get("WOPET_CONTROL_PORT", "")
    if not token or not port_text:
        print(
            "Wopet controls disabled: configure control_token and control_port",
            file=sys.stderr,
            flush=True,
        )
        return None

    port = int(port_text)
    listener = getattr(session, "set_control_listener", None)
    if listener is not None:
        listener(_on_control_frame)
    Thread(target=_auto_set_time, args=(session,), name="wopet-clock", daemon=True).start()

    class ControlHandler(BaseHTTPRequestHandler):
        server_version = "WopetControl/2"

        def log_message(self, _format, *_args):
            return

        def _write_json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            return hmac.compare_digest(supplied, f"Bearer {token}")

        def _run(self, func, *args) -> None:
            if not _stream_is_ready(session):
                self._write_json(503, {"ok": False, "error": "stream not ready"})
                return
            try:
                response = func(session, *args)
            except ValueError as exc:
                self._write_json(400, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:
                print(f"Wopet control failed: {exc!r}", file=sys.stderr, flush=True)
                self._write_json(502, {"ok": False, "error": f"camera command failed: {exc}"})
                return
            self._write_json(200, response)

        def do_GET(self):
            if not self._authorized():
                self._write_json(401, {"ok": False, "error": "unauthorized"})
                return
            if self.path == "/health":
                if not _stream_is_ready(session):
                    self._write_json(503, {"ok": False, "error": "stream not ready"})
                    return
                self._write_json(200, {"ok": True})
            elif self.path == "/plan":
                self._run(get_plan)
            else:
                self._write_json(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if not self._authorized():
                self._write_json(401, {"ok": False, "error": "unauthorized"})
                return
            handlers = {"/plan": set_meal, "/feed": feed, "/time": set_time}
            handler = handlers.get(self.path)
            if handler is None:
                self._write_json(404, {"ok": False, "error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 4096 or (length < 1 and self.path != "/time"):
                    raise ValueError("invalid request size")
                request = json.loads(self.rfile.read(length)) if length else {}
                if not isinstance(request, dict):
                    raise ValueError("request must be a JSON object")
            except (ValueError, json.JSONDecodeError) as exc:
                self._write_json(400, {"ok": False, "error": str(exc)})
                return
            self._run(handler, request)

    server = None
    for attempt in range(10):  # the previous producer may still be releasing the port
        try:
            server = ThreadingHTTPServer(("0.0.0.0", port), ControlHandler)
            break
        except OSError as exc:
            if attempt == 9:
                print(
                    f"Wopet controls disabled: could not bind port {port}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                return None
            time.sleep(1)
    Thread(target=server.serve_forever, name="wopet-control", daemon=True).start()
    print(f"Wopet control API listening on port {port}", file=sys.stderr, flush=True)
    return server

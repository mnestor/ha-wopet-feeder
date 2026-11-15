#!/usr/bin/env python3
"""go2rtc two-way audio backchannel: forward G.711 A-law from stdin to the control API's /talk.

go2rtc starts this (exec source with #backchannel=1#audio=pcma/8000) when a viewer sends its
microphone, writes the audio to stdin, and stops it when the viewer stops talking.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
from pathlib import Path


def _chunks():
    while True:
        data = sys.stdin.buffer.read1(1600)
        if not data:
            return
        yield data


def main() -> int:
    options = json.loads(Path(os.environ.get("WOPET_OPTIONS_FILE", "/data/options.json"))
                         .read_text(encoding="utf-8"))
    token = str(options.get("control_token", "")).strip()
    port = int(options.get("control_port", 1986))
    if not token:
        print("Wopet talk: control_token is not set", file=sys.stderr, flush=True)
        return 1
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request("POST", "/talk", body=_chunks(), encode_chunked=True, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "audio/PCMA",
            "Transfer-Encoding": "chunked",
        })
        resp = conn.getresponse()
        print(f"Wopet talk: {resp.status} {resp.read(200).decode(errors='replace')}",
              file=sys.stderr, flush=True)
    except OSError as exc:
        print(f"Wopet talk failed: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

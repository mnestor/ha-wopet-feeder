#!/usr/bin/env python3
"""Answer the feeder's TUTK P2P server queries on the LAN, so it never needs the internet.

Why: booted with its internet blocked, the feeder (fw 40.0.14.3) never gets an answer
from the TUTK master servers and re-initialises P2P every 10-60 minutes, which closes
its local port 32761 and drops video. Booted online, it gets a referral to two region
servers, logs in, and then stays up even after the internet is blocked again. This
service answers the way the real servers did in the 2026-11-15 10:03 capture.

Two ways to get the queries here: a DNS override of the feeder's master server name
(all-d-master-*.iotcplatform.com, looked up at boot) to this host, or router destination-NAT of the
feeder's UDP 10240. By default the referral then names this host as the region
server, so the feeder's region traffic (UDP 10001) comes straight here too.

Messages (decoded with TUTK inverse TransCodePartial; u16 at +8 is the command):
  master query  0x1007, 54 B: UID at +16              -> 0x1008, 126 B referral
  region probe  0x8003, 24 B: index at +16, id at +20 -> 0x8004, 40 B
  region login  0x0105, 288 B: UID, local addr at +36 -> 0x0106, 288 B echo
  keepalive     4 B every 5 s once logged in (14 B before), never answered by the real servers
Every reply carries the sender's address as the server saw it (family 2, port, IPv4).
Only packets from the configured camera IP carrying its UID are answered.
"""

from __future__ import annotations

import json
import os
import select
import socket
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor" / "cuboai_tutk"))
from cuboai_pure import inv_transcode, transcode  # noqa: E402

MASTER_PORT = 10240
REGION_PORT = 10001

CMD_MASTER_REQ = 0x1007
CMD_PROBE_REQ = 0x8003
CMD_LOGIN_REQ = 0x0105

# Region servers named in the captured referral. With option p2p_region_address
# "captured" the referral names these and the router must redirect UDP 10001 here;
# the default "auto" names this host instead, so no 10001 redirect is needed.
REGION_SERVERS = (("47.252.13.1", REGION_PORT), ("47.254.77.223", REGION_PORT))

# Referral fields after the region list, copied verbatim (meaning unknown).
REFERRAL_TAIL = bytes.fromhex("0300040030000000" "01000600 89a1 0100 1100".replace(" ", ""))

STATS_SECONDS = 600


def _sockaddr(ip: str, port: int) -> bytes:
    """16-byte sockaddr_in as TUTK writes it: LE family, BE port, IPv4, 8 zero bytes."""
    return struct.pack("<H", 2) + struct.pack(">H", port) + socket.inet_aton(ip) + b"\0" * 8


def local_address_toward(ip: str) -> str:
    """This host's address on the route to `ip` (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((ip, MASTER_PORT))
        return sock.getsockname()[0]


def region_servers(setting: str, camera_ip: str) -> tuple[tuple[str, int], ...]:
    """Region servers for the referral: 'auto' (this host), 'captured', or an IPv4."""
    setting = (setting or "auto").strip()
    if setting == "captured":
        return REGION_SERVERS
    host = local_address_toward(camera_ip) if setting == "auto" else setting
    socket.inet_aton(host)  # raises OSError on a bad address
    # The referral always carries two entries; name this host twice.
    return ((host, REGION_PORT), (host, REGION_PORT))


def command(plain: bytes) -> int | None:
    if len(plain) < 16 or plain[0:2] != b"\x04\x02":
        return None
    return struct.unpack_from("<H", plain, 8)[0]


def master_reply(req: bytes, seen: tuple[str, int], regions=REGION_SERVERS) -> bytes:
    """0x1008 referral for a 54 B 0x1007 query."""
    out = bytearray(bytes.fromhex("04021d006e000000" "0810" "83" "0000000000"))
    out += req[16:48]                       # UID field, as sent
    out += b"\0" * 4
    out += _sockaddr(*seen)                 # +52 sender as seen
    out += bytes.fromhex("05000300")        # +68 three entries follow
    out += bytes.fromhex("02002000")        # +72 type 2: 32 B of region servers
    for ip, port in regions:
        out += _sockaddr(ip, port)
    out += REFERRAL_TAIL
    if len(out) != 126:
        raise AssertionError(len(out))
    return bytes(out)


def probe_reply(req: bytes, seen: tuple[str, int]) -> bytes:
    """0x8004 for a 24 B 0x8003 probe."""
    out = bytearray(bytes.fromhex("04021b0018000000" "0480" "4f" "0000000000"))
    out += _sockaddr(*seen)                 # +16
    out += req[16:22]                       # +32 index, +36 probe id
    out += b"\0\0"
    return bytes(out)


def login_reply(req: bytes, seen: tuple[str, int]) -> bytes:
    """0x0106 for a 288 B 0x0105 login: the request echoed with the sender as seen."""
    out = bytearray(req)
    out[2:4] = b"\x1b\x00"
    out[8:11] = b"\x06\x01\x41"
    out[36:44] = _sockaddr(*seen)[:8]
    return bytes(out)


def build_reply(
    plain: bytes, seen: tuple[str, int], uid: bytes, regions=REGION_SERVERS
) -> tuple[str, bytes] | None:
    cmd = command(plain)
    if cmd == CMD_MASTER_REQ and len(plain) == 54 and plain[16:36] == uid:
        return "master", master_reply(plain, seen, regions)
    if cmd == CMD_PROBE_REQ and len(plain) == 24:
        return "probe", probe_reply(plain, seen)
    if cmd == CMD_LOGIN_REQ and len(plain) == 288 and plain[16:36] == uid:
        return "login", login_reply(plain, seen)
    return None


def _log(msg: str) -> None:
    print(f"[p2p-responder] {msg}", file=sys.stderr, flush=True)


def serve(
    camera_ip: str, uid: bytes, regions=REGION_SERVERS, ports=(MASTER_PORT, REGION_PORT)
) -> None:
    socks = []
    for port in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # No SO_REUSEADDR: if anything else holds the port (UniFi discovery also uses
        # UDP 10001), fail loudly instead of silently sharing its datagrams.
        s.bind(("0.0.0.0", port))
        socks.append(s)
    port_list = ", ".join(map(str, ports))
    _log(f"answering TUTK master/region queries from {camera_ip} on UDP {port_list}")
    named = ", ".join(sorted({f"{ip}:{port}" for ip, port in regions}))
    _log(f"referral names region server(s) {named}")
    counts: dict[str, int] = {}
    reported: set[tuple[str, int]] = set()
    next_stats = time.monotonic() + STATS_SECONDS
    while True:
        ready, _, _ = select.select(socks, [], [], 30)
        for s in ready:
            try:
                data, addr = s.recvfrom(2048)
            except OSError:
                continue
            if addr[0] != camera_ip:
                continue
            kind = "keepalive" if len(data) in (4, 14) else "other"
            reply = None
            if len(data) >= 16:
                built = build_reply(inv_transcode(data), addr, uid, regions)
                if built:
                    kind, reply = built
            counts[kind] = counts.get(kind, 0) + 1
            if reply is not None:
                s.sendto(transcode(reply), addr)
            key = (kind, addr[1])
            if key not in reported:   # first of each kind per feeder source port
                reported.add(key)
                _log(f"{kind} from {addr[0]}:{addr[1]} on :{s.getsockname()[1]}"
                     + (" -> answered" if reply is not None else ""))
        if time.monotonic() >= next_stats:
            next_stats = time.monotonic() + STATS_SECONDS
            summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
            _log("last 10 min: " + (summary or "nothing"))
            counts.clear()


def main() -> int:
    options_path = Path(os.environ.get("WOPET_OPTIONS_FILE", "/data/options.json"))
    options = json.loads(options_path.read_text(encoding="utf-8"))
    if not options.get("p2p_responder", True):
        _log("disabled in options; idling")
        while True:
            time.sleep(3600)
    camera_ip = str(options.get("camera_ip", "")).strip()
    uid = str(options.get("device_uid", "")).strip().encode()
    if not camera_ip or len(uid) != 20:
        _log("camera_ip and a 20-character device_uid are required; idling")
        while True:
            time.sleep(3600)
    try:
        regions = region_servers(str(options.get("p2p_region_address", "auto")), camera_ip)
    except OSError as exc:
        _log(f"bad p2p_region_address ({exc}); using this host")
        regions = region_servers("auto", camera_ip)
    serve(camera_ip, uid, regions)
    return 0


if __name__ == "__main__":
    sys.exit(main())

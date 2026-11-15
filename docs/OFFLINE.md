# Running the feeder with no internet access

The feeder keeps its schedule, feeds and streams to the bridge with its internet
blocked. Done naively, though, it drops video every 10 to 60 minutes. This page
is the setup that fixes that, why each piece is needed, and how to check it.

Examples use `192.0.2.23` for the feeder and `192.0.2.100` for the Home
Assistant host. Firewall steps are written for pf-based routers (OPNsense,
pfSense); the same rules translate to any router that can block and allow per
host. The README's [Ways to run it](../README.md#ways-to-run-it) compares the
setups.

## Summary

| # | Rule | Why |
| --- | --- | --- |
| 1 | Block the feeder to all non-private destinations | Cuts the vendor cloud, MQTT, firmware checks |
| 2 | Allow UDP from the feeder to the HA host, any ports | The feeder answers the bridge from a random port; also carries its P2P queries to the responder |
| 3 | Redirect the feeder's UDP 123 to a local NTP server | Clock source (optional; the bridge also sets it) |
| 4 | Send the feeder's P2P server queries to the HA host: **either** a DNS override of its master server name (4a, recommended) **or** destination-NAT of UDP 10240 and 10001 (4b) | The bridge's responder answers them; stops the video drops |
| 5 | Leave the bridge options `p2p_responder: true` and `p2p_region_address: auto` (defaults) | Runs the responder and points the feeder's region login at it |

Do **not** redirect or fake the feeder's TCP 80 internet checks (see below).

## 1. Block the internet

Block `192.0.2.23` to any destination outside RFC 1918. Put it before any
allow rules that would match. DNS to the router can stay; the feeder keeps
looking up its hosts and that is harmless. Kill existing states for the feeder
after adding the rule, or established flows keep working.

## 2. Let the feeder reach the bridge

The bridge connects to the feeder's UDP 32761, but the feeder replies from its
main P2P socket, whose port is random per boot. If the feeder and Home Assistant
are on different networks, a stateful firewall drops that reply as a new
connection. Allow:

```text
pass  udp  from 192.0.2.23 (any port)  to 192.0.2.100 (any port)
```

Same-subnet setups do not need this.

## 3. Clock

Redirect the feeder's outbound UDP 123 to an NTP server on the router. The
feeder only asks about 11 minutes after boot, so the bridge also sends the
clock and timezone (IOCTL `0x2052`) a few seconds into every camera session,
using Home Assistant's timezone. The feeder runs its schedule on that offset,
so a stale offset after a DST change shifts every meal by an hour; the bridge
corrects it at the next session (the integration opens one at least every 6
hours).

## 4. The P2P responder (the video-drop fix)

**What goes wrong without it.** At boot the feeder asks ThroughTek's master
servers (UDP 10240) for its region servers, then logs in to those (UDP 10001)
and keeps that login alive. Blocked, the master query never gets an answer and
the feeder re-initialises its P2P service every 10 to 60 minutes. Each time it
closes UDP 32761 (port-unreachable for up to several minutes) and the camera
drops.

**What we found.** A feeder that booted with internet access, got the referral
and logged in, then had its internet blocked, stayed up indefinitely on one
port. So the trigger is the missing referral, not the lost connection.

**The fix.** The bridge's responder (`wopet_bridge/wopet/p2p_responder.py`)
listens on UDP 10240 and 10001 on the HA host and answers the three messages
the feeder needs: the master query, the region probe and the region login (see
[Protocol](PROTOCOL.md#3-p2p-server-messages-udp-10240-and-10001)). It only
answers the configured camera IP and UID, and ignores keepalives, as the real
servers do. The queries have to reach it; there are two ways.

### 4a. DNS override (recommended)

The feeder finds its master servers by DNS. On firmware 40.0.14.3 it looks up
`all-d-master-bx1kb94488ffwtalxe9a.iotcplatform.com` (a CNAME to ThroughTek's
shared master pool) at boot; check your DNS server's query log for the
`all-d-master-*.iotcplatform.com` name your feeder asks for. Add a host
override that answers that exact name with the HA host's address:

- OPNsense: **Services → Unbound DNS → Overrides → Host overrides**, host
  `all-d-master-bx1kb94488ffwtalxe9a`, domain `iotcplatform.com`, type A,
  IP = HA host. pfSense: the same under **Services → DNS Resolver**.
- Pi-hole: **Local DNS → DNS Records**. AdGuard Home: **Filters → DNS
  rewrites**. dnsmasq: `address=/all-d-master-bx1kb94488ffwtalxe9a.iotcplatform.com/192.0.2.100`.

Override only that name. Other TUTK/Kalay cameras on the network use other
names under `iotcplatform.com` and still need the real servers if they are
online.

With `p2p_region_address: auto` the responder's referral names the HA host
(its address on the route to the feeder) as both region servers, so the
region probe and login on UDP 10001 also go straight to the bridge. No NAT
rule is involved; rule 2 lets the traffic through if the feeder and HA are on
different networks. Set `p2p_region_address` to an IPv4 address if the
feeder should use a different address for the HA host.

Requirements: the feeder must use the DNS server it gets by DHCP (ours does),
and it must look the name up again, so power-cycle it after adding the
override. After that boot our feeder sent no P2P traffic to any public
address: it used only the servers named in the local referral. (Its old
session from before the reboot had kept talking to the vendor's region
servers through existing firewall states; power-cycling clears that.)

### 4b. Router redirects

If you cannot override DNS, or the feeder uses a hard-coded DNS server, the
router can send the queries to the bridge instead:

```text
rdr pass  proto udp  from 192.0.2.23  to !RFC1918  port 10240  ->  192.0.2.100 port 10240
rdr pass  proto udp  from 192.0.2.23  to !RFC1918  port 10001  ->  192.0.2.100 port 10001
```

On OPNsense: **Firewall → NAT → Destination NAT** (Port Forward on older
versions), interface = the feeder's network, protocol UDP, source = feeder,
destination = NOT private networks, port 10240 (and a second rule for 10001),
redirect target = HA host, same port, filter rule association = Pass.

Notes:

- Redirect both ports. With `p2p_region_address: captured` the referral names
  the vendor's two region servers, and a feeder may also remember region
  servers from an earlier online boot; the 10001 redirect catches them
  whatever their address. (Our 63-minute NAT test used `captured`.)
- The replies keep the original server address (the router's redirect state
  rewrites them back), so nothing on the feeder changes.
- pf may change the source port of redirected flows that would otherwise
  collide on the HA host. The replies are translated back; it is harmless.
- UDP 10001 must be free on the HA host. UniFi device discovery also uses it;
  the responder refuses to share the port and logs a bind error instead.

**Results.**

- Redirects (4b, `captured`): the feeder queried the masters, got the local
  referral, probed and logged in within the same second, streamed to the bridge
  from that P2P socket, and kept one port for the whole 63-minute watch with no
  port-unreachable messages.
- DNS override (4a, `auto`, no NAT rules): 4 seconds after its DHCP lease the
  feeder resolved the master name to the HA host, and its master query, region
  probe and login were all answered by the responder from one source port,
  with video about 30 seconds later. It then ran 61 minutes with no P2P
  restarts and no port-unreachable messages, and a second cold boot (power
  cut) came up the same way.

Before the responder, every offline boot restarted P2P within that time. A
multi-day soak is still pending.

## 5. Check it

```bash
# Responder activity (HA host)
docker logs app_<repo>_wopet_feeder_bridge 2>&1 | grep p2p-responder
```

- Right after the feeder boots: `master from <feeder>:<port> ... answered`,
  then `probe` and `login` lines.
- Every 10 minutes: a count of logins and keepalives.
- A **new** `master from` line on a different port later on means the feeder
  restarted P2P: its queries are not reaching the responder.

On the router, a capture of `icmp and src host <feeder>` should show no
`udp port 32761 unreachable` messages.

To check video, pull a few seconds of `http://<ha>:1985/api/stream.mp4?src=wopet`
or open the camera in Home Assistant. Do not use go2rtc's `/api/frame.jpeg`:
it needs ffmpeg, which the bridge image does not include, and always returns 500.

## What not to do

**Do not fake the internet checks.** The feeder checks connectivity with a TCP
connect to `www.baidu.com:80` and an HTTP firmware check to
`dogsndsoftchk.jkgeek.com`. We redirected its TCP 80 to a local stub, first
answering a plain `OK`, then the exact JSON the real server returns. Both times
the checks passed and the feeder then stopped its P2P service and closed UDP
32761; the second time it hung (ping and ARP only) until it was power-cycled.
It also sent a 54-byte binary TUTK payload to the TCP 80 host, which looks like
a TCP fallback path. Leave TCP 80 blocked.

**Do not expect the phone app to work offline.** The WOPET app finds the feeder
through the TUTK cloud, so it cannot reach a blocked feeder even on the same
network. Unblock briefly when you need it (for example to record a voice clip),
then block again and kill the feeder's states.

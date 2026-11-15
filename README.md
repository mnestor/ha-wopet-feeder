# Wopet Feeder (local) for Home Assistant

Local video, manual feeding and feeding-schedule control for the **WOPET Camera
Pet Feeder** in Home Assistant, with the feeder cut off from the internet.

> [!WARNING]
> Experimental interoperability software. Validated on **one** unit: WOPET
> Camera Pet Feeder (ASIN B07N5BBFZY, "smart pet feeder" in the app), firmware
> **40.0.14.3**, H.264 1280x720 video. It is not affiliated with or endorsed by
> WOPET or ThroughTek. Use it only with devices and accounts you own.

This is a fork of [DrDronez/ha-wopet-local](https://github.com/DrDronez/ha-wopet-local),
which brought local video to the WOPET Guardian Plus D100 camera. This fork
replaces the D100 pan and treat controls with the feeder's own feed and schedule
protocol, adds offline operation, and documents everything learned on the way.
If you have a D100, use upstream.

## What works

| Feature | Status | Notes |
| --- | --- | --- |
| Local video (H.264) in Home Assistant | Works | About 15 fps 1280x720 over RTSP through go2rtc, no cloud at runtime |
| Read the six meal slots | Works | Time, portions, on/off, grid, name, sound |
| Edit a meal slot | Works | Read-modify-write; the bridge confirms the feeder stored it |
| Set the feeder clock | Works | Sent at every camera session, with the HA timezone |
| Run with the internet blocked | Works | See [Ways to run it](#ways-to-run-it) |
| Stop offline video drops | Works (soaks of about an hour so far) | Local answers to the feeder's P2P server queries, reached by a DNS override or router redirects |
| Manual feed (Feed button) | Implemented, not yet pressed from HA | Sent exactly once, never retried; reports "bowl full" refusals |
| Edit the schedule on a dashboard | Works | Feeding schedule card (`custom:wopet-schedule-card`), loaded by the integration |
| Choose a clip per meal | Works | `Meal N clip` select: Silent or one of the five clips, with their labels |
| Record a voice clip | Works | Recorder card on the dashboard, or `wopet_local.upload_clip` for an audio file; `wopet_local.play_clip` plays one now; bridge 0.5.8 + integration 0.4.0 |
| Feeding history (`0x20d3`) | Does not work | Query timed out and broke the next session |
| Camera audio | Works (experimental, off by default) | 8 kHz microphone as G.711 A-law next to the video; needs bridge 0.5.7 |
| Talk through the feeder speaker | Works (experimental) | `POST /talk` on the control port; Home app talk button through Homebridge; needs bridge 0.5.8 |
| HomeKit with Secure Video | Works | Through Homebridge, with a software motion trigger; see [HomeKit](docs/HOMEKIT.md) |
| Faking the vendor's internet checks | Does not work, do not try | The feeder stops P2P and can hang until power-cycled |

## How it fits together

```text
WOPET feeder  <--- TUTK/Kalay over UDP (LAN) --->  Wopet Feeder Bridge app (HA host)
   |                                                 |- go2rtc: RTSP 8554, API 1985
   |  UDP 10240 / 10001 (P2P server queries)         |- control API 1986 (bearer token)
   +--- DNS override or router redirect ---------->  |- P2P responder: UDP 10240, 10001
                                                     v
                                     Wopet Local integration (camera, feed, 6 meal slots)
```

1. **Wopet Feeder Bridge** (Home Assistant app): a pure-Python TUTK/Kalay client
   (adapted from MIT `cuboai-tutk`) that logs in to the feeder on the LAN,
   re-serves its video through go2rtc, exposes an authenticated control API, and
   answers the feeder's P2P server queries locally.
2. **Wopet Local** (custom integration, domain `wopet_local`): a camera entity,
   a Feed button with a portion count, and per-slot entities for the schedule.

No proprietary TUTK library is used. Device credentials stay in the app's
private configuration.

## Ways to run it

The bridge and integration are the same in every setup. What changes is
whether the feeder can reach the internet and, if not, how its P2P server
queries reach the bridge's responder. Background: at boot the feeder looks up
its ThroughTek master server by name, asks it (UDP 10240) for region servers,
then logs in to those (UDP 10001). If nothing answers, it restarts its P2P
service every 10 to 60 minutes and the local video drops each time. The bridge
answers those queries itself when they reach it.

| Path | Internet | What you configure | Video | Tested |
| --- | --- | --- | --- | --- |
| A. Online | Allowed | Nothing beyond the install | Stable | Yes |
| B. Offline, DNS override | Blocked | Block rule, one DNS host override, (routed only) one allow rule | Stable | Yes: offline boot, about an hour |
| C. Offline, router redirects | Blocked | Block rule, two destination-NAT rules, (routed only) one allow rule | Stable | Yes: offline boot, 63 minutes |
| D. Offline, no responder | Blocked | Block rule only | Drops every 10-60 min, back about 5 s after each restart | Yes, the original problem |

**Recommended: B.** It needs one DNS record and no NAT, and works with any DNS
server that can override a single name (OPNsense/pfSense Unbound, Pi-hole,
AdGuard Home, dnsmasq). Use C when the feeder ignores your DNS server or your
DNS cannot override names. Details and verification for B, C and D are in
[Offline operation](docs/OFFLINE.md).

### A. Online

Leave the feeder's internet access alone. Local video and control work, the
WOPET phone app keeps working, and the feeder stays in the vendor cloud. The
P2P responder receives nothing and does nothing.

### B. Offline with a DNS override (recommended)

1. Block the feeder from all non-private destinations.
2. If the feeder and Home Assistant are on different networks: allow UDP from
   the feeder to the HA host, any ports (the feeder answers the bridge from a
   random port).
3. Add a DNS host override that resolves the feeder's master server name to
   the HA host. On our firmware the name is
   `all-d-master-bx1kb94488ffwtalxe9a.iotcplatform.com`; check yours in your DNS
   server's query log. Override only that exact name, not all of
   `iotcplatform.com`: other TUTK cameras on your network use the same domain.
4. Keep the bridge options `p2p_responder: true` and `p2p_region_address: auto`
   (the defaults). The responder's referral then names the HA host as region
   server, so the feeder's UDP 10001 comes straight to the bridge.
5. Power-cycle the feeder so it looks the name up again.

The feeder must use the DNS server handed out by DHCP (ours does).

### C. Offline with router redirects

1. and 2. as in B.
3. Destination-NAT the feeder's UDP 10240 and UDP 10001 to non-private
   addresses onto the HA host, same ports.
4. Keep `p2p_responder: true`. `p2p_region_address` can stay `auto`; set it to
   `captured` to hand the feeder the vendor's region-server addresses instead
   (the 10001 redirect then catches them). Our 63-minute test used `captured`.
5. Power-cycle the feeder.

### D. Offline without the responder

Only the block rule (and the allow rule if routed). Schedules and feeds keep
working, and the bridge reconnects about 5 seconds after each P2P restart, but
each restart drops the camera for up to several minutes. Set
`p2p_responder: false` if UDP 10240 or 10001 is needed by something else on
the HA host (UniFi discovery uses 10001).

Do not try to fake the feeder's internet checks on TCP 80 in any path: it then
stops P2P altogether and can hang until power-cycled. See
[Offline operation](docs/OFFLINE.md#what-not-to-do).

## Install

1. **Get the device credentials** (UID, viewer username and password): capture
   the WOPET app's traffic while it loads the device list. The app's API on TCP
   8088 is plain HTTP. Any capture works (PCAPdroid on Android, or `tcpdump` on
   the router for an iPhone); then run `tools/extract_device_credentials.py`.
   See [Bridge setup](docs/BRIDGE_SETUP.md#getting-device-credentials).
2. **Install the bridge app**: add `https://github.com/mnestor/ha-wopet-feeder`
   under **Settings → Apps → App store → Repositories**, install **Wopet Feeder
   Bridge**, fill in the camera IP, credentials and a long random control token,
   and start it.
3. **Install the integration**: add the same URL to HACS as a custom repository
   of type Integration, install **Wopet Local (feeder)**, restart, add the
   integration, then enter the control port and token under **Configure**.
   See [HACS setup](docs/HACS_SETUP.md).
4. **Optional, recommended: go offline.** Pick one of the
   [ways to run it](#ways-to-run-it) below. Blocking the internet without
   pointing the feeder's P2P server queries at the bridge makes video drop every
   10 to 60 minutes.

This integration uses the same `wopet_local` domain as upstream and replaces it;
the two cannot be installed together. Do not let HACS update it from the
upstream repository.

## Entities

| Entity | Purpose |
| --- | --- |
| `camera.<name>` | Live view |
| `button.<name>_feed` | Dispense now (one request, never retried) |
| `number.<name>_feed_portions` | Portions for the Feed button, 1 to 4 (kept in HA) |
| `switch.<name>_meal_N` | Meal N on/off (N = 1 to 6, fixed slots) |
| `time.<name>_meal_N_time` | Meal time |
| `number.<name>_meal_N_portions` | Portions, 1 to 10 |
| `select.<name>_meal_N_grid` | Food grid size: small or big |
| `select.<name>_meal_N_clip` | Voice clip the meal plays: Silent (no sound) or Clip 1-5, with the feeder's labels |
| `text.<name>_meal_N_name` | Meal name |

### Feeding schedule card

The integration also loads a **Wopet feeding schedule** card
(`type: custom:wopet-schedule-card`; optional `title`, and `device_id` if you
have more than one feeder). It lists the six meals sorted by time with an on/off
switch each; open a meal to change its time (type `0630` or `18:15`), label,
portions, kibble size and voice clip in place. A change counts as saved only
when Home Assistant reports the feeder's new value: until then the field shows
*Saving…*, and if the feeder doesn't confirm within 10 seconds the card puts
the old value back and offers **Retry**. Switching a meal off shows **Undo**
for a few seconds.

```yaml
type: custom:wopet-schedule-card
title: Feeding schedule
```

### Voice clips

The feeder stores five clips. The easiest way to record one is the **Wopet
clip recorder** card, which the integration loads on every dashboard (no
resource to add). Edit a dashboard, **Add card**, pick *Wopet clip recorder*
(or `type: custom:wopet-clip-card`). Choose a slot, tap **Record** (it stops by
itself at 10 seconds), listen back, then **Save to feeder**. **Play on feeder**
plays the slot now. The browser only allows the microphone over HTTPS or in the
Companion app.

To use an existing audio file instead, put it in **Media → My media** and call:

```yaml
action: wopet_local.upload_clip
data:
  clip: 3                 # slot 1-5; replaces what is there
  file: media-source://media_source/local/dinner_bell.m4a   # or /media/dinner_bell.m4a
  label: Dinner bell      # optional; defaults to the file name
```

Any format ffmpeg reads works. The first 10 seconds are kept, mixed to mono,
resampled to 8 kHz and normalised to just under full scale. Then pick the clip
in a meal's clip select, or play it now with `wopet_local.play_clip`.

The schedule is stored on the feeder and runs without Home Assistant. HA reads
it every 6 hours and after each change. Every command starts a short camera
session first, so expect 5 to 10 seconds per change.

## Documentation

- [Offline operation](docs/OFFLINE.md): firewall rules, the P2P responder, clock, verification
- [HomeKit](docs/HOMEKIT.md): Homebridge camera, HomeKit Secure Video, motion trigger, audio
- [Protocol](docs/PROTOCOL.md): every decoded message, with byte layouts
- [Findings](docs/FINDINGS.md): research log, what failed and why, open questions
- [Bridge setup](docs/BRIDGE_SETUP.md) and [HACS setup](docs/HACS_SETUP.md)
- [Changelog](CHANGELOG.md)

## Security

- Never commit or post captures, app options, device credentials, UIDs, MAC
  addresses or public IP addresses.
- go2rtc's RTSP (8554), API (1985) and WebRTC (8555) ports are unauthenticated
  on the HA host's LAN; the control API (1986) needs the token. Do not expose
  any of them to the internet.
- The TUTK/Kalay protocol family has had serious vulnerabilities
  (CVE-2021-28372). Keeping the feeder off the internet removes the cloud path.

See [SECURITY.md](SECURITY.md).

## Development

```bash
python -m pip install -r requirements-dev.txt -r tools/requirements.txt
pytest
ruff check .
```

The tests use synthetic packets built from the decoded layouts; no captures are
stored in the repository.

## Credits and license

MIT. Based on [DrDronez/ha-wopet-local](https://github.com/DrDronez/ha-wopet-local)
and [Fredde87/cuboai-tutk](https://github.com/Fredde87/cuboai-tutk). See
[LICENSE](LICENSE) and [third-party notices](THIRD_PARTY_NOTICES.md).

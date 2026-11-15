# Changelog

Versions of the bridge app (`wopet_bridge/config.yaml`) and the integration
(`custom_components/wopet_local/manifest.json`) move independently. This fork
starts from DrDronez/ha-wopet-local 0.2.10.

## Bridge 0.5.7

- Camera audio (`enable_audio`) works on the feeder: its 8 kHz microphone
  track is published as G.711 A-law next to the video. The audio pipe (MPEG-TS)
  now gets the same H.264 fixes as the video-only pipe: the 16-byte trailer is
  stripped (without that, nearly every frame had a fake "slice type 32" NAL),
  damaged frames are dropped until the next keyframe, and frames are stamped
  from the trailer's camera clock instead of arrival time. Audio uses the same
  clock when its trailer agrees with the video's. The feeder sends digital
  silence between sounds.
- The output pacer and the no-video watchdog no longer see audio frames.

## Bridge 0.5.6

- Strip the feeder's 16-byte TUTK FRAMEINFO trailer from every video frame. The
  vendored client only knows the 24-byte version (HEVC cameras) and left the
  feeder's in. Its changing timestamp bytes sometimes form an H.264 start code,
  and go2rtc then cut fake NAL units out of it: one-byte "slices" that decoders
  reject ("slice type 32 too large"), hundreds per 30 seconds at times.

## Bridge 0.5.5

- Paced output. go2rtc stamps raw H.264 frames with the time it reads them, and
  loss recovery releases frames in bursts, so several frames shared a timestamp
  and RTSP viewers merged them (decode errors, lost frames). The stream now
  writes frames at the measured frame rate and catches up when frames queue.
  Live session through go2rtc, 30 s: 387 frames and 107 decode errors before,
  435 frames and 15 errors after, longest gap 0.13 s.

## Bridge 0.5.4 (includes 0.5.3)

- New option `preload_stream` (default on): go2rtc keeps one feeder session open
  (`preload:`), so viewers join instantly. Starting a session takes 3 to 5
  seconds; clients that gave up sooner (Home Assistant's own go2rtc retries
  after a few seconds) started and killed a session on every retry. HomeKit
  Secure Video needs the stream open anyway.
- If the feeder stops sending video on an open session (seen right after a
  restart, while it still held the previous sessions), the stream process exits
  after 20 seconds and reconnects 5 seconds later. Before, with the stream kept
  open, such a session stayed silent forever.

## Bridge 0.5.2

- go2rtc now really stops the stream process when the last viewer leaves. The
  exec source said `#killsignal=SIGTERM`, but go2rtc reads the signal as a
  number, so it sent signal 0 (nothing). Each abandoned process kept its feeder
  session, go2rtc stopped reading it, and the next viewer started another one
  that had to take the session over, so video kept breaking up whenever viewers
  came and went. Now `#killsignal=15#killtimeout=8`.

## Bridge 0.5.1

- Smooth video: the stream checked every frame with HEVC header rules, but the
  feeder sends H.264. A few frames into each 2-second GOP an ordinary P-frame
  failed the HEVC check and everything up to the next keyframe was dropped, so
  only about 2 of the feeder's ~15 frames per second reached go2rtc and playback
  jumped every couple of seconds. The checks now follow the codec
  (`CUBOAI_VIDEO_CODEC=h264`, set by the bridge). Measured on the feeder: 79
  frames in 40 s before, 543 after.

## Bridge 0.5.0

- New option `p2p_region_address` (default `auto`): the responder's referral
  names this host as the feeder's region server, so the feeder's UDP 10001 comes
  straight to the bridge. Together with a DNS override of the feeder's master server name
  (`all-d-master-*.iotcplatform.com`; the feeder looks it up at boot) the offline setup needs no
  NAT rules. `captured` keeps the vendor's region servers for NAT setups.

## Bridge 0.4.3

- Count 4-byte P2P keepalives as keepalives in the responder log.

## Bridge 0.4.2

- The P2P responder no longer sets `SO_REUSEADDR`, so it fails loudly instead of
  silently sharing UDP 10001 with another service (for example UniFi discovery).

## Bridge 0.4.1

- New service `p2p` and option `p2p_responder` (default on): answers the
  feeder's TUTK master query (UDP 10240), region probe and region login
  (UDP 10001) locally, for the configured camera IP and UID only. With the
  router redirects in [docs/OFFLINE.md](docs/OFFLINE.md) this stops the offline
  P2P restarts that dropped video every 10 to 60 minutes.

## Bridge 0.4.0

- Set the feeder clock and timezone (IOCTL `0x2052`) a few seconds after every
  camera session, and on `POST /time`.
- Meal records expose `sound` (clip index 0-4, or none); `POST /plan` accepts
  `sound`. Turning audio on keeps the current clip or uses clip 0.

## Bridge 0.3.6 - 0.3.9

- Before connecting and after a drop, probe the feeder's UDP 32761; while it is
  closed (P2P restarting) wait and poll every 3 s instead of counting a failure.
  Video returns about 5 s after the port reopens.
- 0.3.7 and 0.3.8 tried a local stub for the feeder's TCP 80 internet checks.
  It made the feeder stop P2P and once hang, so 0.3.9 removed it.

## Bridge 0.3.0 - 0.3.5

- Feeder control API: `GET /plan`, `POST /plan` (read-modify-write of one slot,
  confirmed against the feeder's ack or a fresh read), `POST /feed` (IOCTL
  `0x207e`, sent exactly once, reports bowl-full refusals).
- Vendored TUTK client: send-once IOCTLs and content matching of replies, so
  late duplicates and count-0 plan replies are not taken as answers.
- A new go2rtc producer stops the previous one; a producer exits when go2rtc
  stops reading for 15 s. Prevents orphaned sessions from holding the control
  port and tripping the feeder's session rate limit.
- Removed the D100 pan and treat commands.
- App renamed to Wopet Feeder Bridge, slug `wopet_feeder_bridge`.

## Integration 0.3.1

- Meal audio switches show the clip they play (`sound_file` attribute).

## Integration 0.3.0

- Feeder schedule entities for the six fixed slots: on/off, audio, time,
  portions, grid size and name, polled every 6 hours and after each change.
- Feed button with a `Feed portions` number (1-4); keeps the unique id of the
  upstream treat button so existing entities survive.
- Commands wait for live video from go2rtc's current producer before using the
  control port.

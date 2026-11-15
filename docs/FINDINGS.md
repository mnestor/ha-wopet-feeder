# Findings and research log

How the feeder was taken off the cloud, including the wrong turns, so the next
person does not repeat them. Work done 14-15 November 2026 on one WOPET Camera
Pet Feeder (firmware 40.0.14.3), on our own network and hardware.

## Device

- WOPET Camera Pet Feeder, sold as ASIN B07N5BBFZY; the app calls it "smart pet
  feeder". No FCC ID on the case.
- H.264 video, 1280x720 (the D100 camera that upstream supports sends HEVC).
- ThroughTek TUTK/Kalay P2P for video and control; vendor HTTP API on TCP 8088
  (plain HTTP); MQTT on TCP 1883; telnet open on TCP 23.
- Six fixed meal slots, stored on the device. Feeds keep running offline.

## Timeline of what we learned

### Recon: "no local path" (wrong)

- A LAN scan found only telnet open. 16 common default passwords for `root` and
  `admin` failed; `root` exists, its password is not a known default.
- Generic PPPP/Kalay LAN search packets (UDP 32108, 32761, 32100, 10001, 8600)
  and a passive listen got no reply from the feeder.
- Conclusion at the time: cloud-relay only. **Wrong.** The feeder only answers
  an authenticated TUTK session that carries its UID; anonymous probes are
  ignored.

### Credentials without Android

Upstream extracts credentials from a PCAPdroid capture on Android. The WOPET
app's device API on TCP 8088 is plain HTTP, so a router-side `tcpdump` of an
iPhone opening the app works just as well. `tools/extract_device_credentials.py`
reads any classic pcap. The device UID equals the `ucode` the feeder sends in its
cleartext firmware check; the viewer username is `admin`.

### Local video works

The upstream pure-Python client (with `CUBOAI_WOPET_X2043=1`) authenticated
LAN-direct on the first try and returned H.264 frames. The firmware does not
need a cloud login before serving LAN sessions: after a power cycle with the
internet already blocked, a local session worked 15 seconds after boot.

### Routed networks: the reply comes from another port

From a host on a different VLAN every connect failed with "no 0x2041", and the
library's error text blamed rate limiting. Captures on both sides showed the
real cause: the feeder answers the probe to UDP 32761 from its main P2P socket
(random port per boot), so the firewall saw the reply as a new inbound flow and
dropped it. An allow rule for UDP from the feeder to the HA host fixed it.

### Schedule and feed protocol

Captured app sessions while changing one thing at a time (portion count, grid,
on/off, sound). Results are in [Protocol](PROTOCOL.md). Things worth knowing:

- Event code `0x36`, shown in the app as "Dispense excessive food", means the
  feed was **refused because the bowl was full**. Most scheduled feeds in the
  vendor's history were refusals.
- Feeds ran exactly one hour early after the DST change: the device schedules on
  a fixed offset it was given, it does not apply timezone rules.
- The feeder rebooted every night around 01:10-01:35 while online.
- Writes must be read-modify-write: the records contain unknown bytes and
  uninitialised junk after the name.

### Bridge reliability fixes

- **Late and empty replies.** The feeder sends late duplicates of earlier plan
  replies and 372-byte replies with a count of 0. Taking those as the write ack
  reported successful writes as failures. Replies are now matched on content,
  and a write is confirmed by reading back.
- **Feeds must never repeat.** The vendored client retransmitted IOCTLs; feeds
  are now sent exactly once.
- **Orphaned sessions.** go2rtc can abandon an exec producer without killing
  it. The orphan stops reading the camera but keeps the control port, so
  commands timed out, and nine stacked orphans tripped the feeder's session rate
  limit. A new producer now stops the previous one, and a producer exits if
  stdout is blocked for 15 s.
- **Jumpy video (HEVC rules on H.264).** The vendored streamer, written for
  the HEVC D100, rejected any frame whose NAL headers looked invalid as HEVC.
  The feeder's H.264 keyframes passed by accident (the SPS header byte `0x27`
  reads as an HEVC IDR), but within a few frames a P-slice tripped the HEVC
  temporal-id check and the rest of the GOP was dropped: about 2 fps out of ~15.
  The checks are now codec-aware.
- **Processes go2rtc could not stop.** The exec source said
  `#killsignal=SIGTERM`; go2rtc parses the signal as a number, so it sent
  signal 0 and every viewer that left abandoned a running process with its
  feeder session. Now `#killsignal=15#killtimeout=8`.
- **Slow session start.** A feeder session takes 3 to 5 seconds to deliver
  video. Viewers that gave up sooner started and killed a session on each
  retry. The bridge now keeps one session open (go2rtc `preload:`, option
  `preload_stream`), which HomeKit Secure Video needs anyway.
- **Silent sessions.** Right after a restart the feeder sometimes accepted a
  session and never sent video. The stream process now exits after 20 s
  without video and reconnects 5 s later.
- **Merged frames.** go2rtc stamps raw H.264 with the time it reads each
  frame. Loss recovery releases frames in bursts, so frames shared a timestamp
  and players merged them. The stream now writes frames at the measured frame
  rate.
- **A 16-byte trailer inside the video.** Each H.264 frame ends with a 16-byte
  TUTK FRAMEINFO (codec `0x4E`, flags, then a millisecond timestamp). The
  vendored client only stripped the 24-byte HEVC version. When the timestamp
  bytes happened to form a start code, go2rtc cut one-byte fake slices out of
  it ("slice type 32 too large"). After stripping it, 30-second captures had
  no decode errors and no gap over 0.07 s.
- **Restart-aware reconnects.** The bridge probes UDP 32761 before connecting;
  port-unreachable means the feeder's P2P service is restarting, so it waits
  and reconnects about 5 s after the port reopens instead of backing off for up
  to 10 minutes.

### Offline video drops

With the internet blocked, the feeder restarted its P2P service every 10 to 60
minutes, after failed connectivity checks and unanswered master queries.

1. **Faking the connectivity checks failed** (see
   [Offline operation](OFFLINE.md#what-not-to-do)). Passing the checks made it
   stop P2P entirely, and once hang. Do not retry.
2. **Online boot, then block:** with internet at boot the feeder got a master
   referral (UDP 10240) and logged in to two region servers (UDP 10001); after
   blocking, it stayed on one port and did not restart. The missing referral is
   the trigger.
3. **Local responder:** answering the master query, region probe and login from
   the HA host (with router redirects) made an offline boot behave like an
   online one. It kept one P2P port for the whole 63-minute test, with no drops.
4. **DNS instead of NAT:** the feeder finds its master by name
   (`all-d-master-bx1kb94488ffwtalxe9a.iotcplatform.com`), and the referral can
   name any region server. A DNS host override pointing that name at the HA
   host, plus a referral naming the HA host (`p2p_region_address: auto`),
   replaced both NAT rules: master, probe and login all reached the bridge
   directly from one port.

### Audio

The feeder's microphone track is 16-bit PCM at 8 kHz in 50 ms frames, each with
a 16-byte trailer (codec `0x8C`) whose millisecond timestamp (offset 12) runs
on the same clock as the video trailer. The bridge converts it to G.711 A-law.
Between sounds the feeder sends frames of exact zeros. The audio pipe
(MPEG-TS) first had none of the H.264 fixes above; with them, a 40-second
capture had one decode error and audio arrived at a steady 20 frames per
second.

### HomeKit

Home Assistant's HomeKit Bridge has no HomeKit Secure Video, so the camera goes
through Homebridge (see [HomeKit](HOMEKIT.md)). Three traps in
homebridge-camera-ffmpeg 4.1.0: `audio: true` on a stream without audio makes
its ffmpeg exit with code 234 (no live view); its prebuffer crashes the child
bridge whenever the RTSP stream ends; and its local motion endpoint listens on
`::1` only, so `127.0.0.1` is refused.

### Clock

Offline, the feeder lost time across a power cut and made its first NTP request
about 11 minutes after boot. The app sets the clock (`0x2052`) on every connect;
the bridge now does the same at every session.

### Voice clips

The meal "audio" byte is a clip index (`sound0.wav` to `sound4.wav`), not a
flag. Clip upload (bridge 0.5.8) is TUTK RDT on IOTC channel 3; the layout is in
[Protocol](PROTOCOL.md). The first upload worked with the capture's bytes
replayed, then every later one stalled after the ping exchange: byte +17 of the
RDT header is a connection id that belongs to the feeder (1 after boot), not a
constant, and after a few aborted uploads the feeder's id had moved to 8 and it
ignored our pongs. The bridge now takes the id from the feeder's ping, and
closes each transfer (FIN, close) so the id stays put. A 2-second clip uploads
in about 2 seconds; the feeder confirms it with `07 stop` and lists the new
label and time in `0x2087`.

### Talk

Two-way talk (bridge 0.5.8) follows the app: IOCTL `0x0350` on channel 0, the
feeder logs in to us on channel 1, we grant it, then send 50 ms PCM frames with
the feeder's own audio trailer. The feeder acknowledges every frame (`0x09`,
highest index at [38:40]); all test frames were confirmed, and the Home app's
mic button (through Homebridge) was heard at the feeder. Its microphone has
echo cancellation, so a recording of the feeder hears steady tones from its own
speaker fade within about a second; that is not lost audio.

## Open questions

- Long-term stability with the responder (multi-day soak).
- Whether the nightly reboot still happens offline, and whether the responder
  covers every later re-initialisation.
- Meaning of meal record +4..5, +6 (probably a weekday mask) and +11 bit 4.
- Meaning of the referral's type 1 and type 3 entries (replayed as captured).
- Feed log: bridge 0.5.9 reads `0x20c0` (see Protocol). Still open: whether the
  feeder pages a window with more records than one reply holds, and why an
  earlier `0x20d3` query of ours timed out and broke the next session's
  handshake (the app's `0x20d3` request is in the capture, but the bridge
  doesn't send it).
- Manual feed from Home Assistant has not been pressed on the real feeder yet.
- Why camera audio once stopped after about 95 seconds (seen once, not since).
- Other models or firmware versions.

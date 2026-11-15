# Wopet Feeder Bridge

This app must be installed and running before configuring the Wopet Local HACS
integration.

Enter the camera's LAN IP and its TUTK device credentials in the Configuration
tab. All credential fields are treated as passwords by the Home Assistant app
UI. They are read directly from `/data/options.json`; they are not placed in
command-line arguments, generated go2rtc configuration, or normal logs.

Choose an unused **RTSP port** in the Configuration tab. The app uses host
networking because the camera's UDP session handshake does not survive container
NAT. After startup, the stream is available at:

```text
rtsp://HOME_ASSISTANT_HOST:RTSP_PORT/wopet
```

Generate a long random **control token** and enter it in both this app and the
Wopet Local integration's Configure dialog. The integration uses the
authenticated control port for the Feed button and the six meal slots. A feed
is sent exactly once and never retried. The bridge also sets the feeder's clock
and timezone a few seconds into every camera session.

Leave **Answer P2P server queries locally** on if the feeder has no internet
access. Point the feeder's P2P server queries at this host, either with a DNS
host override of its master server name (`all-d-master-*.iotcplatform.com`,
recommended) or with router redirects of its UDP 10240 and 10001. That stops
the feeder restarting its P2P service, which otherwise drops video every 10 to
60 minutes. Leave **Region server address in the referral** on `auto`. See the README and
docs/OFFLINE.md in the repository.

Leave **Keep the camera stream open** on. go2rtc then holds one feeder session
all the time, so viewers join at once instead of each waiting 3 to 5 seconds
for a new session (clients that give up sooner used to start and kill sessions
on every retry). HomeKit Secure Video needs it. Turn it off only to free the
feeder when nothing watches the camera.

Enable **Include camera audio** to publish the feeder's 8 kHz microphone as
G.711 A-law (PCMA) with the video. The feeder sends silence between sounds.
This changes the bridge pipe from raw video to MPEG-TS and remains
experimental; turn it back off if a client cannot play the combined stream. The image builds the tagged go2rtc 1.9.14 source with the
small PCMA MPEG-TS discovery fix that was subsequently accepted upstream.

**Talk** (experimental): the control port accepts `POST /talk` with the
control token and streams the body to the feeder's speaker, as G.711 A-law
(`Content-Type: audio/PCMA`) or 16-bit 8 kHz mono PCM (`audio/L16`). The body
may be chunked, so a live microphone works. The go2rtc stream also has a
two-way audio backchannel, so a WebRTC viewer that sends its microphone talks
through the feeder. For the Home app, see docs/HOMEKIT.md.

**Voice clips**: `GET /clips` lists the feeder's five clip slots;
`POST /clip?clip=N&label=NAME` stores a WAV (PCM 16-bit, mono, 8 kHz, at most
10 s) in slot N (0-4); `POST /clip/play` with `{"clip": N}` plays one. The
Wopet Local integration wraps these as the per-meal clip selects and the
`upload_clip` / `play_clip` services.

The app's **Open Web UI** button opens the protected go2rtc diagnostics page.

If the camera drops a session or temporarily refuses a reconnect, the bridge
waits for the feeder's UDP 32761 to answer (it closes while the feeder
restarts its P2P service), waits for a complete keyframe before handing data to go2rtc, keeps the
go2rtc media pipe open, and applies a persistent 60-second-to-10-minute
exponential cooldown when the camera session fails. The cooldown survives
go2rtc source respawns so the camera cannot be pushed into a rapid-attempt
lockout. A normal on-demand viewer disconnect clears the retry state so the
next viewer can start immediately.

After the opening keyframe establishes the stream, malformed or incomplete
video access units are discarded. Playback resynchronizes at the next keyframe
instead of allowing a damaged NAL unit to restart go2rtc.
Do not expose the selected RTSP port, control port, or diagnostics port `1985`
to the internet.

For complete installation, credential, network, and troubleshooting guidance,
see https://github.com/mnestor/ha-wopet-feeder/blob/main/docs/BRIDGE_SETUP.md

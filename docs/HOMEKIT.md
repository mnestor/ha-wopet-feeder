# HomeKit and HomeKit Secure Video

Home Assistant's HomeKit Bridge can show the camera in the Home app, but it does
not support HomeKit Secure Video (HKSV), so it gives no recordings and no
person or animal alerts. For those, publish the bridge's RTSP stream with
Homebridge and
[homebridge-camera-ffmpeg](https://github.com/homebridge-plugins/homebridge-camera-ffmpeg)
(tested with 4.1.0), and add a small motion helper, because the feeder reports
no motion on the LAN.

```text
feeder ──TUTK──> bridge app (go2rtc, rtsp://<ha-host>:8554/wopet)
                    │
                    ├──> Homebridge camera-ffmpeg ──> Home app (live view, HKSV)
                    └──> wopet_motion.py ──HTTP──> camera-ffmpeg motion sensor
```

HKSV needs a home hub (Apple TV or HomePod) and an iCloud+ plan.

## 1. Bridge app

Leave **Keep the camera stream open** (`preload_stream`) on. HKSV pulls the
stream at any moment, and a fresh feeder session takes 3 to 5 seconds.

## 2. Homebridge camera

Install the plugin and add a camera to its platform. A working example is in
[tools/homekit/homebridge-camera-ffmpeg.example.json](../tools/homekit/homebridge-camera-ffmpeg.example.json):

- `source`: `-rtsp_transport tcp -i rtsp://<ha-host>:8554/wopet`
- `maxWidth` 1280, `maxHeight` 720, `maxFPS` 15
- `audio`: `true` only when the bridge's **Include camera audio** is on (see
  [Audio](#audio)); otherwise `false`.
- `recording: true` for HKSV.
- `prebuffer: false`. The plugin's prebuffer crashes the whole child bridge
  whenever the RTSP stream ends (an unhandled "stream ended during read" in its
  MP4 parser), for example when the bridge app restarts. Without it, HKSV
  clips start when motion fires instead of a few seconds before.
- `motion: true`, `porthttp` (for example 8899) and `localhttp: true` for the
  motion helper.

Run it as a child bridge. The camera is published as its own accessory: add it
in the Home app with the setup code from the Homebridge log, then in the
camera's settings choose to stream and record, and to notify on animals.

The plugin's ffmpeg exits with code 234 when `audio` is on but the stream has
no audio track ("Output file does not contain any stream"); the Home app then
shows the camera but never loads live view.

The plugin declares Node.js 20, 22 or 24. It ran on Node 26 here with a
warning.

## 3. Motion helper

[tools/homekit/wopet_motion.py](../tools/homekit/wopet_motion.py) reads the
RTSP stream with ffmpeg at 4 fps, computes a scene-change score on small grey
frames, and calls the plugin's `/motion?<camera name>` endpoint when the score
stays above a threshold for consecutive frames. HomeKit then records, and its
classifier decides whether it was an animal.

Install on the Homebridge host (it needs `python3` and `ffmpeg`):

```bash
sudo install -m 755 tools/homekit/wopet_motion.py /usr/local/bin/
sudo install -m 644 tools/homekit/wopet-motion.service /etc/systemd/system/
sudo install -m 644 tools/homekit/wopet-motion.env.example /etc/default/wopet-motion
sudo systemctl enable --now wopet-motion
```

Edit `/etc/default/wopet-motion` first:

- `WOPET_RTSP`: the bridge's RTSP URL.
- `HB_MOTION_URL`: `http://localhost:<porthttp>/motion?<camera name, URL-encoded>`.
  With `localhttp` the plugin listens on `::1` only, so `127.0.0.1` is refused;
  use `localhost`.
- `MOTION_THRESHOLD` (default 0.02), `MOTION_FRAMES` (2), `MOTION_RETRIGGER`
  (10 s), `MOTION_FPS` (4).

Every 10 minutes it logs frames analysed, the peak score and the number of
triggers (`journalctl -u wopet-motion`). An empty room peaked at 0.004. Set the
threshold between the quiet peak and what a visit produces.

The feeder's stream loses 2 to 4% of packets, so ffmpeg logs occasional
"error while decoding MB" lines. They are concealed and do not cause triggers.

## Audio

The feeder has a microphone. Turn on the bridge's **Include camera audio**
(`enable_audio`) and set `audio: true` on the Homebridge camera; the plugin
transcodes the 8 kHz G.711 A-law track to AAC-ELD for the Home app.

- The feeder sends digital silence between sounds (a noise gate), so a quiet
  room is truly silent, not broken.
- With audio on, the bridge sends MPEG-TS instead of raw H.264 and stamps each
  frame with the feeder's own clock. Bridge 0.5.7 or later is needed; earlier
  versions left the H.264 trailer in this pipe and the video broke up.
- Viewers that use both tracks (ffmpeg, the Home app) wait for audio. Once, on
  the first run of 0.5.7, the audio stopped reaching go2rtc after about 95
  seconds while video continued, and those viewers froze. It did not happen
  again in later runs; the cause is unknown. If it happens, turn audio off.
  For debugging, the stream script writes a timestamp trace of audio and video
  when `CUBOAI_AV_DEBUG` names a file.

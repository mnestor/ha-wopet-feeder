# Wopet Local Bridge setup

The bridge is the process that talks to the camera. It converts the camera's
TUTK/Kalay H.264 stream into RTSP using go2rtc, serves the authenticated
feed and schedule controls, sets the feeder clock, and answers the feeder's P2P
server queries when it is offline.

## Requirements

- Home Assistant OS or Supervised Home Assistant on `amd64`.
- The camera and Home Assistant must be able to reach each other over the LAN.
- A stable DHCP reservation for the camera is strongly recommended.
- The camera's TUTK device UID, device username, and device password.

## Install

1. Open **Settings → Apps → App store**.
2. Open the repository menu and add:

   ```text
   https://github.com/mnestor/ha-wopet-feeder
   ```

3. Refresh the app store and install **Wopet Feeder Bridge**.
4. Open **Configuration** and set:

   | Option | Meaning | Example |
   | --- | --- | --- |
   | `camera_ip` | Reserved LAN address of the camera | `192.0.2.10` |
   | `device_uid` | TUTK/Kalay device identifier | Enter your own value |
   | `device_username` | Camera viewer username | Enter your own value |
   | `device_password` | Camera viewer password | Enter your own value |
   | `stream_name` | RTSP path name | `wopet` |
   | `rtsp_port` | Unused TCP port on the Home Assistant host | `8554` |
   | `control_port` | Unused authenticated-control TCP port | `1986` |
   | `control_token` | Long random token shared with the HA integration | Enter your own value |
   | `enable_audio` | Include the experimental camera listen track (untested on the feeder) | `false` |
   | `p2p_responder` | Answer the feeder's P2P server queries locally (see [Offline operation](OFFLINE.md)) | `true` |
   | `log_level` | Bridge logging detail | `info` |

5. Save, start the app, and enable **Start on boot** and **Watchdog**.

The RTSP endpoint is:

```text
rtsp://HOME_ASSISTANT_HOST:RTSP_PORT/wopet
```

Replace `HOME_ASSISTANT_HOST`, `RTSP_PORT`, and the stream name with the values
used in your installation.

## Getting device credentials

The bridge intentionally does not ask for the Wopet account password.
The mobile app's account bootstrap traffic has been observed using plain HTTP,
so automatically repeating that login flow would expose reusable account
material in transit.

The WOPET app's device API (TCP 8088) is plain HTTP and returns the UID,
viewer username and viewer password. Capture it once from your own app session,
then extract the three values locally:

1. Start a capture that sees the phone's traffic:
   - Android: PCAPdroid on the phone, exporting a pcap;
   - iPhone or Android: on your router, for example
     `tcpdump -i <lan-interface> -w wopet.pcap host <phone-ip> and tcp port 8088`.
     The phone must be on your Wi-Fi, not mobile data or a VPN.
2. Force-quit the WOPET app, open it, wait for the device list, and open the
   live view briefly. The feeder must still have internet access for this.
3. Stop the capture and copy it to a trusted computer.
4. In a clone of this repository, run:

   ```bash
   python -m pip install -r tools/requirements.txt
   python tools/extract_device_credentials.py /path/to/capture.pcap
   ```

5. Copy the three displayed device values into the bridge configuration.
6. Delete the capture.

The utility processes the file locally and does not print account login fields
or upload anything. Its output still grants camera access: do not paste it into
an issue, chat, screenshot, or public log. Only capture devices and accounts
you own or are explicitly authorized to test.


## Network notes

- UDP from the Home Assistant host to the camera must be permitted. The app uses
  host networking because the TUTK/Kalay session handshake does not survive
  container NAT.
- The configured RTSP TCP port must be unused on the Home Assistant host.
- The control port requires its bearer token but is still intended for the
  trusted LAN only. Do not reuse a password or camera credential as the token.
- TCP port `1985` exposes the go2rtc status page on the Home Assistant host for
  local diagnostics. Do not forward the RTSP, control, or diagnostics ports to
  the internet.
- If the feeder and Home Assistant are on different networks, allow UDP from
  the feeder to the HA host from **any** source port: the feeder answers from a
  random port, not from 32761. See [Offline operation](OFFLINE.md).
- UDP 10240 and 10001 on the HA host are used by the P2P responder.
- Privileged container access is not required.

## Troubleshooting

### The bridge reports missing configuration

All four camera fields must be set to real values. Placeholder values are
rejected.

### Authentication fails

Verify the IP reservation and device credentials. Re-pairing or factory-resetting
the camera can change its device credentials.

### Authentication always fails from another subnet

The library reports `no 0x2041` and mentions rate limiting, but the usual cause
is a firewall dropping the feeder's reply from its random P2P port. Add the
allow rule above.

### Video drops every 10 to 60 minutes

The feeder is offline and its P2P server queries do not reach the bridge
(no DNS override or router redirects). See
[Offline operation](OFFLINE.md#4-the-p2p-responder-the-video-drop-fix).

### RTSP connects but no picture appears

Confirm the Home Assistant integration uses the same RTSP port and stream name
as the bridge. Test the RTSP URL with VLC, then verify go2rtc's protected app
Web UI or local status page at `http://HOME_ASSISTANT_HOST:1985`. go2rtc's
`/api/frame.jpeg` always fails in this image (no ffmpeg); use
`/api/stream.mp4?src=wopet` instead.

### Reporting a problem

Set `log_level` to `debug`, reproduce briefly, return it to `info`, and redact
all network addresses and identifiers before sharing logs. Never share the app
configuration or a packet capture publicly.

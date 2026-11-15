# HACS integration setup

Install and start the [Wopet Feeder Bridge](BRIDGE_SETUP.md) first.

## Add the unlisted repository

1. Open **HACS** in Home Assistant.
2. Open the three-dot menu and select **Custom repositories**.
3. Enter:

   ```text
   https://github.com/mnestor/ha-wopet-feeder
   ```

4. Select category **Integration** and choose **Add**.
5. Search for **Wopet Local (feeder)** and install it. It replaces the upstream
   Wopet Local integration (same `wopet_local` domain); remove that one first
   and do not let HACS update this one from the upstream repository.
6. Restart Home Assistant when HACS requests it.

This project is public so HACS can download it, but it has not been submitted
to HACS's default catalog. Add it as a custom repository using the steps above.

## Add the integration

1. Open **Settings → Devices & services**.
2. Select **Add integration** and search for **Wopet Local**.
3. Enter:

   - **Bridge host:** the IP address or resolvable hostname of your Home
     Assistant host. On HAOS, use the host's LAN address, not `127.0.0.1`,
     because Home Assistant Core runs in a separate container;
   - **RTSP port:** the port selected in the bridge configuration (`8554` by default);
   - **Stream name:** `wopet` unless changed in the bridge configuration;
   - **Camera name:** the friendly name to display in Home Assistant.

4. Open the Wopet Local integration's **Configure** dialog. You can correct
   the bridge host, RTSP port, or stream name there without removing the
   integration, and enter:

   - **Control port:** the bridge control port (`1986` by default);
   - **Control token:** the same private random value saved in the bridge app.

With the control token set, the integration creates:

- a **Feed** button and a **Feed portions** number (1-4). Each press sends one
  feed request and is never repeated automatically; a refusal because the bowl
  is full is reported as an error;
- for each of the six fixed meal slots: on/off, audio (with the clip file as an
  attribute), time, portions (1-10), grid size and name.

The schedule lives on the feeder. Home Assistant reads it every 6 hours and
after each change; each command first starts a camera session, so allow 5 to 10
seconds.

The setup flow checks that the RTSP port is reachable before creating the
camera entity. It never asks for or stores camera credentials.

## Dashboard

After setup, add the created `camera` entity, the Feed button and the meal
entities to the dashboard. Home Assistant consumes the RTSP URL through its stream
integration. If experimental listen audio is enabled in the bridge, unmute the
camera player to hear it.

## Removing the project

Remove the Wopet Local integration first, then uninstall the bridge app. HACS
and the Home Assistant App Store repository entries may be removed afterward.

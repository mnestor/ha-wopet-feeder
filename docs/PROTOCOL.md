# WOPET Camera Pet Feeder protocol notes

Decoded in November 2026 from captures of the WOPET iOS app and the feeder
(firmware 40.0.14.3), on hardware we own. Multi-byte integers are little-endian
unless stated. "Known" fields were confirmed by changing one setting in the app
and diffing; everything else is copied back unchanged.

All TUTK frames are obfuscated with ThroughTek's TransCodePartial. Decode with
`cuboai_pure.inv_transcode()` and encode with `cuboai_pure.transcode()`
(`wopet_bridge/vendor/cuboai_tutk/cuboai_pure.py`). Both are byte-exact against
the native library.

## 1. Local session (LAN)

The bridge uses the upstream `cuboai_tutk` client unchanged in its handshake:
an authenticated session to the feeder's UDP **32761** using the device UID,
viewer username (`admin` on our unit) and password from the WOPET cloud API.
There is no anonymous LAN discovery: generic PPPP/Kalay search packets get no
reply, which is why the feeder first looked "cloud-only".

Routed networks: the feeder answers the 32761 probe **from its main P2P
socket**, a random port chosen at boot, not from 32761. A stateful firewall
between Home Assistant and the feeder therefore drops the reply unless it allows
UDP from the feeder to the HA host from any source port. See
[Offline operation](OFFLINE.md#2-let-the-feeder-reach-the-bridge).

Once the stream runs, IO control messages ("IOCTLs") ride the same session: the
decoded frame carries the u16 IO type at +64 and the payload from +68 (length at
+52, minus 4). The app's own P2P session uses frame magic `04 02 1b 0a` (client)
and `04 02 19 0a` (device) instead of the LAN `04 02 1a 0a`; the layout is the
same.

## 2. IOCTLs

Replies use the request type + 1.

| Request | Purpose | Status |
| --- | --- | --- |
| `0x208c` → `0x208d` | Feeding plan get/set | Implemented |
| `0x207e` → `0x207f` | Manual feed | Implemented |
| `0x2072` (event) | Status after a feed | Implemented (`0x36` = refused, bowl full) |
| `0x2052` → `0x2053` | Set clock and timezone | Implemented |
| `0x03a0` → `0x03a1` | Read timezone (`"GTM -5:00"` (sic), -300 min) | Read once, not used |
| `0x2086` → `0x2087` | List voice clips | Decoded |
| `0x2088` → `0x2089` | Play a clip now (type 2) | Implemented |
| `0x208a` → `0x208b` | Start a clip upload | Implemented |
| `0x20c0` → `0x20c1` | Feed log (epoch window) | Decoded from an app capture |
| `0x20d3` → `0x20d4` | Feed alarms (epoch window, type 2) | Decoded from an app capture |
| `0x20c2` → `0x20c3` | Delete feed log entries | Decoded from an app capture |
| `0x2084` → `0x2085`, `0x20c4` → `0x20c5` | Device location and cloud server settings (the app reads them on connect) | Seen, not used |

### Feeding plan, `0x208c` / `0x208d`

- GET request: 8 zero bytes.
- SET request: `u32 1`, `u32 count`, then `count` x (`u32 slot` + 56 B record).
- Reply: `u32 result`, `u32 set_flag` (0 = reply to a GET, 1 = ack of a SET),
  `u32 count`, then `count` x (`u32 slot` + 56 B record).

The app shows exactly six fixed meal slots (no add or delete, no weekday
picker). The feeder sometimes also sends a 372 B `0x208d` whose count is 0, and
late duplicates of earlier replies arrive; match replies on `set_flag` and a
non-zero count, and confirm a write by reading back.

56-byte meal record:

| Offset | Meaning |
| --- | --- |
| +0..1 | `ff ff` marker |
| +2 | `01` in a request, `02` in a reply |
| +3 | `0a` (constant) |
| +4..5 | unknown, preserve (probably unused date fields) |
| +6 | `7f`, probably an every-day weekday mask (no UI) |
| +7 | hour |
| +8 | minute |
| +9 | **portions x 10** (2 portions = `0x14`) |
| +10 | `00`, preserve |
| +11 | bit 0 = **meal enabled** (`0x11` on, `0x10` off); bit 4 unknown, preserve |
| +12 | slot id |
| +13 | **voice clip**: `0`-`4` play `soundN.wav`, `0x0a` = silent |
| +14 | **food grid**: `1` small, `0` big |
| +15 | `01` marks the edited record in a SET; stored as `00` |
| +16..47 | NUL-terminated name (bytes after the NUL are uninitialised junk) |
| +48..55 | unknown, preserve |

### Manual feed, `0x207e` / `0x207f`

68-byte payload: `01 00 0e 00`, then a 16-byte record in the meal-record shape
stamped with the current local date and time, zero-padded:

```text
ff ff 01 0a  YY MM DD hh  mm PP 00 00  00 0a 00 00
```

`YY` = year - 1960, `PP` = portions x 10. The reply arrives only after the motor
has run (about 7 s). A `0x2072` event follows about 3 s later; a `u32` status at
+16 of its payload of `0x36` means the feed was refused because the bowl is full
(the app calls it "Dispense excessive food"). Never retransmit a feed: the
bridge sends it once and reports a missing reply as "may or may not have fed".

### Feed log, `0x20c0` / `0x20c1`, `0x20d3` / `0x20d4`, `0x20c2` / `0x20c3`

From a WOpet app capture of its history screen (2026-11-15). Times are Unix
epoch seconds (UTC); all integers are little-endian.

- `0x20c0` (16 B): `u32 0`, `u32 start`, `u32 end`, `u32 0`. The app asked for
  the last 11 days.
- `0x20c1`: `u32 0`, `u32 1`, `u8 0`, `u8 1`, `u16 count`, then `count` x 8-byte
  records: `u32 time`, `u8 value`, `u8 0`, `u8 code`, `u8 0`.

  | code | value | Meaning |
  | --- | --- | --- |
  | `0x32` | 20 | Dispensed |
  | `0x36` | 3 | Refused: bowl full ("Dispense excessive food") |

  Scheduled feeds appear whether they dispensed or not; a manual feed
  (`0x207e`) appears only if it was refused. A manual feed that dispensed is
  not logged: none of 103 dispensed records in 30 days was off the schedule,
  and a dispensed one-portion feed from Home Assistant (2026-11-15 18:38) never
  showed up. One 540-byte reply held 66
  records. The feeder answered a 30-day read (three 10-day windows, 181
  records) in about 2.5 s, so it keeps at least 30 days; whether it pages a
  window with more records than one reply holds is unknown. The reply does not
  echo the window, so a late reply to a resent request looks like the answer
  to the next one: the bridge accepts a reply only if all its records fall in
  the window it asked for, and resends after 4 s, not 1.5 s (a reply once took
  about 2 s, and early resends made it queue up and time out). The value
  byte's meaning beyond the two pairings above is unknown.
- `0x20d3` (20 B): `u32 0`, `u32 start`, `u32 end`, `u32 0`, `u32 2`, asked for
  one year. `0x20d4` has the same layout as `0x20c1` and held only the latest
  two refusals, so type 2 looks like the alarm list.
- `0x20c2`: `u32 1`, `u32 1`, `u32 count`, then `count` x (`u32 time`, `u8 0`,
  `u8 0`, `u8 code`, `u8 0`), zero-padded to 172 bytes. `0x20c3`: `u32 1`,
  `u32 1`, `u32 0`, `u32 count` (accepted). The app sent it for two 2022 records
  that only its cloud history had, so app history older than the feeder's log
  comes from the vendor cloud.

### Clock, `0x2052` / `0x2053`

```text
u32 unix_time_utc, i16 timezone_offset_minutes (-300 = UTC-5), 2 zero bytes
```

Reply: 8 zero bytes. The app sends this on every connect. Offline, the feeder
has no clock source until its first NTP query (about 11 minutes after boot) and
loses time across a power cut, so the bridge sends it at every camera session.
The feeder runs its schedule on this fixed offset: when the offset was stale
after a DST change, every meal ran one hour early.

### Voice clips, `0x2086` / `0x2087`, `0x208a`

- `0x2086`: 8 zero bytes. `0x2087` (528 B): `u32 0`, `u32 count` (5), then
  five 104-byte entries: file name[32] (`sound0.wav` to `sound4.wav`),
  label[32] (`invalid` = empty slot), 32 unused bytes, `u32 seconds`,
  `u32 unix_time`.
- Play now: `0x2088` = `u32 2` + the entry → `0x2089` (`u32 0` = played).
- Upload: `0x208a` = `u32 0` + the new entry, zero-padded to 272 bytes →
  `0x208b` (`u32 0` = accepted). Then the WAV goes over **TUTK RDT on IOTC
  channel 3**. Each frame has a 20-byte RDT header at decoded [28:48]:

  | Offset | Field |
  | --- | --- |
  | +0 | magic `5a 97 c2 f1` |
  | +4 | type: `01` ping, `41` pong, `02` data, `46` ack, `03` FIN, `43` FIN-ACK, `04` close |
  | +5 | `05` |
  | +6 | `u16` data length |
  | +8 | `u32` sequence (each direction counts its own data from 0; FIN uses `ffffffff`) |
  | +12 | `u32` 0 (`ffffffff` in FIN) |
  | +16 | `00` for ping/pong/close, `01` for data/ack/FIN |
  | +17 | connection id: the feeder's, from its ping (1 after boot); frames with another id are ignored |

  Exchange: host ping → feeder pong, feeder ping → host pong; feeder data
  `01 soundN.wav`; host data `02 <size in ASCII>`; feeder data `04 Start`; host
  data seq 1..N, the file in 1280-byte pieces, each acked; feeder data
  `07 stop` once stored; feeder FIN → host close + FIN-ACK → close both ways.
  Data messages are 128 bytes: a type byte, then NUL-padded ASCII.
- Format: WAV, 8 kHz, mono, 16-bit, 10 s maximum in the app (iOS adds an
  `FLLR` padding chunk; the feeder accepts plain files too).
- Saving a clip in the app also set the meal record's +13 to that clip.

## 3. P2P server messages (UDP 10240 and 10001)

What the feeder exchanges with ThroughTek's servers, and what the bridge's
responder now answers locally. Command = `u16` at +8 of the decoded frame. Every
reply carries the sender's address as the server saw it, as a 16-byte
`sockaddr_in` (`u16` family 2 little-endian, `u16` port big-endian, IPv4, 8 zero
bytes).

| Message | Size | Content |
| --- | --- | --- |
| Master query `0x1007` → UDP 10240 | 54 B | Header `04 02 19 02 26 00 00 00 07 10 18 ...`, UID at +16 |
| Master referral `0x1008` | 126 B | Header `04 02 1d 00 6e 00 00 00 08 10 83 ...`, UID at +16, sender address at +52, then entries: `05 00 03 00`; type 2, length 32: two region-server `sockaddr_in` (UDP 10001); type 3, length 4: `30 00 00 00`; type 1, length 6: `89 a1 01 00 11 00` |
| Region probe `0x8003` → UDP 10001 | 24 B | Index (u32) at +16, probe id at +20 |
| Probe reply `0x8004` | 40 B | Header `04 02 1b 00 18 00 00 00 04 80 4f ...`, sender address at +16, index and id echoed at +32 |
| Region login `0x0105` | 288 B | UID at +16, the feeder's own LAN address at +36, counters near +236; repeated about every 40 s |
| Login reply `0x0106` | 288 B | The request echoed with bytes 2-3 = `1b 00`, command `0x0106`, +10 = `0x41`, sender address at +36 |
| Keepalive | 4 B (14 B before login) | Every 5 s; the real servers never answer |

The feeder queries several masters at once on UDP 10240 at boot; the meaning of
the type 1 and type 3 entries is unknown and they are replayed as captured. The
responder's replies match all 41 captured server replies byte for byte
(`tests/test_p2p_responder.py` uses the same layouts with test values).

## 4. Other traffic seen (all blocked offline)

| Destination | What |
| --- | --- |
| `www.baidu.com:80` | Connectivity check: DNS lookups about every 25 s and a TCP connect |
| `dogsndsoftchk.jkgeek.com:80` | Firmware check `GET /Srt_Server/server.php?cmd=ckd&mcode=<b64 MAC>&ucode=<b64 UID>&ccode=public&lcode=normal`; the server answers `{"cmd":4003,"code":20000,"msg":"<random 40 hex>"}` |
| `s0.wo-pet.com:8088` (HTTP) | Vendor API: device list with UID and viewer credentials (plain HTTP), a report after each scheduled feed |
| `wo-pet.com` TCP 1883 | MQTT, opened after boot |
| NTP (UDP 123) | Public NIST servers; first query about 11 minutes after boot |
| TCP 23 | Telnet `ipc login:` is open on the LAN; the root password is not a known default |

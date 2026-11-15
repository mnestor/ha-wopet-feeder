"""WOPET Camera Pet Feeder (fw 40.0.14.3) schedule and feed IOCTLs.

Decoded from app captures on 2026-11-15. Only the fields below are known; every
other byte is sent back exactly as the feeder reported it.

Feed plan, 0x208c -> 0x208d
  GET request : 8 zero bytes
  SET request : u32 1, u32 count, then count x (u32 slot + 56 B record)
  reply       : u32 result, u32 set_flag, u32 count, then count x (u32 slot + 56 B record)

56 B record
  +0  ff ff            marker
  +2  01 request / 02 reply
  +7  hour             +8 minute
  +9  portions x 10
  +11 bit 0 = enabled  (0x11 on, 0x10 off; other bits preserved)
  +12 slot id
  +13 meal sound       0-4 = play clip soundN.wav, 0x0a = no sound
  +14 grid size        1 small, 0 big
  +15 01 marks the edited record in a SET (feeder stores 00)
  +16..47 NUL-terminated name

Manual feed, 0x207e -> 0x207f (reply arrives after the motor runs, ~7 s)
  01 00 0e 00 + 16 B record stamped with the current date/time, +9 = portions x 10
  A later 0x2072 event carries a status code at +16; 0x36 = refused, bowl full.

Set clock, 0x2052 -> 0x2053 (the app sends it on every connect; the feeder has
no working NTP while offline and loses time across a power cut)
  u32 Unix time (UTC), i16 timezone offset in minutes (-300 = GMT-5), 2 zero bytes
  reply: 8 zero bytes

Sound clips (decoded 2026-11-15): 0x2086 (8 zero bytes) -> 0x2087 lists five
slots sound0..sound4.wav; an upload is 0x208a -> 0x208b followed by the WAV
(8 kHz mono 16-bit, app limit 10 s) over a separate TUTK RDT channel. Upload is
not implemented here.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

PLAN_REQ = 0x208C
PLAN_RESP = 0x208D
FEED_REQ = 0x207E
FEED_RESP = 0x207F
EVENT = 0x2072
TIME_REQ = 0x2052
TIME_RESP = 0x2053

RECORD_LEN = 56
ENTRY_LEN = 4 + RECORD_LEN
NAME_OFF = 16
NAME_LEN = 32  # +16..47, NUL-terminated, so at most 31 bytes of text

AUDIO_OFF = 0x0A
SOUND_SLOTS = 5  # sound0..sound4.wav
DEFAULT_SOUND = 0
GRID_SMALL = 1
GRID_BIG = 0

MAX_MEAL_PORTIONS = 10
MAX_FEED_PORTIONS = 4

EVENT_CODES = {0x36: "refused: bowl full (dispense excessive food)"}


class ProtocolError(ValueError):
    """The feeder sent something that does not match the decoded layout."""


@dataclass
class Meal:
    slot: int
    record: bytearray

    @property
    def hour(self) -> int:
        return self.record[7]

    @property
    def minute(self) -> int:
        return self.record[8]

    @property
    def portions(self) -> float:
        raw = self.record[9]
        return raw // 10 if raw % 10 == 0 else raw / 10

    @property
    def enabled(self) -> bool:
        return bool(self.record[11] & 0x01)

    @property
    def audio(self) -> bool:
        return self.record[13] != AUDIO_OFF

    @property
    def sound(self) -> int | None:
        """Index of the clip played with this meal (soundN.wav), None when silent."""
        return None if self.record[13] == AUDIO_OFF else self.record[13]

    @property
    def grid(self) -> str:
        return "small" if self.record[14] == GRID_SMALL else "big"

    @property
    def name(self) -> str:
        raw = bytes(self.record[NAME_OFF : NAME_OFF + NAME_LEN]).split(b"\0", 1)[0]
        return raw.decode("utf-8", "replace")

    def as_dict(self) -> dict[str, object]:
        return {
            "slot": self.slot,
            "name": self.name,
            "enabled": self.enabled,
            "hour": self.hour,
            "minute": self.minute,
            "portions": self.portions,
            "grid": self.grid,
            "audio": self.audio,
            "sound": self.sound,
            "raw": bytes(self.record).hex(),
        }


def build_plan_get() -> bytes:
    return b"\0" * 8


def plan_count(body: bytes) -> int | None:
    if len(body) < 12:
        return None
    return struct.unpack_from("<I", body, 8)[0]


def plan_set_flag(body: bytes) -> int | None:
    """0 for a GET reply, 1 for a SET acknowledgement."""
    if len(body) < 8:
        return None
    return struct.unpack_from("<I", body, 4)[0]


def parse_plan(body: bytes) -> list[Meal]:
    """Parse a 0x208d reply into meals, keeping each 56 B record verbatim."""
    if len(body) < 12:
        raise ProtocolError(f"plan reply too short ({len(body)} B)")
    result, _set_flag, count = struct.unpack_from("<III", body, 0)
    head = body[:16].hex(" ")
    if result != 0:
        raise ProtocolError(f"feeder returned plan result {result} (header {head})")
    if not 0 < count <= 16 or len(body) < 12 + count * ENTRY_LEN:
        raise ProtocolError(f"plan reply has count {count} but {len(body)} B (header {head})")
    meals = []
    for i in range(count):
        off = 12 + i * ENTRY_LEN
        slot = struct.unpack_from("<I", body, off)[0]
        rec = bytearray(body[off + 4 : off + 4 + RECORD_LEN])
        if rec[0:2] != b"\xff\xff":
            raise ProtocolError(f"plan record {i} has no ff ff marker")
        meals.append(Meal(slot, rec))
    return meals


def apply_changes(meal: Meal, changes: dict[str, object]) -> None:
    """Change only the decoded fields of one meal, validating every value."""
    rec = meal.record
    for key, value in changes.items():
        if key == "enabled":
            if not isinstance(value, bool):
                raise ValueError("enabled must be true or false")
            rec[11] = (rec[11] | 0x01) if value else (rec[11] & ~0x01 & 0xFF)
        elif key == "audio":
            if not isinstance(value, bool):
                raise ValueError("audio must be true or false")
            if not value:
                rec[13] = AUDIO_OFF
            elif rec[13] == AUDIO_OFF:
                rec[13] = DEFAULT_SOUND
        elif key == "sound":
            is_int = isinstance(value, int) and not isinstance(value, bool)
            if not is_int or not 0 <= value < SOUND_SLOTS:
                raise ValueError(f"sound must be 0-{SOUND_SLOTS - 1}")
            rec[13] = value
        elif key == "grid":
            if value not in ("small", "big"):
                raise ValueError("grid must be 'small' or 'big'")
            rec[14] = GRID_SMALL if value == "small" else GRID_BIG
        elif key == "portions":
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError("portions must be a whole number")
            if not 1 <= value <= MAX_MEAL_PORTIONS:
                raise ValueError(f"portions must be 1-{MAX_MEAL_PORTIONS}")
            rec[9] = value * 10
        elif key == "hour":
            if not isinstance(value, int) or not 0 <= value <= 23:
                raise ValueError("hour must be 0-23")
            rec[7] = value
        elif key == "minute":
            if not isinstance(value, int) or not 0 <= value <= 59:
                raise ValueError("minute must be 0-59")
            rec[8] = value
        elif key == "name":
            if not isinstance(value, str) or not value.strip():
                raise ValueError("name must be a non-empty string")
            raw = value.strip().encode("utf-8")
            if len(raw) > NAME_LEN - 1:
                raise ValueError(f"name must be at most {NAME_LEN - 1} bytes")
            rec[NAME_OFF : NAME_OFF + NAME_LEN] = raw.ljust(NAME_LEN, b"\0")
        else:
            raise ValueError(f"unknown meal field: {key}")


def build_plan_set(meals: list[Meal], edited_slot: int | None) -> bytes:
    """Build a 0x208c SET carrying every meal, marked as a request."""
    out = bytearray(struct.pack("<II", 1, len(meals)))
    for meal in meals:
        rec = bytearray(meal.record)
        rec[2] = 0x01
        rec[15] = 0x01 if meal.slot == edited_slot else 0x00
        out += struct.pack("<I", meal.slot) + rec
    return bytes(out)


KNOWN_OFFSETS = (7, 8, 9, 11, 13, 14)


def plans_match(expected: list[Meal], actual: list[Meal]) -> bool:
    """Compare the decoded fields and names of two plans."""
    if [m.slot for m in expected] != [m.slot for m in actual]:
        return False
    for e, a in zip(expected, actual):
        if any(e.record[o] != a.record[o] for o in KNOWN_OFFSETS):
            return False
        if e.name != a.name:
            return False
    return True


def build_feed(portions: int, now: time.struct_time | None = None) -> bytes:
    """Build a 0x207e manual feed in the same shape the app sends."""
    if not isinstance(portions, int) or isinstance(portions, bool):
        raise ValueError("portions must be a whole number")
    if not 1 <= portions <= MAX_FEED_PORTIONS:
        raise ValueError(f"portions must be 1-{MAX_FEED_PORTIONS}")
    t = now or time.localtime()
    rec = bytes(
        [
            0xFF, 0xFF, 0x01, 0x0A,
            (t.tm_year - 1960) & 0xFF, t.tm_mon, t.tm_mday,
            t.tm_hour, t.tm_min,
            portions * 10,
            0x00, 0x00, 0x00,
            0x0A,
            0x00, 0x00,
        ]
    )
    return (b"\x01\x00\x0e\x00" + rec).ljust(68, b"\0")


def build_set_time(epoch: int, tz_minutes: int) -> bytes:
    """Build a 0x2052 clock set exactly like the app's."""
    if not -14 * 60 <= tz_minutes <= 14 * 60:
        raise ValueError("timezone offset out of range")
    return struct.pack("<Ih2x", int(epoch), int(tz_minutes))


def parse_feed_reply(body: bytes) -> int:
    if len(body) < 4:
        raise ProtocolError("feed reply too short")
    return struct.unpack_from("<I", body, 0)[0]


def parse_event(payload: bytes) -> int | None:
    """Return the status code of a 0x2072 event payload, if present."""
    if len(payload) < 20:
        return None
    return struct.unpack_from("<I", payload, 16)[0]

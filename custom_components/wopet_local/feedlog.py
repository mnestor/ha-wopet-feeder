"""The feeder's own feed log: merge polls and summarise it (no Home Assistant imports).

Each record is {"time": Unix seconds, "result": "dispensed" | "bowl_full" | "unknown",
"code": int, "value": int}. A dispensed record's value is portions x 10; a bowl_full
record means the feeder skipped a scheduled or manual feed because the bowl was full.

The feeder logs scheduled feeds and every refusal, but not a manual feed that
dispensed, so Home Assistant adds those itself (manual_feed), with "source": "manual".
"""

from __future__ import annotations

from typing import Any

DISPENSED = "dispensed"
BOWL_FULL = "bowl_full"
RESULTS = (DISPENSED, BOWL_FULL, "unknown")
KEEP_DAYS = 30

Record = dict[str, Any]


def merge(
    old: list[Record], new: list[Record], now: float, keep_days: int = KEEP_DAYS
) -> list[Record]:
    """Union of two reads, oldest first, without duplicates or records older than keep_days."""
    cutoff = now - keep_days * 86400
    merged = {(int(r["time"]), int(r["code"])): r for r in (*old, *new)}
    return sorted((r for r in merged.values() if r["time"] >= cutoff), key=lambda r: r["time"])


def last(records: list[Record], result: str | None = None) -> Record | None:
    for record in reversed(records):
        if result is None or record["result"] == result:
            return record
    return None


def refused_streak(records: list[Record]) -> list[Record]:
    """The bowl_full records since the last successful feed (empty if the latest fed)."""
    streak: list[Record] = []
    for record in reversed(records):
        if record["result"] != BOWL_FULL:
            break
        streak.append(record)
    return streak[::-1]


def portions(record: Record) -> float | None:
    if record["result"] != DISPENSED:
        return None
    value = int(record["value"])
    return value // 10 if value % 10 == 0 else value / 10


def manual_feed(stamp: int, portions: int) -> Record:
    """A dispensed manual feed, which the feeder's own log leaves out."""
    return {"time": stamp, "result": DISPENSED, "code": 0x32, "value": portions * 10,
            "source": "manual"}


def newer_than(records: list[Record], marker: int) -> list[Record]:
    return [r for r in records if r["time"] > marker]

"""When the feeder last dispensed food, from its own feed log."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import feedlog
from .entity import WopetFeedLogEntity

RECENT = 10


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    feed_log = entry.runtime_data.feed_log
    if feed_log is not None:
        async_add_entities([WopetLastFedSensor(entry, feed_log)])


def _iso(stamp: int) -> str:
    return dt_util.as_local(dt_util.utc_from_timestamp(stamp)).isoformat()


class WopetLastFedSensor(WopetFeedLogEntity, SensorEntity):
    """Time of the last feed the feeder actually dispensed (scheduled or manual)."""

    key = "last_fed"
    _attr_name = "Last fed"
    _attr_icon = "mdi:bowl-mix"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _unrecorded_attributes = frozenset({"recent"})

    @property
    def native_value(self) -> datetime | None:
        record = feedlog.last(self.records, feedlog.DISPENSED)
        return None if record is None else dt_util.utc_from_timestamp(record["time"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        fed = feedlog.last(self.records, feedlog.DISPENSED)
        attempt = feedlog.last(self.records)
        return {
            "portions": None if fed is None else feedlog.portions(fed),
            "last_attempt": None if attempt is None else _iso(attempt["time"]),
            "last_attempt_result": None if attempt is None else attempt["result"],
            "recent": [
                {"time": _iso(r["time"]), "result": r["result"], "portions": feedlog.portions(r)}
                for r in reversed(self.records[-RECENT:])
            ],
        }

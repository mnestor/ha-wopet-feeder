"""Whether the feeder's last feed was skipped because the bowl was still full."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import feedlog
from .entity import WopetFeedLogEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    feed_log = entry.runtime_data.feed_log
    if feed_log is not None:
        async_add_entities([WopetBowlFullSensor(entry, feed_log)])


class WopetBowlFullSensor(WopetFeedLogEntity, BinarySensorEntity):
    """On while the feeder's latest feed attempt was refused as "bowl full".

    It turns off at the next feed the feeder dispenses. A bowl that stays full across
    several meals usually means the cat isn't eating, or kibble is jammed in the chute.
    """

    key = "bowl_full"
    _attr_name = "Bowl full"
    _attr_translation_key = "bowl_full"

    @property
    def is_on(self) -> bool | None:
        latest = feedlog.last(self.records)
        return None if latest is None else latest["result"] == feedlog.BOWL_FULL

    @property
    def icon(self) -> str:
        return "mdi:bowl" if self.is_on else "mdi:bowl-outline"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        streak = feedlog.refused_streak(self.records)
        since = (
            dt_util.as_local(dt_util.utc_from_timestamp(streak[0]["time"])).isoformat()
            if streak else None
        )
        return {"skipped_feeds": len(streak), "since": since}

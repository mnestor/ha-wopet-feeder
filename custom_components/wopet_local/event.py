"""One event per feed the feeder logs: dispensed, or skipped because the bowl was full."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import feedlog
from .entity import WopetFeedLogEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    feed_log = entry.runtime_data.feed_log
    if feed_log is not None:
        async_add_entities([WopetFeedEvent(entry, feed_log)])


class WopetFeedEvent(WopetFeedLogEntity, EventEntity):
    """Fires for records that appear after Home Assistant started; history is not replayed.

    The feed log is polled, so an event can arrive up to one poll after the feed itself;
    the feed_time attribute is when the feeder logged it.
    """

    key = "feed_event"
    _attr_name = "Feed"
    _attr_icon = "mdi:food-variant"
    _attr_translation_key = "feed"
    _attr_event_types = list(feedlog.RESULTS)

    def __init__(self, entry: ConfigEntry, coordinator) -> None:
        super().__init__(entry, coordinator)
        self._marker: int | None = None

    def _sync_marker(self) -> None:
        if self._marker is None and self.coordinator.data is not None:
            latest = feedlog.last(self.records)
            self._marker = 0 if latest is None else int(latest["time"])

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._sync_marker()

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._marker is None:
            self._sync_marker()
            self.async_write_ha_state()
            return
        new = feedlog.newer_than(self.records, self._marker)
        for record in new:
            # One state write per record, so each shows up as its own event.
            self._trigger_event(record["result"], {
                "feed_time": dt_util.as_local(
                    dt_util.utc_from_timestamp(record["time"])).isoformat(),
                "portions": feedlog.portions(record),
                "source": record.get("source", "feeder"),
                "code": record["code"],
            })
            self._marker = int(record["time"])
            self.async_write_ha_state()
        if not new:
            self.async_write_ha_state()

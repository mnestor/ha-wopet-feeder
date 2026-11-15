"""Feed button for Wopet Local."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Feed button."""
    async_add_entities([WopetFeedButton(entry)])


class WopetFeedButton(ButtonEntity):
    """Dispense the 'Feed portions' amount once. Never retried automatically."""

    _attr_has_entity_name = True
    _attr_name = "Feed"
    _attr_icon = "mdi:food-variant"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        # Same unique id as the upstream "dispense_treat" button, so the existing
        # button.cat_feeder_feed entity is kept.
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_dispense_treat"
        self._attr_device_info = device_info(entry)
        self._attr_available = entry.runtime_data.client is not None

    async def async_press(self) -> None:
        runtime = self._entry.runtime_data
        await runtime.client.async_feed(int(runtime.feed_portions))

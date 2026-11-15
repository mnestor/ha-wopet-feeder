"""Meal food-grid size."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import SLOTS, WopetMealEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    if coordinator is not None:
        async_add_entities(MealGridSelect(entry, coordinator, s) for s in SLOTS)


class MealGridSelect(WopetMealEntity, SelectEntity):
    field_key = "grid"
    label = "grid"
    _attr_icon = "mdi:grid"
    _attr_options = ["small", "big"]

    @property
    def current_option(self) -> str | None:
        return None if self.meal is None else self.meal["grid"]

    async def async_select_option(self, option: str) -> None:
        await self._set(grid=option)

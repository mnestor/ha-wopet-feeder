"""Meal name."""

from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import SLOTS, WopetMealEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    if coordinator is not None:
        async_add_entities(MealNameText(entry, coordinator, s) for s in SLOTS)


class MealNameText(WopetMealEntity, TextEntity):
    field_key = "name"
    label = "name"
    _attr_icon = "mdi:label-outline"
    _attr_native_min = 1
    _attr_native_max = 31

    @property
    def native_value(self) -> str | None:
        return None if self.meal is None else self.meal["name"]

    async def async_set_value(self, value: str) -> None:
        if len(value.strip().encode("utf-8")) > 31:
            raise HomeAssistantError("Meal names are limited to 31 bytes")
        await self._set(name=value.strip())

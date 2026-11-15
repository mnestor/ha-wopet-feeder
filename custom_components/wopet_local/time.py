"""Meal time."""

from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import SLOTS, WopetMealEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    if coordinator is not None:
        async_add_entities(MealTime(entry, coordinator, s) for s in SLOTS)


class MealTime(WopetMealEntity, TimeEntity):
    field_key = "time"
    label = "time"
    _attr_icon = "mdi:clock-outline"

    @property
    def native_value(self) -> time | None:
        meal = self.meal
        return None if meal is None else time(meal["hour"], meal["minute"])

    async def async_set_value(self, value: time) -> None:
        await self._set(hour=value.hour, minute=value.minute)

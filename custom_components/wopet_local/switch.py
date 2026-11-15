"""Meal on/off switches.

A meal's sound is set with its voice-clip select (Silent or a clip); the feeder keeps one
byte for both, so a separate audio switch would only duplicate it.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import SLOTS, WopetMealEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    if coordinator is None:
        return
    async_add_entities(MealEnabledSwitch(entry, coordinator, slot) for slot in SLOTS)


class MealEnabledSwitch(WopetMealEntity, SwitchEntity):
    field_key = "enabled"
    label = ""
    _attr_icon = "mdi:food-drumstick"

    @property
    def is_on(self) -> bool | None:
        return None if self.meal is None else bool(self.meal["enabled"])

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(enabled=False)


"""Meal on/off and meal audio switches."""

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
    entities: list[SwitchEntity] = []
    for slot in SLOTS:
        entities.append(MealEnabledSwitch(entry, coordinator, slot))
        entities.append(MealAudioSwitch(entry, coordinator, slot))
    async_add_entities(entities)


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


class MealAudioSwitch(WopetMealEntity, SwitchEntity):
    field_key = "audio"
    label = "audio"
    _attr_icon = "mdi:volume-high"

    @property
    def is_on(self) -> bool | None:
        return None if self.meal is None else bool(self.meal["audio"])

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs = super().extra_state_attributes
        if attrs is None:
            return None
        sound = self.meal.get("sound")
        # The feeder stores a clip index per meal (sound0..sound4.wav); "on" from HA
        # keeps the current clip, or picks sound0 if the meal was silent.
        return {**attrs, "sound_file": None if sound is None else f"sound{sound}.wav"}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(audio=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(audio=False)

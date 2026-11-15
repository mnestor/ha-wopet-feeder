"""Meal food-grid size and voice clip."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import SLOTS, WopetMealEntity

CLIP_SLOTS = range(5)  # the feeder's sound0..sound4.wav
SILENT = "Silent"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    if coordinator is not None:
        entities: list[SelectEntity] = []
        for slot in SLOTS:
            entities.append(MealGridSelect(entry, coordinator, slot))
            entities.append(MealClipSelect(entry, coordinator, slot))
        async_add_entities(entities)


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


def clip_option(index: int, clips: list[dict]) -> str:
    """'Clip 1: Dinner bell', or 'Clip 1' when the feeder has no label for it."""
    clip = next((c for c in clips if c.get("clip") == index), None)
    label = None if clip is None or clip.get("empty") else clip.get("label")
    return f"Clip {index + 1}: {label}" if label else f"Clip {index + 1}"


class MealClipSelect(WopetMealEntity, SelectEntity):
    """Which of the feeder's five voice clips this meal plays, or none."""

    field_key = "clip"
    label = "clip"
    _attr_icon = "mdi:account-voice"

    @property
    def options(self) -> list[str]:
        clips = self.coordinator.clips
        return [SILENT, *(clip_option(i, clips) for i in CLIP_SLOTS)]

    @property
    def current_option(self) -> str | None:
        if self.meal is None:
            return None
        if not self.meal["audio"] or self.meal.get("sound") is None:
            return SILENT
        return clip_option(int(self.meal["sound"]), self.coordinator.clips)

    async def async_select_option(self, option: str) -> None:
        if option == SILENT:
            await self._set(audio=False)
            return
        index = self.options.index(option) - 1
        await self._set(sound=index)

"""Meal portions and the manual-feed portion count."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import SLOTS, WopetMealEntity, device_info

MAX_MEAL_PORTIONS = 10
MAX_FEED_PORTIONS = 4


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    runtime = entry.runtime_data
    entities: list[NumberEntity] = []
    if runtime.client is not None:
        entities.append(FeedPortionsNumber(entry))
    if runtime.coordinator is not None:
        entities.extend(MealPortionsNumber(entry, runtime.coordinator, s) for s in SLOTS)
    async_add_entities(entities)


class MealPortionsNumber(WopetMealEntity, NumberEntity):
    field_key = "portions"
    label = "portions"
    _attr_icon = "mdi:bowl"
    _attr_native_min_value = 1
    _attr_native_max_value = MAX_MEAL_PORTIONS
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float | None:
        if self.meal is None:
            return None
        value = self.meal["portions"]
        return int(value) if float(value).is_integer() else float(value)

    async def async_set_native_value(self, value: float) -> None:
        await self._set(portions=int(value))


class FeedPortionsNumber(RestoreNumber):
    """How many portions the Feed button dispenses (kept in HA, not on the feeder)."""

    _attr_has_entity_name = True
    _attr_name = "Feed portions"
    _attr_icon = "mdi:numeric"
    _attr_native_min_value = 1
    _attr_native_max_value = MAX_FEED_PORTIONS
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_feed_portions"
        self._attr_device_info = device_info(entry)
        self._attr_native_value = 1

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._attr_native_value = int(last.native_value)
        self._entry.runtime_data.feed_portions = int(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = int(value)
        self._entry.runtime_data.feed_portions = int(value)
        self.async_write_ha_state()

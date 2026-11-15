"""Shared entity helpers for Wopet Local."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WopetPlanCoordinator

SLOTS = range(1, 7)  # the feeder has exactly six fixed meal slots


def device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        name=entry.title,
        manufacturer="Wopet",
        model="Camera Pet Feeder",
    )


class WopetMealEntity(CoordinatorEntity[WopetPlanCoordinator]):
    """One field of one schedule slot."""

    _attr_has_entity_name = True
    field_key: str = ""
    label: str = ""

    def __init__(self, entry: ConfigEntry, coordinator: WopetPlanCoordinator, slot: int) -> None:
        super().__init__(coordinator)
        self.slot = slot
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_meal{slot}_{self.field_key}"
        self._attr_name = f"Meal {slot} {self.label}".strip()
        self._attr_device_info = device_info(entry)

    @property
    def meal(self) -> dict[str, Any] | None:
        data = self.coordinator.data or {}
        return data.get(self.slot)

    @property
    def available(self) -> bool:
        return super().available and self.meal is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        meal = self.meal
        if meal is None:
            return None
        return {
            "meal_name": meal["name"],
            "meal_time": f"{meal['hour']:02d}:{meal['minute']:02d}",
            "slot": self.slot,
        }

    async def _set(self, **changes: Any) -> None:
        try:
            await self.coordinator.async_set_meal(self.slot, **changes)
        except HomeAssistantError:
            await self.coordinator.async_request_refresh()
            raise

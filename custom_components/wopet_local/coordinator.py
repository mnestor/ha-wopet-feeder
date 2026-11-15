"""Feeding-schedule coordinator for Wopet Local."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .control import WopetControlClient

_LOGGER = logging.getLogger(__name__)

# Only Home Assistant edits the schedule once the feeder is off the cloud, and each
# read briefly starts the camera session, so poll rarely.
SCAN_INTERVAL = timedelta(hours=6)
RETRY_INTERVAL = timedelta(minutes=5)


class WopetPlanCoordinator(DataUpdateCoordinator[dict[int, dict[str, Any]]]):
    """Hold the six schedule slots, keyed by slot number."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: WopetControlClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} schedule",
            update_interval=SCAN_INTERVAL,
        )
        self.client = client
        self.clips: list[dict[str, Any]] = []

    @staticmethod
    def _index(meals: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        return {int(m["slot"]): m for m in meals}

    async def _async_update_data(self) -> dict[int, dict[str, Any]]:
        try:
            data = self._index(await self.client.async_get_plan())
        except HomeAssistantError as exc:
            # Retry soon (e.g. the bridge app was restarting) instead of in 6 hours.
            self.update_interval = RETRY_INTERVAL
            raise UpdateFailed(str(exc)) from exc
        self.update_interval = SCAN_INTERVAL
        try:
            self.clips = await self.client.async_get_clips()
        except HomeAssistantError as exc:
            # Older bridges have no clip list; the schedule still works.
            _LOGGER.debug("Wopet clip list unavailable: %s", exc)
        return data

    async def async_set_clips(self, clips: list[dict[str, Any]]) -> None:
        self.clips = clips
        self.async_update_listeners()

    async def async_set_meal(self, slot: int, **changes: Any) -> None:
        meals = await self.client.async_set_meal(slot, **changes)
        self.update_interval = SCAN_INTERVAL
        self.async_set_updated_data(self._index(meals))


@dataclass
class WopetRuntimeData:
    client: WopetControlClient | None
    coordinator: WopetPlanCoordinator | None
    feed_portions: int = 1
    extra: dict[str, Any] = field(default_factory=dict)

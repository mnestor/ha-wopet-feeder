"""Wopet Local integration (WOPET camera feeder: video, feed, schedule)."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CONTROL_PORT,
    CONF_CONTROL_TOKEN,
    CONF_STREAM_NAME,
    DEFAULT_CONTROL_PORT,
)
from .control import WopetControlClient
from .coordinator import WopetFeedLogCoordinator, WopetPlanCoordinator, WopetRuntimeData
from .recorder import async_setup_recorder
from .services import async_setup_services

PLATFORMS = (
    Platform.BINARY_SENSOR,
    Platform.CAMERA,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
    Platform.TIME,
)


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration after its control options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Wopet Local from a config entry."""
    token = str(entry.options.get(CONF_CONTROL_TOKEN, ""))
    client = coordinator = feed_log = None
    if token:
        client = WopetControlClient(
            hass,
            entry.options.get(CONF_HOST, entry.data[CONF_HOST]),
            int(entry.options.get(CONF_CONTROL_PORT, DEFAULT_CONTROL_PORT)),
            token,
            entry.options.get(CONF_STREAM_NAME, entry.data[CONF_STREAM_NAME]),
        )
        coordinator = WopetPlanCoordinator(hass, entry, client)
        # Do not block setup (or the camera) on the feeder answering.
        await coordinator.async_refresh()
        feed_log = WopetFeedLogCoordinator(hass, entry, client)
        await feed_log.async_refresh()
    entry.runtime_data = WopetRuntimeData(
        client=client, coordinator=coordinator, feed_log=feed_log
    )
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    await async_setup_services(hass)
    await async_setup_recorder(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Wopet Local config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

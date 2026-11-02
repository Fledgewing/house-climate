"""Coordinate several systems as one house thermostat."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import HouseCoordinator

    type HouseClimateEntry = ConfigEntry[HouseCoordinator]

from .const import PLATFORMS


async def async_setup_entry(hass: HomeAssistant, entry: HouseClimateEntry) -> bool:
    from .coordinator import HouseCoordinator

    coordinator = entry.runtime_data = HouseCoordinator(hass, entry)
    await coordinator.async_load()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await coordinator.async_tick()
    entry.async_on_unload(coordinator.start())
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_reload_entry(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

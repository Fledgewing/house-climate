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
    from .priority import modes

    if "priority_modes" not in entry.options:
        settings = dict(entry.options or entry.data)
        settings["priority_modes"] = modes(settings)
        hass.config_entries.async_update_entry(entry, options=settings)
    coordinator = entry.runtime_data = HouseCoordinator(hass, entry)
    await coordinator.async_load()
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers import issue_registry as ir

    registry = er.async_get(hass)
    sensors = {s["entity_id"] for s in coordinator.config["sensors"]}
    for entity in list(er.async_entries_for_config_entry(registry, entry.entry_id)):
        for suffix in ("_accept", "_relearn", "_temporary"):
            prefix = f"{entry.entry_id}_"
            if entity.unique_id.startswith(prefix) and entity.unique_id.endswith(
                suffix
            ):
                sensor = entity.unique_id[len(prefix) : -len(suffix)]
                if sensor not in sensors:
                    registry.async_remove(entity.entity_id)
    issues = ir.async_get(hass)
    for domain, issue_id in list(issues.issues):
        prefix = f"{entry.entry_id}_"
        if (
            domain == "house_climate"
            and issue_id.startswith(prefix)
            and issue_id.endswith("_regime")
        ):
            if issue_id[len(prefix) : -len("_regime")] not in sensors:
                ir.async_delete_issue(hass, domain, issue_id)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def begin(_event=None):
        entry.async_on_unload(coordinator.start())
        await coordinator.async_tick()

    from homeassistant.core import CoreState

    if hass.state is CoreState.running:
        await begin()
    else:
        from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, begin)
        )
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_reload_entry(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass, entry):
    """Migrate room mappings, priority modes and the separate average weight."""
    if entry.version > 4:
        return False
    if entry.version == 1:

        def clean(settings):
            result = dict(settings)
            if "systems" in result:
                result["systems"] = [
                    {k: v for k, v in system.items() if k != "influence"}
                    for system in result["systems"]
                ]
            result.pop("influence", None)
            return result

        hass.config_entries.async_update_entry(
            entry, data=clean(entry.data), options=clean(entry.options), version=2
        )
    if entry.version < 3:
        from .priority import modes

        settings = dict(entry.options or entry.data)
        settings["priority_modes"] = modes(settings)
        hass.config_entries.async_update_entry(entry, options=settings, version=3)
    if entry.version < 4:
        settings = dict(entry.options or entry.data)
        settings.setdefault("average_weight", 1.0)
        hass.config_entries.async_update_entry(entry, options=settings, version=4)
    return True

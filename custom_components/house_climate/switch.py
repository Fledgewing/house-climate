"""Observe-only control toggle."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([HouseSwitch(entry.runtime_data, entry.entry_id)])


class HouseSwitch(CoordinatorEntity, SwitchEntity):
    _attr_name = "House climate control"
    _attr_icon = "mdi:thermostat"

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_control"

    @property
    def is_on(self):
        return self.coordinator.enabled

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_change(enabled=True)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_change(enabled=False)

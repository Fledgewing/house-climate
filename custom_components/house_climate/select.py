"""Room priority selection for manual overrides and HA automations."""

from homeassistant.components.select import SelectEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .priority import modes


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([PrioritySelect(entry.runtime_data, entry.entry_id)])


class PrioritySelect(CoordinatorEntity, SelectEntity):
    _attr_name = "House priority mode"
    _attr_icon = "mdi:scale-balance"

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_priority_mode"

    @property
    def options(self):
        return list(modes(self.coordinator.config))

    @property
    def current_option(self):
        return self.coordinator.priority_mode

    async def async_select_option(self, option):
        await self.coordinator.async_change(priority_mode=option)

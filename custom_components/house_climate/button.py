"""Warmer and colder house controls."""

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        [
            *[
                HouseButton(entry.runtime_data, entry.entry_id, delta)
                for delta in (0.5, -0.5)
            ],
            ResetLearningButton(entry.runtime_data, entry.entry_id),
        ]
    )


class HouseButton(CoordinatorEntity, ButtonEntity):
    def __init__(self, coordinator, entry_id, delta):
        super().__init__(coordinator)
        self.delta = delta
        action = "warmer" if delta > 0 else "colder"
        self._attr_name = f"House {action}"
        self._attr_unique_id = f"{entry_id}_{action}"
        self._attr_icon = (
            "mdi:thermometer-plus" if delta > 0 else "mdi:thermometer-minus"
        )

    async def async_press(self):
        await self.coordinator.async_change(delta=self.delta)


class ResetLearningButton(CoordinatorEntity, ButtonEntity):
    _attr_name = "House reset learning"
    _attr_icon = "mdi:brain"

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_reset_learning"

    async def async_press(self):
        await self.coordinator.async_reset_learning()

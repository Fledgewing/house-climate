"""Observe-only control toggle."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        [
            HouseSwitch(entry.runtime_data, entry.entry_id),
            FanSwitch(entry.runtime_data, entry.entry_id),
            LearningSwitch(entry.runtime_data, entry.entry_id),
            *[
                SystemControlSwitch(
                    entry.runtime_data, entry.entry_id, system["entity_id"]
                )
                for system in entry.runtime_data.active_systems(include_inactive=True)
            ],
            *[
                TemporarySwitch(entry.runtime_data, entry.entry_id, sensor["entity_id"])
                for sensor in entry.runtime_data.config["sensors"]
            ],
        ]
    )


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


class FanSwitch(HouseSwitch):
    _attr_name = "House fan control"
    _attr_icon = "mdi:fan"

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_fan_control"

    @property
    def is_on(self):
        return self.coordinator.fan_enabled

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_change(fan_enabled=True)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_change(fan_enabled=False)


class LearningSwitch(HouseSwitch):
    _attr_name = "House learning"
    _attr_icon = "mdi:brain"

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_learning_switch"

    @property
    def is_on(self):
        return self.coordinator.learning_enabled

    @property
    def extra_state_attributes(self):
        return {"resume_at": self.coordinator.learning_resume_at}

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_change(learning_enabled=True)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_change(learning_enabled=False)


class TemporarySwitch(CoordinatorEntity, SwitchEntity):
    _attr_icon = "mdi:timer-pause"

    def __init__(self, coordinator, entry_id, sensor):
        super().__init__(coordinator)
        self.sensor = sensor
        entity = coordinator.hass.states.get(sensor)
        self._attr_name = f"{entity.name if entity else sensor} Temporary change"
        self._attr_unique_id = f"{entry_id}_{sensor}_temporary"

    @property
    def is_on(self):
        return self.coordinator.learning.regimes[self.sensor]["temporary"]

    @property
    def extra_state_attributes(self):
        return {
            "auto_off_at": self.coordinator.learning.regimes[self.sensor][
                "temporary_until"
            ]
        }

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_room_learning(self.sensor, "temporary", True)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_room_learning(self.sensor, "temporary", False)


class SystemControlSwitch(CoordinatorEntity, SwitchEntity):
    _attr_icon = "mdi:thermostat"
    _attr_translation_key = "system_control"

    def __init__(self, coordinator, entry_id, system):
        super().__init__(coordinator)
        self.system = system
        object_id = system.split(".", 1)[1]
        self._attr_unique_id = f"{entry_id}_{system}_control"
        self.entity_id = f"switch.house_{object_id}_control"
        entity = coordinator.hass.states.get(system)
        self._attr_translation_placeholders = {
            "system": entity.name if entity else object_id
        }

    @property
    def is_on(self):
        return self.coordinator.system_control.get(self.system, True)

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_system_control(self.system, True)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_system_control(self.system, False)

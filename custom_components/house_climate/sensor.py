"""House temperature, spread and system offsets."""

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.update_coordinator import CoordinatorEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    async_add_entities(
        [
            HouseSensor(coordinator, entry.entry_id, "temperature"),
            HouseSensor(coordinator, entry.entry_id, "temperature_spread"),
            *[
                HouseSensor(coordinator, entry.entry_id, "offset", s["entity_id"])
                for s in coordinator.config["systems"]
            ],
        ]
    )


class HouseSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator, entry_id, kind, system=None):
        super().__init__(coordinator)
        self.kind, self.system = kind, system
        suffix = f"{system.split('.', 1)[1]}_offset" if system else kind
        self._attr_name = f"House {suffix.replace('_', ' ')}"
        self._attr_unique_id = f"{entry_id}_{suffix}"
        if kind == "temperature":
            self._attr_device_class = SensorDeviceClass.TEMPERATURE

    @property
    def native_value(self):
        if self.kind == "offset":
            return self.coordinator.state.offsets.get(self.system, 0)
        if self.coordinator.data is None:
            return None
        return (
            self.coordinator.data.temperature
            if self.kind == "temperature"
            else self.coordinator.data.spread
        )

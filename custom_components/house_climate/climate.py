"""The single house thermostat."""

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([HouseClimate(entry.runtime_data, entry.entry_id)])


class HouseClimate(CoordinatorEntity, ClimateEntity, RestoreEntity):
    _attr_name = "House"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_min_temp = 7
    _attr_max_temp = 35
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_house"

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        if not self.coordinator.restored and (
            last := await self.async_get_last_state()
        ):
            if last.state in self.hvac_modes:
                self.coordinator.state.mode = last.state
            if (target := last.attributes.get(ATTR_TEMPERATURE)) is not None:
                self.coordinator.state.setpoint = float(target)
            self.coordinator.restored = True

    @property
    def available(self):
        return True

    @property
    def hvac_mode(self):
        return HVACMode(self.coordinator.state.mode)

    @property
    def target_temperature(self):
        return self.coordinator.state.setpoint

    @property
    def current_temperature(self):
        return self.coordinator.data.temperature if self.coordinator.data else None

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        return {
            "last_decision": data.explanation if data else "Starting",
            "disturbed": dict(self.coordinator.learning.disturbed),
            "learning_enabled": self.coordinator.learning_enabled,
            "learning_resume_at": self.coordinator.learning_resume_at,
            "not_reporting": self.coordinator.not_reporting,
            "raw_sensor_readings": self.coordinator.raw_readings,
            "filtered_sensor_readings": data.readings if data else {},
            "sensor_readings": data.readings if data else {},
            "priority_mode": self.coordinator.priority_mode,
            "priority_weights": self.coordinator.priority_weights,
            "cycling": {
                system["entity_id"]: self.coordinator.state.cycling_status(
                    system["entity_id"], dt_util.utcnow().timestamp()
                )
                for system in self.coordinator.config["systems"]
            },
            "active_mode": self.coordinator.state.active_mode,
        }

    async def async_set_temperature(self, **kwargs):
        await self.coordinator.async_change(setpoint=kwargs[ATTR_TEMPERATURE])

    async def async_set_hvac_mode(self, hvac_mode):
        await self.coordinator.async_change(mode=hvac_mode)

"""House temperature, spread and system offsets."""

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .controller import excitation_scores
from .priority import members


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    async_add_entities(
        [
            LearningSensor(coordinator, entry.entry_id),
            FanLearningSensor(coordinator, entry.entry_id),
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


class LearningSensor(CoordinatorEntity, SensorEntity):
    _attr_name = "House learning"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_learning"

    @property
    def native_value(self):
        return self.coordinator.learning.confidence_percent

    @property
    def extra_state_attributes(self):
        outdoor = self.coordinator.number(self.coordinator.config.get("outdoor_sensor"))
        if outdoor is None:
            outdoor = self.coordinator.number(
                self.coordinator.config.get("weather_entity"), "temperature"
            )
        diagnostics = self.coordinator.learning.diagnostics(
            outdoor, self.coordinator.current_temperatures
        )
        for source in self.coordinator.config.get("extra_heat_sources", []):
            diagnostics["sources"][source["entity_id"]].update(
                response=source.get("response", "on_off"),
                effort=self.coordinator.source_value(source),
            )
        coordinator = self.coordinator
        config = {
            **coordinator.config,
            "sensors": members(coordinator.config, coordinator.priority_mode),
        }
        scores = excitation_scores(
            config,
            coordinator.state,
            coordinator.current_temperatures,
            coordinator.learning,
            outdoor,
        )
        now = dt_util.utcnow().timestamp()
        state = coordinator.state
        diagnostics["experiments"] = {
            key: {
                "uncertainty_score": score,
                "last_steady_test": state.last_steady_test.get(key),
                "next_eligible": max(
                    state.last_steady_test.get(
                        key, state.test_observed_since.get(key, now)
                    ),
                    state.last_excite.get(key, float("-inf")),
                )
                + config.get("excite_after_minutes", 120) * 60,
            }
            for key, (score, _) in scores.items()
        }
        return diagnostics


class FanLearningSensor(LearningSensor):
    _attr_name = "House fan learning"

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_fan_learning"

    @property
    def native_value(self):
        return self.coordinator.fans.confidence_percent

    @property
    def extra_state_attributes(self):
        return self.coordinator.fans.diagnostics()

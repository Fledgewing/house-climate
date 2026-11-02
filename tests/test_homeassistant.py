"""Home Assistant 2026.10 wiring, flows, commands, and persisted samples."""

from copy import deepcopy
from dataclasses import asdict
from unittest.mock import patch

import pytest
from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.climate.const import DATA_COMPONENT
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import UnitOfTemperature
from homeassistant.core import State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import selector
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.house_climate.const import DEFAULTS, DOMAIN
from custom_components.house_climate.controller import ControlState

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture
def settings():
    data = deepcopy(DEFAULTS)
    data["sensors"] = [{"entity_id": "sensor.room", "weight": 1, "zone": "Main"}]
    data["systems"] = [
        {
            "entity_id": "climate.ducted",
            "influence": ["sensor.room"],
            "preset": "manual",
        },
        {"entity_id": "climate.split", "influence": ["sensor.room"], "preset": ""},
    ]
    return data


class MockSystem(ClimateEntity):
    """Real HA service dispatch backed by an in-memory HVAC device."""

    _attr_should_poll = False
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
    )
    _attr_preset_modes = ["manual", "schedule"]
    _attr_hvac_action = HVACAction.IDLE
    _attr_hvac_mode = HVACMode.OFF
    _attr_target_temperature = 19
    _attr_preset_mode = "schedule"

    def __init__(self, entity_id, calls):
        self.entity_id = entity_id
        self._attr_name = entity_id.split(".")[1]
        self.calls = calls

    async def async_set_hvac_mode(self, hvac_mode):
        self.calls.append((self.entity_id, "set_hvac_mode", {"hvac_mode": hvac_mode}))
        self._attr_hvac_mode = hvac_mode
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs):
        self.calls.append((self.entity_id, "set_temperature", kwargs))
        self._attr_target_temperature = kwargs["temperature"]
        self.async_write_ha_state()

    async def async_set_preset_mode(self, preset_mode):
        self.calls.append(
            (self.entity_id, "set_preset_mode", {"preset_mode": preset_mode})
        )
        self._attr_preset_mode = preset_mode
        self.async_write_ha_state()


@pytest.fixture
async def installed(hass, settings, request):
    hass.config.units = METRIC_SYSTEM
    hass.states.async_set("sensor.room", "19", {"unit_of_measurement": "°C"})
    calls = []
    assert await async_setup_component(hass, "climate", {})
    systems = [MockSystem(system["entity_id"], calls) for system in settings["systems"]]
    for system, (mode, target) in zip(
        systems, getattr(request, "param", [("off", 19), ("off", 19)]), strict=True
    ):
        system._attr_available = mode != "unavailable"
        system._attr_hvac_mode = HVACMode(mode) if system.available else HVACMode.OFF
        system._attr_target_temperature = target
    await hass.data[DATA_COMPONENT].async_add_entities(systems)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data=settings)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, calls


@pytest.mark.parametrize("installed", [[("heat", 23), ("heat", 21)]], indirect=True)
async def test_fresh_setup_adopts_heat_without_turning_systems_off(hass, installed):
    entry, calls = installed
    house = hass.states.get("climate.house")
    assert house.state == "heat"
    assert house.attributes["temperature"] == 22.0
    assert "Adopted" in house.attributes["last_decision"]
    assert hass.states.get("switch.house_climate_control").state == "on"
    await entry.runtime_data.async_tick()
    assert not any(
        service == "set_hvac_mode" and data["hvac_mode"] == "off"
        for _, service, data in calls
    )


@pytest.mark.parametrize("installed", [[("heat", 23), ("cool", 21)]], indirect=True)
async def test_fresh_setup_mixed_modes_observes_without_commands(hass, installed):
    entry, calls = installed
    house = hass.states.get("climate.house")
    assert house.state == "off"
    assert house.attributes["temperature"] == 22.0
    assert "Systems disagree" in house.attributes["last_decision"]
    assert hass.states.get("switch.house_climate_control").state == "off"
    await entry.runtime_data.async_tick()
    assert not calls
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": "climate.house", "hvac_mode": "heat"},
        blocking=True,
    )
    assert entry.runtime_data.enabled
    assert calls


@pytest.mark.parametrize(
    ("installed", "mode", "target", "enabled"),
    [
        ([("heat", 23), ("off", 21)], "off", 22.0, False),
        ([("off", None), ("off", None)], "off", 21.0, False),
        ([("unavailable", 23), ("unavailable", 21)], "off", 21.0, False),
        ([("heat", 23), ("unavailable", 21)], "heat", 23.0, True),
        ([("cool", 23.3), ("cool", 21.3)], "cool", 22.5, True),
        ([("heat", 1), ("heat", 2)], "heat", 7.0, True),
        ([("cool", 40), ("cool", 41)], "cool", 35.0, True),
        ([("heat_cool", 19), ("heat_cool", 19)], "heat_cool", 19.0, True),
    ],
    indirect=["installed"],
)
async def test_fresh_adoption_fallbacks(hass, installed, mode, target, enabled):
    entry, calls = installed
    house = hass.states.get("climate.house")
    assert house.state == mode
    assert house.attributes["temperature"] == target
    assert entry.runtime_data.enabled is enabled
    assert not any(
        service == "set_hvac_mode" and data["hvac_mode"] == "off"
        for _, service, data in calls
    )
    if not enabled:
        assert not calls


@pytest.mark.parametrize("stored", [False, True])
async def test_restored_state_wins_over_adoption(hass, settings, stored):
    mock_restore_cache(hass, [State("climate.house", "cool", {"temperature": 24})])
    for system in settings["systems"]:
        hass.states.async_set(system["entity_id"], "heat", {"temperature": 23})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data=settings)
    entry.add_to_hass(hass)
    saved = None
    if stored:
        saved = {
            "state": asdict(ControlState(mode="heat", setpoint=20)),
            "enabled": False,
        }
    with (
        patch(
            "custom_components.house_climate.coordinator.Store.async_load",
            return_value=saved,
        ),
        patch("homeassistant.core.ServiceRegistry.async_call") as service_call,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    house = hass.states.get("climate.house")
    assert house.state == ("heat" if stored else "cool")
    assert house.attributes["temperature"] == (20 if stored else 24)
    assert "Adopted" not in house.attributes["last_decision"]
    if stored:
        assert not entry.runtime_data.enabled
        service_call.assert_not_called()


async def test_setup_entities(hass, installed):
    entry, _ = installed
    for entity_id in (
        "climate.house",
        "button.house_warmer",
        "button.house_colder",
        "sensor.house_temperature",
        "sensor.house_temperature_spread",
        "sensor.house_ducted_offset",
        "sensor.house_split_offset",
        "switch.house_climate_control",
    ):
        assert hass.states.get(entity_id) is not None
    assert hass.states.get("sensor.house_temperature").state == "19.0"
    assert entry.runtime_data.data.temperature == 19
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_warmer_commands_and_disabled(hass, installed):
    entry, calls = installed
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": "climate.house", "hvac_mode": "heat"},
        blocking=True,
    )
    calls.clear()
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.house_warmer"}, blocking=True
    )
    assert hass.states.get("climate.house").attributes["temperature"] == 19.5
    targets = {
        entity_id for entity_id, service, _ in calls if service == "set_temperature"
    }
    assert targets == {"climate.ducted", "climate.split"}
    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": "switch.house_climate_control"},
        blocking=True,
    )
    calls.clear()
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.house_warmer"}, blocking=True
    )
    await entry.runtime_data.async_tick()
    assert not calls
    assert entry.runtime_data.state.setpoint == 20
    assert "observing" in entry.runtime_data.data.explanation


async def test_sample_retention_and_restore(hass, installed):
    entry, _ = installed
    coordinator = entry.runtime_data
    coordinator.samples.insert(
        0, {"timestamp": dt_util.utcnow().timestamp() - 31 * 86400}
    )
    await coordinator.async_change(setpoint=22, mode="heat", enabled=False)
    await coordinator.async_tick()
    saved = await coordinator.store.async_load()
    assert all(
        s["timestamp"] >= dt_util.utcnow().timestamp() - 30 * 86400
        for s in saved["samples"]
    )
    sample = saved["samples"][-1]
    assert sample["sensors"]["sensor.room"]["value"] == 19
    assert sample["systems"]["climate.ducted"]["hvac_action"] == "idle"
    assert set(sample) >= {"outdoor", "solar", "occupancy"}
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert entry.runtime_data.state.setpoint == 22
    assert entry.runtime_data.state.mode == "heat"
    assert not entry.runtime_data.enabled


async def complete_flow(manager, result, sensors, systems):
    result = await manager.async_configure(
        result["flow_id"], {"sensors": sensors, "systems": systems}
    )
    for i, _ in enumerate(sensors):
        assert result["step_id"] == "sensor"
        result = await manager.async_configure(
            result["flow_id"], {"weight": i + 1.0, "zone": "Room"}
        )
    for _ in systems:
        assert result["step_id"] == "system"
        result = await manager.async_configure(
            result["flow_id"], {"influence": sensors, "preset": ""}
        )
    assert result["step_id"] == "tunables"
    # Unselected optional selectors are omitted by the HA frontend.
    tunables = {
        k: v for k, v in DEFAULTS.items() if k not in ("sensors", "systems") and v != ""
    }
    return await manager.async_configure(result["flow_id"], tunables)


async def test_config_flow_creates_entry(hass):
    with patch("custom_components.house_climate.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] == FlowResultType.FORM
        result = await complete_flow(
            hass.config_entries.flow,
            result,
            ["sensor.room"],
            ["climate.first", "climate.second", "climate.third"],
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert all(
            result["data"][key] == ""
            for key in (
                "outdoor_sensor",
                "weather_entity",
                "solar_sensor",
                "occupancy_sensor",
            )
        )
        assert len(result["data"]["systems"]) == 3
        assert result["data"]["sensors"][0]["weight"] == 1
        await hass.async_block_till_done()
        duplicate = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert duplicate["reason"] == "already_configured"


async def test_options_add_remove_reweight(hass, settings):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data=settings)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await complete_flow(
        hass.config_entries.options,
        result,
        ["sensor.new", "sensor.another"],
        ["climate.third"],
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["sensors"] == [
        {"entity_id": "sensor.new", "weight": 1.0, "zone": "Room"},
        {"entity_id": "sensor.another", "weight": 2.0, "zone": "Room"},
    ]
    assert entry.options["systems"][0]["influence"] == ["sensor.new", "sensor.another"]


async def test_empty_selection(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"sensors": [], "systems": []}
    )
    assert result["errors"] == {"base": "empty_selection"}


async def test_external_changes_are_explained_and_reasserted(hass, installed):
    entry, calls = installed
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": "climate.house", "hvac_mode": "heat"},
        blocking=True,
    )
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": "climate.ducted", "temperature": 18},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert "changed outside house control" in entry.runtime_data.data.explanation
    calls.clear()
    await entry.runtime_data.async_tick()
    assert any(
        entity_id == "climate.ducted"
        and service == "set_temperature"
        and data["temperature"] == 19
        for entity_id, service, data in calls
    )


async def test_config_flow_empty_defaults_and_selector_filters(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    fields = result["data_schema"].schema
    for field, entity_selector in fields.items():
        assert field.default() == []
        assert isinstance(entity_selector, selector.EntitySelector)
        assert entity_selector.config["multiple"] is True
        if field.schema == "sensors":
            assert entity_selector.config["domain"] == ["sensor"]
            assert entity_selector.config["device_class"] == ["temperature"]
        else:
            assert entity_selector.config["domain"] == ["climate"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"sensors": ["sensor.room"], "systems": ["climate.ducted"]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"weight": 1.0}
    )
    assert result["step_id"] == "system"
    for field, entity_selector in result["data_schema"].schema.items():
        if field.schema == "influence":
            assert field.default() == []
            assert isinstance(entity_selector, selector.EntitySelector)
            assert entity_selector.config["domain"] == ["sensor"]
            assert entity_selector.config["device_class"] == ["temperature"]
            assert entity_selector.config["include_entities"] == ["sensor.room"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"influence": ["sensor.room"]}
    )
    assert result["step_id"] == "tunables"
    expected = {
        "outdoor_sensor": ("sensor", "temperature"),
        "weather_entity": ("weather", None),
        "solar_sensor": ("sensor", "power"),
        "occupancy_sensor": ("binary_sensor", "occupancy"),
    }
    for field, entity_selector in result["data_schema"].schema.items():
        if field.schema not in expected:
            continue
        domain, device_class = expected[field.schema]
        assert not field.description.get("suggested_value")
        assert isinstance(entity_selector, selector.EntitySelector)
        assert entity_selector.config["domain"] == [domain]
        if device_class:
            assert entity_selector.config["device_class"] == [device_class]

"""Home Assistant 2026.10 wiring, flows, commands, and persisted samples."""

from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
from types import SimpleNamespace
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
from homeassistant.core import CoreState, State
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
            "preset": "manual",
        },
        {"entity_id": "climate.split", "preset": ""},
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

    async def async_set_fan_mode(self, fan_mode):
        self.calls.append((self.entity_id, "set_fan_mode", {"fan_mode": fan_mode}))
        self._attr_fan_mode = fan_mode
        self.async_write_ha_state()

    async def async_set_swing_mode(self, swing_mode):
        self.calls.append(
            (self.entity_id, "set_swing_mode", {"swing_mode": swing_mode})
        )
        self._attr_swing_mode = swing_mode
        self.async_write_ha_state()


@pytest.fixture
async def installed(hass, settings, request):
    hass.set_state(CoreState.running)
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
    hass.set_state(CoreState.running)
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
        "sensor.house_learning",
        "button.house_reset_learning",
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
    # Own-sensor effort targets can stay saturated after a house target change.
    assert targets <= {"climate.ducted", "climate.split"}
    assert entry.runtime_data.state.setpoint == 19.5
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


async def complete_flow(
    manager,
    result,
    sensors,
    systems,
    stale_after=720,
    average_weight=1.0,
    fan_reward_lambda=None,
    test_overshoot=1.5,
):
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
        result = await manager.async_configure(result["flow_id"], {"preset": ""})
    assert result["step_id"] == "tunables"
    # Unselected optional selectors are omitted by the HA frontend.
    tunables = {
        k: v
        for k, v in DEFAULTS.items()
        if k not in ("sensors", "systems", "extra_heat_sources") and v not in ("", None)
    }
    fields = {field.schema: field for field in result["data_schema"].schema}
    assert fields["stale_after"].default() == 720
    tunables["stale_after"] = stale_after
    tunables["test_overshoot"] = test_overshoot
    tunables["average_weight"] = average_weight
    if fan_reward_lambda is not None:
        tunables["fan_reward_lambda"] = fan_reward_lambda
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
        assert result["data"]["average_weight"] == 1.0
        assert result["data"]["fan_reward_lambda"] is None
        assert len(result["data"]["systems"]) == 3
        assert result["data"]["sensors"][0]["weight"] == 1
        await hass.async_block_till_done()
        duplicate = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert duplicate["reason"] == "already_configured"


async def test_options_add_remove_reweight(hass, settings):
    # Existing 0.1.1 entries have no stale_after option.
    settings.pop("stale_after")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data=settings)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await complete_flow(
        hass.config_entries.options,
        result,
        ["sensor.new", "sensor.another"],
        ["climate.third"],
        stale_after=180,
        test_overshoot=2.1,
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["sensors"] == [
        {"entity_id": "sensor.new", "weight": 1.0, "zone": "Room"},
        {"entity_id": "sensor.another", "weight": 2.0, "zone": "Room"},
    ]
    assert entry.options["systems"] == [{"entity_id": "climate.third", "preset": ""}]
    assert entry.options["stale_after"] == 180
    assert entry.options["test_overshoot"] == 2.1


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
    event_time = entry.runtime_data.last_commands["climate.ducted"][2] + timedelta(
        seconds=121
    )
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=event_time,
    ):
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": "climate.ducted", "temperature": 16},
            blocking=True,
        )
        await hass.async_block_till_done()
    assert "changed outside house control" in entry.runtime_data.data.explanation
    calls.clear()
    await entry.runtime_data.async_tick()
    assert any(
        entity_id == "climate.ducted"
        and service == "set_temperature"
        and data["temperature"] == entry.runtime_data.last_commands["climate.ducted"][1]
        for entity_id, service, data in calls
    )


@pytest.mark.parametrize("delay", [10, 121])
async def test_delayed_command_echo_is_not_an_outside_change(hass, installed, delay):
    entry, calls = installed
    # Simulate a device accepting the command but reporting its target later.
    with patch.object(MockSystem, "async_set_temperature", return_value=None):
        await entry.runtime_data.async_change(mode="heat", setpoint=21)
    target = entry.runtime_data.last_commands["climate.ducted"][1]
    assert hass.states.get("climate.ducted").attributes["temperature"] != target
    event_time = entry.runtime_data.last_commands["climate.ducted"][2] + timedelta(
        seconds=delay
    )
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=event_time,
    ):
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": "climate.ducted", "temperature": target},
            blocking=True,
        )
        await hass.async_block_till_done()
    assert "changed outside house control" not in entry.runtime_data.data.explanation
    assert (
        "changed outside house control"
        not in hass.states.get("climate.house").attributes["last_decision"]
    )


@pytest.mark.parametrize("delay", [10, 120, 121])
@pytest.mark.parametrize(
    "service,data",
    [
        ("set_temperature", {"temperature": 17}),
        ("set_hvac_mode", {"hvac_mode": "cool"}),
    ],
)
async def test_outside_change_grace_period(hass, installed, delay, service, data):
    entry, _ = installed
    await entry.runtime_data.async_change(mode="heat", setpoint=21)
    event_time = entry.runtime_data.last_commands["climate.ducted"][2] + timedelta(
        seconds=delay
    )
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=event_time,
    ):
        await hass.services.async_call(
            "climate",
            service,
            {"entity_id": "climate.ducted", **data},
            blocking=True,
        )
        await hass.async_block_till_done()
    assert ("changed outside house control" in entry.runtime_data.data.explanation) is (
        delay > 120
    )


@pytest.mark.parametrize("has_last_reported", [True, False])
async def test_sensor_age_uses_report_time_with_update_fallback(
    hass, installed, has_last_reported
):
    entry, _ = installed
    now = dt_util.utcnow()
    sensor = SimpleNamespace(
        state="19",
        attributes={},
        last_updated=now - timedelta(hours=13),
    )
    if has_last_reported:
        sensor.last_reported = now - timedelta(minutes=93)
    get_state = hass.states.get
    with patch.object(
        type(hass.states),
        "get",
        side_effect=lambda entity_id: (
            sensor if entity_id == "sensor.room" else get_state(entity_id)
        ),
    ):
        await entry.runtime_data.async_tick()
    result = entry.runtime_data.data
    if has_last_reported:
        assert result.readings == {"sensor.room": 19}
    else:
        assert result.readings == {}
        assert "sensor.room: stale reading" in result.explanation


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
        elif field.schema == "systems":
            assert entity_selector.config["domain"] == ["climate"]
        else:
            assert field.schema == "extra_heat_sources"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"sensors": ["sensor.room"], "systems": ["climate.ducted"]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"weight": 1.0}
    )
    assert result["step_id"] == "system"
    assert {field.schema for field in result["data_schema"].schema} == {
        "preset",
        "min_run_minutes",
        "min_off_minutes",
        "max_starts_per_hour",
        "modulating",
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "tunables"
    fields = {
        field.schema: (field, value)
        for field, value in result["data_schema"].schema.items()
    }
    for key, default in {
        "average_weight": 1.0,
        "explore_threshold": 0.8,
        "excite_after_minutes": 120,
        "excite_minutes": 60,
        "test_overshoot": 1.5,
        "lag_minutes": 60,
        "lag_prior": 0.01,
    }.items():
        field, value = fields[key]
        assert field.default() == default
        assert isinstance(value, selector.NumberSelector)
    assert fields["average_weight"][1].config["min"] == 0.01
    assert fields["average_weight"][1].config["max"] == 10
    assert fields["fan_reward_lambda"][0].description == {}
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


@pytest.mark.parametrize("options", [False, True])
async def test_v1_entry_migration(hass, settings, options):
    from custom_components.house_climate import async_migrate_entry

    for system in settings["systems"]:
        system["influence"] = ["sensor.room"]
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data=settings,
        options=deepcopy(settings) if options else {},
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 4
    assert all("influence" not in s for s in entry.data["systems"])
    assert all("influence" not in s for s in entry.options.get("systems", []))
    assert entry.data["systems"][0]["preset"] == "manual"
    assert entry.data["sensors"] == settings["sensors"]


async def test_learning_action_events_store_restart_and_reset(hass, installed):
    entry, _ = installed
    coordinator = entry.runtime_data
    coordinator.config["outdoor_sensor"] = "sensor.outdoor"
    coordinator.config["solar_sensor"] = "sensor.solar"
    hass.states.async_set("sensor.outdoor", "5")
    hass.states.async_set("sensor.solar", "2000", {"unit_of_measurement": "W"})
    assert coordinator.solar_kw() == 2
    start = dt_util.utcnow()
    # Establish a fresh interval with known weather and solar context.
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow", return_value=start
    ):
        await coordinator.async_tick()
    for seconds, action in [(60, "heating"), (210, "idle")]:
        with patch(
            "custom_components.house_climate.coordinator.dt_util.utcnow",
            return_value=start + timedelta(seconds=seconds),
        ):
            state = hass.states.get("climate.ducted")
            hass.states.async_set(
                "climate.ducted",
                state.state,
                {**state.attributes, "hvac_action": action},
            )
            await hass.async_block_till_done()
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=start + timedelta(seconds=300),
    ):
        hass.states.async_set("sensor.room", "19.1")
        await hass.async_block_till_done()
        await coordinator.async_tick()
    assert coordinator.samples[-1]["efforts"]["climate.ducted"] == pytest.approx(0.5)
    assert coordinator.learning.rows["sensor.room"].samples == 1
    saved_model = coordinator.learning.to_dict()
    assert (await coordinator.store.async_load())["learning"] == saved_model
    assert (
        hass.states.get("sensor.house_learning").attributes["sensors"]["sensor.room"][
            "samples"
        ]
        == 1
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert entry.runtime_data.learning.to_dict() == saved_model
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.house_reset_learning"}, blocking=True
    )
    assert entry.runtime_data.learning.rows["sensor.room"].samples == 0
    assert entry.runtime_data.learning.rows["sensor.room"].theta[1:3] == [0.03, 0.03]
    assert (await entry.runtime_data.store.async_load())[
        "learning"
    ] == entry.runtime_data.learning.to_dict()
    assert hass.states.get("sensor.house_learning").state == "0.0"


async def test_learning_reconciles_options_on_reload(hass, installed):
    entry, _ = installed
    model = entry.runtime_data.learning
    model.update(
        {"sensor.room": 19},
        {"sensor.room": 19.1},
        5,
        {"climate.ducted": 1, "climate.split": 0},
    )
    await entry.runtime_data.async_tick(record=False)
    retained = model.to_dict()["rows"]["sensor.room"]
    options = deepcopy(dict(entry.data))
    options["sensors"].append({"entity_id": "sensor.new", "weight": 1})
    hass.config_entries.async_update_entry(entry, options=options)
    await hass.async_block_till_done()
    model = entry.runtime_data.learning
    assert model.to_dict()["rows"]["sensor.room"] == retained
    assert model.rows["sensor.new"].samples == 0
    assert model.rows["sensor.new"].theta[1:3] == [0.03, 0.03]
    options = deepcopy(options)
    options["sensors"] = [options["sensors"][1]]
    hass.config_entries.async_update_entry(entry, options=options)
    await hass.async_block_till_done()
    assert set(entry.runtime_data.learning.rows) == {
        "sensor.new",
        "climate.ducted",
        "climate.split",
    }
    assert set((await entry.runtime_data.store.async_load())["learning"]["rows"]) == {
        "sensor.new",
        "climate.ducted",
        "climate.split",
    }


async def test_action_events_during_command_lock_are_tracked(hass, installed):
    entry, _ = installed
    coordinator = entry.runtime_data
    now = dt_util.utcnow()
    coordinator.effort.finish(
        {"climate.ducted": "idle", "climate.split": "idle"}, now.timestamp()
    )
    async with coordinator.lock:
        with patch(
            "custom_components.house_climate.coordinator.dt_util.utcnow",
            return_value=now + timedelta(seconds=60),
        ):
            state = hass.states.get("climate.ducted")
            hass.states.async_set(
                state.entity_id,
                state.state,
                {**state.attributes, "hvac_action": "heating"},
            )
            await hass.async_block_till_done()
    efforts, _ = coordinator.effort.finish(
        {"climate.ducted": "idle", "climate.split": "idle"}, now.timestamp() + 300
    )
    assert efforts["climate.ducted"] == pytest.approx(0.8)


@pytest.mark.parametrize("active", [True, False])
async def test_frozen_sensor_exclusion_repair_recovery_and_learning(
    hass, installed, active
):
    """Five hours of repeated values only freezes a sensor with actual HVAC use."""
    from homeassistant.helpers import issue_registry as ir

    entry, _ = installed
    coordinator = entry.runtime_data
    coordinator.config["sensors"].append({"entity_id": "sensor.healthy", "weight": 1})
    coordinator.learning.reconcile(
        ["sensor.room", "sensor.healthy"], coordinator.learning.systems
    )
    coordinator.config["outdoor_sensor"] = "sensor.outdoor"
    hass.states.async_set("sensor.outdoor", "5")
    hass.states.async_set("sensor.healthy", "21")
    start = dt_util.utcnow()
    state = hass.states.get("climate.ducted")
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow", return_value=start
    ):
        hass.states.async_set(
            state.entity_id,
            state.state,
            {**state.attributes, "hvac_action": "heating" if active else "idle"},
        )
        await hass.async_block_till_done()
        await coordinator.async_tick()
    before = deepcopy(coordinator.learning.to_dict()["rows"]["sensor.room"])
    end = start + timedelta(hours=5)
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow", return_value=end
    ):
        # Attribute changes/report metadata must not count as a temperature change.
        hass.states.async_set("sensor.room", "19.0", {"battery": 90})
        hass.states.async_set("sensor.healthy", "21.5")
        await hass.async_block_till_done()
        await coordinator.async_tick()
    issue_id = f"{entry.entry_id}_sensor.room_not_reporting"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert ("sensor.room" in coordinator.not_reporting) is active
    assert ("sensor.room" not in coordinator.data.readings) is active
    if not active:
        assert issue is None
        assert coordinator.data.temperature == 20
        return
    assert coordinator.data.temperature == 21
    assert coordinator.data.spread == 0
    assert issue.severity == ir.IssueSeverity.WARNING
    assert issue.translation_placeholders["sensor"] == "sensor.room"
    assert "sensor.room: not reporting" in coordinator.data.explanation
    assert "check its battery" in coordinator.data.explanation
    assert coordinator.learning.to_dict()["rows"]["sensor.room"] == before
    assert coordinator.learning.rows["sensor.healthy"].samples == 1
    assert "sensor.room" in hass.states.get("climate.house").attributes["not_reporting"]
    saved = await coordinator.store.async_load()
    assert saved["reporting"]["sensors"]["sensor.room"]["frozen"]
    # A restart refreshing HA state metadata cannot revive the sensor.
    assert await hass.config_entries.async_reload(entry.entry_id)
    coordinator = entry.runtime_data
    assert "sensor.room" in coordinator.not_reporting
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=end + timedelta(minutes=1),
    ):
        hass.states.async_set("sensor.room", "19.1")
        await hass.async_block_till_done()
    assert "sensor.room" not in coordinator.not_reporting
    # The value-change immediately clears the repair, while control stays filtered.
    assert coordinator.data.readings["sensor.room"] == 19
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_device_last_seen_excludes_at_startup(hass, settings):
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers import issue_registry as ir

    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data=settings)
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("generic", "room")}
    )
    registry = er.async_get(hass)
    for name in ("room", "room_last_seen", "unrelated_last_seen"):
        registry.async_get_or_create(
            "sensor",
            "generic",
            name,
            suggested_object_id=name,
            device_id=device.id if name != "unrelated_last_seen" else None,
        )
    now = dt_util.utcnow()
    hass.states.async_set("sensor.room", "15")
    hass.states.async_set(
        "sensor.room_last_seen",
        (now - timedelta(days=120)).isoformat(),
        {"device_class": "timestamp"},
    )
    hass.states.async_set(
        "sensor.unrelated_last_seen", now.isoformat(), {"device_class": "timestamp"}
    )
    with patch("homeassistant.core.ServiceRegistry.async_call"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = entry.runtime_data
    assert hass.states.get("sensor.room").last_updated >= now
    assert coordinator.data.readings == {}
    assert coordinator.data.temperature is None
    assert (
        coordinator.not_reporting["sensor.room"]["reason"]
        == "device last seen is too old"
    )
    issue_id = f"{entry.entry_id}_sensor.room_not_reporting"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    # Timestamp signal independently recovers without needing temperature motion.
    hass.states.async_set(
        "sensor.room_last_seen", now.isoformat(), {"device_class": "timestamp"}
    )
    await coordinator.async_tick()
    assert coordinator.data.readings == {"sensor.room": 15}
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_recorder_seeds_numeric_changes_and_activity_on_startup(hass, settings):
    """Restart/attribute updates of the same value are not real changes."""
    from homeassistant.helpers.recorder import DATA_INSTANCE

    from custom_components.house_climate.coordinator import HouseCoordinator

    entry = MockConfigEntry(domain=DOMAIN, data=settings)
    entry.add_to_hass(hass)
    coordinator = HouseCoordinator(hass, entry)
    now = dt_util.utcnow()
    old = now - timedelta(hours=5)
    hass.states.async_set("sensor.room", "19")
    hass.states.async_set(
        "climate.ducted", "heat", {"hvac_action": "heating", "temperature": 21}
    )
    history = {
        "sensor.room": [
            State("sensor.room", "18", last_updated=old - timedelta(minutes=5)),
            State("sensor.room", "19", last_updated=old),
            State(
                "sensor.room", "unavailable", last_updated=now - timedelta(minutes=2)
            ),
            State("sensor.room", "19.0", last_updated=now - timedelta(minutes=1)),
        ]
    }
    activity = {
        "climate.ducted": [
            State(
                "climate.ducted", "heat", {"hvac_action": "heating"}, last_updated=old
            )
        ]
    }

    class Recorder:
        async def async_add_executor_job(self, job):
            return job()

    hass.data[DATA_INSTANCE] = Recorder()
    try:
        with (
            patch(
                "homeassistant.components.recorder.history.get_significant_states",
                side_effect=[history, activity],
            ),
            patch("homeassistant.core.ServiceRegistry.async_call"),
        ):
            await coordinator.async_tick()
        assert (
            coordinator.reporting.sensors["sensor.room"]["changed"] == old.timestamp()
        )
        assert "sensor.room" in coordinator.not_reporting
        assert coordinator.data.readings == {}
    finally:
        hass.data.pop(DATA_INSTANCE)


DEFROST_SEQUENCE = [
    22,
    22.5,
    22,
    21.5,
    20.5,
    18.5,
    18,
    19,
    20,
    21,
    21.5,
    22,
    23,
    23.5,
    24,
    23.5,
    23,
    22.5,
    22,
]


async def test_real_house_transient_replay(hass, installed):
    """All minute events reach the filter; control/learning run every five minutes."""
    from custom_components.house_climate.filtering import SensorFilter

    entry, calls = installed
    c = entry.runtime_data
    c.config["sensors"].append({"entity_id": "sensor.other", "weight": 1})
    c.learning.reconcile(["sensor.room", "sensor.other"], c.learning.systems)
    c.config["outdoor_sensor"] = "sensor.outdoor"
    hass.states.async_set("sensor.outdoor", "3")
    c.state = ControlState(setpoint=22.5, mode="heat")
    c.enabled = True
    c.filters = {"sensor.room": SensorFilter(), "sensor.other": SensorFilter()}
    c.learning_baseline = None
    # Keep the exploration square wave in one phase throughout this replay.
    start = dt_util.parse_datetime("2026-11-02T17:30:00+00:00")
    temperatures = []
    targets = []
    for minute in range(-15, 26):
        value = DEFROST_SEQUENCE[minute] if 0 <= minute < len(DEFROST_SEQUENCE) else 22
        # The other room remains in the observed 23–24 °C band.
        other = 23.5 + 0.5 * min(1, max(0, minute) / 25)
        with patch(
            "custom_components.house_climate.coordinator.dt_util.utcnow",
            return_value=start + timedelta(minutes=minute),
        ):
            hass.states.async_set(
                "sensor.room", str(value), {"friendly_name": "living-room"}
            )
            hass.states.async_set("sensor.other", str(other))
            await hass.async_block_till_done()
            if minute % 5 == 0:
                await c.async_tick()
                if minute >= 0:
                    temperatures.append(c.data.temperature)
                    targets.append(c.last_commands["climate.split"][1])
                    if minute in (5, 10, 15, 20):
                        assert (
                            "living-room sensor transient ignored" in c.data.explanation
                        )
    assert max(temperatures) - min(temperatures) <= 0.5
    # Proportional efforts alternate adjacent target steps to avoid rounding bias.
    assert max(targets) - min(targets) <= 0.5
    # Warmup rows learn; the cold and hot excursions add no contaminated rows.
    assert c.learning.rows["sensor.room"].samples == 3
    assert c.learning.rows["sensor.other"].samples == 8
    attrs = hass.states.get("climate.house").attributes
    assert attrs["raw_sensor_readings"]["sensor.room"]["value"] == 22
    assert attrs["filtered_sensor_readings"] == c.data.readings
    assert (await c.store.async_load())["filters"]["sensor.room"] == c.filters[
        "sensor.room"
    ].to_dict()


async def test_completed_transient_between_ticks_skips_learning(hass, installed):
    entry, _ = installed
    c = entry.runtime_data
    c.config["outdoor_sensor"] = "sensor.outdoor"
    hass.states.async_set("sensor.outdoor", "5")
    start = dt_util.utcnow()
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow", return_value=start
    ):
        await c.async_tick()
    before = deepcopy(c.learning.to_dict())
    for minute, value in [(1, 15), (3, 19)]:
        with patch(
            "custom_components.house_climate.coordinator.dt_util.utcnow",
            return_value=start + timedelta(minutes=minute),
        ):
            hass.states.async_set("sensor.room", str(value))
            await hass.async_block_till_done()
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=start + timedelta(minutes=5),
    ):
        await c.async_tick()
    assert {
        k: v
        for k, v in c.learning.to_dict().items()
        if k not in ("effort_history", "effort_clock")
    } == {
        k: v for k, v in before.items() if k not in ("effort_history", "effort_clock")
    }
    assert c.data.readings == {"sensor.room": 19}
    assert "sensor transient ignored" in c.data.explanation
    # The median window and rejected interval survive an actual integration reload.
    saved = c.filters["sensor.room"].to_dict()
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=start + timedelta(minutes=6),
    ):
        assert await hass.config_entries.async_reload(entry.entry_id)
    restored = entry.runtime_data.filters["sensor.room"]
    assert restored.rejected == saved["rejected"]
    assert restored.value((start + timedelta(minutes=6)).timestamp()) == 19


async def prepare_fans(hass, installed):
    from random import Random

    entry, calls = installed
    c = entry.runtime_data
    c.fans.rng = Random(1)
    c.config["outdoor_sensor"] = "sensor.outdoor"
    hass.states.async_set("sensor.outdoor", "3")
    system = hass.data[DATA_COMPONENT].get_entity("climate.ducted")
    system._attr_supported_features |= (
        ClimateEntityFeature.FAN_MODE | ClimateEntityFeature.SWING_MODE
    )
    system._attr_fan_modes = ["gentle", "strong", "auto"]
    system._attr_fan_mode = "strong"
    system._attr_swing_modes = ["fixed", "moving"]
    system._attr_swing_mode = "moving"
    system.async_write_ha_state()
    await hass.async_block_till_done()
    await c.async_change(mode="heat")
    return c, system, calls


async def test_fan_vane_commands_hold_and_switches(hass, installed):
    c, system, calls = await prepare_fans(hass, installed)
    assert hass.states.get("switch.house_fan_control").state == "on"
    assert hass.states.get("sensor.house_fan_learning").state == "0.0"
    assert any(service == "set_fan_mode" for _, service, _ in calls)
    assert system.fan_mode == "gentle"
    assert system.preset_mode == "manual"
    assert "trying ducted fan gentle to learn" in c.data.explanation
    calls.clear()
    await c.async_tick()
    assert not any(
        service in ("set_fan_mode", "set_swing_mode") for _, service, _ in calls
    )
    now = dt_util.utc_from_timestamp(c.fans.window["start"] + 1800)
    # The next action is the same system's vane, only after the full hold.
    c.fans.rng.seed(1)
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow", return_value=now
    ):
        await c.async_tick()
    assert any(service == "set_swing_mode" for _, service, _ in calls)
    for switch in ("switch.house_fan_control", "switch.house_climate_control"):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": switch}, blocking=True
        )
        system._attr_fan_mode = "strong"
        system._attr_swing_mode = "moving"
        system.async_write_ha_state()
        await hass.async_block_till_done()
        calls.clear()
        with patch(
            "custom_components.house_climate.coordinator.dt_util.utcnow",
            return_value=now + timedelta(hours=1),
        ):
            await c.async_tick()
        assert not any(
            service in ("set_fan_mode", "set_swing_mode") for _, service, _ in calls
        )
        if switch.endswith("climate_control"):
            assert not calls
        else:
            await hass.services.async_call(
                "switch", "turn_on", {"entity_id": switch}, blocking=True
            )


@pytest.mark.parametrize("delay", [10, 121])
async def test_external_fan_change_grace_and_reassert(hass, installed, delay):
    c, system, calls = await prepare_fans(hass, installed)
    choice, sent_at = c.last_fan_commands[("climate.ducted", "fan")]
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=sent_at + timedelta(seconds=delay),
    ):
        await system.async_set_fan_mode("strong")
        await hass.async_block_till_done()
        assert ("fan changed outside house control" in c.data.explanation) is (
            delay > 120
        )
        calls.clear()
        await c.async_tick()
    assert ("climate.ducted", "set_fan_mode", {"fan_mode": choice}) in calls


async def test_fans_store_restart_and_contaminated_event(hass, installed):
    c, _, _ = await prepare_fans(hass, installed)
    start = dt_util.utc_from_timestamp(c.fans.window["start"])
    for minute, value in [(1, "unavailable"), (2, "19")]:
        with patch(
            "custom_components.house_climate.coordinator.dt_util.utcnow",
            return_value=start + timedelta(minutes=minute),
        ):
            hass.states.async_set("sensor.room", value)
            await hass.async_block_till_done()
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=start + timedelta(minutes=30),
    ):
        await c.async_tick()
    assert all(
        v["samples"] == 0
        for actions in c.fans.arms.values()
        for buckets in actions.values()
        for arms in buckets.values()
        for v in arms.values()
    )
    saved = (await c.store.async_load())["fans"]
    await c.async_change(fan_enabled=False)
    entry, _ = installed
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert entry.runtime_data.fans.arms == saved["arms"]
    assert entry.runtime_data.fans.chosen == saved["chosen"]
    assert not entry.runtime_data.fan_enabled


async def test_options_quiet_limit_from_runtime_modes(hass, settings):
    hass.states.async_set(
        "climate.ducted", "heat", {"fan_modes": ["slowish", "faster", "auto"]}
    )
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data=settings)
    entry.add_to_hass(hass)
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(
        result["flow_id"], {"sensors": ["sensor.room"], "systems": ["climate.ducted"]}
    )
    result = await flow.async_configure(result["flow_id"], {"weight": 1})
    fields = {k.schema: v for k, v in result["data_schema"].schema.items()}
    assert fields["quiet_max_fan"].config["options"] == ["slowish", "faster", "auto"]
    result = await flow.async_configure(result["flow_id"], {"quiet_max_fan": "slowish"})
    fields = {k.schema: k for k in result["data_schema"].schema}
    assert fields["quiet_hours_enabled"].default() is False
    assert fields["quiet_start"].default() == "22:00"
    assert fields["quiet_end"].default() == "07:00"
    assert fields["learn_exclude_modes"].default() == "auto"
    inputs = {
        k: v
        for k, v in DEFAULTS.items()
        if k not in ("sensors", "systems", "extra_heat_sources") and v not in ("", None)
    }
    inputs["quiet_hours_enabled"] = True
    inputs["learn_exclude_modes"] = "auto, strong"
    result = await flow.async_configure(result["flow_id"], inputs)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["systems"][0]["quiet_max_fan"] == "slowish"
    assert entry.options["learn_exclude_modes"] == "auto, strong"


@pytest.mark.parametrize("reason", ["not_reporting", "mode", "setpoint"])
async def test_fan_windows_skip_real_contamination(hass, installed, reason):
    c, system, _ = await prepare_fans(hass, installed)
    start = dt_util.utc_from_timestamp(c.fans.window["start"])
    for minute in (1, 3):
        with patch(
            "custom_components.house_climate.coordinator.dt_util.utcnow",
            return_value=start + timedelta(minutes=minute),
        ):
            if reason == "not_reporting":
                if minute == 1:
                    c.reporting.sensors["sensor.room"]["frozen"] = True
                    await c.async_tick()
                else:
                    hass.states.async_set("sensor.room", "19.1")
            elif reason == "mode":
                await system.async_set_hvac_mode("cool" if minute == 1 else "heat")
            else:
                await c.async_change(setpoint=20 if minute == 1 else 19)
            await hass.async_block_till_done()
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=start + timedelta(minutes=30),
    ):
        await c.async_tick()
    assert all(
        v["samples"] == 0
        for actions in c.fans.arms.values()
        for buckets in actions.values()
        for arms in buckets.values()
        for v in arms.values()
    )


async def test_startup_waits_and_restores_pi_without_restart_excursion(hass, installed):
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

    entry, calls = installed
    c = entry.runtime_data
    c.enabled = False
    c.state.offsets = {"climate.ducted": 1.5, "climate.split": -0.5}
    c.state.integrals = {"climate.ducted": 0.7, "climate.split": -0.2}
    await c.async_tick(record=False)
    saved = deepcopy((await c.store.async_load())["state"])
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.set_state(CoreState.starting)
    hass.states.async_set("sensor.room", "unavailable")
    calls.clear()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    c = entry.runtime_data
    assert c.data is None
    assert not c.initialized
    assert asdict(c.state) == saved
    assert not calls
    # A newly loaded value differs sharply from the pre-restart Store value.
    hass.states.async_set("sensor.room", "23")
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert c.initialized
    assert c.data.readings["sensor.room"] == 23
    assert not c.filters["sensor.room"].blocked
    assert c.state.offsets == saved["offsets"]
    assert c.state.integrals == saved["integrals"]
    # Config-entry reload while HA is running takes the immediate path.
    assert await hass.config_entries.async_reload(entry.entry_id)
    c = entry.runtime_data
    assert c.initialized
    assert c.data is not None
    assert c.state.offsets == saved["offsets"]
    assert c.state.integrals == saved["integrals"]


@pytest.mark.parametrize("problem", ["transient", "missing", "not_reporting"])
async def test_minor_sensor_does_not_block_fan_samples(
    hass, installed, problem, freezer
):
    from custom_components.house_climate.filtering import SensorFilter

    # Keep this sensor test within one thermal exploration phase.
    freezer.move_to("2026-11-02 12:05:00+00:00")
    c, _, _ = await prepare_fans(hass, installed)
    c.fans.window["dirty"] = False  # Initial HVAC mode change is already observed.
    c.config["sensors"].append({"entity_id": "sensor.minor", "weight": 0.01})
    c.config["priority_modes"]["balance"]["sensor.minor"] = 0.01
    c.filters["sensor.minor"] = SensorFilter()
    c.learning.reconcile(["sensor.room", "sensor.minor"], list(c.learning.systems))
    start = dt_util.utc_from_timestamp(c.fans.window["start"])
    c.filters["sensor.minor"].observe(19, start.timestamp())
    assert not c.sensor_blocks_fans("sensor.minor")
    c.config["minor_weight_share"] = 0.001
    assert c.sensor_blocks_fans("sensor.minor")
    c.config["minor_weight_share"] = 0.05
    for minute in range(1, 31):
        now = start + timedelta(minutes=minute)
        with patch(
            "custom_components.house_climate.coordinator.dt_util.utcnow",
            return_value=now,
        ):
            value = (
                str(15 if minute % 2 else 19)
                if problem == "transient"
                else "unavailable"
                if problem == "missing"
                else "19"
            )
            hass.states.async_set("sensor.minor", value)
            # This member was added after listener registration in the fixture.
            c._sensor_changed(
                SimpleNamespace(
                    data={
                        "entity_id": "sensor.minor",
                        "new_state": hass.states.get("sensor.minor"),
                    }
                )
            )
            if problem == "not_reporting":
                c.reporting.sensors["sensor.minor"]["frozen"] = True
            await hass.async_block_till_done()
            if minute % 5 == 0:
                await c.async_tick()
                if minute < 30:
                    assert not c.fans.window["dirty"]
    assert (
        sum(
            arm["samples"]
            for actions in c.fans.arms.values()
            for buckets in actions.values()
            for arms in buckets.values()
            for arm in arms.values()
        )
        == 1
    )
    assert c.learning.rows["sensor.minor"].samples == 0


async def test_held_step_resolves_without_reports_and_fans_resume(hass, installed):
    from custom_components.house_climate.fans import FanLearning

    c, _, _ = await prepare_fans(hass, installed)
    c.fans = FanLearning()
    start = dt_util.utcnow()
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=start + timedelta(minutes=1),
    ):
        hass.states.async_set("sensor.room", "23")
        await hass.async_block_till_done()
    for minute in (5, 10, 15, 16):
        with patch(
            "custom_components.house_climate.coordinator.dt_util.utcnow",
            return_value=start + timedelta(minutes=minute),
        ):
            await c.async_tick()
        if minute < 16:
            assert c.filters["sensor.room"].blocked
            assert c.fans.window is not None
            assert not c.fans.window["dirty"]
    assert not c.filters["sensor.room"].blocked
    assert c.data.readings["sensor.room"] == 23
    assert c.fans.window is not None
    assert c.filters["sensor.room"].points == [
        [(start + timedelta(minutes=1)).timestamp(), 23]
    ]


async def test_quiet_window_caps_fan_on_first_pass(hass, installed, freezer):
    before = dt_util.now().replace(hour=21, minute=59, second=0, microsecond=0)
    freezer.move_to(dt_util.as_utc(before))
    # Refresh the fixture member at the simulated evening, not the real clock.
    hass.states.async_set("sensor.room", "19.1", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    c, system, calls = await prepare_fans(hass, installed)
    c.config.update(quiet_hours_enabled=True, quiet_start="22:00", quiet_end="07:00")
    c.config["systems"][0]["quiet_max_fan"] = "gentle"
    before = dt_util.now().replace(hour=21, minute=59, second=0, microsecond=0)
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=dt_util.as_utc(before),
    ):
        await system.async_set_fan_mode("strong")
        c.fans.chosen["climate.ducted"]["fan"] = "strong"
        c.fans.window["start"] = before.timestamp()
        c.fans.window["last"] = before.timestamp()
        await hass.async_block_till_done()
    calls.clear()
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=dt_util.as_utc(before + timedelta(minutes=1)),
    ):
        await c.async_tick()
    fan_calls = [
        (service, data)
        for _, service, data in calls
        if service in ("set_fan_mode", "set_swing_mode")
    ]
    assert fan_calls == [("set_fan_mode", {"fan_mode": "gentle"})]
    assert system.fan_mode == "gentle"
    assert c.fans.window["start"] == (before + timedelta(minutes=1)).timestamp()


@pytest.mark.parametrize("action", ["fan", "swing"])
async def test_current_auto_moves_to_learnable_mode(hass, installed, action):
    c, system, calls = await prepare_fans(hass, installed)
    await c.async_reset_learning()
    setattr(system, f"_attr_{action}_modes", ["low", "Auto"])
    setattr(system, f"_attr_{action}_mode", "Auto")
    system.async_write_ha_state()
    await hass.async_block_till_done()
    c.fans.window = None
    c.fans.chosen = {}
    calls.clear()
    await c.async_tick()
    fan_calls = [
        (service, data)
        for _, service, data in calls
        if service in ("set_fan_mode", "set_swing_mode")
    ]
    assert fan_calls == [(f"set_{action}_mode", {f"{action}_mode": "low"})]
    assert "Auto" not in c.fans.arms["climate.ducted"][action][c.fans.window["bucket"]]


async def test_load_reconciles_configured_fan_exclusions(hass, installed):
    c, _, _ = await prepare_fans(hass, installed)
    saved = await c.store.async_load()
    bucket = c.fans.window["bucket"]
    saved["fans"]["arms"]["climate.ducted"]["fan"][bucket]["auto"] = {
        "samples": 10,
        "mean_reward": 1,
    }
    await c.store.async_save(saved)
    await c.async_load()
    assert "auto" not in c.fans.arms["climate.ducted"]["fan"][bucket]
    c.config["learn_exclude_modes"] = "auto, STRONG"
    await c.async_reset_learning()
    assert "strong" not in c.fans.arms["climate.ducted"]["fan"][bucket]


async def test_priority_select_immediate_errors_learning_and_restart(hass, installed):
    entry, _ = installed
    from custom_components.house_climate.coordinator import HouseCoordinator

    settings = deepcopy(dict(entry.options))
    settings["sensors"].append({"entity_id": "sensor.other", "weight": 1})
    settings["priority_modes"] = {
        "balance": {"sensor.room": 3, "sensor.other": 1},
        "guests": {"sensor.room": 1, "sensor.other": 3},
    }
    hass.states.async_set("sensor.other", "23", {"unit_of_measurement": "°C"})
    hass.config_entries.async_update_entry(entry, options=settings)
    await hass.async_block_till_done()
    c = entry.runtime_data
    await c.async_change(mode="heat", setpoint=21, enabled=True)
    c.learning.update(
        {"sensor.room": 19, "sensor.other": 23},
        {"sensor.room": 19.1, "sensor.other": 23.1},
        5,
        {"climate.ducted": 1, "climate.split": 0},
    )
    for row in c.learning.rows.values():
        row.theta[1:3] = [0.03, 0.03]
    before = deepcopy(c.learning.to_dict())
    await c.async_tick(record=False)
    assert c.data.temperature == 20
    assert "House 20.00" in c.data.explanation
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.house_priority_mode", "option": "guests"},
        blocking=True,
    )
    assert c.data.temperature == 22
    assert "House 22.00" in c.data.explanation
    assert {
        k: v
        for k, v in c.learning.to_dict().items()
        if k not in ("effort_history", "effort_clock")
    } == {
        k: v for k, v in before.items() if k not in ("effort_history", "effort_clock")
    }
    assert c.sensor_blocks_fans("sensor.other")
    c.config["minor_weight_share"] = 0.5
    assert c.sensor_blocks_fans("sensor.other")
    assert not c.sensor_blocks_fans("sensor.room")
    house = hass.states.get("climate.house")
    assert house.attributes["priority_mode"] == "guests"
    assert house.attributes["priority_weights"] == settings["priority_modes"]["guests"]
    assert "Priority mode guests" in house.attributes["last_decision"]
    assert hass.states.get("select.house_priority_mode").state == "guests"
    restored = HouseCoordinator(hass, entry)
    await restored.async_load()
    assert restored.priority_mode == "guests"
    assert restored.learning.to_dict() == before
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("select.house_priority_mode").state == "guests"
    assert hass.states.get("climate.house").attributes["current_temperature"] == 22
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(
        result["flow_id"],
        {
            "sensors": ["sensor.room", "sensor.other"],
            "systems": ["climate.ducted", "climate.split"],
            "priority_action": "rename",
        },
    )
    result = await flow.async_configure(
        result["flow_id"], {"mode": "guests", "name": "day"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert hass.states.get("select.house_priority_mode").state == "day"
    assert {
        k: v
        for k, v in entry.runtime_data.learning.to_dict().items()
        if k not in ("effort_history", "effort_clock")
    } == {
        k: v for k, v in before.items() if k not in ("effort_history", "effort_clock")
    }
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(
        result["flow_id"],
        {
            "sensors": ["sensor.room", "sensor.other"],
            "systems": ["climate.ducted", "climate.split"],
            "priority_action": "delete",
        },
    )
    await flow.async_configure(result["flow_id"], {"mode": "day"})
    await hass.async_block_till_done()
    assert hass.states.get("select.house_priority_mode").state == "balance"
    assert {
        k: v
        for k, v in entry.runtime_data.learning.to_dict().items()
        if k not in ("effort_history", "effort_clock")
    } == {
        k: v for k, v in before.items() if k not in ("effort_history", "effort_clock")
    }


@pytest.mark.parametrize("options", [False, True])
async def test_v2_priority_migration_preserves_output(hass, settings, options):
    from custom_components.house_climate import async_migrate_entry
    from custom_components.house_climate.controller import Device, Reading, control
    from custom_components.house_climate.priority import members

    settings["sensors"] += [{"entity_id": "sensor.second", "weight": 4}]
    configured = deepcopy(settings)
    configured["sensors"][0]["weight"] = 2
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, data=settings, options=configured if options else {}
    )
    entry.add_to_hass(hass)
    original = dict(entry.options or entry.data)
    devices = {s["entity_id"]: Device() for s in settings["systems"]}
    readings = {"sensor.room": Reading(19), "sensor.second": Reading(23)}
    before = control(original, ControlState(mode="heat"), readings, devices, 0)
    assert await async_migrate_entry(hass, entry)
    assert entry.options["priority_modes"] == {
        "balance": {s["entity_id"]: s["weight"] for s in original["sensors"]}
    }
    after = control(
        {**entry.options, "sensors": members(entry.options, "balance")},
        ControlState(mode="heat"),
        readings,
        devices,
        0,
    )
    assert after == before


async def finish_priority_wizard(
    flow, result, sensor_count=1, system_count=2, weight=2
):
    for _ in range(sensor_count):
        assert result["step_id"] == "sensor"
        result = await flow.async_configure(result["flow_id"], {"weight": weight})
    if result["type"] == FlowResultType.CREATE_ENTRY:
        return result
    for _ in range(system_count):
        result = await flow.async_configure(result["flow_id"], {"preset": ""})
    tunables = {
        k: v
        for k, v in DEFAULTS.items()
        if k not in ("sensors", "systems", "extra_heat_sources") and v not in ("", None)
    }
    return await flow.async_configure(result["flow_id"], tunables)


async def test_priority_options_add_edit_rename_delete_last_protected(hass, settings):
    entry = MockConfigEntry(domain=DOMAIN, data=settings, version=3)
    entry.add_to_hass(hass)
    flow = hass.config_entries.options
    selection = {
        "sensors": ["sensor.room"],
        "systems": [s["entity_id"] for s in settings["systems"]],
    }

    async def action(name):
        result = await flow.async_init(entry.entry_id)
        return await flow.async_configure(
            result["flow_id"], {**selection, "priority_action": name}
        )

    result = await action("delete")
    result = await flow.async_configure(result["flow_id"], {"mode": "balance"})
    assert result["errors"] == {"base": "last_mode"}
    flow.async_abort(result["flow_id"])
    result = await action("add")
    result = await flow.async_configure(result["flow_id"], {"name": "balance"})
    assert result["errors"] == {"base": "invalid_mode_name"}
    result = await flow.async_configure(result["flow_id"], {"name": "guests"})
    result = await finish_priority_wizard(flow, result, weight=5)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["priority_modes"] == {
        "balance": {"sensor.room": 1},
        "guests": {"sensor.room": 5},
    }
    result = await action("edit")
    result = await flow.async_configure(result["flow_id"], {"mode": "guests"})
    await finish_priority_wizard(flow, result, weight=8)
    assert entry.options["priority_modes"]["guests"]["sensor.room"] == 8
    result = await action("rename")
    result = await flow.async_configure(
        result["flow_id"], {"mode": "guests", "name": "day"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["priority_modes"]["day"] == {"sensor.room": 8}
    result = await action("delete")
    result = await flow.async_configure(result["flow_id"], {"mode": "day"})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert list(entry.options["priority_modes"]) == ["balance"]


async def test_options_new_sensor_all_modes_and_default_edit_active(hass, settings):
    settings["priority_modes"] = {
        "balance": {"sensor.room": 1},
        "night": {"sensor.room": 7},
    }
    entry = MockConfigEntry(domain=DOMAIN, data=settings, version=3)
    entry.runtime_data = SimpleNamespace(priority_mode="night")
    entry.add_to_hass(hass)
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(
        result["flow_id"],
        {
            "sensors": ["sensor.room", "sensor.new"],
            "systems": [s["entity_id"] for s in settings["systems"]],
        },
    )
    fields = {f.schema: f for f in result["data_schema"].schema}
    assert fields["weight"].default() == 7
    result = await flow.async_configure(result["flow_id"], {"weight": 9})
    fields = {f.schema: f for f in result["data_schema"].schema}
    assert fields["weight"].default() == 1
    await finish_priority_wizard(flow, result, weight=1)
    assert entry.options["priority_modes"] == {
        "balance": {"sensor.room": 1, "sensor.new": 1},
        "night": {"sensor.room": 9, "sensor.new": 1},
    }
    result = await flow.async_init(entry.entry_id)
    await complete_flow(flow, result, ["sensor.new"], ["climate.ducted"])
    assert entry.options["priority_modes"] == {
        "balance": {"sensor.new": 1},
        "night": {"sensor.new": 1},
    }


async def test_major_defrost_replay_completes_fan_windows(hass, installed, freezer):
    """v0.2.2 minute replay repeats every 40 minutes for four hours."""
    from custom_components.house_climate.filtering import SensorFilter

    freezer.move_to("2026-11-02 12:05:00+00:00")
    c, _, _ = await prepare_fans(hass, installed)
    c.config["priority_modes"]["balance"]["sensor.room"] = 0.5
    c.config["sensors"].append({"entity_id": "sensor.other", "weight": 1})
    c.config["priority_modes"]["balance"]["sensor.other"] = 1
    c.filters = {key: SensorFilter() for key in ("sensor.room", "sensor.other")}
    c.learning.reconcile(list(c.filters), c.learning.systems)
    c.learning_baseline = None
    hass.states.async_set("sensor.outdoor", "7")
    c.fans.window = None  # Begin after the initial HVAC mode switch.
    start = dt_util.utcnow()
    initial = sum(
        arm["samples"]
        for actions in c.fans.arms.values()
        for buckets in actions.values()
        for arms in buckets.values()
        for arm in arms.values()
    )
    assert c.sensor_blocks_fans("sensor.room")
    transients = 0
    for minute in range(241):
        freezer.move_to(start + timedelta(minutes=minute))
        phase = minute % 40
        value = DEFROST_SEQUENCE[phase] if phase < len(DEFROST_SEQUENCE) else 22
        hass.states.async_set(
            "sensor.room", str(value), {"friendly_name": "living-room"}
        )
        hass.states.async_set("sensor.other", "23.5")
        await hass.async_block_till_done()
        if minute % 5 == 0:
            await c.async_tick()
            if "living-room sensor transient ignored" in c.data.explanation:
                transients += 1
                assert not c.fans.window["dirty"], (minute, c.fans.window)
            assert "Waiting for fan learning context" not in c.fans.last_decision
    count = sum(
        arm["samples"]
        for actions in c.fans.arms.values()
        for buckets in actions.values()
        for arms in buckets.values()
        for arm in arms.values()
    )
    assert transients >= 18
    assert count - initial == 8
    assert any(
        arm["samples"] and "sensor.room" in arm["rooms"]
        for actions in c.fans.arms.values()
        for buckets in actions.values()
        for arms in buckets.values()
        for arm in arms.values()
    )


@pytest.mark.parametrize("cause", ["missing", "stale", "not_reporting", "outdoor"])
async def test_fan_startup_correction_explains_blocker(hass, installed, cause):
    from custom_components.house_climate.fans import FanLearning

    c, system, calls = await prepare_fans(hass, installed)
    c.fans = FanLearning()
    system._attr_fan_mode = "auto"
    system.async_write_ha_state()
    await hass.async_block_till_done()
    if cause == "missing":
        hass.states.async_set("sensor.room", "unavailable")
    elif cause == "stale":
        c.config["stale_after"] = -1
    elif cause == "not_reporting":
        c.reporting.sensors["sensor.room"]["frozen"] = True
    else:
        hass.states.async_set("sensor.outdoor", "unavailable")
    await hass.async_block_till_done()
    calls.clear()
    await c.async_tick()
    assert system.fan_mode in ("gentle", "strong")
    assert any(service == "set_fan_mode" for _, service, _ in calls)
    expected = {
        "missing": "sensor.room missing",
        "stale": "sensor.room stale",
        "not_reporting": "sensor.room not reporting",
        "outdoor": "missing outdoor",
    }[cause]
    assert expected in c.fans.last_decision
    assert expected in c.data.explanation
    attrs = hass.states.get("sensor.house_fan_learning").attributes
    assert expected in attrs["wait_reason"]
    # The subsequent no-command return must refresh the explanation too.
    await c.async_tick()
    assert expected in c.fans.last_decision


async def test_excitation_dispatch_between_tick_runtime_and_restart(hass, installed):
    from custom_components.house_climate.coordinator import HouseCoordinator

    entry, calls = installed
    coordinator = entry.runtime_data
    start = dt_util.utcnow()
    for system in coordinator.config["systems"]:
        key = system["entity_id"]
        entity = hass.data[DATA_COMPONENT].get_entity(key)
        entity._attr_current_temperature = 24
        entity.async_write_ha_state()
    await hass.async_block_till_done()
    coordinator.state.last_run = {
        system["entity_id"]: start.timestamp() - 3 * 3600
        for system in coordinator.config["systems"]
    }
    coordinator.state.last_stop = {
        key: start.timestamp() - 3 * 3600 for key in coordinator.state.last_run
    }
    coordinator.state.test_observed_since = dict(coordinator.state.last_run)
    with (
        patch(
            "custom_components.house_climate.coordinator.dt_util.utcnow",
            return_value=start,
        ),
        patch(
            "custom_components.house_climate.controller.allocate",
            return_value=({"climate.ducted": 0, "climate.split": 0}, 22.5),
        ),
    ):
        await coordinator.async_change(setpoint=22.5, mode="heat", enabled=True)
    assert coordinator.state.excitation["system"] == "climate.ducted"
    assert any(
        key == "climate.ducted"
        and service == "set_temperature"
        and data["temperature"] == 25
        for key, service, data in calls
    )
    assert (
        "testing ducted for 60 min to learn its effect on"
        in coordinator.data.explanation
    )
    # Changes during commands under the coordinator lock still count accurately.
    entity = hass.data[DATA_COMPONENT].get_entity("climate.ducted")
    async with coordinator.lock:
        for minutes, action in [(1, HVACAction.HEATING), (11, HVACAction.IDLE)]:
            with patch(
                "custom_components.house_climate.coordinator.dt_util.utcnow",
                return_value=start + timedelta(minutes=minutes),
            ):
                entity._attr_hvac_action = action
                entity.async_write_ha_state()
                await hass.async_block_till_done()
    assert coordinator.state.excitation["runtime"] == 0
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=start + timedelta(minutes=12),
    ):
        entity._attr_hvac_action = HVACAction.HEATING
        entity.async_write_ha_state()
        await hass.async_block_till_done()
        await coordinator.async_tick(record=False)
    restored = HouseCoordinator(hass, entry)
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=start + timedelta(hours=3),
    ):
        await restored.async_load()
    assert not restored.state.excitation
    assert restored.state.actions == coordinator.state.actions
    assert set(restored.state.action_times.values()) == {
        (start + timedelta(hours=3)).timestamp()
    }
    assert restored.state.last_run == coordinator.state.last_run
    assert (
        restored.state.last_excite["climate.ducted"]
        == (start + timedelta(hours=3)).timestamp()
    )


async def test_options_onboard_sources(hass, settings):
    # An existing source without response must keep the historical default.
    settings["extra_heat_sources"] = [
        {"entity_id": "switch.source", "kind": "binary", "threshold": 10}
    ]
    entry = MockConfigEntry(domain=DOMAIN, data=settings, version=3)
    entry.add_to_hass(hass)
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(
        result["flow_id"],
        {
            "sensors": ["sensor.room"],
            "systems": ["climate.ducted"],
            "extra_heat_sources": ["switch.source", "sensor.power"],
        },
    )
    result = await flow.async_configure(result["flow_id"], {"weight": 1})
    result = await flow.async_configure(result["flow_id"], {"preset": ""})
    assert result["step_id"] == "source"
    result = await flow.async_configure(
        result["flow_id"], {"kind": "binary", "threshold": 10}
    )
    fields = {k.schema: k for k in result["data_schema"].schema}
    assert fields["kind"].default() == "power"
    assert fields["threshold"].default() == 10
    assert fields["response"].default() == "on_off"
    result = await flow.async_configure(
        result["flow_id"],
        {"kind": "power", "threshold": 25, "response": "proportional"},
    )
    tunables = {
        k: v
        for k, v in DEFAULTS.items()
        if k not in ("sensors", "systems", "extra_heat_sources") and v not in ("", None)
    }
    result = await flow.async_configure(result["flow_id"], tunables)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["extra_heat_sources"] == [
        {
            "entity_id": "switch.source",
            "kind": "binary",
            "threshold": 10,
            "response": "on_off",
        },
        {
            "entity_id": "sensor.power",
            "kind": "power",
            "threshold": 25,
            "response": "proportional",
        },
    ]


async def test_source_events_model_reload_and_missing(hass, installed):
    entry, _ = installed
    c = entry.runtime_data
    retained = deepcopy(c.learning.to_dict()["rows"]["sensor.room"])
    settings = deepcopy(dict(entry.options))
    settings["extra_heat_sources"] = [
        {"entity_id": "switch.source", "kind": "binary"},
        {"entity_id": "sensor.power", "kind": "power", "threshold": 10},
    ]
    settings["outdoor_sensor"] = "sensor.outdoor"
    hass.states.async_set("sensor.outdoor", "5")
    hass.states.async_set("switch.source", "off")
    hass.states.async_set("sensor.power", "0.01", {"unit_of_measurement": "kW"})
    hass.config_entries.async_update_entry(entry, options=settings)
    await hass.async_block_till_done()
    c = entry.runtime_data
    assert c.learning.rows["sensor.room"].theta[:3] == retained["theta"][:3]
    assert c.learning.rows["sensor.room"].samples == retained["samples"]
    start = dt_util.utcnow()
    # Reload establishes both interval endpoints; start explicitly at this time.
    c.reset_learning_interval()
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow", return_value=start
    ):
        await c.async_tick()
    for seconds, state in [(60, "on"), (210, "off")]:
        with patch(
            "custom_components.house_climate.coordinator.dt_util.utcnow",
            return_value=start + timedelta(seconds=seconds),
        ):
            async with c.lock:
                hass.states.async_set("switch.source", state)
                hass.states.async_set(
                    "sensor.power",
                    "0.02" if state == "on" else "0.01",
                    {"unit_of_measurement": "kW"},
                )
                await hass.async_block_till_done()
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=start + timedelta(minutes=5),
    ):
        await c.async_tick()
    assert c.samples[-1]["extra_heat_sources"] == {
        "switch.source": 0.5,
        "sensor.power": 0.5,
    }
    assert c.learning.rows["sensor.room"].samples == 1
    assert set(hass.states.get("sensor.house_learning").attributes["sources"]) == {
        "switch.source",
        "sensor.power",
    }
    before = deepcopy(c.learning.to_dict())
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=start + timedelta(minutes=6),
    ):
        hass.states.async_set("switch.source", "unavailable")
        await hass.async_block_till_done()
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=start + timedelta(minutes=10),
    ):
        await c.async_tick()
    assert {
        k: v
        for k, v in c.learning.to_dict().items()
        if k not in ("effort_history", "effort_clock")
    } == {
        k: v for k, v in before.items() if k not in ("effort_history", "effort_clock")
    }
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=start + timedelta(minutes=10),
    ):
        assert await hass.config_entries.async_reload(entry.entry_id)
    assert {
        k: v
        for k, v in entry.runtime_data.learning.to_dict().items()
        if k not in ("effort_history", "effort_clock")
    } == {
        k: v for k, v in before.items() if k not in ("effort_history", "effort_clock")
    }
    settings = deepcopy(settings)
    settings["extra_heat_sources"] = []
    hass.config_entries.async_update_entry(entry, options=settings)
    await hass.async_block_till_done()
    assert entry.runtime_data.learning.rows["sensor.room"].samples == 1
    assert len(entry.runtime_data.learning.rows["sensor.room"].theta) == 9


@pytest.mark.parametrize("hours", [0, 1])
async def test_learning_pause_switch_freezes_resumes_and_persists(
    hass, installed, hours
):
    entry, _ = installed
    c, _, calls = await prepare_fans(hass, installed)
    c.config["learning_pause_hours"] = hours
    start = dt_util.utcnow()
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow", return_value=start
    ):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": "switch.house_learning"}, blocking=True
        )
    assert hass.states.get("switch.house_learning").state == "off"
    before = deepcopy(c.learning.to_dict())
    arms = deepcopy(c.fans.arms)
    deadline = start.timestamp() + 3600 if hours else None
    assert c.learning_resume_at == deadline
    calls.clear()
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=start + timedelta(minutes=30),
    ):
        await c.async_change(setpoint=23, enabled=True)
        await c.async_tick()
    assert any(service == "set_temperature" for _, service, _ in calls)
    assert {
        k: v
        for k, v in c.learning.to_dict().items()
        if k not in ("effort_history", "effort_clock")
    } == {
        k: v for k, v in before.items() if k not in ("effort_history", "effort_clock")
    }
    assert c.fans.arms == arms
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=start + timedelta(minutes=31),
    ):
        assert await hass.config_entries.async_reload(entry.entry_id)
    c = entry.runtime_data
    assert not c.learning_enabled
    assert c.learning_resume_at == deadline
    c.config["outdoor_sensor"] = "sensor.outdoor"
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=start + timedelta(hours=1),
    ):
        await c.async_tick()
        if hours == 0:
            assert not c.learning_enabled
            await hass.services.async_call(
                "switch",
                "turn_on",
                {"entity_id": "switch.house_learning"},
                blocking=True,
            )
            await c.async_tick()
    assert c.learning_enabled
    assert c.learning_resume_at is None
    # Auto-resume establishes a fresh baseline; no paused interval can train.
    assert {
        k: v
        for k, v in c.learning.to_dict().items()
        if k not in ("effort_history", "effort_clock")
    } == {
        k: v for k, v in before.items() if k not in ("effort_history", "effort_clock")
    }
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=start + timedelta(minutes=65),
    ):
        await c.async_tick()
    assert (
        c.learning.rows["sensor.room"].samples
        == before["rows"]["sensor.room"]["samples"] + 1
    )


@pytest.mark.parametrize("weight", [0.01, 1])
async def test_disturbance_skips_room_learning_and_weight_gates_fans(
    hass, installed, weight
):
    from custom_components.house_climate.filtering import SensorFilter

    c, _, _ = await prepare_fans(hass, installed)
    c.config["sensors"].append({"entity_id": "sensor.other", "weight": 1})
    c.config["priority_modes"]["balance"] = {"sensor.room": weight, "sensor.other": 1}
    c.learning.reconcile(["sensor.room", "sensor.other"], c.learning.systems)
    for row in c.learning.rows.values():
        row.counts = [100, 100]
        row.theta = [0] * 9
        row.covariance = [[0.001 if i == j else 0 for j in range(9)] for i in range(9)]
    start = dt_util.utcnow()
    c.learning_baseline = ({"sensor.room": 19, "sensor.other": 19}, 3, 0)
    c.fans.window["dirty"] = False
    for step in range(1, 4):
        now = start + timedelta(minutes=5 * step)
        with patch(
            "custom_components.house_climate.coordinator.dt_util.utcnow",
            return_value=now,
        ):
            hass.states.async_set(
                "sensor.room", str(19 + step), {"friendly_name": "Guest Bedroom"}
            )
            hass.states.async_set("sensor.other", "19")
            await hass.async_block_till_done()
            for key, value in (("sensor.room", 19 + step), ("sensor.other", 19)):
                c.filters[key] = SensorFilter(minutes=1)
                c.filters[key].observe(value, now.timestamp() - 60)
            await c.async_tick()
        if step == 2:
            frozen = deepcopy(c.learning.rows["sensor.room"])
    assert c.learning.disturbed == {"sensor.room": "heat"}
    assert c.learning.rows["sensor.room"] == frozen
    assert c.learning.rows["sensor.other"].samples == 3
    assert c.fans.window["dirty"] is (weight == 1)
    assert (
        "unexplained heat in Guest Bedroom; not learning from it" in c.data.explanation
    )
    assert hass.states.get("climate.house").attributes["disturbed"] == {
        "sensor.room": "heat"
    }
    assert c.data.readings["sensor.room"] == 22  # Still used by control.


async def test_last_run_seeds_from_full_history_before_control_and_persists(
    hass, settings
):
    from homeassistant.helpers.recorder import DATA_INSTANCE

    from custom_components.house_climate.coordinator import HouseCoordinator

    entry = MockConfigEntry(domain=DOMAIN, data=settings)
    entry.add_to_hass(hass)
    c = HouseCoordinator(hass, entry)
    now = dt_util.utcnow()
    last_stop = now - timedelta(hours=14)
    c.state.last_run["climate.split"] = now.timestamp() - 60
    activity = {}
    for key in ("climate.ducted", "climate.split"):
        hass.states.async_set(
            key,
            "heat",
            {"hvac_action": "idle", "temperature": 19, "current_temperature": 24},
        )
        activity[key] = [
            State(
                key,
                "heat",
                {"hvac_action": "heating"},
                last_updated=last_stop - timedelta(hours=1),
            ),
            State(
                key,
                "heat",
                {"hvac_action": "cooling"},
                last_updated=last_stop - timedelta(minutes=30),
            ),
            State(key, "heat", {"hvac_action": "idle"}, last_updated=last_stop),
            State(key, "unavailable", last_updated=now - timedelta(minutes=5)),
        ]

    class Recorder:
        async def async_add_executor_job(self, job):
            return job()

    hass.data[DATA_INSTANCE] = Recorder()
    try:
        with patch(
            "homeassistant.components.recorder.history.get_significant_states",
            side_effect=[{}, activity],
        ) as history:
            await c.async_seed_history(now)
        assert history.call_args.args[1] == dt_util.utc_from_timestamp(0)
        assert c.state.last_run == {
            "climate.ducted": last_stop.timestamp(),
            "climate.split": now.timestamp() - 60,
        }
        assert not c.reporting.intervals  # Old activity is outside frozen window.
        c.history_seeded = True
        c.restored = True
        c.state.mode = "heat"
        c.state.setpoint = 22.5
        hass.states.async_set("sensor.room", "19")
        with (
            patch("homeassistant.core.ServiceRegistry.async_call"),
            patch(
                "custom_components.house_climate.controller.allocate",
                return_value=({"climate.ducted": 0, "climate.split": 0}, 22.5),
            ),
        ):
            await c.async_tick()
        assert c.state.excitation["system"] == "climate.ducted"
        restored = HouseCoordinator(hass, entry)
        await restored.async_load()
        assert restored.state.last_run == c.state.last_run
        assert not restored.state.excitation
    finally:
        hass.data.pop(DATA_INSTANCE)


async def test_modelled_heat_keeps_thermal_samples_and_fan_window_valid(
    hass, installed
):
    from custom_components.house_climate.filtering import SensorFilter

    c, _, _ = await prepare_fans(hass, installed)
    c.config["extra_heat_sources"] = [{"entity_id": "switch.source", "kind": "binary"}]
    hass.states.async_set("switch.source", "on")
    c.learning.reconcile(["sensor.room"], c.learning.systems, ["switch.source"])
    row = c.learning.rows["sensor.room"]
    row.theta = [0, 0, 0, 0.8, 0, 0, 0, 0, 0, 0, 0, 0]
    row.counts = [100, 100, 100]
    start = dt_util.utc_from_timestamp(c.fans.window["start"])
    c.source_effort.finish({"switch.source": "heating"}, start.timestamp())
    c.learning_baseline = ({"sensor.room": 19}, 3, 0)
    c.fans.window["dirty"] = False
    for step in range(1, 7):
        now = start + timedelta(minutes=5 * step)
        value = 19 + 0.8 * step
        with patch(
            "custom_components.house_climate.coordinator.dt_util.utcnow",
            return_value=now,
        ):
            hass.states.async_set("sensor.room", str(value))
            await hass.async_block_till_done()
            c.filters["sensor.room"] = SensorFilter(minutes=1)
            c.filters["sensor.room"].observe(value, now.timestamp() - 60)
            await c.async_tick()
        assert not c.learning.disturbed
        assert not c.fans.window["dirty"]
    assert row.samples == 6
    assert (
        sum(
            arm["samples"]
            for actions in c.fans.arms.values()
            for buckets in actions.values()
            for arms in buckets.values()
            for arm in arms.values()
        )
        == 1
    )


async def test_pause_deadline_expired_during_restart_resumes_without_sample(
    hass, installed
):
    entry, _ = installed
    c = entry.runtime_data
    c.config["learning_pause_hours"] = 1
    start = dt_util.utcnow()
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow", return_value=start
    ):
        await c.async_change(learning_enabled=False)
    before = deepcopy(c.learning.to_dict())
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=start + timedelta(hours=2),
    ):
        assert await hass.config_entries.async_reload(entry.entry_id)
    c = entry.runtime_data
    assert c.learning_enabled
    assert c.learning_resume_at is None
    assert {
        k: v
        for k, v in c.learning.to_dict().items()
        if k not in ("effort_history", "effort_clock")
    } == {
        k: v for k, v in before.items() if k not in ("effort_history", "effort_clock")
    }
    assert hass.states.get("switch.house_learning").state == "on"


async def test_recorder_running_tail_uses_observation_without_inventing_downtime(
    hass, settings
):
    from homeassistant.helpers.recorder import DATA_INSTANCE

    from custom_components.house_climate.coordinator import HouseCoordinator

    entry = MockConfigEntry(domain=DOMAIN, data=settings)
    entry.add_to_hass(hass)
    c = HouseCoordinator(hass, entry)
    now = dt_util.utcnow()
    observed = now - timedelta(hours=14)

    class Recorder:
        async def async_add_executor_job(self, job):
            return job()

    hass.data[DATA_INSTANCE] = Recorder()
    try:
        with patch(
            "homeassistant.components.recorder.history.get_significant_states",
            side_effect=[
                {},
                {
                    "climate.ducted": [
                        State(
                            "climate.ducted",
                            "heat",
                            {"hvac_action": "heating"},
                            last_updated=observed,
                        )
                    ]
                },
            ],
        ):
            await c.async_seed_history(now)
        assert c.state.last_run["climate.ducted"] == observed.timestamp()
    finally:
        hass.data.pop(DATA_INSTANCE)


async def test_room_learning_entities_repairs_and_restore(hass, installed):
    from homeassistant.helpers import issue_registry as ir

    from custom_components.house_climate.coordinator import HouseCoordinator

    entry, _ = installed
    c = entry.runtime_data
    now = dt_util.utcnow().timestamp()
    row = c.learning.rows["sensor.room"]
    row.theta[0] = 0.021
    c.learning.regimes["sensor.room"]["challenger"].theta[0] = 0.047
    c.learning.disturbed["sensor.room"] = "cooling"
    c.learning.regimes["sensor.room"]["disturbed_since"] = now - 12 * 3600
    await c.async_tick(record=False)
    issue_id = f"{entry.entry_id}_sensor.room_regime"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.room_accept_new_normal"}, blocking=True
    )
    assert c.learning.rows["sensor.room"].theta[0] == 0.047
    assert c.learning.regimes["sensor.room"]["saved"][0]["row"].theta[0] == 0.021
    assert not c.learning.disturbed
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    diagnostics = hass.states.get("sensor.house_learning").attributes["sensors"][
        "sensor.room"
    ]
    assert diagnostics["active_model"].startswith("promoted at")
    assert diagnostics["saved_models"][0]["last_fit"] is not None
    assert "challenger_advantage" in diagnostics
    c.config["temporary_hours"] = 1
    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": "switch.room_temporary_change"},
        blocking=True,
    )
    assert hass.states.get("switch.room_temporary_change").state == "on"
    deadline = c.learning.regimes["sensor.room"]["temporary_until"]
    assert deadline == pytest.approx(now + 3600, abs=1)
    frozen = deepcopy(c.learning.rows["sensor.room"])
    challenger = deepcopy(c.learning.regimes["sensor.room"]["challenger"])
    c.learning.update(
        {"sensor.room": 20},
        {"sensor.room": 22},
        5,
        {"climate.ducted": 1, "climate.split": 0},
        now=now + 100,
    )
    assert c.learning.rows["sensor.room"] == frozen
    assert c.learning.regimes["sensor.room"]["challenger"] == challenger
    await c.async_tick(record=False)
    restored = HouseCoordinator(hass, entry)
    await restored.async_load()
    assert restored.learning.to_dict() == c.learning.to_dict()
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=dt_util.utc_from_timestamp(deadline + 1),
    ):
        await c.async_tick(record=False)
    assert hass.states.get("switch.room_temporary_change").state == "off"
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.room_relearn_room"}, blocking=True
    )
    assert c.learning.rows["sensor.room"].samples == 0
    assert c.learning.rows["sensor.room"].theta[0] == 0
    assert c.learning.regimes["sensor.room"]["saved"][0]["row"].theta[0] == 0.021
    # A settled room clears its repair independently of a promotion.
    c.learning.regimes["sensor.room"]["disturbed_since"] = now - 13 * 3600
    await c.async_tick(record=False)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    c.learning.regimes["sensor.room"]["disturbed_since"] = None
    await c.async_tick(record=False)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_room_controls_follow_sensor_membership(hass, installed):
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers import issue_registry as ir

    entry, _ = installed
    c = entry.runtime_data
    c.learning.regimes["sensor.room"]["disturbed_since"] = (
        dt_util.utcnow().timestamp() - 13 * 3600
    )
    await c.async_tick(record=False)
    options = deepcopy(dict(entry.options or entry.data))
    options["sensors"] = [{"entity_id": "sensor.guest", "weight": 1}]
    hass.states.async_set("sensor.guest", "20", {"friendly_name": "Guest bedroom"})
    hass.config_entries.async_update_entry(entry, options=options)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    for entity_id in (
        "button.room_accept_new_normal",
        "button.room_relearn_room",
        "switch.room_temporary_change",
    ):
        assert registry.async_get(entity_id) is None
        assert hass.states.get(entity_id) is None
    for entity_id in (
        "button.guest_bedroom_accept_new_normal",
        "button.guest_bedroom_relearn_room",
        "switch.guest_bedroom_temporary_change",
    ):
        assert hass.states.get(entity_id) is not None
    assert set(entry.runtime_data.learning.regimes) == {
        "sensor.guest",
        "climate.ducted",
        "climate.split",
    }
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"{entry.entry_id}_sensor.room_regime"
        )
        is None
    )


@pytest.mark.parametrize("capped", [False, True])
async def test_cycling_history_and_store_restart_keep_constraints(
    hass, settings, capped
):
    from homeassistant.helpers.recorder import DATA_INSTANCE

    from custom_components.house_climate.controller import cycling_hold
    from custom_components.house_climate.coordinator import HouseCoordinator

    entry = MockConfigEntry(domain=DOMAIN, data=settings)
    entry.add_to_hass(hass)
    c = HouseCoordinator(hass, entry)
    now = dt_util.utcnow()
    timestamp = now.timestamp()

    def recorded(key, action, minutes):
        return State(
            key,
            "heat",
            {"hvac_action": action},
            last_updated=now - timedelta(minutes=minutes),
        )

    activity = {
        "climate.ducted": [
            recorded("climate.ducted", "idle", 20),
            recorded("climate.ducted", "heating", 4),
            recorded("climate.ducted", "heating", 1),
        ],
        "climate.split": [
            recorded("climate.split", "heating", 20),
            recorded("climate.split", "idle", 2),
        ],
    }
    if capped:
        activity["climate.split"] = [
            recorded("climate.split", action, minute)
            for minute, action in [
                (58, "heating"),
                (48, "idle"),
                (38, "heating"),
                (28, "idle"),
                (18, "heating"),
                (8, "idle"),
            ]
        ]

    class Recorder:
        async def async_add_executor_job(self, job):
            return job()

    hass.data[DATA_INSTANCE] = Recorder()
    try:
        with patch(
            "homeassistant.components.recorder.history.get_significant_states",
            side_effect=[{}, activity],
        ):
            await c.async_seed_history(now)
        assert c.state.last_start["climate.ducted"] == timestamp - 240
        assert cycling_hold(c.config, c.state, "climate.ducted", timestamp) is True
        assert cycling_hold(c.config, c.state, "climate.split", timestamp) is False
        if capped:
            assert (
                c.state.cycling_status("climate.split", timestamp)["starts_last_hour"]
                == 3
            )
        await c.store.async_save({"state": asdict(c.state)})
        restored = HouseCoordinator(hass, entry)
        with patch(
            "custom_components.house_climate.coordinator.dt_util.utcnow",
            return_value=now,
        ):
            await restored.async_load()
        for key, action in [("climate.ducted", "heating"), ("climate.split", "idle")]:
            restored.state.observe_action(key, action, timestamp + 60)
            assert restored.state.last_start[key] == c.state.last_start[key]
            assert restored.state.last_stop.get(key) == c.state.last_stop.get(key)
            assert restored.state.starts[key] == c.state.starts[key]
        assert (
            cycling_hold(
                restored.config, restored.state, "climate.ducted", timestamp + 60
            )
            is True
        )
        assert (
            cycling_hold(
                restored.config, restored.state, "climate.split", timestamp + 60
            )
            is False
        )
    finally:
        hass.data.pop(DATA_INSTANCE)


async def test_climate_cycling_attributes_follow_reported_actions(hass, installed):
    entry, _ = installed
    c = entry.runtime_data
    now = dt_util.utcnow().timestamp()
    c.state.observe_action("climate.ducted", "heating", now - 120)
    c.state.observe_action("climate.split", "heating", now - 600)
    c.state.observe_action("climate.split", "idle", now - 60)
    c.async_update_listeners()
    await hass.async_block_till_done()
    cycling = hass.states.get("climate.house").attributes["cycling"]
    assert cycling["climate.ducted"]["starts_last_hour"] == 1
    assert cycling["climate.ducted"]["run_minutes"] == pytest.approx(2, abs=0.1)
    assert cycling["climate.split"]["off_minutes"] == pytest.approx(1, abs=0.1)


async def test_newer_recorder_stop_replaces_stale_store_action_without_retiming(
    hass, settings
):
    from homeassistant.helpers.recorder import DATA_INSTANCE

    from custom_components.house_climate.coordinator import HouseCoordinator

    entry = MockConfigEntry(domain=DOMAIN, data=settings)
    entry.add_to_hass(hass)
    c = HouseCoordinator(hass, entry)
    now = dt_util.utcnow()
    key = "climate.ducted"
    c.state.observe_action(key, "heating", now.timestamp() - 1200)
    activity = {
        key: [
            State(
                key,
                "heat",
                {"hvac_action": "heating"},
                last_updated=now - timedelta(minutes=20),
            ),
            State(
                key,
                "heat",
                {"hvac_action": "idle"},
                last_updated=now - timedelta(minutes=2),
            ),
        ]
    }

    class Recorder:
        async def async_add_executor_job(self, job):
            return job()

    hass.data[DATA_INSTANCE] = Recorder()
    try:
        with patch(
            "homeassistant.components.recorder.history.get_significant_states",
            side_effect=[{}, activity],
        ):
            await c.async_seed_history(now)
        c.state.observe_action(key, "idle", now.timestamp())
        assert c.state.last_stop[key] == now.timestamp() - 120
        assert c.state.actions[key] == "idle"
    finally:
        hass.data.pop(DATA_INSTANCE)


async def test_outage_report_survives_restart_without_commands_or_learning(
    hass, installed
):
    from custom_components.house_climate.coordinator import HouseCoordinator

    entry, calls = installed
    c = entry.runtime_data
    key = "climate.ducted"
    c.enabled = True
    c.state.mode = c.state.active_mode = "heat"
    c.config["systems"][0]["modulating"] = True
    hass.states.async_set(
        key,
        "heat",
        {
            "hvac_action": "heating",
            "temperature": 20.4,
            "current_temperature": 20,
            "friendly_name": "Home ZONE 1",
        },
    )
    await hass.async_block_till_done()
    # Reports between control ticks must also be retained.
    assert c.state.reported_efforts[key] == pytest.approx(0.4)
    hass.states.async_set(key, "unavailable")
    await hass.async_block_till_done()
    calls.clear()
    await c.async_tick()
    assert not any(entity == key for entity, _, _ in calls)
    assert c.samples[-1]["efforts"][key] is None
    restored = HouseCoordinator(hass, entry)
    await restored.async_load()
    calls.clear()
    await restored.async_tick()
    assert restored.state.reported_actions[key] == "heating"
    assert restored.state.reported_efforts[key] == pytest.approx(0.4)
    assert (
        "Home ZONE 1 unavailable; predicted own temperature 20.0 °C vs its target 20.4 °C, assuming heating until it reaches it"
        in restored.data.explanation
    )
    assert restored.samples[-1]["efforts"][key] is None
    assert not any(entity == key for entity, _, _ in calls)


async def test_unavailable_thermostat_uses_independent_sensor_and_persists_latch(
    hass, installed
):
    from custom_components.house_climate.coordinator import HouseCoordinator

    entry, calls = installed
    c = entry.runtime_data
    key = "climate.ducted"
    c.config["systems"][0]["own_sensor"] = "sensor.independent"
    c.state.mode = c.state.active_mode = "heat"
    hass.states.async_set(
        key,
        "heat",
        {
            "hvac_action": "heating",
            "temperature": 21,
            "current_temperature": 20,
        },
    )
    await hass.async_block_till_done()
    hass.states.async_set("sensor.independent", "22")
    hass.states.async_set(key, "unavailable")
    await hass.async_block_till_done()
    calls.clear()
    c.enabled = True
    await c.async_tick()
    assert c.state.thermostats[key]["own"] == 22
    assert c.state.thermostats[key]["running"] is False
    assert (
        "predicted own temperature 22.0 °C vs its target 21 °C, assuming off"
        in c.data.explanation
    )
    assert c.samples[-1]["efforts"][key] is None
    assert not any(entity == key for entity, _, _ in calls)
    restored = HouseCoordinator(hass, entry)
    await restored.async_load()
    assert restored.state.thermostats[key] == c.state.thermostats[key]
    assert key in restored.learning.rows
    assert key not in c.data.readings  # Own models never change comfort membership.


async def test_upgrade_seeds_outage_thermostat_from_samples_without_recorder(
    hass, installed
):
    from custom_components.house_climate.coordinator import HouseCoordinator

    entry, _ = installed
    c = entry.runtime_data
    saved = await c.store.async_load()
    saved["state"].pop("thermostats", None)
    saved["state"].pop("thermostat_points", None)
    now = dt_util.utcnow().timestamp()
    saved["samples"] = [
        {
            "timestamp": now - 600,
            "systems": {
                "climate.ducted": {
                    "available": True,
                    "mode": "heat",
                    "temperature": 25,
                    "current_temperature": 25,
                    "hvac_action": "idle",
                }
            },
        },
        {
            "timestamp": now - 300,
            "systems": {
                "climate.ducted": {
                    "available": True,
                    "mode": "heat",
                    "temperature": 25,
                    "current_temperature": 24.3,
                    "hvac_action": "heating",
                }
            },
        },
        {"timestamp": now, "systems": {"climate.ducted": {"available": False}}},
    ]
    await c.store.async_save(saved)
    restored = HouseCoordinator(hass, entry)
    await restored.async_load()
    thermostat = restored.state.thermostats["climate.ducted"]
    assert thermostat["own"] == 24.3
    assert thermostat["target"] == 25
    assert thermostat["time"] == now - 300
    assert thermostat["running"] is True
    assert restored.state.thermostat_points["climate.ducted"] == pytest.approx([0.7])


@pytest.mark.parametrize("options", [False, True])
@pytest.mark.parametrize("average_weight", [None, 0.2])
async def test_v3_average_weight_migration(hass, settings, options, average_weight):
    from custom_components.house_climate import async_migrate_entry

    settings.pop("average_weight", None)
    settings["fan_reward_lambda"] = 3.0
    if average_weight is not None:
        settings["average_weight"] = average_weight
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data=settings,
        options=deepcopy(settings) if options else {},
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 4
    assert entry.options["average_weight"] == (
        1.0 if average_weight is None else average_weight
    )
    assert entry.options["fan_reward_lambda"] == 3.0


@pytest.mark.parametrize("override,expected", [(None, 0.2), (0, 0), (3, 3)])
async def test_fan_weight_follows_allocator_on_load_and_reset(
    hass, settings, override, expected
):
    from custom_components.house_climate.coordinator import HouseCoordinator

    settings.update(average_weight=0.2)
    settings.pop("fan_reward_lambda", None)
    if override is not None:
        settings["fan_reward_lambda"] = override
    entry = MockConfigEntry(domain=DOMAIN, data=settings, version=4)
    entry.add_to_hass(hass)
    c = entry.runtime_data = HouseCoordinator(hass, entry)
    assert c.fans.penalty == expected
    await c.store.async_save({"state": asdict(c.state), "fans": c.fans.to_dict()})
    await c.async_load()
    assert c.fans.penalty == expected
    with patch.object(c, "async_tick"):
        await c.async_reset_learning()
    assert c.fans.penalty == expected


@pytest.mark.parametrize("override", [None, 0, 3])
async def test_options_average_and_fan_override(hass, settings, override):
    settings["fan_reward_lambda"] = 4
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data=settings, version=4)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await complete_flow(
        hass.config_entries.options,
        result,
        ["sensor.room"],
        ["climate.ducted"],
        average_weight=0.2,
        fan_reward_lambda=override,
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["average_weight"] == 0.2
    assert entry.options["fan_reward_lambda"] == override


@pytest.mark.parametrize(
    "unit,value", [("W", "150"), ("kW", "0.15"), ("MW", "0.00015")]
)
async def test_proportional_source_events_and_response_reset(
    hass, installed, unit, value
):
    entry, _ = installed
    settings = deepcopy(dict(entry.options))
    source = {
        "entity_id": "sensor.server",
        "kind": "power",
        "threshold": 10,
        "response": "proportional",
    }
    settings["extra_heat_sources"] = [source]
    hass.states.async_set("sensor.server", value, {"unit_of_measurement": unit})
    hass.config_entries.async_update_entry(entry, options=settings)
    await hass.async_block_till_done()
    c = entry.runtime_data
    assert c.source_value(source) == pytest.approx(0.15)
    start = dt_util.utcnow()
    c.reset_learning_interval()
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow", return_value=start
    ):
        await c.async_tick()
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=start + timedelta(seconds=150),
    ):
        async with c.lock:
            hass.states.async_set("sensor.server", "200", {"unit_of_measurement": "W"})
            await hass.async_block_till_done()
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=start + timedelta(seconds=300),
    ):
        await c.async_tick()
    assert c.samples[-1]["extra_heat_sources"]["sensor.server"] == pytest.approx(0.175)
    attrs = hass.states.get("sensor.house_learning").attributes["sources"][
        "sensor.server"
    ]
    assert attrs["response"] == "proportional"
    assert attrs["effort"] == pytest.approx(0.2)
    hass.states.async_set("sensor.server", "5", {"unit_of_measurement": "W"})
    assert c.source_value(source) == 0
    hass.states.async_set("sensor.server", "unknown")
    assert c.source_value(source) is None
    hass.states.async_set("sensor.server", "200", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    row = c.learning.rows["sensor.room"]
    row.theta[3] = 1.7
    row.theta[6] = 0.012
    row.covariance[6][1] = row.covariance[1][6] = 0.02
    row.counts[2] = 60
    row.covariance[3][0] = row.covariance[0][3] = 0.4
    # Persist a fitted column before changing its units via an options reload.
    await c.async_tick(record=False)
    retained = deepcopy(row.theta)
    settings = deepcopy(settings)
    settings["extra_heat_sources"][0]["response"] = "on_off"
    hass.config_entries.async_update_entry(entry, options=settings)
    await hass.async_block_till_done()
    c = entry.runtime_data
    row = c.learning.rows["sensor.room"]
    assert row.theta[:3] == retained[:3]
    assert row.theta[4:6] == retained[4:6]
    assert row.theta[7:] == retained[7:]
    assert row.theta[6] == 0
    assert row.covariance[6][6] == c.learning.interaction_prior
    assert row.covariance[6][1] == row.covariance[1][6] == 0
    assert row.theta[3] == 0
    assert row.counts[2] == 0
    assert row.covariance[3][3] == 100
    assert row.covariance[3][0] == row.covariance[0][3] == 0
    assert c.source_value(settings["extra_heat_sources"][0]) == 1


async def test_interaction_diagnostics_include_zero_weight_rooms_and_device_rows(
    hass, installed
):
    from custom_components.house_climate.filtering import SensorFilter

    entry, _ = installed
    c = entry.runtime_data
    c.config["sensors"].append({"entity_id": "sensor.ignored", "weight": 0})
    c.config["priority_modes"] = {"balance": {"sensor.room": 1, "sensor.ignored": 0}}
    c.priority_mode = "balance"
    c.config["outdoor_sensor"] = "sensor.outdoor"
    c.config["extra_heat_sources"] = [{"entity_id": "switch.fan", "kind": "binary"}]
    c.filters["sensor.ignored"] = SensorFilter()
    c.filters["sensor.ignored"].observe(20, dt_util.utcnow().timestamp() - 300)
    hass.states.async_set("sensor.ignored", "20")
    hass.states.async_set("sensor.outdoor", "5")
    hass.states.async_set("switch.fan", "on")
    # These device rows have no member-room counterpart in Decision.readings.
    for settings in c.config["systems"]:
        system = hass.data[DATA_COMPONENT].get_entity(settings["entity_id"])
        system._attr_current_temperature = 22
        system.async_write_ha_state()
    c.learning.reconcile(c.learning_sensors, c.learning.systems, ["switch.fan"])
    for row in c.learning.rows.values():
        row.theta = [0, 0.3, 0, 0.01, 0.01, 0, 0.02, 0, 0, 0, 0, 0]
        row.counts = [100, 100, 100]
    c.learning_enabled = False
    await c.async_tick(record=False)
    attrs = hass.states.get("sensor.house_learning").attributes
    assert "sensor.ignored" not in c.data.readings
    assert attrs["systems"]["climate.ducted"]["sensor.room"][
        "effective_effect"
    ] == pytest.approx(0.16)
    assert attrs["systems"]["climate.ducted"]["sensor.ignored"][
        "effective_effect"
    ] == pytest.approx(0.15)
    assert attrs["systems"]["climate.ducted"]["climate.ducted"][
        "effective_effect"
    ] == pytest.approx(0.13)
    assert attrs["sources"]["switch.fan"]["sensor.ignored"]["g"] == 0.02
    assert attrs["sources"]["switch.fan"]["sensor.ignored"][
        "effective_effect"
    ] == pytest.approx(-0.29)


async def test_experiment_skip_reasons_exposed_in_learning_sensor(hass, installed):
    entry, _ = installed
    c = entry.runtime_data
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": "climate.house", "hvac_mode": "heat"},
        blocking=True,
    )
    await c.async_tick()
    experiments = hass.states.get("sensor.house_learning").attributes["experiments"]
    assert experiments
    for key, info in experiments.items():
        assert "skip_reason" in info
        assert info["skip_reason"] == c.state.experiment_info[key]["skip_reason"]
        assert info["skip_reason"] is not None


async def test_unit_control_handoff_learning_and_restart(hass, installed, freezer):
    from custom_components.house_climate.coordinator import HouseCoordinator

    c, system, calls = await prepare_fans(hass, installed)
    entry, _ = installed
    unit = "climate.ducted"
    freezer.tick(timedelta(minutes=5))
    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": "switch.house_ducted_control"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get("switch.house_ducted_control").state == "off"
    assert [
        x
        for x in calls
        if x[0] == unit and x[1] == "set_hvac_mode" and x[2]["hvac_mode"] == "off"
    ] == [(unit, "set_hvac_mode", {"hvac_mode": "off"})]
    assert c.fans.window is None
    assert any(
        a["samples"]
        for arms in c.fans.arms[unit]["fan"].values()
        for a in arms.values()
    )
    calls.clear()
    # Hand control, including after echo grace, must remain untouched.
    freezer.tick(timedelta(minutes=5))
    system._attr_hvac_mode = HVACMode.HEAT
    system._attr_hvac_action = HVACAction.HEATING
    system._attr_target_temperature = 28
    system.async_write_ha_state()
    await hass.async_block_till_done()
    before = c.learning.rows["sensor.room"].samples
    await c.async_tick()
    assert not [x for x in calls if x[0] == unit]
    assert "re-assert" not in c.data.explanation
    assert "Under hand control: climate.ducted" in c.data.explanation
    assert c.samples[-1]["efforts"][unit] == 0
    assert c.learning.rows["sensor.room"].samples > before
    assert [s["entity_id"] for s in c.active_systems()] == ["climate.split"]
    assert unit not in hass.states.get("climate.house").attributes["cycling"]
    assert (
        unit not in hass.states.get("sensor.house_learning").attributes["experiments"]
    )
    restored = HouseCoordinator(hass, entry)
    await restored.async_load()
    assert not restored.system_control[unit]
    # The switch state persists without sending another off on restart.
    calls.clear()
    await restored.async_tick(record=False)
    assert not [x for x in calls if x[0] == unit]
    system._attr_hvac_mode = HVACMode.OFF
    system._attr_hvac_action = HVACAction.IDLE
    system.async_write_ha_state()
    await hass.async_block_till_done()
    await c.async_system_control(unit, True)
    assert any(x == (unit, "set_hvac_mode", {"hvac_mode": "heat"}) for x in calls)
    await c.async_system_control(unit, False)
    await c.async_system_control("climate.split", False)
    assert "Every unit is under hand control" in c.data.explanation


@pytest.mark.parametrize("window_unit", ["climate.ducted", "climate.split"])
@pytest.mark.parametrize("early_stop", [False, True])
async def test_heating_test_does_not_interrupt_either_fan_window(
    hass, installed, freezer, window_unit, early_stop
):
    c, bryant, _ = await prepare_fans(hass, installed)
    mitsubishi = hass.data[DATA_COMPONENT].get_entity("climate.split")
    for unit in (bryant, mitsubishi):
        unit._attr_current_temperature = 24
        unit._attr_supported_features |= ClimateEntityFeature.FAN_MODE
        unit._attr_fan_modes = ["gentle", "strong"]
        unit._attr_fan_mode = "gentle"
        unit.async_write_ha_state()
    await hass.async_block_till_done()
    c.config["excite_after_minutes"] = 0
    c.config["excite_minutes"] = 10
    c.state.last_stop = {
        s["entity_id"]: dt_util.utcnow().timestamp() - 10800 for s in c.active_systems()
    }
    c.fans.window = None
    c.fans.chosen = {window_unit: {"fan": "gentle"}}
    c.fans.options(window_unit, "fan", "heat|0|0", ["gentle"])
    start = dt_util.utcnow().timestamp()
    c.fans._start(
        start,
        window_unit,
        "fan",
        "heat|0|0",
        "gentle",
        c.state.setpoint,
        {"sensor.room": -2},
    )
    with patch(
        "custom_components.house_climate.controller.allocate",
        return_value=({"climate.ducted": 0, "climate.split": 0}, c.state.setpoint),
    ):
        await c.async_tick(record=False)
        assert c.state.excitation["system"] == "climate.ducted"
        assert not c.fans.window["dirty"]
        bryant._attr_hvac_action = HVACAction.HEATING
        bryant.async_write_ha_state()
        await hass.async_block_till_done()
        freezer.tick(timedelta(minutes=5 if early_stop else 10))
        if early_stop:
            c.config["explore_threshold"] = 0
            # End early through the test's safety predicate.
            with patch(
                "custom_components.house_climate.controller.cycling_hold",
                return_value=False,
            ):
                await c.async_tick(record=False)
        else:
            await c.async_tick(record=False)
        assert not c.state.excitation
        assert not c.fans.window["dirty"]
        bryant._attr_hvac_action = HVACAction.IDLE
        bryant.async_write_ha_state()
        await hass.async_block_till_done()
        c.config["explore_threshold"] = 0
        freezer.tick(timedelta(minutes=25 if early_stop else 20))
        await c.async_tick(record=False)
    arms = c.fans.arms[window_unit]["fan"]
    assert sum(bucket["gentle"]["samples"] for bucket in arms.values()) >= 1
    # A physical user setpoint change still dirties the next window.
    bryant._attr_target_temperature += 1
    bryant.async_write_ha_state()
    await hass.async_block_till_done()
    assert c.fans.window["dirty"]


async def test_disabled_unit_excluded_from_all_control_boundaries(hass, installed):
    from custom_components.house_climate import controller

    c, _, _ = await prepare_fans(hass, installed)
    await c.async_system_control("climate.ducted", False)
    with (
        patch.object(controller, "allocate", wraps=controller.allocate) as allocation,
        patch.object(
            controller, "excitation_target", wraps=controller.excitation_target
        ) as excitation,
        patch.object(
            controller, "observe_thermostats", wraps=controller.observe_thermostats
        ) as outage,
        patch.object(
            controller, "effort_limits", wraps=controller.effort_limits
        ) as holds,
        patch.object(c.fans, "tick", wraps=c.fans.tick) as fans,
    ):
        await c.async_tick(record=False)
    for boundary in (allocation, excitation, outage):
        assert boundary.called
        args = boundary.call_args.args
        assert [s["entity_id"] for s in args[0]["systems"]] == ["climate.split"]
    assert all(call.args[2] == "climate.split" for call in holds.call_args_list)
    assert set(fans.call_args.args[3]) == {"climate.split"}
    assert set(c.state.experiment_info) <= {"climate.split"}
    assert not any(
        "ducted" in sentence
        for sentence in c.data.explanation.split(".")
        if "lifts" in sentence or "limits" in sentence
    )


async def test_own_mode_restore_does_not_dirty_fan_window(hass, installed):
    c, system, _ = await prepare_fans(hass, installed)
    system._attr_hvac_mode = HVACMode.OFF
    system.async_write_ha_state()
    await hass.async_block_till_done()
    assert c.fans.window["dirty"]  # Real user mode change.
    c.fans.window["dirty"] = False
    await c.async_tick(record=False)
    await hass.async_block_till_done()
    assert system.hvac_mode == HVACMode.HEAT
    assert not c.fans.window["dirty"]  # House re-assert echo.

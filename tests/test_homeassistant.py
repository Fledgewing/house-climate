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


async def complete_flow(manager, result, sensors, systems, stale_after=720):
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
        k: v for k, v in DEFAULTS.items() if k not in ("sensors", "systems") and v != ""
    }
    fields = {field.schema: field for field in result["data_schema"].schema}
    assert fields["stale_after"].default() == 720
    tunables["stale_after"] = stale_after
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
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["sensors"] == [
        {"entity_id": "sensor.new", "weight": 1.0, "zone": "Room"},
        {"entity_id": "sensor.another", "weight": 2.0, "zone": "Room"},
    ]
    assert entry.options["systems"] == [{"entity_id": "climate.third", "preset": ""}]
    assert entry.options["stale_after"] == 180


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
        ("set_temperature", {"temperature": 18}),
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
        else:
            assert entity_selector.config["domain"] == ["climate"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"sensors": ["sensor.room"], "systems": ["climate.ducted"]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"weight": 1.0}
    )
    assert result["step_id"] == "system"
    assert {field.schema for field in result["data_schema"].schema} == {"preset"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
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
    assert entry.version == 2
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
    hass.states.async_set("sensor.room", "19.1")
    with patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=start + timedelta(seconds=300),
    ):
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
    assert set(entry.runtime_data.learning.rows) == {"sensor.new"}
    assert set((await entry.runtime_data.store.async_load())["learning"]["rows"]) == {
        "sensor.new"
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
        assert coordinator.data.temperature == 20.25
        return
    assert coordinator.data.temperature == 21.5
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
    assert coordinator.data.readings["sensor.room"] == 19.1
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

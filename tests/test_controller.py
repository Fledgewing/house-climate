"""Pure control behavior, independent of a running Home Assistant."""

from copy import deepcopy

import pytest

from custom_components.house_climate.const import DEFAULTS
from custom_components.house_climate.controller import (
    ControlState,
    Device,
    Reading,
    control,
)


@pytest.fixture
def config():
    return {
        "sensors": [
            {"entity_id": "sensor.a", "weight": 1},
            {"entity_id": "sensor.b", "weight": 3},
        ],
        "systems": [
            {
                "entity_id": "climate.a",
                "preset": "manual",
            }
        ],
    }


def run(config, state=None, readings=None, device=None, now=10000, **kwargs):
    return control(
        config,
        state or ControlState(mode="heat"),
        readings
        if readings is not None
        else {"sensor.a": Reading(18), "sensor.b": Reading(18)},
        {"climate.a": device or Device()},
        now,
        **kwargs,
    )


def test_weighted_house_and_spread(config):
    result = run(config, readings={"sensor.a": Reading(20), "sensor.b": Reading(24)})
    assert result.temperature == 23
    assert result.spread == 4


@pytest.mark.parametrize(
    "bad",
    [
        Reading(None),
        Reading(23, 720 * 60 + 1),
        Reading(float("nan")),
        Reading(float("inf")),
    ],
)
def test_invalid_and_stale(config, bad):
    result = run(config, readings={"sensor.a": Reading(20), "sensor.b": bad})
    assert result.temperature == 20
    assert result.spread == 0
    assert result.readings == {"sensor.a": 20}


def test_staleness_boundary(config):
    assert run(config, readings={"sensor.a": Reading(19, 720 * 60)}).temperature == 19


@pytest.mark.parametrize("enabled", [True, False])
def test_configured_staleness_drops_and_names_sensor(config, enabled):
    config["stale_after"] = 120
    result = run(
        config,
        readings={"sensor.a": Reading(19, 7200), "sensor.b": Reading(25, 7201)},
        enabled=enabled,
    )
    assert result.readings == {"sensor.a": 19}
    assert result.temperature == 19
    assert (
        "sensor.b: stale reading older than 120 minutes; dropped." in result.explanation
    )


@pytest.mark.parametrize(
    ("temperature", "expected"),
    [(19, "heat"), (23, "cool"), (21, "off"), (20, "off"), (22, "off")],
)
def test_auto_deadband(config, temperature, expected):
    state = ControlState(mode="heat_cool")
    run(config, state, {"sensor.a": Reading(temperature)})
    assert state.active_mode == expected


def test_dwell_and_retention(config):
    state = ControlState(mode="heat_cool", active_mode="heat", last_switch=9000)
    run(config, state, {"sensor.a": Reading(24)}, now=10000)
    assert state.active_mode == "heat"
    run(config, state, {"sensor.a": Reading(24)}, now=12600)
    assert state.active_mode == "cool"
    run(config, state, {"sensor.a": Reading(21)}, now=13000)
    assert state.active_mode == "cool"


@pytest.mark.parametrize(("mode", "outdoor"), [("heat", 25), ("cool", 9)])
def test_outdoor_veto(config, mode, outdoor):
    state = ControlState(mode=mode)
    result = run(config, state, device=Device(mode=mode), outdoor=outdoor)
    assert state.active_mode == "off"
    assert result.commands == [("climate.a", "set_hvac_mode", {"hvac_mode": "off"})]


def test_effort_switches_in_one_pass_despite_legacy_slew(config):
    config.update(max_offset_change=0.01, offset_clamp=0.5)
    state = ControlState(mode="heat", setpoint=22.5, offsets={"climate.a": -3})
    device = Device(mode="heat", current_temperature=25)
    result = run(config, state, device=device)
    assert result.commands[-1][2] == {"temperature": 28}
    result = run(config, state, {"sensor.a": Reading(26)}, device, now=10600)
    assert result.commands[-1][2] == {"temperature": 22}


def test_mean_integrates_once_per_interval(config):
    config["objective"] = "variance"
    config["mean_integral_gain"] = 0.001
    state = ControlState(mode="heat")
    for i in range(3):
        run(config, state, {"sensor.a": Reading(20.9)}, now=10000 + i * 300)
    assert state.integrals["mean"] == pytest.approx(0.00015)
    before = dict(state.integrals)
    run(config, state, {"sensor.a": Reading(20.9)}, now=10601)
    assert state.integrals == before


def test_immediate_calls_retain_dwell_but_next_pass_can_switch(config):
    state = ControlState(mode="heat")
    device = Device(mode="heat", current_temperature=24)
    run(config, state, device=device)
    for now in (10001, 10002, 10299):
        result = run(config, state, {"sensor.a": Reading(25)}, device, now=now)
        assert result.commands[-1][2]["temperature"] > 24
    result = run(config, state, {"sensor.a": Reading(25)}, device, now=10600)
    assert result.commands[-1][2]["temperature"] < 24


@pytest.mark.parametrize(
    ("setpoint", "reading", "expected"), [(35, 35, 30), (7, 7, 10)]
)
def test_device_limits(config, setpoint, reading, expected):
    state = ControlState(mode="heat", setpoint=setpoint)
    result = run(
        config,
        state,
        {"sensor.a": Reading(reading)},
        Device(minimum=10, maximum=30, current_temperature=reading),
    )
    if reading < 10:
        # A clamped target above own cannot satisfy off demand.
        assert not result.commands  # Device is already HVAC off.
        assert not state.commanded["climate.a"]
    else:
        assert result.commands[-1][2] == {"temperature": expected}


def test_preset_reassert_and_changed_only(config):
    device = Device(
        mode="heat", temperature=18, current_temperature=21, preset="schedule"
    )
    result = run(config, readings={"sensor.a": Reading(21)}, device=device)
    assert result.commands == [
        ("climate.a", "set_preset_mode", {"preset_mode": "manual"})
    ]
    device.preset = "manual"
    assert not run(config, readings={"sensor.a": Reading(21)}, device=device).commands


def test_no_sensors_zeroes_offsets(config):
    state = ControlState(mode="heat", offsets={"climate.a": 3})
    result = run(config, state, readings={})
    assert result.temperature is None
    assert state.offsets["climate.a"] == 0
    assert result.commands[-1][2] == {"temperature": 21}


def test_unavailable_and_disabled(config):
    state = ControlState(mode="heat")
    assert not run(config, state, device=Device(available=False)).commands
    before = deepcopy(state)
    assert not run(config, state, enabled=False).commands
    assert state == before


def test_off(config):
    result = run(config, ControlState(mode="off"), device=Device(mode="heat"))
    assert result.commands == [("climate.a", "set_hvac_mode", {"hvac_mode": "off"})]


def test_multiroom_with_four_quiet_sensors_raises_whole_house_offset():
    config = deepcopy(DEFAULTS)
    sensors = [f"sensor.room_{i}" for i in range(6)]
    config["sensors"] = [{"entity_id": entity_id, "weight": 1} for entity_id in sensors]
    config["systems"] = [
        {"entity_id": "climate.ducted", "preset": "manual"},
        {"entity_id": "climate.split", "preset": ""},
    ]
    values = [22.5, 20.0, 20.3, 18.7, 21.7, 16.9]
    readings = {
        entity_id: Reading(t, 0 if i < 2 else 93 * 60)
        for i, (entity_id, t) in enumerate(zip(sensors, values, strict=True))
    }
    state = ControlState(setpoint=21, mode="heat")
    devices = {
        s["entity_id"]: Device(current_temperature=22.5) for s in config["systems"]
    }
    result = control(config, state, readings, devices, 10000)
    assert result.readings == dict(zip(sensors, values, strict=True))
    assert result.temperature == pytest.approx(20.0166666667)
    assert state.offsets[config["systems"][0]["entity_id"]] == 4.5
    assert (
        "climate.ducted",
        "set_temperature",
        {"temperature": 25.5},
    ) in result.commands


def test_device_limit_antiwindup(config):
    config.update(mean_integral_gain=0.1)
    state = ControlState(mode="heat", setpoint=30)
    for i in range(10):
        run(
            config,
            state,
            {"sensor.a": Reading(29)},
            Device(maximum=30),
            now=10000 + i * 300,
        )
    assert abs(state.integrals.get("mean", 0)) <= 1


def test_one_unavailable_system_does_not_block_others(config):
    config["systems"].append({"entity_id": "climate.b"})
    state = ControlState(mode="heat")
    result = control(
        config,
        state,
        {"sensor.a": Reading(19)},
        {"climate.a": Device(available=False), "climate.b": Device()},
        10000,
    )
    assert result.commands
    assert all(entity_id == "climate.b" for entity_id, _, _ in result.commands)


def test_missing_room_uses_available_house_mean(config):
    result = run(config, readings={"sensor.a": Reading(19)})
    assert result.temperature == 19
    assert result.commands[-1][2] == {"temperature": 22}


def test_outdoor_veto_thresholds_are_configurable(config):
    config.update(cool_outdoor_min=5, heat_outdoor_max=30)
    for mode, outdoor in [("cool", 9), ("heat", 25)]:
        state = ControlState(mode=mode)
        run(config, state, outdoor=outdoor)
        assert state.active_mode == mode


@pytest.mark.parametrize("weight", [1, 0.5, 0.2, 0.1])
def test_mean_integral_respects_average_weight(config, weight):
    config["objective"] = "variance"
    config.update(average_weight=weight, mean_integral_gain=0.001)
    state = ControlState(mode="heat")
    run(config, state, {"sensor.a": Reading(20.9)})
    assert state.integrals["mean"] == pytest.approx(0.001 * 0.1 * weight / (1 + weight))

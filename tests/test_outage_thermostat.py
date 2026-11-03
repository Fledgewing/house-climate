"""Local thermostats constrain unavailable inputs throughout the horizon."""

import json
from dataclasses import asdict

import pytest

from custom_components.house_climate.controller import (
    ControlState,
    Device,
    Reading,
    allocate,
    control,
    outage_step,
    thermostat_effort,
)
from custom_components.house_climate.learning import Learning


@pytest.fixture(params=[("heat", "heating", 1), ("cool", "cooling", -1)])
def outage(request):
    mode, action, sign = request.param
    config = {
        "sensors": [{"entity_id": "room"}],
        "systems": [
            {"entity_id": "lost"},
            {"entity_id": "live", "modulating": True},
        ],
        "interval": 5,
        "control_horizon_minutes": 20,
        "switch_penalty": 0,
        "effort_change_penalty": 0,
        "comfort_band": 0,
    }
    model = Learning(["room", "lost"], ["lost", "live"])
    model.rows["room"].theta = [0, 0.2, 0.2, 0, 0]
    model.rows["lost"].theta = [0, 1, 0, 0, 0]
    for row in model.rows.values():
        row.counts = [1e30, 1e30]
    state = ControlState(mode=mode, active_mode=mode, setpoint=20 + sign)
    state.observe_report(
        config["systems"][0],
        Device(
            mode=mode,
            hvac_action=action,
            temperature=20 + 2 * sign,
            current_temperature=20,
            name="Home ZONE 1",
        ),
        config,
    )
    state.thermostats["lost"]["time"] = 0
    devices = {"lost": Device(available=False), "live": Device(current_temperature=20)}
    return config, model, state, devices, sign


def test_target_crossing_changes_fixed_horizon_input(outage):
    config, model, state, devices, sign = outage
    before = json.dumps(asdict(state), sort_keys=True)
    effort, predicted = allocate(
        config,
        {"room": 20},
        devices,
        state.mode,
        state.setpoint,
        learning=model,
        state=state,
    )
    # Two intervals on, two off: 0.4 degrees from lost, 0.6 from live.
    assert effort == pytest.approx({"live": sign * 0.75})
    assert predicted == pytest.approx(state.setpoint)
    assert json.dumps(asdict(state), sort_keys=True) == before


def test_elapsed_outage_crosses_target_and_restart_keeps_latch(outage):
    config, model, state, devices, sign = outage
    # Disable available effort to isolate the lost unit's actual elapsed model.
    devices["live"].available = False
    for now in (300, 600):
        decision = control(
            config, state, {"room": Reading(20)}, devices, now, learning=model
        )
    assert state.thermostats["lost"]["own"] == pytest.approx(20 + sign * 2)
    assert state.thermostats["lost"]["running"] is False
    assert "assuming off" in decision.explanation
    restored = ControlState(**json.loads(json.dumps(asdict(state))))
    control(config, restored, {"room": Reading(20)}, devices, 900, learning=model)
    assert restored.thermostats["lost"]["own"] == pytest.approx(20 + sign * 2)
    assert restored.thermostats["lost"]["running"] is False
    assert restored.reported_actions["lost"] in ("heating", "cooling")


@pytest.mark.parametrize("source", ["sensor", "device"])
def test_real_own_reading_overrides_prediction(outage, source):
    config, model, state, devices, sign = outage
    config["systems"][0]["own_sensor"] = "own"
    readings = {"room": Reading(20)}
    real = 20 + sign * 3
    if source == "sensor":
        readings["own"] = Reading(real)
    else:
        devices["lost"].current_temperature = real
    decision = control(config, state, readings, devices, 300, learning=model)
    assert state.thermostats["lost"]["own"] == real
    assert state.thermostats["lost"]["running"] is False
    assert f"predicted own temperature {real:.1f} °C" in decision.explanation
    assert all(key != "lost" for key, _, _ in decision.commands)


def test_hysteresis_is_latched_and_learned_from_real_starts(outage):
    config, _, state, _, sign = outage
    thermostat = state.thermostats["lost"]
    target = thermostat["target"]
    thermostat.update(own=target, running=True)
    assert thermostat_effort(config, state, "lost", thermostat) == 0
    thermostat["own"] = target - sign * 0.5
    assert thermostat_effort(config, state, "lost", thermostat) == 0
    thermostat["own"] = target - sign * 0.6
    assert thermostat_effort(config, state, "lost", thermostat) == sign
    thermostat["own"] = target - sign * 0.1
    assert thermostat_effort(config, state, "lost", thermostat) == sign
    # A real idle-to-running transition at an unchanged target estimates its gap.
    for action, gap in [("idle", 0), ("heating" if sign == 1 else "cooling", 0.8)]:
        state.observe_report(
            config["systems"][0],
            Device(
                mode=state.mode,
                hvac_action=action,
                temperature=target,
                current_temperature=target - sign * gap,
            ),
            config,
        )
    assert state.thermostat_points["lost"] == pytest.approx([0.8])
    restored = ControlState(**json.loads(json.dumps(asdict(state))))
    thermostat = restored.thermostats["lost"]
    thermostat.update(own=target - sign * 0.6, running=False)
    assert thermostat_effort(config, restored, "lost", thermostat) == 0
    thermostat["own"] = target - sign * 0.9
    assert thermostat_effort(config, restored, "lost", thermostat) == sign


def test_own_model_uses_joint_efforts_outdoor_solar_and_sources(outage):
    config, _, state, _, sign = outage
    model = Learning(["lost"], ["lost", "live"], sources=["fire"])
    model.rows["lost"].theta = [0.1, 0.3, 0.4, 0.5, 0.6, 0.7]
    model.rows["lost"].counts = [1e30, 1e30, 1e30]
    thermostat = state.thermostats["lost"]
    joint = outage_step(
        config,
        state,
        {"lost": thermostat},
        {"live": 0.25 * sign},
        model,
        10,
        2,
        {"fire": 0.5},
    )
    assert joint == {"lost": sign, "live": 0.25 * sign}
    assert thermostat["own"] == pytest.approx(20 - 1 + sign * 0.4 + 0.25 + 1.2 + 0.7)


@pytest.mark.parametrize(
    "reading", [Reading(99, reporting=False), Reading(99, age=999999)]
)
def test_nonreporting_or_stale_own_sensor_does_not_replace_model(outage, reading):
    config, model, state, devices, sign = outage
    config["systems"][0]["own_sensor"] = "own"
    control(
        config,
        state,
        {"room": Reading(20), "own": reading},
        devices,
        300,
        learning=model,
    )
    assert state.thermostats["lost"]["own"] == pytest.approx(20 + sign)
    assert state.thermostats["lost"]["running"] is True

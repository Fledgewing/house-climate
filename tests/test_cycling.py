"""Reported-action protection, steady effort, comfort swing and mode safety."""

import random
from copy import deepcopy
from dataclasses import asdict

import pytest

from custom_components.house_climate.controller import (
    ControlState,
    Device,
    Reading,
    allocate,
    control,
    cycling_hold,
)
from custom_components.house_climate.learning import Learning


def setup(mode="heat", modulating=False):
    config = {
        "systems": [{"entity_id": "unit", "modulating": modulating}],
        "sensors": [{"entity_id": "a"}, {"entity_id": "b"}],
        "explore_threshold": 0,
        "effort_dwell_minutes": 0,
        "effort_hysteresis": 0,
    }
    state = ControlState(mode=mode, active_mode=mode)
    device = Device(mode=mode, current_temperature=21, step=0.1 if modulating else 0.5)
    return config, state, {"unit": device}


@pytest.mark.parametrize("mode,action", [("heat", "heating"), ("cool", "cooling")])
def test_reported_clocks_not_commands_and_exact_limits(mode, action):
    config, state, devices = setup(mode)
    state.observe_action("unit", "idle", 0)
    wants_on = 18 if mode == "heat" else 24
    readings = {"a": Reading(wants_on), "b": Reading(wants_on)}
    for now in [0, 60, 299]:
        result = control(config, state, readings, devices, now)
        assert state.efforts["unit"] == 0
        assert "held off for cycling protection" in result.explanation
    assert state.starts["unit"] == []
    control(config, state, readings, devices, 300)
    assert abs(state.efforts["unit"]) == 1
    assert state.starts["unit"] == []  # Service success is no compressor evidence.
    devices["unit"].hvac_action = action
    state.observe_action("unit", action, 340)  # Delayed device response.
    opposite = {"a": Reading(24 if mode == "heat" else 18)}
    for now in [350, 600, 939]:
        result = control(config, state, opposite, devices, now)
        assert abs(state.efforts["unit"]) > 0
        assert "held on for cycling protection" in result.explanation
    assert cycling_hold(config, state, "unit", 940) is None
    assert state.last_start["unit"] == 340
    assert state.cycling_status("unit", 940) == {
        "starts_last_hour": 1,
        "run_minutes": 10,
        "off_minutes": 0,
    }


def test_start_cap_rolling_window_and_system_override():
    config, state, _ = setup()
    for start in [0, 900, 1800]:
        state.observe_action("unit", "heating", start)
        state.observe_action("unit", "idle", start + 600)
    assert cycling_hold(config, state, "unit", 3599) is False
    assert cycling_hold(config, state, "unit", 3600) is None
    config["systems"][0]["min_off_minutes"] = 30
    assert cycling_hold(config, state, "unit", 3600) is False
    assert cycling_hold(config, state, "unit", 4200) is None


@pytest.mark.parametrize("mode,temperatures", [("heat", [19, 23]), ("cool", [23, 19])])
def test_average_in_band_but_outlier_can_start_for_spread(mode, temperatures):
    config, state, devices = setup(mode)
    model = Learning(["a", "b"], ["unit"])
    for room, effect in [("a", 0.2), ("b", 0)]:
        model.rows[room].theta[1] = effect
        model.rows[room].counts = [1e9]
    state.observe_action("unit", "idle", -600)
    devices["unit"].hvac_action = "idle"
    decision = control(
        config,
        state,
        dict(zip(["a", "b"], map(Reading, temperatures))),
        devices,
        0,
        learning=model,
    )
    assert decision.temperature == 21
    assert abs(state.efforts["unit"]) > 0


@pytest.mark.parametrize("mode,action", [("heat", "heating"), ("cool", "cooling")])
@pytest.mark.parametrize("running", [False, True])
def test_all_weighted_rooms_in_band_hold_state_despite_forecast_and_excitation(
    mode, action, running
):
    config, state, devices = setup(mode, modulating=True)
    config.update(explore_threshold=0, excite_after_minutes=1)
    devices["unit"].hvac_action = action if running else "idle"
    state.observe_action("unit", devices["unit"].hvac_action, -7200)
    state.last_run["unit"] = -7200
    model = Learning(["a", "b"], ["unit"])
    # Forecast drift strongly asks for a state change, despite comfortable rooms.
    for row in model.rows.values():
        row.theta[-1] = (1 if running else -1) * (1 if mode == "heat" else -1)
    readings = {"a": Reading(20.7), "b": Reading(21.2)}
    control(config, state, readings, devices, 0, learning=model)
    assert (abs(state.efforts["unit"]) > 0) is running
    assert not state.excitation


def test_modulating_low_effort_is_continuous_close_to_own_sensor():
    config, state, devices = setup(modulating=True)
    config.update(comfort_band=0, switch_penalty=0, effort_change_penalty=0)
    state.observe_action("unit", "heating", -7200)
    devices["unit"].hvac_action = "heating"
    for now in range(0, 1800, 300):
        decision = control(
            config, state, {"a": Reading(20.95), "b": Reading(20.95)}, devices, now
        )
        assert 0 < state.efforts["unit"] < 0.5
        target = next(
            data["temperature"]
            for _, service, data in decision.commands
            if service == "set_temperature"
        )
        assert 21 < target <= 21.3
    assert state.starts["unit"] == []  # The old start aged out, no new reported start.


def test_switching_cost_and_effort_movement_enter_objective():
    config, state, devices = setup(modulating=True)
    config.update(comfort_band=0, switch_penalty=1, effort_change_penalty=0)
    state.observe_action("unit", "idle", -600)
    values = {"a": 20.8, "b": 20.8}
    efforts, _ = allocate(config, values, devices, "heat", 21, state=state)
    assert efforts["unit"] == 0
    config["switch_penalty"] = 0
    efforts, _ = allocate(config, values, devices, "heat", 21, state=state)
    assert efforts["unit"] > 0
    state.efforts["unit"] = 0.25
    config["effort_change_penalty"] = 1
    efforts, _ = allocate(config, values, devices, "heat", 21, state=state)
    assert efforts["unit"] == 0.25


@pytest.mark.parametrize("mode", ["heat", "cool"])
@pytest.mark.parametrize(
    "path",
    [
        "allocation",
        "excitation",
        "held_on",
        "held_off",
        "fallback",
        "rounding",
        "opposite_report",
    ],
)
def test_fixed_house_mode_never_commands_opposite_mode(monkeypatch, mode, path):
    config, state, devices = setup(mode, modulating=True)
    other = "cool" if mode == "heat" else "heat"
    action = "heating" if mode == "heat" else "cooling"
    devices["unit"].mode = other
    devices["unit"].hvac_action = "idle"
    state.observe_action("unit", "idle", -7200)
    readings = {"a": Reading(18 if mode == "heat" else 24)}
    model = Learning(["a", "b"], ["unit"])
    if path == "excitation":
        config.update(explore_threshold=0.8, excite_after_minutes=1)
        # Keep the unit idle under normal allocation to exercise forced excitation.
        monkeypatch.setattr(
            "custom_components.house_climate.controller.allocate",
            lambda *args: ({"unit": 0}, state.setpoint),
        )
    elif path == "held_on":
        state.observe_action("unit", action, -60)
        devices["unit"].hvac_action = action
        readings = {"a": Reading(24 if mode == "heat" else 18)}
    elif path == "held_off":
        state.last_stop["unit"] = -60
    elif path == "fallback":
        readings = {}
    elif path == "rounding":
        state.rounding_errors["unit"] = 0.5
    elif path == "opposite_report":
        devices["unit"].hvac_action = "cooling" if mode == "heat" else "heating"
    decision = control(config, state, readings, devices, 0, learning=model)
    assert state.active_mode == mode
    modes = []
    for _, service, data in decision.commands:
        if service == "set_hvac_mode":
            modes.append(data["hvac_mode"])
            assert data["hvac_mode"] in (mode, "off")
        if service == "set_temperature":
            assert set(data) == {
                "temperature"
            }  # No auto-mode high/low or alternate mode.
            assert modes[-1] == mode  # Mode is corrected before setting a target.
    if path == "excitation":
        assert state.excitation["mode"] == mode


def noisy_replay(protected=True):
    """Four hours, minute passes, alternating sensor noise around a binary threshold.

    Both rooms stay outside the comfort band, so balancing is always eligible.
    Physical heat/loss is small; the unprotected optimum changes each pass.
    """
    config, state, devices = setup()
    config.update(interval=1, control_horizon_minutes=5, mean_integral_gain=0)
    if not protected:
        config.update(
            min_run_minutes=0,
            min_off_minutes=0,
            max_starts_per_hour=1000,
            switch_penalty=0,
            effort_change_penalty=0,
        )
    device = devices["unit"]
    device.hvac_action = "idle"
    state.observe_action("unit", "idle", -3600)
    rng = random.Random(533)
    temperatures = [20.0, 21.85]
    transitions, starts, runs, offs, means, spreads = [], [], [], [], [], []
    since, running = 0, False
    for minute in range(240):
        now = minute * 60
        device.current_temperature = sum(temperatures) / 2
        noise = (0.15 if minute % 2 else -0.15) + rng.uniform(-0.005, 0.005)
        readings = {
            room: Reading(t + noise) for room, t in zip(["a", "b"], temperatures)
        }
        decision = control(config, state, readings, devices, now)
        for _, service, data in decision.commands:
            if service == "set_temperature":
                device.temperature = data["temperature"]
        on = device.temperature > device.current_temperature
        if on != running:
            transitions.append(now)
            (runs if running else offs).append((now - since) / 60)
            if on:
                starts.append(now)
            since, running = now, on
        device.hvac_action = "heating" if on else "idle"
        state.observe_action("unit", device.hvac_action, now)
        if minute == 117:  # A real state serialization/restart within the replay.
            state = ControlState(**deepcopy(asdict(state)))
        temperatures = [
            t + b * (on - 0.5) for t, b in zip(temperatures, [0.013, 0.011])
        ]
        means.append(abs(sum(temperatures) / 2 - 21))
        spreads.append(max(temperatures) - min(temperatures))
    return {
        "transitions": len(transitions),
        "starts": len(starts),
        "min_run": min(runs),
        "min_off": min(offs[1:]),
        "max_starts_hour": max(sum(t - 3600 < s <= t for s in starts) for t in starts),
        "mean_error": sum(means) / len(means),
        "spread": sum(spreads) / len(spreads),
    }


def test_noisy_simulation_reduces_cycling_with_small_comfort_cost():
    protected, baseline = noisy_replay(), noisy_replay(False)
    print(
        f"\nCycling four-hour replay: protected={protected}; unconstrained={baseline}"
    )
    assert baseline["transitions"] == 240
    assert protected["min_run"] >= 10
    assert protected["min_off"] >= 5
    assert protected["max_starts_hour"] <= 3
    assert protected["mean_error"] <= baseline["mean_error"] + 0.15
    assert protected["spread"] <= baseline["spread"] + 0.05


@pytest.mark.parametrize("mode,action", [("heat", "heating"), ("cool", "cooling")])
@pytest.mark.parametrize("running", [False, True])
def test_no_sensor_fallback_respects_reported_holds_without_temperature_reference(
    mode, action, running
):
    config, state, devices = setup(mode)
    device = devices["unit"]
    device.current_temperature = None
    device.temperature = 23
    device.hvac_action = action if running else "idle"
    state.observe_action("unit", device.hvac_action, -60)
    decision = control(config, state, {}, devices, 0)
    assert "cycling protection" in decision.explanation
    if running:
        assert not decision.commands  # Keep the active target when no reference exists.
    else:
        assert decision.commands == [("unit", "set_hvac_mode", {"hvac_mode": "off"})]


def test_device_limits_cannot_force_start_inside_minimum_off():
    config, state, devices = setup()
    device = devices["unit"]
    device.current_temperature = 6
    device.minimum = 7
    device.hvac_action = "idle"
    state.observe_action("unit", "idle", -60)
    decision = control(config, state, {"a": Reading(6)}, devices, 0)
    assert decision.commands == [("unit", "set_hvac_mode", {"hvac_mode": "off"})]


def test_heat_cool_is_the_only_house_mode_that_can_reverse_direction():
    config, state, devices = setup("heat_cool")
    state.active_mode = "heat"
    state.last_switch = -7200
    devices["unit"].mode = "heat"
    decision = control(config, state, {"a": Reading(25)}, devices, 0, outdoor=20)
    assert state.active_mode == "cool"
    assert ("unit", "set_hvac_mode", {"hvac_mode": "cool"}) in decision.commands


@pytest.mark.parametrize("mode,action", [("heat", "heating"), ("cool", "cooling")])
def test_modulating_minimum_run_survives_feedback_dither_and_rounding(mode, action):
    config, state, devices = setup(mode, modulating=True)
    config["explore_threshold"] = 0.8
    state.observe_action("unit", action, -60)
    state.drive_bias["unit"] = -0.5
    state.rounding_errors["unit"] = -0.05 if mode == "heat" else 0.05
    device = devices["unit"]
    device.hvac_action = action
    model = Learning(["a", "b"], ["unit"])
    readings = {"a": Reading(25 if mode == "heat" else 17)}
    decision = control(config, state, readings, devices, 0, learning=model)
    assert abs(state.efforts["unit"]) > 0
    target = next(
        data["temperature"]
        for _, service, data in decision.commands
        if service == "set_temperature"
    )
    assert (
        (target > device.current_temperature)
        if mode == "heat"
        else (target < device.current_temperature)
    )
    assert "held on for cycling protection" in decision.explanation


def test_active_excitation_cannot_restart_reported_stop_inside_off_delay():
    config, state, devices = setup(modulating=True)
    config["explore_threshold"] = 0.8
    state.excitation = {
        "system": "unit",
        "mode": "heat",
        "started": -600,
        "runtime": 0,
        "target": 25,
    }
    state.observe_action("unit", "heating", -600)
    state.observe_action("unit", "idle", -60)
    devices["unit"].hvac_action = "idle"
    decision = control(
        config,
        state,
        {"a": Reading(18)},
        devices,
        0,
        learning=Learning(["a", "b"], ["unit"]),
    )
    assert state.efforts["unit"] == 0
    assert "held off for cycling protection" in decision.explanation
    assert (
        next(
            data["temperature"]
            for _, service, data in decision.commands
            if service == "set_temperature"
        )
        < 21
    )


def test_unknown_gap_cannot_reuse_an_old_off_clock_for_a_new_reported_stop():
    config, state, _ = setup()
    state.observe_action("unit", "idle", -3600)
    state.observe_action("unit", "heating", -600)
    state.observe_action("unit", None, -120)
    state.observe_action("unit", "idle", -60)
    assert state.last_stop["unit"] == -60
    assert cycling_hold(config, state, "unit", 0) is False

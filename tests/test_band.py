"""Centered-band allocation and measured experiments, including 11-06 13:03.

The snapshot supplies a confidence range, not per-room counts, passive terms,
lag history or the saved fit. The reconstruction below explicitly chooses
confidence within that range and zero passive/lag terms. It tests the existing
optimistic blend, without claiming to recover the exact live forecast.
"""

from copy import deepcopy
from dataclasses import asdict

import pytest

from custom_components.house_climate.controller import (
    ControlState,
    Device,
    Reading,
    allocate,
    control,
    counted_rooms,
)
from custom_components.house_climate.learning import Learning

UNIT = "climate.home_zone_1"
ROOMS = [
    "living",
    "downstairs",
    "guest",
    "parent",
    "annabelle",
    "wesley_gabriel",
    "office",
    "own",
]
TEMPERATURES = [20.5, 20.33, 21.39, 22.73, 21.98, 22.46, 24.56, 22]
WEIGHTS = [0.5, 1, 1, 1, 1, 1, 0.01, 0.01]
RATES = [0.89, 1.40, 1.56, 0.80, 1.82, 2.07, 0, 0]
CONFIDENCE = [0.06, 0.04, 0.04, 0.12, 0.02, 0.02, 0, 0]


def live(mode="heat"):
    direction = 1 if mode == "heat" else -1
    config = {
        "systems": [{"entity_id": UNIT, "modulating": False}],
        "sensors": [
            {
                "entity_id": room,
                "weight": weight,
                "name": room.replace("_", " ").title() + " Room",
            }
            for room, weight in zip(ROOMS, WEIGHTS)
        ],
    }
    values = {room: 22 + direction * (t - 22) for room, t in zip(ROOMS, TEMPERATURES)}
    model = Learning(ROOMS, [UNIT])
    for room, rate, confidence in zip(ROOMS, RATES, CONFIDENCE):
        model.rows[room].theta[1] = rate / 12
        model.rows[room].counts = [24 * confidence / (1 - confidence)]
    devices = {
        UNIT: Device(
            mode=mode, current_temperature=22, hvac_action="idle", name="Bryant"
        )
    }
    state = ControlState(
        mode=mode,
        active_mode=mode,
        setpoint=22,
        test_observed_since={UNIT: 0},
        last_stop={UNIT: -3600},
    )
    return config, state, model, devices, values


def max_deviation(config, model, values, effort):
    counted = counted_rooms(config, values)
    maximum = max(counted.values())
    predictions = {
        r: base + coefficients[0] * effort
        for r in counted
        for base, coefficients in [model.forecast(r, values[r], [UNIT], 9)]
    }
    return max(counted[r] / maximum * abs(t - 22) for r, t in predictions.items())


@pytest.mark.parametrize("mode,direction", [("heat", 1), ("cool", -1)])
def test_1303_band_runs_bryant_variance_stays_off(mode, direction):
    config, state, model, devices, values = live(mode)
    off = max_deviation(config, model, values, 0)
    on = max_deviation(config, model, values, direction)
    assert off == pytest.approx(1.67)
    assert on == pytest.approx(1.56232)
    assert (
        allocate(config, values, devices, mode, 22, learning=model, state=state)[0][
            UNIT
        ]
        == direction
    )
    config["objective"] = "variance"
    assert (
        allocate(config, values, devices, mode, 22, learning=model, state=state)[0][
            UNIT
        ]
        == 0
    )
    # Exercise control separately from due experiments; a saved mean bias is
    # neither applied nor integrated under the band objective.
    config["objective"] = "band"
    state.integrals["mean"] = -1 * direction
    decision = control(
        config,
        state,
        {r: Reading(t) for r, t in values.items()},
        devices,
        0,
        learning=model,
    )
    assert direction * state.efforts[UNIT] > 0
    assert state.requested_efforts[UNIT] == 1
    assert not state.excitation
    assert state.integrals["mean"] == -1 * direction
    assert any(service == "set_temperature" for _, service, _ in decision.commands)
    print(
        f"\n13:03 {mode}, confidence-blended 45-min max deviation: off={off:.5f}, on={on:.5f} C"
    )


def test_1303_raw_rates_are_not_the_confidence_blended_forecast():
    # Without per-room confidence, the snapshot cannot uniquely specify (a).
    config, state, model, devices, values = live()
    for row in model.rows.values():
        row.counts = [1e30]
    assert max_deviation(config, model, values, 0) == pytest.approx(1.67)
    assert max_deviation(config, model, values, 1) == pytest.approx(2.0125)
    assert (
        allocate(config, values, devices, "heat", 22, learning=model, state=state)[0][
            UNIT
        ]
        == 0
    )


@pytest.mark.parametrize("mode,direction", [("heat", 1), ("cool", -1)])
@pytest.mark.parametrize("running", [False, True])
def test_minor_hot_office_cannot_break_in_band_hold(mode, direction, running):
    config, state, model, devices, values = live(mode)
    values = dict.fromkeys(values, 22)
    values["office"] = 24.5
    assert set(counted_rooms(config, values)) == set(ROOMS[:6])
    state.commanded[UNIT] = running
    state.efforts[UNIT] = direction if running else 0
    devices[UNIT].hvac_action = (
        ("heating" if direction > 0 else "cooling") if running else "idle"
    )
    # Strong future drift asks the allocator to switch; the band holds instead.
    for row in model.rows.values():
        row.theta[-1] = direction * (1 if running else -1)
    state.test_observed_since[UNIT] = -10000
    control(
        config,
        state,
        {r: Reading(t) for r, t in values.items()},
        devices,
        0,
        learning=model,
    )
    assert bool(state.efforts[UNIT]) is running
    assert not state.excitation


@pytest.mark.parametrize("mode,direction", [("heat", 1), ("cool", -1)])
def test_no_room_on_helpful_side_neither_runs_nor_tests(mode, direction):
    config, state, model, devices, values = live(mode)
    values = dict.fromkeys(values, 22 + direction * 0.7)
    state.test_observed_since[UNIT] = -10000
    control(
        config,
        state,
        {r: Reading(t) for r, t in values.items()},
        devices,
        0,
        learning=model,
    )
    assert state.efforts[UNIT] == 0
    assert not state.excitation
    assert state.experiment_info[UNIT]["skip_reason"].startswith(
        "no counted room needs"
    )


@pytest.mark.parametrize("mode,direction", [("heat", 1), ("cool", -1)])
def test_1303_test_ignores_forecast_aborts_on_reading_after_min_run(mode, direction):
    config, state, model, devices, values = live(mode)
    # Isolate the promised experiment from allocation; variance keeps this unit idle.
    config["objective"] = "variance"
    parent_base, parent_effect = model.forecast("parent", values["parent"], [UNIT], 12)
    assert direction * (parent_base + direction * parent_effect[0] - 22) > 1.5
    readings = {r: Reading(t) for r, t in values.items()}
    start = 7200  # 13:03, already due since 11:44.
    control(config, state, readings, devices, start, learning=model)
    assert state.excitation["system"] == UNIT
    assert state.efforts[UNIT] == direction
    devices[UNIT].hvac_action = "heating" if direction > 0 else "cooling"
    state.observe_action(UNIT, devices[UNIT].hvac_action, start)
    readings["parent"] = Reading(22 + direction * 1.5)
    control(config, state, readings, devices, start + 300, learning=model)
    assert state.excitation  # Equality does not cross the ceiling.
    readings["parent"] = Reading(22 + direction * 1.51)
    control(config, state, readings, devices, start + 599, learning=model)
    assert state.excitation  # Never stop before ten minutes.
    assert state.efforts[UNIT] == direction
    result = control(config, state, readings, devices, start + 600, learning=model)
    assert not state.excitation
    assert state.efforts[UNIT] == 0
    info = state.experiment_info[UNIT]
    assert info["blocked_by"] == "parent"
    assert info["blocked_reading"] == readings["parent"].value
    assert info["blocked_ceiling"] == 22 + direction * 1.5
    assert "blocked_forecast" not in info
    assert "Parent Room" in info["skip_reason"]
    assert "Parent Room" in result.explanation
    assert info["next_eligible"] == start + 600 + 120 * 60
    assert (
        UNIT not in state.last_steady_test
    )  # Attempt is distinct from steady evidence.
    # The attempt clock survives Store roundtrips and a still-unsafe next pass.
    state = ControlState(**deepcopy(asdict(state)))
    devices[UNIT].hvac_action = "idle"
    readings["parent"] = Reading(values["parent"])
    control(config, state, readings, devices, start + 900, learning=model)
    assert not state.excitation
    assert state.experiment_info[UNIT]["next_eligible"] == start + 600 + 120 * 60
    control(config, state, readings, devices, start + 600 + 120 * 60, learning=model)
    assert state.excitation["system"] == UNIT


def test_never_started_due_test_retries_after_cycling_hold():
    config, state, model, devices, values = live()
    state.last_stop[UNIT] = 7199
    readings = {r: Reading(t) for r, t in values.items()}
    control(config, state, readings, devices, 7200, learning=model)
    assert not state.excitation
    assert state.experiment_info[UNIT]["skip_reason"] == "cycling hold"
    assert state.experiment_info[UNIT]["next_eligible"] == 7200
    control(config, state, readings, devices, 7499, learning=model)
    assert state.excitation["system"] == UNIT


def test_modulating_band_cost_beats_005_grid_with_priority_scaling():
    config = {
        "sensors": [
            {"entity_id": r, "weight": w}
            for r, w in zip(["cold", "warm", "minor"], [1, 0.5, 0.01])
        ],
        "systems": [{"entity_id": "unit", "modulating": True}],
        "control_horizon_minutes": 5,
        "comfort_band": 0,
        "switch_penalty": 0.03,
        "effort_change_penalty": 0.02,
    }
    model = Learning(["cold", "warm", "minor"], ["unit"])
    for row, effect in zip(model.rows.values(), [1.4, 0.2, 30]):
        row.theta[1] = effect
        row.counts = [1e30]
    values = {"cold": 20.8, "warm": 22.1, "minor": 100}
    state = ControlState(setpoint=22, efforts={"unit": 0.3})
    efforts, _ = allocate(
        config, values, {"unit": Device()}, "heat", 22, learning=model, state=state
    )

    def cost(u):
        cold, warm = -1.2 + 1.4 * u, 0.1 + 0.2 * u
        return (
            max(abs(cold), 0.5 * abs(warm))
            + 0.1 * (cold**2 + 0.5 * warm**2) / 2
            + 0.03 * (u > 0)
            + 0.02 * abs(u - 0.3)
        )

    assert 0 < efforts["unit"] < 1
    assert cost(efforts["unit"]) <= min(cost(k / 20) for k in range(21)) + 1e-12


@pytest.mark.parametrize("clock", ["last_start", "command_start"])
def test_existing_run_does_not_restart_minimum_clock_when_test_begins(clock):
    config, state, model, devices, values = live()
    state.commanded[UNIT] = True
    state.efforts[UNIT] = 1
    getattr(state, clock)[UNIT] = 6000
    if clock == "last_start":
        state.actions[UNIT] = "heating"
        devices[UNIT].hvac_action = "heating"
    else:
        devices[UNIT].hvac_action = None
    readings = {r: Reading(t) for r, t in values.items()}
    control(config, state, readings, devices, 7200, learning=model)
    assert state.excitation
    readings["parent"] = Reading(23.6)
    control(config, state, readings, devices, 7260, learning=model)
    assert not state.excitation
    assert state.efforts[UNIT] == 0
    assert state.experiment_info[UNIT]["next_eligible"] == 14460


@pytest.mark.parametrize("minimum", [10, 15])
def test_ceiling_abort_honors_later_reported_start_and_system_minimum(minimum):
    config, state, model, devices, values = live()
    config["systems"][0]["min_run_minutes"] = minimum
    readings = {r: Reading(t) for r, t in values.items()}
    control(config, state, readings, devices, 7200, learning=model)
    assert state.excitation
    # The thermostat acknowledges five minutes after the command.
    devices[UNIT].hvac_action = "heating"
    state.observe_action(UNIT, "heating", 7500)
    readings["parent"] = Reading(23.6)
    control(config, state, readings, devices, 7500 + minimum * 60 - 1, learning=model)
    assert state.excitation
    assert state.efforts[UNIT] > 0
    control(config, state, readings, devices, 7500 + minimum * 60, learning=model)
    assert not state.excitation
    assert state.efforts[UNIT] == 0
    assert state.experiment_info[UNIT]["next_eligible"] == 7500 + minimum * 60 + 7200


def test_confidence_range_alone_does_not_determine_live_allocation():
    config, state, model, devices, values = live()
    # Another assignment entirely inside the supplied 2–12% range chooses off.
    # Keep this explicit so the conditional on-reconstruction cannot be mistaken
    # for recovery of a saved live forecast from an aggregate confidence range.
    for room, confidence in zip(ROOMS[:6], [0.02, 0.04, 0.06, 0.08, 0.10, 0.12]):
        model.rows[room].counts = [24 * confidence / (1 - confidence)]
    assert max_deviation(config, model, values, 1) == pytest.approx(1.865242857)
    assert (
        allocate(config, values, devices, "heat", 22, learning=model, state=state)[0][
            UNIT
        ]
        == 0
    )

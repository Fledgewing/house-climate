"""Deterministic identification and closed-loop thermal behavior."""

import json
import math
import random

import pytest

from custom_components.house_climate.controller import (
    ControlState,
    Device,
    Reading,
    control,
)
from custom_components.house_climate.learning import PRIOR_B, EffortTracker, Learning


def test_rls_update():
    model = Learning(["room"], ["heat"], forgetting=1)
    model.update({"room": 20}, {"room": 20.23}, 20, {"heat": 1})
    row = model.rows["room"]
    assert row.theta[1] == pytest.approx(0.03 + 100 / 201 * 0.2)
    assert row.theta[-1] == pytest.approx(100 / 201 * 0.2)
    assert row.covariance[1][1] == pytest.approx(100 - 10000 / 201)
    assert row.counts == [1]
    assert row.samples == 1


def test_forgetting_and_blend():
    model = Learning(["room"], ["heat"], forgetting=0.5, n0=24)
    model.update({"room": 20}, {"room": 20.1}, 20, {"heat": 1})
    model.update({"room": 20}, {"room": 20}, 20, {"heat": 0})
    assert model.rows["room"].counts == [0.5]
    assert model.rows["room"].covariance[0][0] == 400
    model.rows["room"].counts = [24]
    model.rows["room"].theta[1] = 0.2
    assert model.confidence("room", "heat") == 0.5
    assert model.blended("room", "heat") == pytest.approx(0.115)


def test_roundtrip_and_membership():
    model = Learning(["a", "b"], ["x", "y"])
    model.update({"a": 20, "b": 18}, {"a": 20.1, "b": 18.2}, 5, {"x": 1, "y": 0})
    restored = Learning.from_dict(json.loads(json.dumps(model.to_dict())))
    assert restored.to_dict() == model.to_dict()
    restored.reconcile(["a", "new"], ["y", "x", "new_system"])
    assert set(restored.rows) == {"a", "new"}
    assert restored.rows["a"].theta[2] == model.rows["a"].theta[1]
    assert restored.rows["a"].covariance[2][2] == model.rows["a"].covariance[1][1]
    assert restored.rows["a"].theta[3] == PRIOR_B
    assert restored.rows["new"].theta == [0, 0.03, 0.03, 0.03, 0, 0]
    assert restored.system_confidence("x") == 0


@pytest.mark.parametrize("missing", [None, float("nan"), float("inf")])
def test_missing_sample(missing):
    model = Learning(["a", "b"], ["x"])
    model.update({"a": 20, "b": 20}, {"a": missing, "b": 20.1}, 5, {"x": 1})
    assert model.rows["a"].samples == 0
    assert model.rows["b"].samples == 1
    model.update({"b": 20}, {"b": 20.1}, missing, {"x": 1})
    assert model.rows["b"].samples == 1


def test_effort_transitions_and_unknown():
    tracker = EffortTracker()
    tracker.finish({"x": "idle", "y": "cooling"}, 0)
    tracker.change("x", "heating", 60)
    tracker.change("x", "idle", 210)
    effort, duration = tracker.finish({"x": "idle", "y": "cooling"}, 300)
    assert duration == 300
    assert effort == {"x": 0.5, "y": -1}
    tracker.change("x", None, 310)
    tracker.change("x", "heating", 320)
    effort, _ = tracker.finish({"x": "heating", "y": "idle"}, 600)
    assert effort["x"] is None
    assert effort["y"] == -1


def test_neutral_weights_fallback_and_dither():
    model = Learning(["a", "b"], ["x", "y"])
    assert model.influences("x") == {"a": 0.5, "b": 0.5}
    assert model.dither("x", 0) == 0.5
    assert model.dither("y", 0) == -0.5
    assert model.dither("x", 3600) == -0.5
    for row in model.rows.values():
        row.counts = [96, 96]
        row.theta[1] = -1
    assert model.dither("x", 0) == 0
    assert model.influences("x") == {}
    cfg = {
        "sensors": [{"entity_id": "a"}, {"entity_id": "b"}],
        "systems": [{"entity_id": "x"}],
    }
    result = control(
        cfg,
        ControlState(mode="heat"),
        {"a": Reading(20), "b": Reading(22)},
        {"x": Device()},
        0,
        learning=model,
    )
    assert "error +0.00" in result.explanation
    assert "exploring" not in result.explanation


ROOMS = ["living", "office", "upstairs", "downstairs", "bedroom", "guest"]
SYSTEMS = ["ducted", "split"]
TRUE_B = [
    [0.16, 0.24],
    [0.16, 0.05],
    [0.18, 0.005],
    [0.17, 0.005],
    [0.15, 0.002],
    [0.012, 0.001],
]


def simulate(enabled=True):
    """Two days, 5-min passes, fractionally cycling local thermostats.

    Each device senses the living room. A proportional duty band approximates
    cycling within the interval; effort is actual signed running fraction, not
    requested offset. Losses allow a uniform equilibrium at 50%/25% duty.
    Both runs share weather, physical house, initial state and seeded noise.
    """
    rng = random.Random(742)
    config = {
        "sensors": [{"entity_id": r} for r in ROOMS],
        "systems": [{"entity_id": s} for s in SYSTEMS],
    }
    state = ControlState(mode="heat", setpoint=21)
    model = Learning(ROOMS, SYSTEMS)
    temperatures = [20.0] * 6
    devices = {s: Device(mode="heat", temperature=21) for s in SYSTEMS}
    means, spreads, exploring = [], [], []
    checkpoints = {}
    runtime = EffortTracker()
    runtime.finish(dict.fromkeys(SYSTEMS, "idle"), 0)
    losses = [(b[0] * 0.5 + b[1] * 0.25) / 16 for b in TRUE_B]
    for step in range(576):
        now = step * 300
        outdoor = 5 + 2 * math.sin(2 * math.pi * step / 288)
        solar = max(0, math.sin(2 * math.pi * (step % 288 - 72) / 288)) * 3
        decision = control(
            config,
            state,
            dict(zip(ROOMS, map(Reading, temperatures))),
            devices,
            now,
            outdoor,
            learning=model if enabled else None,
        )
        for system, service, data in decision.commands:
            if service == "set_temperature":
                devices[system].temperature = data["temperature"]
            elif service == "set_hvac_mode":
                devices[system].mode = data["hvac_mode"]
        efforts = {
            s: max(0, min(1, 0.375 + 0.6 * (devices[s].temperature - temperatures[0])))
            for s in SYSTEMS
        }
        before = temperatures[:]
        temperatures = [
            t
            + a * (outdoor - t)
            + sum(b * efforts[s] for b, s in zip(bs, SYSTEMS))
            + 0.002 * solar
            + rng.gauss(0, 0.0005)
            for t, a, bs in zip(before, losses, TRUE_B)
        ]
        # Real action transitions: each thermostat cycles on, then off, within
        # a pass. Integrate those transitions rather than sampling the endpoint.
        for system, effort in efforts.items():
            runtime.change(system, "heating" if effort else "idle", now)
            runtime.change(system, "idle", now + effort * 300)
        measured, _ = runtime.finish(dict.fromkeys(SYSTEMS, "idle"), now + 300)
        if enabled:
            model.update(
                dict(zip(ROOMS, before)),
                dict(zip(ROOMS, temperatures)),
                outdoor,
                measured,
                solar,
            )
        if step in (47, 71):
            checkpoints[step] = [[model.blended(r, s) for s in SYSTEMS] for r in ROOMS]
        means.append(sum(temperatures) / 6)
        spreads.append(max(temperatures) - min(temperatures))
        exploring.append("exploring" in decision.explanation)
    return model, means, spreads, exploring, checkpoints


def test_two_day_closed_loop():
    model, means, spreads, exploring, checkpoints = simulate()
    _, _, baseline, _, _ = simulate(False)
    learned = [[model.rows[r].theta[j + 1] for j in range(2)] for r in ROOMS]
    print("\nlearned / true:", list(zip(ROOMS, learned, TRUE_B)))
    print(
        "spread learned / disabled:",
        spreads[-1],
        baseline[-1],
        "day2 mean range:",
        min(means[288:]),
        max(means[288:]),
        "confidence:",
        model.confidence_percent,
    )
    # Absolute coefficient tolerance: 0.003 °C per full-running interval.
    for actual, expected in zip(learned, TRUE_B):
        assert actual == pytest.approx(expected, abs=0.003)
    assert max(abs(t - 21) for t in means[288:]) <= 0.5
    assert spreads[-1] < baseline[-1]
    # Four hours: learned weights are already much closer than neutral priors.
    prior_error = sum(abs(b - PRIOR_B) for row in TRUE_B for b in row)
    early_error = sum(
        abs(a - b)
        for row, truth in zip(checkpoints[47], TRUE_B)
        for a, b in zip(row, truth)
    )
    assert early_error < prior_error / 2
    # Six hours: the changed weights already improve the physical room spread.
    assert spreads[71] < baseline[71]
    assert any(exploring[:24])
    assert not any(exploring[-24:])


@pytest.mark.parametrize("sign", [1, -1])
def test_identifies_full_model_and_signed_effort(sign):
    rng = random.Random(194)
    model = Learning(["room"], ["one", "two"])
    truth = [0.004, 0.12, 0.007, 0.003, 0.015]
    for _ in range(240):
        temperature = rng.uniform(18, 24)
        outdoor = rng.uniform(0, 15)
        efforts = {"one": sign * rng.random(), "two": sign * rng.random()}
        solar = rng.uniform(0, 4)
        x = [outdoor - temperature, *efforts.values(), solar, 1]
        delta = sum(a * b for a, b in zip(truth, x))
        model.update(
            {"room": temperature},
            {"room": temperature + delta},
            outdoor,
            efforts,
            solar,
        )
    assert model.rows["room"].theta == pytest.approx(truth, abs=0.0001)


def test_learned_error_uses_sensor_weights_and_preserves_safeguards():
    model = Learning(["cold", "warm"], ["one", "two"])
    for sensor, bs in [("cold", [0.2, 0]), ("warm", [0, 0.2])]:
        model.rows[sensor].theta[1:3] = bs
        model.rows[sensor].counts = [200, 200]
    config = {
        "sensors": [
            {"entity_id": "cold", "weight": 2},
            {"entity_id": "warm", "weight": 1},
        ],
        "systems": [{"entity_id": "one"}, {"entity_id": "two"}],
    }
    state = ControlState(mode="heat")
    devices = {s: Device(mode="heat") for s in model.systems}
    readings = {"cold": Reading(19), "warm": Reading(23)}
    decision = control(config, state, readings, devices, 0, learning=model)
    assert state.offsets == {"one": 0.5, "two": -0.5}
    assert "driven by cold, warm" in decision.explanation
    assert "driven by warm, cold" in decision.explanation
    for step in range(1, 30):
        before = dict(state.offsets)
        control(config, state, readings, devices, step * 300, learning=model)
        assert all(abs(state.offsets[s] - before[s]) <= 0.5 for s in devices)
        assert all(abs(offset) <= 3 for offset in state.offsets.values())


def test_exploration_obeys_clamp_slew_and_immediate_calls():
    model = Learning(["room"], ["one", "two"])
    config = {
        "sensors": [{"entity_id": "room"}],
        "systems": [{"entity_id": s} for s in model.systems],
        "offset_clamp": 0.5,
    }
    state = ControlState(mode="heat")
    readings = {"room": Reading(21)}
    devices = {s: Device(mode="heat") for s in model.systems}
    decision = control(config, state, readings, devices, 0, learning=model)
    assert "exploring" in decision.explanation
    assert state.offsets == {"one": 0.5, "two": -0.5}
    control(config, state, readings, devices, 1, learning=model)
    assert state.offsets == {"one": 0.5, "two": -0.5}
    control(config, state, readings, devices, 3600, learning=model)
    assert state.offsets == {"one": 0, "two": 0}
    assert not control(
        config, state, readings, devices, 3900, enabled=False, learning=model
    ).commands


def test_missing_influential_room_reports_house_fallback():
    model = Learning(["a", "b"], ["one"])
    model.rows["a"].theta[1] = -1
    for row in model.rows.values():
        row.counts = [200]
    config = {
        "sensors": [{"entity_id": "a"}, {"entity_id": "b"}],
        "systems": [{"entity_id": "one"}],
    }
    decision = control(
        config,
        ControlState(mode="heat"),
        {"a": Reading(20)},
        {"one": Device()},
        0,
        learning=model,
    )
    assert "error +1.00 °C driven by a" in decision.explanation

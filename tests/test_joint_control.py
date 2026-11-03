"""Replay the shared-living-sensor failure and check the joint objective directly."""

from math import sqrt

import pytest

from custom_components.house_climate.controller import (
    ControlState,
    Device,
    Reading,
    allocate,
    control,
)
from custom_components.house_climate.learning import Learning

ROOMS = ["living", "downstairs", "guest bedroom"]
SYSTEMS = ["Mitsubishi", "Bryant"]
WEIGHTS = [1, 2, 2]
TRUTH = [[0.85, 0.3], [0.15, 0.4], [0.15, 0.4]]
LOSSES = [bs[1] * 0.5 / 15.5 for bs in TRUTH]


def replay(available=True, hours=12):
    config = {
        "sensors": [{"entity_id": r, "weight": w} for r, w in zip(ROOMS, WEIGHTS)],
        "systems": [{"entity_id": s} for s in SYSTEMS],
    }
    model = Learning(ROOMS, SYSTEMS)
    for i, room in enumerate(ROOMS):
        model.rows[room].theta = [LOSSES[i], TRUTH[i][0], 0.3 if i == 0 else 0.03, 0, 0]
        model.rows[room].counts = [200, 80 if i == 0 else 1]
    state = ControlState(mode="heat", setpoint=22.5, offsets={"Mitsubishi": 1.5})
    devices = {
        s: Device(
            mode="heat",
            temperature=24,
            current_temperature=24.5,
            name=s,
            available=available or s == "Mitsubishi",
        )
        for s in SYSTEMS
    }
    temperatures = [24.5, 19.4, 18.7]
    spreads, means, initial = [], [], []
    for step in range(hours * 12):
        for device in devices.values():
            device.current_temperature = temperatures[0]
        decision = control(
            config,
            state,
            dict(zip(ROOMS, map(Reading, temperatures))),
            devices,
            step * 300,
            outdoor=7,
            learning=model,
        )
        for system, service, data in decision.commands:
            if service == "set_temperature":
                devices[system].temperature = data["temperature"]
        if step < 2:
            initial.append(
                {s: d.temperature - d.current_temperature for s, d in devices.items()}
            )
        before = temperatures[:]
        efforts = dict.fromkeys(SYSTEMS, 0.0)
        for minute in range(5):
            running = {
                s: d.available and d.temperature > temperatures[0]
                for s, d in devices.items()
            }
            for s, on in running.items():
                efforts[s] += on / 5
                devices[s].hvac_action = "heating" if on else "idle"
                state.observe_action(
                    s, devices[s].hvac_action, step * 300 + minute * 60
                )
            temperatures = [
                t + (a * (7 - t) + sum(b * running[s] for b, s in zip(bs, SYSTEMS))) / 5
                for t, a, bs in zip(temperatures, LOSSES, TRUTH)
            ]
        model.update(
            dict(zip(ROOMS, before)), dict(zip(ROOMS, temperatures)), 7, efforts
        )
        average = sum(w * t for w, t in zip(WEIGHTS, temperatures)) / sum(WEIGHTS)
        means.append(average)
        spreads.append(
            sqrt(
                sum(w * (t - average) ** 2 for w, t in zip(WEIGHTS, temperatures))
                / sum(WEIGHTS)
            )
        )
    return initial, means, spreads, decision


def test_shared_sensor_house_replay():
    initial, means, spreads, decision = replay()
    assert any(p["Bryant"] > 0 and p["Mitsubishi"] < 0 for p in initial)
    assert abs(means[-1] - 22.5) < 0.5
    assert spreads[-1] < 1.5
    assert "Bryant" in decision.explanation and "average" in decision.explanation
    print(
        f"\nShared sensor 12h: mean {means[-1]:.4f}, weighted spread {spreads[-1]:.4f}; first targets relative to own {initial}"
    )


def test_bryant_unavailable_replay():
    _, means, spreads, _ = replay(False)
    assert max(spreads) < 9
    # This actuator cannot equalize these rooms; test a bounded equilibrium,
    # rather than imposing a comfort cap the objective does not contain.
    _, _, long_spreads, _ = replay(False, hours=168)
    assert max(long_spreads) < 10
    assert max(long_spreads[-288:]) - min(long_spreads[-288:]) < 0.5
    print(
        f"\nBryant unavailable 12h: mean {means[-1]:.4f}, weighted spread {spreads[-1]:.4f}, peak {max(spreads):.4f}"
    )


@pytest.mark.parametrize("mode,sign", [("heat", 1), ("cool", -1)])
@pytest.mark.parametrize("penalty", [0, 1, 4])
def test_one_available_system_is_objective_minimum(mode, sign, penalty):
    config = {
        "sensors": [{"entity_id": r, "weight": w} for r, w in zip(ROOMS, WEIGHTS)],
        "systems": [{"entity_id": s} for s in SYSTEMS],
        "fan_reward_lambda": penalty,
    }
    model = Learning(ROOMS, SYSTEMS)
    values = dict(zip(ROOMS, [24.5, 19.4, 18.7] if sign == 1 else [20.5, 25.6, 26.3]))
    for room, bs in zip(ROOMS, TRUTH):
        model.rows[room].theta[1:3] = bs
        model.rows[room].counts = [1e9, 1e9]
    efforts, _ = allocate(
        config,
        values,
        {"Mitsubishi": Device(), "Bryant": Device(available=False)},
        mode,
        22.5,
        learning=model,
    )

    def objective(u):
        predicted = [
            values[r] + 9 * max(0, model.blended(r, "Mitsubishi")) * u for r in ROOMS
        ]
        average = sum(w * t for w, t in zip(WEIGHTS, predicted)) / sum(WEIGHTS)
        return (
            sum(w * (t - average) ** 2 for w, t in zip(WEIGHTS, predicted))
            / sum(WEIGHTS)
            + penalty * (average - 22.5) ** 2
        )

    assert set(efforts) == {"Mitsubishi"}
    assert (
        objective(efforts["Mitsubishi"])
        <= min(objective(sign * k / 1000) for k in range(1001)) + 1e-9
    )


@pytest.mark.parametrize(
    "mode,target,expected", [("heat", 20.6, 0.5), ("cool", 19.8, -0.5)]
)
def test_forecast_uses_losses_sources_solar_and_drift(mode, target, expected):
    model = Learning(["room"], ["system"], sources=["source"])
    model.rows["room"].theta = [0.01, 0.8, 0.1, 0.05, 0.1]
    model.rows["room"].counts = [80, 80]
    config = {
        "sensors": [{"entity_id": "room"}],
        "systems": [{"entity_id": "system"}],
        "control_horizon_minutes": 5,
    }
    efforts, predicted = allocate(
        config,
        {"room": 20},
        {"system": Device()},
        mode,
        target,
        10,
        model,
        solar=2,
        sources={"source": 1},
    )
    assert efforts["system"] == pytest.approx(expected)
    assert predicted == pytest.approx(target)


def test_joint_allocation_beats_every_two_system_grid_point():
    config = {
        "sensors": [{"entity_id": r, "weight": w} for r, w in zip(ROOMS, WEIGHTS)],
        "systems": [{"entity_id": s} for s in SYSTEMS],
    }
    model = Learning(ROOMS, SYSTEMS)
    values = dict(zip(ROOMS, [24.5, 19.4, 18.7]))
    for room, bs in zip(ROOMS, TRUTH):
        model.rows[room].theta[1:3] = bs
        model.rows[room].counts = [200, 1 if room != "living" else 80]
    efforts, _ = allocate(
        config, values, dict.fromkeys(SYSTEMS, Device()), "heat", 22.5, learning=model
    )

    def objective(a, b):
        predicted = [
            values[r]
            + 9 * (model.blended(r, SYSTEMS[0]) * a + model.blended(r, SYSTEMS[1]) * b)
            for r in ROOMS
        ]
        average = sum(w * t for w, t in zip(WEIGHTS, predicted)) / sum(WEIGHTS)
        return (
            sum(w * (t - average) ** 2 for w, t in zip(WEIGHTS, predicted))
            / sum(WEIGHTS)
            + (average - 22.5) ** 2
        )

    assert (
        objective(*efforts.values())
        <= min(objective(i / 50, j / 50) for i in range(51) for j in range(51)) + 1e-9
    )
    assert efforts["Mitsubishi"] == 0
    assert efforts["Bryant"] > 0.5

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
        model.rows[room].theta = [
            LOSSES[i],
            TRUTH[i][0],
            0.3 if i == 0 else 0.03,
            0,
            0,
            0,
            0,
        ]
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
        "average_weight": penalty,
        "fan_reward_lambda": 99,  # Explicit fan tuning must not alter allocation.
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
    model.rows["room"].theta = [0.01, 0.8, 0.1, 0, 0, 0.05, 0.1]
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


@pytest.mark.parametrize(
    "mode,action,sign", [("heat", "heating", 1), ("cool", "cooling", -1)]
)
@pytest.mark.parametrize("modulating,fraction", [(False, 1), (True, 0.4)])
def test_unavailable_reported_input_reduces_remaining_effort(
    mode, action, sign, modulating, fraction
):
    config = {
        "sensors": [{"entity_id": "room"}],
        "systems": [
            {"entity_id": "lost", "modulating": modulating},
            {"entity_id": "live", "modulating": True},
        ],
        "switch_penalty": 0,
        "effort_change_penalty": 0,
        "comfort_band": 0,
    }
    model = Learning(["room"], ["lost", "live"])
    model.rows["room"].theta[1:3] = [0.1, 0.2]
    model.rows["room"].counts = [1e9, 1e9]
    state = ControlState(mode=mode, active_mode=mode)
    state.observe_report(
        config["systems"][0],
        Device(
            hvac_action=action,
            temperature=20 + sign * fraction,
            current_temperature=20,
            name="Home ZONE 1",
        ),
        config,
    )
    devices = {"lost": Device(available=False), "live": Device(current_temperature=20)}
    values = {"room": 21 - sign}
    off, _ = allocate(
        config, values, devices, mode, 21, learning=model, state=ControlState()
    )
    running, predicted = allocate(
        config, values, devices, mode, 21, learning=model, state=state
    )
    assert set(running) == {"live"}
    assert abs(running["live"]) < abs(off["live"])
    assert abs(off["live"] - running["live"]) == pytest.approx(0.5)
    assert predicted == pytest.approx(21)
    decision = control(
        config, state, {"room": Reading(values["room"])}, devices, 0, learning=model
    )
    assert all(key == "live" for key, _, _ in decision.commands)
    assert f"assuming {action} until it reaches it" in decision.explanation
    assert state.actions["lost"] is None
    assert state.reported_actions["lost"] == action
    # A known idle report replaces running evidence and contributes exactly zero.
    state.observe_report(config["systems"][0], Device(hvac_action="idle"), config)
    idle, _ = allocate(config, values, devices, mode, 21, learning=model, state=state)
    assert idle == pytest.approx(off)


def balance_replay(average_weight, integral_gain=0.001):
    """12h fixed-model replay: weak basement reach, explicit passive losses.

    Effects are degrees per five-minute interval at full output. Outdoor is 7 C,
    passive losses are 0.01/interval in living and b_B*0.5/13.2 in the
    cold rooms (half of B offsets their initial loss); weights are 1:2:2.
    Fine targets model continuous output; no new learning obscures the comparison.
    """
    truth = [[0.83, 0.37], [0.03, 0.02], [0.09, 0.09]]
    config = {
        "sensors": [{"entity_id": r, "weight": w} for r, w in zip(ROOMS, WEIGHTS)],
        "systems": [{"entity_id": s, "modulating": True} for s in SYSTEMS],
        "average_weight": average_weight,
        "mean_integral_gain": integral_gain,
        "excite_after_minutes": 10080,
    }
    losses = [0.01, 0.02 * 0.5 / 13.2, 0.09 * 0.5 / 13.2]
    model = Learning(ROOMS, SYSTEMS)
    for room, bs, loss in zip(ROOMS, truth, losses):
        model.rows[room].theta = [loss, *bs, *([0] * len(bs)), 0, 0]
        model.rows[room].counts = [1e12, 1e12]
    state = ControlState(mode="heat", setpoint=22.5)
    temperatures = [25, 20.2, 20.2]
    devices = {s: Device(mode="heat", step=0.01) for s in SYSTEMS}
    peak_integral = 0
    for step in range(12 * 12):
        for device in devices.values():
            device.current_temperature = temperatures[0]
        control(
            config,
            state,
            dict(zip(ROOMS, map(Reading, temperatures))),
            devices,
            step * 300,
            outdoor=7,
            learning=model,
        )
        # Perfect continuous actuator tracks the requested thermal allocation.
        efforts = [state.requested_efforts[s] for s in SYSTEMS]
        temperatures = [
            t + loss * (7 - t) + sum(b * u for b, u in zip(bs, efforts))
            for t, bs, loss in zip(temperatures, truth, losses)
        ]
        peak_integral = max(peak_integral, abs(state.integrals.get("mean", 0)))
    average = sum(w * t for w, t in zip(WEIGHTS, temperatures)) / sum(WEIGHTS)
    variance = sum(w * (t - average) ** 2 for w, t in zip(WEIGHTS, temperatures)) / sum(
        WEIGHTS
    )
    return temperatures, average, sqrt(variance), peak_integral


def test_weak_basement_reach_balance_replay():
    results = {weight: balance_replay(weight) for weight in (1, 0.5, 0.2, 0.1)}
    for weight, (rooms, average, spread, integral) in results.items():
        print(
            f"\nlambda={weight}: rooms={rooms}, mean={average:.4f}, weighted RMS spread={spread:.4f}, peak integral={integral:.6f}"
        )
    assert results[1][0][0] > 24
    assert results[0.2][1] < 22.5
    assert results[0.2][2] < results[1][2] * 0.6
    assert results[0.2][3] == 0
    assert results[0.2] == balance_replay(0.2, integral_gain=0)
    assert all(
        results[a][2] > results[b][2] for a, b in zip((1, 0.5, 0.2), (0.5, 0.2, 0.1))
    )

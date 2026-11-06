"""Late sensor onboarding must not trap an unobserved system at zero runtime."""

from copy import deepcopy
from dataclasses import asdict
from math import floor, sqrt
from unittest.mock import patch

import pytest

from custom_components.house_climate.controller import (
    ControlState,
    Device,
    Reading,
    allocate,
    control,
    excitation_target,
)
from custom_components.house_climate.learning import PRIOR_B, Learning


def setup(mode="heat"):
    config = {
        "sensors": [{"entity_id": "warm"}, {"entity_id": "cold", "weight": 2}],
        "systems": [{"entity_id": "a"}, {"entity_id": "b"}],
        "excite_minutes": 30,  # Explicit shorter experiment for this fixture.
    }
    state = ControlState(mode=mode, setpoint=22.5)
    model = Learning(["warm", "cold"], ["a", "b"])
    devices = {
        s: Device(
            mode=mode,
            current_temperature=24,
            temperature=19.5,
            name=s.upper(),
            hvac_action="idle",
        )
        for s in model.systems
    }
    readings = {"warm": Reading(22), "cold": Reading(18.8)}
    return config, state, model, devices, readings


def test_system_mean_prior_and_confidence_blend():
    model = Learning(["one", "two", "new"], ["a", "b"])
    assert model.blended("new", "a") == PRIOR_B
    model.rows["one"].counts = [24, 0]  # confidence 0.5
    model.rows["two"].counts = [72, 0]  # confidence 0.75
    model.rows["one"].theta[1] = 0.2
    model.rows["two"].theta[1] = 0.5
    prior = (0.5 * 0.2 + 0.75 * 0.5) / 1.25
    assert model.blended("new", "a") == pytest.approx(prior)
    assert model.blended("one", "a") == pytest.approx(0.5 * 0.2 + 0.5 * prior)
    assert model.blended("new", "b") == PRIOR_B
    model.rows["new"].theta[1] = 100  # no runtime: this estimate is no evidence
    assert model.system_prior("a") == pytest.approx(prior)


def tick(parts, now, **kwargs):
    config, state, model, devices, readings = parts
    # Isolate the idle-unit experiment path from normal thermal allocation.
    with patch(
        "custom_components.house_climate.controller.allocate",
        return_value=(dict.fromkeys(devices, 0.0), state.setpoint),
    ):
        return control(config, state, readings, devices, now, learning=model, **kwargs)


def test_excitation_waits_then_holds_local_target_and_one_system():
    parts = setup()
    config, state, _, devices, _ = parts
    tick(parts, 0)
    tick(parts, 7199)
    assert not state.excitation
    result = tick(parts, 7200)
    assert state.excitation["system"] == "a"
    assert state.excitation["target"] == 25
    assert (
        "testing A for 30 min to learn its effect on cold, warm" in result.explanation
    )
    assert ("a", "set_temperature", {"temperature": 25}) in result.commands
    assert len(state.excitation) > 0
    devices["a"].current_temperature = 24.5
    devices["a"].hvac_action = "heating"
    tick(parts, 7500)
    assert state.excitation["runtime"] == 0
    assert state.excitation["target"] == 25
    # Transition between ticks: only actual heating seconds count.
    state.observe_action("a", "idle", 8100)
    tick(parts, 8400)
    assert state.excitation["runtime"] == 0
    state.observe_action("a", "heating", 8500)
    tick(parts, 10300)
    assert not state.excitation  # 30 observed running minutes, not 30 wall minutes
    tick(parts, 10600)
    assert state.excitation["system"] == "b"


@pytest.mark.parametrize(
    "reason",
    [
        "past",
        "equal",
        "disabled",
        "off",
        "veto",
        "confident",
        "missing",
        "zero_weight",
        "stale",
        "not_reporting",
        "no_local",
        "unavailable",
        "recent_run",
    ],
)
def test_excitation_conditions(reason):
    parts = setup()
    config, state, model, devices, readings = parts
    tick(parts, 0)
    kwargs = {}
    if reason in ("past", "equal"):
        readings["cold"] = Reading(24 if reason == "past" else 22.75)
    elif reason == "disabled":
        kwargs["enabled"] = False
    elif reason == "off":
        state.mode = "off"
    elif reason == "veto":
        kwargs["outdoor"] = 30
    elif reason in ("confident", "missing", "zero_weight", "stale", "not_reporting"):
        model.rows["warm"].covariance = [[0] * 9 for _ in range(9)]
        model.rows["warm"].counts = [200, 200]
        readings["warm"] = Reading(21)
        if reason == "confident":
            model.rows["cold"].counts = [200, 200]
            model.rows["cold"].covariance = [[0] * 9 for _ in range(9)]
        elif reason == "missing":
            readings["cold"] = Reading(None)
        elif reason == "zero_weight":
            config["sensors"][1]["weight"] = 0
        elif reason == "stale":
            readings["cold"].age = 50000
        else:
            readings["cold"].reporting = False
    elif reason == "no_local":
        for d in devices.values():
            d.current_temperature = None
    elif reason == "unavailable":
        for d in devices.values():
            d.available = False
    else:
        for s in devices:
            state.observe_action(s, "heating", 3500)
            state.observe_action(s, "idle", 7150)
    result = tick(parts, 7200, **kwargs)
    if reason == "disabled":
        assert not result.commands
    if reason == "equal":
        assert state.excitation  # Equality is eligible under the v0.5.9 safety gate.
    else:
        assert not state.excitation


@pytest.mark.parametrize("mode,local,expected", [("heat", 26, 27), ("cool", 18, 17)])
def test_excitation_clamp_cooling_and_device_limits(mode, local, expected):
    parts = setup(mode)
    _, state, _, devices, readings = parts
    for d in devices.values():
        d.current_temperature = local
    if mode == "cool":
        readings["cold"] = Reading(26)
        readings["warm"] = Reading(24)
    tick(parts, 0)
    tick(parts, 7200)
    assert state.excitation["target"] == expected
    parts = setup(mode)
    _, state, _, devices, readings = parts
    for d in devices.values():
        d.current_temperature = 24 if mode == "heat" else 22
        d.maximum = 24.5
        d.minimum = 21.5
    readings["cold"] = Reading(18 if mode == "heat" else 26)
    if mode == "cool":
        readings["warm"] = Reading(24)
    tick(parts, 0)
    tick(parts, 7200)
    assert state.excitation["target"] == (24.5 if mode == "heat" else 21.5)


def test_timeout_abort_retries_without_postponing_evidence_and_persistence():
    parts = setup()
    _, state, _, _, readings = parts
    tick(parts, 0)
    tick(parts, 7200)
    saved = asdict(state)
    assert ControlState(**deepcopy(saved)) == state
    tick(parts, 10800)
    assert not state.excitation
    tick(parts, 11100)
    assert state.excitation["system"] == "a"  # timeout leaves the test due
    tick(parts, 11200, enabled=False)
    assert not state.excitation
    tick(parts, 11400)
    assert state.excitation["system"] == "a"
    tick(parts, 18300)
    assert not state.excitation  # Another unacknowledged attempt times out.
    tick(parts, 18600)
    assert state.excitation
    readings["cold"] = Reading(25)
    tick(parts, 18700)
    assert not state.excitation


class LegacyLearning(Learning):
    """v0.4.1 comparison: fixed prior blend and dither, no forced runs."""

    def blended(self, sensor, system, outdoor=None, temperature=None):
        c = self.confidence(sensor, system)
        return (
            c * self.rows[sensor].theta[self.systems.index(system) + 1]
            + (1 - c) * PRIOR_B
        )


def legacy_control(config, state, readings, devices, now, outdoor=None, learning=None):
    """Frozen v0.4.1 PI target law for the historical comparison only."""
    from custom_components.house_climate.controller import Decision, clamp, mean

    values = {r: reading.value for r, reading in readings.items()}
    dt = int(state.last_update is None or now - state.last_update >= 300)
    commands = []
    for system in learning.systems:
        influence = learning.influences(system)
        members = [
            {**m, "weight": m.get("weight", 1) * influence.get(m["entity_id"], 0)}
            for m in config["sensors"]
        ]
        local = mean(values, members) or mean(values, config["sensors"])
        error = state.setpoint - local
        old = state.offsets.get(system, 0)
        candidate = state.integrals.get(system, 0) + 0.1 * error * dt
        raw = error + candidate + learning.dither(system, now, values.keys())
        bounded = clamp(clamp(raw, -3, 3), old - 0.5 * dt, old + 0.5 * dt)
        if (raw - bounded) * error <= 0:
            state.integrals[system] = candidate
        offset = clamp(floor(bounded * 2 + 0.5) / 2, -3, 3)
        state.offsets[system] = offset
        target = clamp(
            floor((state.setpoint + offset) * 2 + 0.5) / 2,
            devices[system].minimum,
            devices[system].maximum,
        )
        if target != devices[system].temperature:
            commands.append((system, "set_temperature", {"temperature": target}))
    if dt:
        state.last_update = now
    return Decision(
        mean(values, config["sensors"]),
        max(values.values()) - min(values.values()),
        values,
        commands,
        "",
    )


def simulate_late_sensors(legacy=False):
    rooms, systems = ["living", "living_other", "downstairs", "guest"], ["a", "b"]
    truth = [[0.50, 0.26], [0.50, 0.50], [0.005, 0.40], [0.004, 0.45]]
    losses = [0.5 / 17, 0.5 / 17, 0.005 / 11.8, 0.004 / 11.8]
    model = (LegacyLearning if legacy else Learning)(rooms[:2], systems)
    for i, room in enumerate(rooms[:2]):
        model.rows[room].theta = [losses[i], *truth[i], 0, 0, 0, 0, 0, 0]
        model.rows[room].counts = [80, 80]
    model.reconcile(rooms, systems)  # cold sensors join AFTER b last ran
    config = {
        "sensors": [{"entity_id": r, "weight": w} for r, w in zip(rooms, [1, 1, 2, 2])],
        "systems": [{"entity_id": s} for s in systems],
    }
    if legacy:
        config["excite_after_minutes"] = float("inf")
    state = ControlState(
        mode="heat",
        setpoint=22.5,
        offsets={"a": -3, "b": -3},
        last_run={"a": 0, "b": -14 * 3600},
    )
    devices = {
        s: Device(mode="heat", temperature=19.5, hvac_action="idle") for s in systems
    }
    temperatures = [24, 24, 18.8, 18.8]
    first_b = None
    spreads = []
    weights = [1, 1, 2, 2]
    for step in range(576):
        now = step * 300
        # A's local sensor is 4.5 C cooler than the living-room member sensor.
        # At its 19.5 C target, A keeps B's local sensor near 24 C.
        # The cold-room losses balance A's tiny effects near 18.8 C; B's
        # much stronger reach is unknown because these sensors just joined.
        devices["a"].current_temperature = temperatures[0] - 4.5
        devices["b"].current_temperature = temperatures[0]
        result = (legacy_control if legacy else control)(
            config,
            state,
            dict(zip(rooms, map(Reading, temperatures))),
            devices,
            now,
            outdoor=7,
            learning=model,
        )
        for s, service, data in result.commands:
            if service == "set_temperature":
                devices[s].temperature = data["temperature"]
        before = temperatures[:]
        efforts = dict.fromkeys(systems, 0.0)
        for minute in range(5):
            for s, sensor in [("a", 0), ("b", 0)]:
                running = (
                    temperatures[sensor] - (4.5 if s == "a" else 0)
                    < devices[s].temperature
                )
                devices[s].hvac_action = "heating" if running else "idle"
                state.observe_action(s, devices[s].hvac_action, now + minute * 60)
                efforts[s] += running / 5
                if s == "b" and running and first_b is None:
                    first_b = (now + minute * 60) / 60
            temperatures = [
                t
                + (
                    loss * (7 - t)
                    + sum(
                        b * (devices[s].hvac_action == "heating")
                        for b, s in zip(bs, systems)
                    )
                )
                / 5
                for t, loss, bs in zip(temperatures, losses, truth)
            ]
        model.update(
            dict(zip(rooms, before)), dict(zip(rooms, temperatures)), 7, efforts
        )
        average = sum(w * t for w, t in zip(weights, temperatures)) / sum(weights)
        spreads.append(
            sqrt(
                sum(w * (t - average) ** 2 for w, t in zip(weights, temperatures))
                / sum(weights)
            )
        )
    return model, first_b, spreads


def test_late_sensor_two_day_regression():
    model, first_b, spreads = simulate_late_sensors()
    baseline_model, baseline_first, baseline = simulate_late_sensors(True)
    coefficients = {r: model.rows[r].theta[1:3] for r in ("downstairs", "guest")}
    print(
        f"\nLate sensors: first B run {first_b} min (v0.4.1 {baseline_first}); weighted spread final {spreads[-1]:.4f} vs {baseline[-1]:.4f} C; day2 mean spread {sum(spreads[288:]) / 288:.4f} vs {sum(baseline[288:]) / 288:.4f} C; learned [A,B] {coefficients}"
    )
    assert first_b is not None and first_b <= 360
    assert baseline_first is None
    assert baseline_model.rows["guest"].counts[1] == 0
    for room in coefficients:
        assert model.rows[room].theta[2] > model.rows[room].theta[1]
    assert spreads[-1] < baseline[-1]
    assert sum(spreads[288:]) < sum(baseline[288:])


@pytest.mark.parametrize("mode", ["heat", "cool"])
def test_safety_ignores_passive_drift_but_reports_test_heated_room(mode):
    direction = 1 if mode == "heat" else -1
    rooms = ["sensor.office", "sensor.warm", "sensor.cold"]
    config = {
        "systems": [{"entity_id": "unit"}],
        "sensors": [{"entity_id": r} for r in rooms],
    }
    state = ControlState(
        mode=mode, active_mode=mode, setpoint=22, test_observed_since={"unit": 0}
    )
    model = Learning(rooms, ["unit"])
    for room, rate in zip(rooms, [0.007, 0.1, 0.1]):
        row = model.rows[room]
        row.counts = [1e9]
        row.theta[1] = rate / 12
    model.rows[rooms[0]].theta[-1] = direction * 0.36 / 12
    devices = {"unit": Device(mode=mode, current_temperature=22, hvac_action="idle")}
    values = dict(
        zip(rooms, [22 + direction * 3.7, 22 + direction * 1.2, 22 - direction * 3])
    )
    excitation_target(
        config,
        state,
        devices,
        values,
        22 - direction * 0.4,
        mode,
        7200,
        model,
        allocation={"unit": 0},
    )
    assert state.excitation["system"] == "unit"
    assert state.experiment_info["unit"]["blocked_by"] is None
    # Its observed independent drift can also carry it beyond the anchored
    # ceiling during the test without attributing that movement to the unit.
    values[rooms[0]] += direction * 0.4
    excitation_target(
        config,
        state,
        devices,
        values,
        22 - direction * 0.4,
        mode,
        7300,
        model,
        allocation={"unit": 0},
    )
    assert state.excitation["system"] == "unit"
    # Still protect a room whose crossing is actually caused by this test.
    state.excitation.clear()
    model.rows[rooms[1]].theta[1] = 0.6 / 12
    excitation_target(
        config,
        state,
        devices,
        values,
        22 - direction * 0.4,
        mode,
        7500,
        model,
        allocation={"unit": 0},
    )
    assert not state.excitation
    info = state.experiment_info["unit"]
    assert info["blocked_by"] == rooms[1]
    assert direction * (info["blocked_forecast"] - info["blocked_ceiling"]) > 0
    assert rooms[1] in info["skip_reason"]
    assert info["next_eligible"] == 7200
    model.rows[rooms[1]].theta[1] = 0.1 / 12
    excitation_target(
        config,
        state,
        devices,
        values,
        22 - direction * 0.4,
        mode,
        7800,
        model,
        allocation={"unit": 0},
    )
    assert state.excitation["system"] == "unit"
    # Abort is immediately retryable too, without resetting the evidence clock.
    model.rows[rooms[1]].theta[1] = 0.6 / 12
    excitation_target(
        config,
        state,
        devices,
        values,
        22 - direction * 0.4,
        mode,
        8100,
        model,
        allocation={"unit": 0},
    )
    assert not state.excitation
    assert state.experiment_info["unit"]["next_eligible"] == 7200
    model.rows[rooms[1]].theta[1] = 0.1 / 12
    excitation_target(
        config,
        state,
        devices,
        values,
        22 - direction * 0.4,
        mode,
        8400,
        model,
        allocation={"unit": 0},
    )
    assert state.excitation["system"] == "unit"


@pytest.mark.parametrize("mode", ["heat", "cool"])
def test_balance_veto_cannot_starve_two_full_steady_tests(mode):
    direction = 1 if mode == "heat" else -1
    rooms = ["warm", "cold"]
    config = {
        "systems": [{"entity_id": "unit"}],
        "sensors": [{"entity_id": r} for r in rooms],
        "average_weight": 0.01,
    }
    state = ControlState(mode=mode, active_mode=mode, setpoint=22)
    model = Learning(rooms, ["unit"])
    for room, effect in zip(rooms, [0.06, 0.001]):
        model.rows[room].counts = [1e9]
        model.rows[room].theta[1] = effect
    values = {"warm": 22 + direction * 0.6, "cold": 22 - direction * 1.4}
    devices = {"unit": Device(mode=mode, current_temperature=22, hvac_action="idle")}
    assert allocate(config, values, devices, mode, 22, learning=model)[0]["unit"] == 0
    readings = {r: Reading(t) for r, t in values.items()}
    for now in range(0, 7200, 300):
        control(config, state, readings, devices, now, learning=model)
        assert not state.excitation
    for start in (7200, 18000):
        result = control(config, state, readings, devices, start, learning=model)
        assert result.temperature == pytest.approx(22 - direction * 0.4)
        assert state.excitation["system"] == "unit"
        assert state.efforts["unit"] == direction
        devices["unit"].hvac_action = "heating" if mode == "heat" else "cooling"
        state.reported_efforts["unit"] = direction
        state.observe_action("unit", devices["unit"].hvac_action, start)
        for now in range(start + 300, start + 3600, 300):
            control(config, state, readings, devices, now, learning=model)
            assert state.excitation["system"] == "unit"
        control(config, state, readings, devices, start + 3600, learning=model)
        assert not state.excitation
        assert state.last_steady_test["unit"] == start + 3600
        devices["unit"].hvac_action = "idle"
        state.observe_action("unit", "idle", start + 3600)
        control(config, state, readings, devices, start + 3900, learning=model)
        assert not state.excitation


@pytest.mark.parametrize("reported,effort", [("heating", 0), ("idle", 0.1)])
def test_short_normal_operation_does_not_veto_due_test(reported, effort):
    config, state, model, devices, readings = setup()
    state.test_observed_since = dict.fromkeys(devices, 0)
    devices.pop("b")
    config["systems"] = [{"entity_id": "a"}]
    devices["a"].hvac_action = reported
    excitation_target(
        config,
        state,
        devices,
        {r: v.value for r, v in readings.items()},
        20,
        "heat",
        7200,
        model,
        allocation={"a": effort},
    )
    assert state.excitation["system"] == "a"


def test_november_5_1914_bryant_replay():
    # Temperatures and office reach are the supplied live values. Other effects
    # and drift are representative under-learned estimates, not a saved HA fit.
    temperatures = dict(
        zip(
            [
                "downstairs",
                "living_room",
                "guest",
                "parent",
                "annabelle",
                "wesley_gabriel",
                "office",
            ],
            [19.9, 20.0, 21.1, 22.6, 22.2, 22.3, 25.7],
        )
    )
    rooms = [f"sensor.{r}" for r in temperatures]
    unit = "climate.home_zone_1"
    config = {
        "systems": [{"entity_id": unit}],
        "sensors": [{"entity_id": r} for r in rooms],
        "average_weight": 0.01,
    }
    # The live priority weights were not supplied; reproduce its 21.6 C mean.
    config["sensors"][0]["weight"] = 1 + 2.6 / 1.7
    model = Learning(rooms, [unit])
    for room, rate in zip(rooms, [0.01, 0.01, 0.05, 0.2, 0.2, 0.2, 0.007]):
        model.rows[room].counts = [1e9]
        model.rows[room].theta[1] = rate / 12
    model.rows["sensor.office"].theta[-1] = 0.3 / 12
    state = ControlState(
        mode="heat",
        active_mode="heat",
        setpoint=22,
        test_observed_since={unit: 0},
        last_stop={unit: 0},
    )
    devices = {unit: Device(mode="heat", current_temperature=20, hvac_action="idle")}
    values = dict(zip(rooms, temperatures.values()))
    assert allocate(config, values, devices, "heat", 22, learning=model)[0][unit] == 0
    base, effects = model.forecast("sensor.office", 25.7, [unit], 12)
    assert base + effects[0] == pytest.approx(26.007)
    assert base + effects[0] > 25.7 + 0.3  # Old gate vetoed Office.
    result = control(
        config,
        state,
        {r: Reading(t) for r, t in values.items()},
        devices,
        7200,
        learning=model,
    )
    assert result.temperature == pytest.approx(21.6)
    assert state.excitation["system"] == unit
    assert state.efforts[unit] == 1
    assert "testing" in result.explanation


def test_active_safety_uses_current_normal_allocation():
    config, state, model, devices, _ = setup()
    config["systems"] = [{"entity_id": "a"}]
    state.test_observed_since["a"] = 0
    values = {"warm": 22, "cold": 18.8}
    # A due test may begin during normal full effort: it adds no heat then.
    model.rows["warm"].theta[1] = 0.5
    model.rows["warm"].counts[0] = 1e9
    excitation_target(
        config, state, devices, values, 20, "heat", 7200, model, allocation={"a": 1}
    )
    assert state.excitation
    # If normal control subsequently stops, the continuing test adds heat and
    # must pass safety again, rather than keeping its initial exemption.
    excitation_target(
        config, state, devices, values, 20, "heat", 7500, model, allocation={"a": 0}
    )
    assert not state.excitation
    assert state.experiment_info["a"]["blocked_by"] == "warm"
    assert state.experiment_info["a"]["next_eligible"] == 7200

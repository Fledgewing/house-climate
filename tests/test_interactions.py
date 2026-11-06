"""Temperature-dependent effects, legacy migration and deterministic plants."""

from copy import deepcopy
from dataclasses import asdict
from math import pi, sin, sqrt
from random import Random

import pytest

from custom_components.house_climate.controller import (
    ControlState,
    Device,
    allocate,
    excitation_target,
    model_step,
)
from custom_components.house_climate.learning import INTERACTION_PRIOR, Learning, Row


class FixedOnly:
    """The v0.5.7 RLS features and single-room control prediction."""

    def __init__(self):
        self.row = Row(
            [0, 0.03, 0, 0],
            [[100.0 if i == j else 0 for j in range(4)] for i in range(4)],
            [0],
        )

    def predict(self, temperature, outdoor, effort):
        x = [outdoor - temperature, effort, 0, 1]
        return temperature + sum(a * b for a, b in zip(self.row.theta, x))

    def update(self, before, after, outdoor, effort):
        error = after - self.predict(before, outdoor, effort)
        Learning.fit_row(
            self.row, [outdoor - before, effort, 0, 1], error, [effort], 1, 0.998
        )


def test_v057_migration_preserves_all_rows_covariances_and_predictions():
    old = Learning(["room"], ["pump"], sources=["fan"]).to_dict()
    old.pop("interaction_prior")
    theta = [0.01, 0.3, -0.05, 0.02, 0.004]
    covariance = [
        [(i + 1) * (j + 1) + (20 if i == j else 0) for j in range(5)] for i in range(5)
    ]
    legacy = asdict(Row(theta, covariance, [50, 60], 100, 0.07))
    old["rows"]["room"] = deepcopy(legacy)
    state = old["regimes"]["room"]
    state["challenger"] = deepcopy(legacy)
    state["saved"] = [
        {"id": "saved #1", "row": deepcopy(legacy), "last_fit": 123, "origin": "main"}
    ]
    original = deepcopy(old)
    model = Learning.from_dict(old)
    assert old == original  # Loading does not mutate Store data.
    mapping = [0, 1, 2, 7, 8]
    for row in [
        model.rows["room"],
        model.regimes["room"]["challenger"],
        model.regimes["room"]["saved"][0]["row"],
    ]:
        assert [row.theta[i] for i in mapping] == theta
        assert [[row.covariance[i][j] for j in mapping] for i in mapping] == covariance
        assert row.counts == [50, 60]
        assert (row.samples, row.squared_error) == (100, 0.07)
        for i in (3, 4):
            assert row.theta[i] == 0
            assert row.covariance[i][i] == INTERACTION_PRIOR
            assert all(v == 0 for j, v in enumerate(row.covariance[i]) if i != j)
    for outdoor in (0, 7.5, 15):
        base, effects = model.forecast(
            "room", 21, ["pump"], 9, outdoor, 2, {"fan": 0.5}
        )
        gain = sum(0.99**k for k in range(9))
        assert base == pytest.approx(
            0.99**9 * 21 + gain * (0.01 * outdoor - 0.05 * 0.5 + 0.02 * 2 + 0.004)
        )
        assert effects == pytest.approx([gain * 0.3])
    assert Learning.from_dict(model.to_dict()).to_dict() == model.to_dict()


def test_interaction_reconcile_and_response_change_reset():
    model = Learning(
        ["room"], ["a", "b"], sources=["fan", "heater"], interaction_prior=0.002
    )
    row = model.rows["room"]
    row.theta = list(range(15))
    row.covariance = [[i * 15 + j for j in range(15)] for i in range(15)]
    model.regimes["room"]["challenger"] = deepcopy(row)
    model.promote("room")
    model.rows["room"] = deepcopy(row)
    # Removing/reintroducing fan is the coordinator's response-change reset.
    model.reconcile(["room"], ["b", "a"], ["heater"])
    model.reconcile(["room"], ["b", "a"], ["heater", "fan"])
    for row in [model.rows["room"], model.regimes["room"]["saved"][0]["row"]]:
        assert row.theta == [0, 2, 1, 4, 0, 6, 5, 8, 0, 10, 9, 12, 0, 13, 14]
        assert row.covariance[2][6] == 1 * 15 + 5
        assert row.covariance[4][4] == 100
        assert row.covariance[8][8] == 0.002
        assert row.covariance[8][6] == 0
    assert model.regimes["room"]["challenger"].covariance[8][8] == 0.002


@pytest.mark.parametrize("sign", [1, -1])
def test_duration_scaled_interaction_update(sign):
    model = Learning(["room"], ["pump"], sources=["fan"], forgetting=1)
    model.update(
        {"room": 20},
        {"room": 20.1},
        5,
        {"pump": sign * 0.5},
        duration=2,
        sources={"fan": 0.25},
    )
    x = [-30, sign, 0.5, -15 * sign, -7.5, 0, 0, 0, 2]
    variance = [
        100,
        100,
        100,
        INTERACTION_PRIOR,
        INTERACTION_PRIOR,
        0.01,
        0.01,
        100,
        100,
    ]
    error = 0.1 - 0.03 * sign
    denominator = 1 + sum(p * v * v for p, v in zip(variance, x))
    expected = [
        prior + p * v * error / denominator
        for prior, p, v in zip([0, 0.03, 0, 0, 0, 0, 0, 0, 0], variance, x)
    ]
    assert model.rows["room"].theta == pytest.approx(expected)


def simulate_pump(constant=False):
    rng = Random(508)
    model = Learning(["room"], ["pump"], disturbance_sigma=0, regime_hours=1e6)
    baseline = FixedOnly()
    temperature = 20.0
    errors, fixed_errors, output_differences = [], [], []
    config = {
        "objective": "variance",
        "sensors": [{"entity_id": "room"}],
        "systems": [{"entity_id": "pump"}],
        "control_horizon_minutes": 5,
    }
    for step in range(15 * 288):
        outdoor = 7.5 if constant else 7.5 + 7.5 * sin(2 * pi * step / 288)
        effort = rng.random() if rng.random() < 0.8 else 0
        # The constant-weather fixture has constant indoor conditions as well:
        # no independent delta variety exists to identify a slope.
        if constant:
            temperature = 20.0
        delta = outdoor - temperature
        after = (
            temperature
            + 0.008 * delta
            + (0.4 + 0.012 * delta) * effort
            + rng.gauss(0, 0.0005)
        )
        prediction = model_step(
            model, "room", temperature, {"pump": effort}, outdoor, 0, {}
        )
        if step >= 12 * 288:
            errors.append(prediction - after)
            fixed_errors.append(baseline.predict(temperature, outdoor, effort) - after)
            if constant:
                target = temperature + 0.01
                u, _ = allocate(
                    config,
                    {"room": temperature},
                    {"pump": Device()},
                    "heat",
                    target,
                    outdoor,
                    model,
                )
                passive = (
                    temperature + baseline.row.theta[0] * delta + baseline.row.theta[-1]
                )
                old_u = max(
                    0, min(1, (target - passive) / max(1e-9, baseline.row.theta[1]))
                )
                output_differences.append(abs(u["pump"] - old_u))
        if step < 12 * 288:
            model.update(
                {"room": temperature}, {"room": after}, outdoor, {"pump": effort}
            )
            baseline.update(temperature, after, outdoor, effort)
        temperature = after

    def rmse(values):
        return sqrt(sum(v * v for v in values) / len(values))

    return model, rmse(errors), rmse(fixed_errors), max(output_differences, default=0)


def test_heat_pump_learns_weather_slope_and_beats_fixed_only():
    model, error, baseline_error, _ = simulate_pump()
    b, g = model.rows["room"].theta[1:3]
    print(
        f"\nheat pump: b={b:.8f}, g={g:.8f}; held-out RMSE interaction={error:.8f}, fixed={baseline_error:.8f}"
    )
    assert g == pytest.approx(0.012, abs=0.0005)
    assert b == pytest.approx(0.4, abs=0.005)
    assert error < baseline_error / 10
    assert model.blended("room", "pump", 0, 20) < model.blended("room", "pump", 15, 20)


def test_constant_weather_stays_near_legacy_control():
    model, error, baseline_error, difference = simulate_pump(constant=True)
    g = model.rows["room"].theta[2]
    print(
        f"\nconstant delta: g={g:.10f}; RMSE={error:.8f}/{baseline_error:.8f}; max effort difference={difference:.10f}"
    )
    assert abs(g) < 1e-5
    assert difference < 0.001


def test_whole_house_fan_learns_signed_temperature_effect():
    rng = Random(580)
    model = Learning(
        ["room"], ["pump"], sources=["fan"], disturbance_sigma=0, regime_hours=1e6
    )
    temperature = 20.0
    for step in range(12 * 288):
        outdoor = 7.5 + 7.5 * sin(2 * pi * step / 288)
        effort = rng.random()
        fan = float(rng.random() < 0.35)
        after = (
            temperature
            + 0.005 * (outdoor - temperature)
            + 0.3 * effort
            + 0.02 * fan * (outdoor - temperature)
            + rng.gauss(0, 0.0005)
        )
        model.update(
            {"room": temperature},
            {"room": after},
            outdoor,
            {"pump": effort},
            sources={"fan": fan},
        )
        temperature = after
    row = model.rows["room"]
    e, g = row.theta[2], row.theta[4]
    print(f"\nwhole-house fan: fixed={e:.8f}, g={g:.8f} (truth 0 / 0.02)")
    assert e == pytest.approx(0, abs=0.005)
    assert g == pytest.approx(0.02, abs=0.0005)
    assert model.source_effect("room", "fan", 0, 20) < 0
    assert model.source_effect("room", "fan", 25, 20) > 0


def test_current_effects_forecast_affinity_diagnostics_outage_and_heat_clamp():
    model = Learning(["room", "other"], ["pump"], sources=["fan"])
    row = model.rows["room"]
    row.theta = [0.01, 0.3, 0.02, 0.01, 0.02, 0, 0, 0, 0]
    row.counts = [100, 100]
    model.rows["other"].theta = list(row.theta)
    model.rows["other"].counts = [100, 100]
    # delta=-10: system=.2, fan=-.18. Hold delta fixed for the horizon.
    base, effects = model.forecast("room", 20, ["pump"], 9, 10, sources={"fan": 1})
    gain = sum(0.99**k for k in range(9))
    assert effects == pytest.approx([gain * 0.2])
    assert base == pytest.approx(0.99**9 * 20 + gain * (0.1 - 0.18))
    assert model_step(
        model, "room", 20, {"pump": 0.5}, 10, 0, {"fan": 1}
    ) == pytest.approx(19.82)
    assert model.influences(
        "pump", outdoor=10, temperatures={"room": 20, "other": 10}
    ) == pytest.approx({"room": 0.4, "other": 0.6})
    diag = model.diagnostics(10, {"room": 20})
    assert diag["systems"]["pump"]["room"]["g"] == pytest.approx(0.01 * 12)
    assert diag["systems"]["pump"]["room"]["effective_effect"] == pytest.approx(
        0.2 * 12
    )
    assert diag["sources"]["fan"]["room"]["effective_effect"] == pytest.approx(
        -0.18 * 12
    )
    assert model.forecast("room", 20, ["pump"], 9, -20)[1] == [0]
    assert model.forecast("room", 20, ["pump"], 9)[1] == pytest.approx([9 * 0.3])
    # Clamp the combined effect, allowing a negative fixed part offset by g.
    for r in model.rows.values():
        r.theta[1], r.theta[3] = -0.1, -0.02
    assert model.forecast("room", 20, ["pump"], 1, 10)[1] == pytest.approx([0.1])
    assert model.forecast("room", 20, ["pump"], 1, 25)[1] == [0]
    config = {
        "sensors": [{"entity_id": "room"}],
        "systems": [{"entity_id": "pump"}],
        "explore_threshold": 1,
    }
    state = ControlState(mode="heat", setpoint=22, last_run={"pump": 0})
    excitation_target(
        config,
        state,
        {"pump": Device(current_temperature=20)},
        {"room": 20},
        20,
        "heat",
        10000,
        model,
        25,
    )
    assert not state.excitation

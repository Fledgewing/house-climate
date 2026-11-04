"""Delayed thermal response and information-driven steady experiments."""

from copy import deepcopy
from dataclasses import asdict
from random import Random

import pytest

from custom_components.house_climate.controller import (
    ControlState,
    Device,
    Reading,
    allocate,
    control,
    excitation_target,
)
from custom_components.house_climate.learning import (
    LAG_PRIOR,
    EffortTracker,
    Learning,
    Row,
)


def test_v058_migration_preserves_main_challenger_library_and_predictions():
    model = Learning(["D"], ["A"], sources=["fire"])
    store = model.to_dict()
    theta = [0.01, 0.025, 0.02, 0.001, -0.002, 0.01, -0.005]
    covariance = [[10 + i if i == j else 0.01 for j in range(7)] for i in range(7)]
    legacy = asdict(Row(theta, covariance, [100, 50], 120, 0.2))
    store["rows"]["D"] = deepcopy(legacy)
    store["regimes"]["D"]["challenger"] = deepcopy(legacy)
    store["regimes"]["D"]["saved"] = [
        {"id": "saved #1", "row": deepcopy(legacy), "last_fit": 100}
    ]
    for key in ("lag_minutes", "lag_prior", "effort_history"):
        store.pop(key)
    original = deepcopy(store)
    restored = Learning.from_dict(store)
    assert store == original
    indices = [0, 1, 2, 3, 4, 7, 8]
    for row in (
        restored.rows["D"],
        restored.regimes["D"]["challenger"],
        restored.regimes["D"]["saved"][0]["row"],
    ):
        assert [row.theta[i] for i in indices] == theta
        assert [[row.covariance[i][j] for j in indices] for i in indices] == covariance
        assert row.counts == [100, 50]
        assert (row.samples, row.squared_error) == (120, 0.2)
        for k in (5, 6):
            assert row.theta[k] == 0
            assert row.covariance[k][k] == LAG_PRIOR
    base, effects = restored.forecast("D", 20, ["A"], 9, 10, 2, {"fire": 0.5})
    gain = sum(0.99**k for k in range(9))
    assert effects == pytest.approx([gain * (0.025 - 0.01)])
    assert base == pytest.approx(
        0.99**9 * 20 + gain * (0.1 + 0.04 * 0.5 + 0.02 - 0.005)
    )


def test_lag_duration_sources_missing_history_restart_and_reconcile():
    model = Learning(["D"], ["A"], sources=["fire"], forgetting=1)
    model.record_efforts({"A": 1}, {"fire": 0.2}, 0, 1800)
    model.record_efforts({"A": 0}, {"fire": 0.8}, 1800, 3600)
    model = Learning.from_dict(model.to_dict())
    assert model.past_effort("system", "A", 3600) == 0.5
    assert model.past_effort("source", "fire", 3600) == pytest.approx(0.5)
    model.update(
        {"D": 20}, {"D": 20.1}, 20, {"A": 0}, duration=2, sources={"fire": 0}, now=4200
    )
    assert model.rows["D"].theta[model.lag_index(0)] > 0
    assert model.rows["D"].counts == [
        0,
        0,
    ]  # Runtime confidence keeps its original meaning.
    saved = model.to_dict()
    model.reconcile(["D"], ["A"], [])
    model.reconcile(["D"], ["A"], ["fire"])
    assert model.rows["D"].theta[model.lag_index(1)] == 0
    assert model.past_effort("source", "fire", 4200) is None
    restored = Learning.from_dict(saved)
    index = restored.lag_index(0)
    before = restored.rows["D"].theta[index]
    covariance = list(restored.rows["D"].covariance[index])
    restored.update({"D": 20}, {"D": 20.1}, 20, {"A": 1}, sources={"fire": 1}, now=9000)
    assert restored.rows["D"].theta[index] == before
    assert restored.rows["D"].covariance[index][index] == covariance[index]


def test_horizon_known_past_then_candidate_affine_and_steady_clamp():
    model = Learning(["D"], ["A"], sources=["fire"])
    row = model.rows["D"]
    row.theta[1], row.theta[2] = 0.01, 0.02
    row.theta[model.lag_index(0)], row.theta[model.lag_index(1)] = 0.03, -0.01
    row.counts = [1e30, 1e30]
    model.record_efforts({"A": 0.5}, {"fire": 1}, 0, 3600)
    base, effects = model.forecast("D", 20, ["A"], 13, sources={"fire": 0.2})
    assert effects == pytest.approx([13 * 0.01 + 0.03 * sum(k / 12 for k in range(13))])
    expected = 20 + sum(
        0.03 * 0.5 * (1 - k / 12) - 0.01 * (1 - k / 12) + (0.02 - 0.01 * k / 12) * 0.2
        for k in range(13)
    )
    assert base == pytest.approx(expected)
    assert model.blended("D", "A") == pytest.approx(0.04)
    row.theta[1] = -0.1
    assert model.forecast("D", 20, ["A"], 25)[1][0] <= 0
    assert model.diagnostics()["systems"]["A"]["D"]["effective_effect"] == 0


def delayed_house():
    rng = Random(509)
    model = Learning(["D"], ["A"], disturbance_sigma=0, regime_hours=1e9)
    old = Row(
        [0, 0.03, 0, 0, 0],
        [
            [100 if i == j and i != 2 else 0.0001 if i == j else 0 for j in range(5)]
            for i in range(5)
        ],
        [0],
    )
    efforts = [0] * 8
    for step in range(12000):
        effort = float(rng.random() < 0.5)
        response = 0.025 * efforts[-8]  # 40-minute transport delay; 0.3 C/h.
        temperature = rng.uniform(18, 22)
        outdoor = rng.uniform(5, 15)
        x = [outdoor - temperature, effort, effort * (outdoor - temperature), 0, 1]
        error = response - sum(a * b for a, b in zip(old.theta, x))
        Learning.fit_row(old, x, error, [effort], 1, 0.998)
        model.update(
            {"D": temperature}, {"D": temperature + response}, outdoor, {"A": effort}
        )
        efforts.append(effort)
    new = model.blended("D", "A", 10, 20) * 12
    baseline = (old.theta[1] - 10 * old.theta[2]) * 12
    return model, baseline, new


def test_delayed_house_learns_steady_reach_and_allocator_uses_it():
    model, baseline, new = delayed_house()
    print(
        f"\n40-minute delay: v0.5.8={baseline:.5f}, v0.5.9={new:.5f} C/h; truth=0.30000"
    )
    assert baseline <= 0.1
    assert new == pytest.approx(0.3, abs=0.05)
    config = {
        "sensors": [{"entity_id": "D"}],
        "systems": [{"entity_id": "A"}],
        "control_horizon_minutes": 120,
    }
    efforts, _ = allocate(config, {"D": 19}, {"A": Device()}, "heat", 21, 10, model)
    assert efforts["A"] > 0.9


def experiment_setup(mode="heat"):
    config = {
        "sensors": [
            {"entity_id": "D", "weight": 10},
            {"entity_id": "minor", "weight": 0.01},
        ],
        "systems": [{"entity_id": "A"}, {"entity_id": "B"}],
        "excite_after_minutes": 1,
    }
    state = ControlState(mode=mode, active_mode=mode, setpoint=21)
    devices = {
        key: Device(mode=mode, current_temperature=21, hvac_action="idle")
        for key in ("A", "B")
    }
    model = Learning(["D", "minor"], ["A", "B"], disturbance_sigma=0)
    return config, state, devices, model


def test_short_allocator_runs_trigger_full_steady_experiment_and_confidence_rises():
    config, state, devices, model = experiment_setup()
    config["excite_after_minutes"] = 120
    config["max_starts_per_hour"] = 12
    # The ordinary allocator repeatedly runs A for five minutes, never a steady test.
    for now in range(0, 7200, 600):
        state.observe_action("A", "heating", now)
        state.observe_action("A", "idle", now + 300)
    devices["A"].hvac_action = "idle"
    for row in model.rows.values():
        row.theta[2] = 0
        row.counts[1] = 1e30
    initial = model.confidence("D", "A")
    start = control(config, state, {"D": Reading(20.5)}, devices, 7200, learning=model)
    assert state.excitation["system"] == "A"
    assert "testing A for 60 min to learn its effect on D" in start.explanation
    targets = []
    for step in range(13):
        now = 7200 + step * 300
        devices["A"].hvac_action = "heating"
        temperature = 20.5 + step * 0.01
        result = control(
            config, state, {"D": Reading(temperature)}, devices, now, learning=model
        )
        targets += [
            data["temperature"]
            for key, action, data in result.commands
            if key == "A" and action == "set_temperature"
        ]
        after = temperature + 0.01
        assert after <= state.setpoint + config.get("comfort_band", 1) / 2
        model.update(
            {"D": temperature}, {"D": after}, 10, {"A": 1, "B": 0}, now=now + 300
        )
        if step < 12:
            assert state.excitation["system"] == "A"
            assert state.efforts["A"] == 1
    assert not state.excitation
    assert set(targets) == {22}
    assert state.last_steady_test["A"] == 10800
    assert model.confidence("D", "A") > initial
    assert state.experiment_info["A"]["last_steady_test"] == 10800


@pytest.mark.parametrize(
    "mode,temperature,drift", [("heat", 21, 0.05), ("cool", 21, -0.05)]
)
def test_experiment_safety_stops_early_cycling_mode_and_minor_rooms(
    mode, temperature, drift
):
    config, state, devices, model = experiment_setup(mode)
    for key in devices:
        state.observe_action(key, "idle", 0)
    values = {"D": temperature, "minor": 40 if mode == "heat" else 0}
    excitation_target(config, state, devices, values, temperature, mode, 300, model)
    assert state.excitation["system"] == "A"
    model.rows["D"].theta[-1] = drift
    excitation_target(config, state, devices, values, temperature, mode, 600, model)
    assert not state.excitation
    assert "A" not in state.last_steady_test
    # Forecast also rejects a new experiment with unsafe drift.
    excitation_target(config, state, devices, values, temperature, mode, 900, model)
    assert not state.excitation
    model.rows["D"].theta[-1] = 0
    state.last_stop = {"A": 1000, "B": 1000}
    excitation_target(config, state, devices, values, temperature, mode, 1001, model)
    assert not state.excitation
    excitation_target(config, state, devices, values, temperature, "off", 2000, model)
    assert not state.excitation


def test_information_ranking_uses_weighted_room_covariance_not_runtime_confidence():
    config, state, devices, model = experiment_setup()
    for key in devices:
        state.observe_action(key, "idle", 0)
    for row in model.rows.values():
        row.counts = [1e9, 1e9]  # High runtime confidence does not veto a test.
        row.covariance = [[0.0] * len(row.theta) for _ in row.theta]
    model.rows["D"].covariance[1][1] = 0.001
    model.rows["D"].covariance[model.lag_index(1)][model.lag_index(1)] = 0.1
    model.rows["minor"].covariance[1][1] = 1e9
    excitation_target(
        config, state, devices, {"D": 20.5, "minor": 40}, 20.5, "heat", 300, model
    )
    assert state.excitation["system"] == "B"
    assert state.excitation["rooms"] == ["D"]
    assert (
        state.experiment_info["B"]["uncertainty_score"]
        > state.experiment_info["A"]["uncertainty_score"]
    )


def test_qualifying_allocator_run_counts_even_when_it_ends_between_passes():
    config, state, devices, model = experiment_setup()
    state.observe_action("A", "heating", 0)
    state.observe_action("A", "idle", 3600)
    # A later short run must not erase the completed long run's evidence.
    state.observe_action("A", "heating", 4000)
    state.observe_action("A", "idle", 4100)
    excitation_target(config, state, devices, {"D": 20.5}, 20.5, "off", 4200, model)
    assert state.last_steady_test["A"] == 3600
    assert state.experiment_info["A"]["next_eligible"] == 3660


def test_experiment_preserves_other_allocated_effort_and_disables_dither():
    config, state, devices, model = experiment_setup()
    for key in devices:
        state.observe_action(key, "idle", 0)
    state.efforts = {"A": 0, "B": 0.25}
    excitation_target(
        config,
        state,
        devices,
        {"D": 20},
        20,
        "heat",
        300,
        model,
        allocation={"A": 0, "B": 0.25},
    )
    assert state.excitation["others"] == {"B": 0.25}
    devices["B"].step = 0.01
    devices["B"].hvac_action = "heating"
    devices["B"].temperature = 21.25
    result = control(config, state, {"D": Reading(20)}, devices, 600, learning=model)
    assert state.efforts["B"] == 0.25
    assert "dither" not in result.explanation


def test_outage_rollout_carries_delayed_heat_after_local_thermostat_stops():
    from custom_components.house_climate.controller import observe_thermostats

    model = Learning(["A"], ["A"])
    row = model.rows["A"]
    row.theta[1] = 0
    row.counts[0] = 1e30
    row.theta[model.lag_index(0)] = 0.025
    model.record_efforts({"A": 1}, {}, 0, 3600)
    config = {"systems": [{"entity_id": "A"}]}
    state = ControlState(
        thermostats={
            "A": {
                "own": 20,
                "target": 19,
                "running": False,
                "mode": "heat",
                "time": 3600,
            }
        }
    )
    before = deepcopy(model.to_dict())
    observe_thermostats(
        config, state, {}, {"A": Device(available=False)}, 4200, model, None, 0, {}
    )
    assert state.thermostats["A"]["own"] == pytest.approx(20 + 0.025 * (1 + 11 / 12))
    assert model.to_dict() == before


def test_missing_lag_has_no_credit_and_covariance_stays_positive():
    row = Row([0.0, 0.0], [[1.0, 0.9], [0.9, 1.0]], [])
    for _ in range(30):
        Learning.fit_row(row, [1, 0], 0.1, [], 1, 0.998, frozen=[1])
        p = row.covariance
        assert row.theta[1] == 0
        assert p[1][1] == 1
        assert p[0][1] == pytest.approx(p[1][0])
        assert p[0][0] * p[1][1] - p[0][1] ** 2 >= -1e-12


def test_unavailable_unit_does_not_block_the_most_informative_available_test():
    config, state, devices, model = experiment_setup()
    for key in devices:
        state.observe_action(key, "idle", 0)
    devices["A"].available = False
    model.rows["D"].covariance[1][1] = 10000
    excitation_target(config, state, devices, {"D": 20.5}, 20.5, "heat", 300, model)
    assert state.excitation["system"] == "B"


def test_transition_history_exact_partial_windows_and_restart():
    systems, sources = EffortTracker(), EffortTracker()
    systems.finish({"A": "idle"}, 0)
    sources.finish({"fire": 0.2}, 0)
    systems.change("A", "heating", 0)
    systems.change("A", "idle", 300)
    sources.change("fire", 0.8, 600)
    efforts, _ = systems.finish({"A": "idle"}, 900)
    fractions, _ = sources.finish({"fire": 0.8}, 900)
    assert efforts["A"] == pytest.approx(1 / 3)
    model = Learning(["D"], ["A"], sources=["fire"], lag_minutes=7)
    model.record_efforts(
        efforts,
        fractions,
        0,
        900,
        {
            "system:A": systems.finished_segments["A"],
            "source:fire": sources.finished_segments["fire"],
        },
    )
    restored = Learning.from_dict(model.to_dict())
    assert restored.past_effort("system", "A", 900) == 0
    assert restored.past_effort("system", "A", 600) == pytest.approx(120 / 420)
    assert restored.past_effort("source", "fire", 900) == pytest.approx(
        (120 * 0.2 + 300 * 0.8) / 420
    )
    assert restored.to_dict() == model.to_dict()


def test_experiment_checks_the_start_of_the_forecast_too():
    config, state, devices, model = experiment_setup()
    for key in devices:
        state.observe_action(key, "idle", 0)
    # Passive cooling would bring this room inside the band by the first step;
    # it is already beyond the heating ceiling at the start of the run.
    model.rows["D"].theta[0] = 0.1
    excitation_target(
        config, state, devices, {"D": 21.6}, 20.5, "heat", 300, model, outdoor=0
    )
    assert not state.excitation

"""Measured and unmeasured heat in a reproducible physical house."""

from copy import deepcopy
from math import pi, sin
from random import Random

import pytest

from custom_components.house_climate.learning import Learning

ROOMS = ["living", "guest", "office"]
SYSTEMS = ["a", "b"]
TRUTH_B = [[0.16, 0.08], [0.12, 0.18], [0.10, 0.04]]
TRUTH_E = [0.85, 0.7, 0]


def simulate_source(configured=False, protection=True):
    """Ten days, random evening source use after a clean commissioning day.

    Actual HVAC fractions correlate with evening use, creating omitted-input
    bias. Weather, solar, room losses and measurement noise are identical in
    all comparisons. Room temperature evolves freely from the physical model.
    """
    rng = Random(500)
    model = Learning(
        ROOMS,
        SYSTEMS,
        sources=["source"] if configured else [],
        disturbance_sigma=3 if protection else 0,
    )
    temperatures = dict.fromkeys(ROOMS, 20.0)
    starts = [rng.randrange(192, 252) for _ in range(10)]
    lengths = [rng.randrange(24, 49) for _ in range(10)]
    flags = dict.fromkeys(ROOMS, 0)
    on_intervals = 0
    for step in range(2880):
        day, phase = divmod(step, 288)
        on = day > 0 and starts[day] <= phase < starts[day] + lengths[day]
        on_intervals += on
        outdoor = 8 + 4 * sin(2 * pi * phase / 288)
        solar = max(0, sin(2 * pi * (phase - 72) / 288)) * 3
        efforts = {"a": min(1, rng.random() * 0.6 + 0.4 * on), "b": rng.random()}
        before = dict(temperatures)
        for i, room in enumerate(ROOMS):
            temperatures[room] += (
                0.012 * (outdoor - before[room])
                + sum(b * efforts[s] for b, s in zip(TRUTH_B[i], SYSTEMS))
                + TRUTH_E[i] * on
                + 0.003 * solar
                + rng.gauss(0, 0.001)
            )
        model.update(
            before,
            temperatures,
            outdoor,
            efforts,
            solar,
            sources={"source": float(on)} if configured else {},
        )
        if on:
            for room in model.disturbed:
                flags[room] += 1
    error = (
        sum(
            abs(model.rows[r].theta[j + 1] - TRUTH_B[i][j])
            for i, r in enumerate(ROOMS)
            for j in range(2)
        )
        / 6
    )
    return model, error, flags, on_intervals


def test_simulated_evening_source():
    measured, measured_error, measured_flags, count = simulate_source(True)
    detected, detected_error, flags, _ = simulate_source()
    _, unprotected_error, _, _ = simulate_source(protection=False)
    effects = [measured.diagnostics()["sources"]["source"][r]["e"] for r in ROOMS]
    print(
        f"\nSource simulation: HVAC mean absolute coefficient error measured={measured_error:.6f}, detected={detected_error:.6f}, protection off={unprotected_error:.6f} C/interval; source effects={effects}; disturbed source-on intervals={flags}/{count}; measured flags={measured_flags}"
    )
    for i, room in enumerate(ROOMS):
        assert measured.rows[room].theta[1:3] == pytest.approx(TRUTH_B[i], abs=0.003)
    assert effects == pytest.approx(TRUTH_E, abs=0.005)
    assert measured_flags == dict.fromkeys(ROOMS, 0)
    assert flags["living"] > count * 0.85
    assert flags["guest"] > count * 0.85
    assert flags["office"] == 0
    assert detected_error < unprotected_error / 5
    assert detected.rows["office"].samples == 2880


def calibrated():
    model = Learning(["one", "two"], ["system"])
    for row in model.rows.values():
        row.theta = [0, 0.1, 0, 0]
        row.counts = [100]
        row.samples = 100
        row.covariance = [[0.01 if i == j else 0 for j in range(4)] for i in range(4)]
    return model


@pytest.mark.parametrize("sign", [1, -1])
def test_two_hour_single_room_burst_freezes_only_that_row_and_clears(sign):
    model = calibrated()
    for step in range(24):
        model.update(
            {"one": 20, "two": 20}, {"one": 20.1 + sign, "two": 20.1}, 20, {"system": 1}
        )
        if step == 0:
            assert not model.disturbed
        elif step == 1:
            frozen = deepcopy(model.rows["one"])
        else:
            assert model.rows["one"] == frozen
    assert model.disturbed == {"one": "heat" if sign > 0 else "cooling"}
    assert model.rows["two"].samples == 124
    assert model.rows["one"].samples == 101
    for step in range(3):
        model.update(
            {"one": 20, "two": 20}, {"one": 20.1, "two": 20.1}, 20, {"system": 1}
        )
        assert bool(model.disturbed) is (step < 2)
    assert model.rows["one"].samples == 102


def test_cold_start_never_flags_and_clips_innovation():
    model = Learning(["room"], ["system"], n0=1000)
    for step in range(100):
        model.update({"room": 20}, {"room": 30 + step}, 20, {"system": 1})
        assert not model.disturbed
    model = Learning(["room"], ["system"])
    model.update({"room": 20}, {"room": 1000}, 20, {"system": 1})
    assert model.rows["room"].theta[1] < 0.2
    assert model.fit_error("room") == pytest.approx(0.3)


def test_same_direction_consecutive_bounds_and_missing_intervals():
    model = calibrated()
    for delta in (1, -1, 1, -1):
        model.update({"one": 20}, {"one": 20.1 + delta}, 20, {"system": 1})
        assert not model.disturbed
    model.update({}, {}, 20, {"system": 1})
    model.update({"one": 20}, {"one": 21.1}, 20, {"system": 1})
    assert not model.disturbed
    model.update({"one": 20}, {"one": 21.1}, 20, {"system": 1})
    assert model.disturbed == {"one": "heat"}
    # A missing interval cannot count toward clearing either.
    model.update({}, {}, None, {"system": 1})
    for _ in range(2):
        model.update({"one": 20}, {"one": 20.1}, 20, {"system": 1})
        assert model.disturbed


def test_source_membership_preserves_full_existing_covariance_and_counts():
    model = calibrated()
    before = deepcopy(model.to_dict())
    # Loading a pre-0.5 model works too.
    for key in (
        "sources",
        "disturbance_sigma",
        "disturbance_intervals",
        "disturbance_clear_intervals",
    ):
        before.pop(key)
    model = Learning.from_dict(before)
    model.reconcile(["one", "two", "new"], ["system"], ["x", "y"])
    assert model.rows["one"].theta == [0, 0.1, 0, 0, 0, 0]
    assert model.rows["one"].counts == [100, 0, 0]
    assert model.rows["one"].covariance[2][2] == 100
    assert model.rows["new"].samples == 0
    model.rows["one"].theta[2:4] = [0.8, 0.4]
    model.rows["one"].counts[1:] = [50, 70]
    model.rows["one"].covariance[1][3] = 0.007
    model.reconcile(["one", "two"], ["system", "new_system"], ["y", "x"])
    assert model.rows["one"].theta[3:5] == [0.4, 0.8]
    assert model.rows["one"].counts == [100, 0, 70, 50]
    assert model.rows["one"].covariance[1][3] == 0.007
    model.reconcile(["one", "two"], ["system"])
    assert model.to_dict()["rows"] == before["rows"]

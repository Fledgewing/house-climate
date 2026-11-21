"""Closed-loop heating identification under low excitation (v0.5.21).

Synthetic plant; no recorder data, room names or house coefficients enter
production code. One heat pump heats six rooms at 1.0-1.5 °C/h through a
first-order room loss. Outdoor is cold (5-10 °C), rooms sit at 20-24 °C, sensors
report 0.1 °C steps. The unit runs short on/off cycles when the mean room is
cool, plus four-hourly tests that are cut short when the warmest room passes
its ceiling. That is the live low-excitation pattern of November 2026.
"""

from math import pi, sin
from random import Random

import pytest

from custom_components.house_climate.learning import EffortTracker, Learning

UNIT = "climate.unit"
RATES = {"r1": 1.0, "r2": 1.2, "r3": 1.5, "r4": 1.1, "r5": 1.3, "r6": 1.4}
LOSS = {"r1": 0.002, "r2": 0.0025, "r3": 0.003, "r4": 0.002, "r5": 0.0028, "r6": 0.0022}


def plant(model, *, days=11, disturbance=0.0, delay=0, slope=0.0, swing=2.5):
    """Run the closed loop; return the final outdoor temperature.

    `disturbance` scales a shared, persistent unmodeled heat input (AR(1), about
    2.75 h time constant, ±0.2 °C/h at 1). The loop heats when the house cools,
    so this input decides when the unit runs. `delay` is a transport delay in
    five-minute intervals and `slope` a true per-interval effect change per °C
    of outdoor-minus-room difference.
    """
    rng = Random(1120)
    tracker = EffortTracker()
    temperatures = {r: 22.0 + rng.uniform(-0.5, 1.5) for r in RATES}
    on, since, last_test, drift = False, -100, 0, 0.0
    history = [0.0] * (delay + 1)
    tracker.change(UNIT, "idle", 0)
    tracker.finish({UNIT: "idle"}, 0)

    def read():
        return {
            r: round((t + rng.gauss(0, 0.02)) / 0.1) * 0.1
            for r, t in temperatures.items()
        }

    readings = read()
    outdoor = 7.5
    for step in range(1, days * 288 + 1):
        minute = step * 5
        outdoor = 7.5 + swing * sin(2 * pi * (minute / 1440 - 0.375))
        mean = sum(readings.values()) / len(readings)
        warmest = max(readings.values())
        due = minute - last_test >= 240
        if not on and (mean < 21.7 or due) and warmest < 23.3 and minute - since >= 15:
            on, since = True, minute
            if mean >= 21.7:
                last_test = minute
        elif (
            on
            and minute - since >= 10
            and (
                warmest > 23.5
                or mean > 22.0
                and minute - since >= 20
                or minute - since >= 60
            )
        ):
            on, since, last_test = False, minute, minute
        history.append(1.0 if on else 0.0)
        effort = history[-1 - delay]
        drift = 0.97 * drift + rng.gauss(0, 0.004 * disturbance)
        before = readings
        for _ in range(5):
            for room in temperatures:
                delta = outdoor - temperatures[room]
                temperatures[room] += (
                    LOSS[room] / 5 * delta
                    + (RATES[room] / 60 + slope / 5 * delta) * effort
                    + (0.004 + drift) / 5
                )
        readings = read()
        action = "heating" if on else "idle"
        tracker.change(UNIT, action, (minute - 5) * 60)
        efforts, seconds = tracker.finish({UNIT: action}, minute * 60)
        model.update(
            before,
            readings,
            outdoor,
            efforts,
            duration=seconds / 300,
            now=minute * 60,
            observed_history={f"system:{UNIT}": tracker.finished_segments[UNIT]},
            local_hour=(minute / 60) % 24,
        )
    return outdoor


def model(**kwargs):
    return Learning(RATES, [UNIT], window_minutes=60, **kwargs)


def effects(learned, outdoor=7.5, room=22.0):
    """Hourly diagnostics at a cold evening context: outdoor-minus-room = -14.5."""
    return learned.diagnostics(outdoor, dict.fromkeys(RATES, room))["systems"][UNIT]


def truth(slope=0.0, outdoor=7.5, room=22.0):
    return {r: rate + 12 * slope * (outdoor - room) for r, rate in RATES.items()}


def test_low_excitation_alone_is_identified():
    """Short cycles, cut-short tests and a nearly constant gap are not the cause."""
    learned = model()
    plant(learned)
    for room, values in effects(learned).items():
        assert values["effective_effect"] == pytest.approx(RATES[room], rel=0.1)
        correction = values["blended_b"] - values["b"]
        assert abs(correction) < 0.1 * RATES[room]


def test_constant_background_reproduces_the_live_collapse():
    """Persistent unmodeled heat that decides heating biases every room to ~0.

    Live 2026-11-20 signature: b well below the earlier 1.06-1.75 °C/h, positive
    g and negative h cancelling it, effective effects near zero and overall
    confidence around 2%. Tightening g/h does not recover the effect: b itself
    is biased, so regularising the corrections alone is not a fix.
    """
    learned = model()
    plant(learned, disturbance=1)
    reported = effects(learned)
    ratios = [reported[r]["effective_effect"] / RATES[r] for r in RATES]
    assert sum(ratios) / len(ratios) < 0.3
    assert all(reported[r]["b"] < 0.6 * RATES[r] for r in RATES)
    assert all(reported[r]["g"] > 0 and reported[r]["h"] < -0.1 for r in RATES)
    assert learned.confidence_percent < 5

    pinned = model(interaction_prior=1e-9, lag_prior=1e-9)
    plant(pinned, disturbance=1)
    reported = effects(pinned)
    ratios = [reported[r]["effective_effect"] / RATES[r] for r in RATES]
    assert sum(ratios) / len(ratios) < 0.4


def test_local_level_background_learns_closed_loop_effects():
    learned = model(drift_noise=1)
    plant(learned, disturbance=1)
    for room, values in effects(learned).items():
        assert values["effective_effect"] == pytest.approx(RATES[room], rel=0.25)
        assert abs(values["blended_b"] - values["b"]) < 0.1 * RATES[room]


def test_local_level_background_keeps_a_genuine_delay():
    learned = model(drift_noise=1)
    plant(learned, disturbance=1, delay=3)
    for room, values in effects(learned).items():
        assert values["effective_effect"] == pytest.approx(RATES[room], rel=0.25)
        assert values["h"] > 0.2


def test_local_level_background_keeps_a_genuine_weather_slope():
    """Colder outdoor air truly weakens the unit; the operating effect follows."""
    slope = 0.0015
    expected = truth(slope)
    for drift in (0, 1):
        learned = model(drift_noise=drift)
        plant(learned, slope=slope, swing=8)
        for room, values in effects(learned).items():
            assert values["effective_effect"] == pytest.approx(expected[room], rel=0.1)
            assert values["g"] > 0
    learned = model(drift_noise=1)
    plant(learned, disturbance=1, slope=slope, swing=8)
    for room, values in effects(learned).items():
        assert values["effective_effect"] == pytest.approx(expected[room], rel=0.25)


def test_background_drift_roundtrip_and_validation():
    learned = model(drift_noise=1)
    restored = Learning.from_dict(learned.to_dict())
    assert restored.drift_noise == 1
    legacy = learned.to_dict()
    legacy.pop("drift_noise")
    assert Learning.from_dict(legacy).drift_noise == 0
    assert Learning(["room"], ["unit"], drift_noise=1).background_drift == 0
    with pytest.raises(ValueError):
        model(drift_noise=-1)

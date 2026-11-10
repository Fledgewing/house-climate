"""Synthetic November 6/7 replay; rates/times supplied by the owner, not recorder data."""

import json
from copy import deepcopy
from math import pi, sin

import pytest

from custom_components.house_climate.learning import EffortTracker, Learning

RATES = dict(
    zip(
        [
            "downstairs",
            "guest",
            "parents",
            "annabelle",
            "wesley",
            "living",
            "office",
            "climate.home_zone_1",
        ],
        [1.16, 1.47, 1.06, 1.75, 1.56, 0.84, 0.7, 1.2],
    )
)
UNIT = "climate.home_zone_1"


def mode_replay(*, halve=False):
    """Yield at restart for a caller to exercise the real HA Store load path.

    Assumptions: first-order room loss, smooth weather, eight independent rooms,
    two-hour heating/idle cycles plus 60-minute tests every 120 minutes. The
    reports are held at the exact supplied PT transition times. This is not a
    reconstruction of unavailable recorder readings/covariances.
    """
    model = Learning(RATES, [UNIT], window_minutes=60)
    tracker = EffortTracker()
    values = dict.fromkeys(RATES, 20.0)
    switch = 3 * 1440 + 22 * 60 + 11
    restart = 3 * 1440 + 23 * 60 + 22
    back = 4 * 1440 + 5
    settled = 4 * 1440 + 83
    end = settled + (8 * 1440 if halve else 3600)
    times = sorted(set(range(0, end + 1, 5)) | {switch, restart, back, settled, end})
    previous = times[0]
    previous_mode, previous_action = "heat", "heating"
    fitted = deepcopy(model.rows)
    reference = None
    minimum = 100
    for minute in times:
        if minute < switch:
            mode, action = "heat", "heating" if minute % 240 < 120 else "idle"
        elif minute < restart:
            mode, action = "cool", "heating"
        elif minute < back:
            mode, action = "cool", "cooling"
        elif minute < settled:
            mode, action = "heat", "cooling"
        else:
            mode = "heat"
            action = "heating" if minute % 240 < 120 or minute % 120 < 60 else "idle"
        if minute:
            duration = minute - previous
            outdoor = 10 + 3 * sin(2 * pi * previous / 1440)
            before = dict(values)
            u = tracker.actions[UNIT]
            # Even while a device finishes its old run, the rooms really receive
            # that old signed action. Add a small coasting load during transition.
            actual = {"heating": 1, "cooling": -1}.get(previous_action, 0)
            for room, rate in RATES.items():
                factor = 0.5 if halve and previous >= settled else 1
                values[room] += (
                    duration
                    / 5
                    * (
                        0.002 * (outdoor - values[room])
                        + rate / 12 * factor * actual
                        + (0.025 if switch <= previous < settled else 0)
                    )
                )
            efforts, seconds = tracker.finish(
                {UNIT: previous_action}, minute * 60, modes={UNIT: previous_mode}
            )
            model.update(
                before,
                values,
                outdoor,
                efforts,
                duration=seconds / 300,
                now=minute * 60,
                observed_history={f"system:{UNIT}": tracker.finished_segments[UNIT]},
                local_hour=(minute / 60) % 24,
            )
            if reference is not None and not halve:
                minimum = min(minimum, model.confidence_percent)
                for room, effect in reference[0].items():
                    assert model.blended(room, UNIT) * 12 == pytest.approx(
                        effect, rel=0.2
                    )
                assert minimum >= reference[1] - 0.5
            if u is None:
                # No forgetting, covariance change or regime scores on uncertain
                # intervals. Subsequent windows must also refill cleanly.
                assert model.rows == fitted
        if minute == switch:
            reference = (
                {r: model.blended(r, UNIT) * 12 for r in RATES},
                model.confidence_percent,
            )
            assert reference[1] > 0
        tracker.change(UNIT, action, minute * 60, mode=mode)
        if not minute:
            tracker.finish({UNIT: action}, 0, modes={UNIT: mode})
        if minute == restart:
            snapshot = deepcopy(model.to_dict())
            restored = yield model
            if restored is not None:
                assert restored.to_dict() == snapshot
                model = restored
                tracker = EffortTracker()
                tracker.finish({UNIT: action}, minute * 60, modes={UNIT: mode})
        fitted = deepcopy(model.rows)
        previous, previous_mode, previous_action = minute, mode, action
    if halve:
        for room, effect in reference[0].items():
            assert model.blended(
                room, UNIT, outdoor, values[room]
            ) * 12 == pytest.approx(effect / 2, rel=0.2)
    assert all(row.samples > 100 for row in model.rows.values())
    yield model


@pytest.mark.parametrize("restart", [False, True])
def test_auto_mode_replay_preserves_learning(restart):
    replay = mode_replay()
    model = next(replay)
    final = replay.send(
        Learning.from_dict(json.loads(json.dumps(model.to_dict()))) if restart else None
    )
    assert final.confidence_percent > 0


def test_persistent_halved_effect_is_still_learned():
    replay = mode_replay(halve=True)
    next(replay)
    replay.send(None)


@pytest.mark.parametrize(
    "mode,action,expected",
    [
        ("heat", "cooling", None),
        ("cool", "heating", None),
        ("heat", "heating", 1),
        ("cool", "cooling", -1),
        ("heat_cool", "heating", 1),
        ("heat_cool", "cooling", -1),
        ("off", "heating", 1),
        ("off", "cooling", -1),
    ],
)
def test_learning_effort_respects_action_and_mode(mode, action, expected):
    tracker = EffortTracker()
    tracker.finish({UNIT: action}, 0, modes={UNIT: mode})
    efforts, _ = tracker.finish({UNIT: action}, 300, modes={UNIT: mode})
    assert efforts[UNIT] == expected


def test_between_tick_mode_change_invalidates_entire_history():
    tracker = EffortTracker()
    tracker.finish({UNIT: "heating"}, 0, modes={UNIT: "heat"})
    tracker.change(UNIT, "cooling", 60, mode="cool")
    efforts, _ = tracker.finish({UNIT: "cooling"}, 300, modes={UNIT: "cool"})
    assert efforts[UNIT] is None
    assert tracker.finished_segments[UNIT] == [[0, 300, None]]
    efforts, _ = tracker.finish({UNIT: "cooling"}, 600, modes={UNIT: "cool"})
    assert efforts[UNIT] == -1


def test_challenger_reset_cannot_replace_main_or_keep_old_scores():
    model = Learning(["room"], [UNIT])
    row = model.rows["room"]
    row.theta = [0, 0.1, 0, 0, 0, 0]
    row.counts = [100]
    row.samples = 100
    main = deepcopy(row)
    model.streaks["room"] = (1, 1, 0)
    state = model.regimes["room"]
    state["challenger"].samples = 100
    state["challenger"].theta = [0, 0, 0, 0, 0, 0]
    state["surprises"] = 1
    state["blocks"] = [
        {
            "bucket": i,
            "seconds": 7200,
            "errors": {"active": 720, "challenger": 0},
            "contexts": ["day", "night", f"{UNIT}:0", f"{UNIT}:1"],
        }
        for i in range(12)
    ]
    model.update({"room": 20}, {"room": 21}, 20, {UNIT: 1}, now=86400)
    assert state["identified_since"] == 86400
    assert state["blocks"] == []
    # This interval begins a disturbance streak; it must not promote a reset fit.
    assert state["active"] == "main"
    assert state["saved"] == []
    assert model.rows["room"] == main
    assert not model.choose_regime("room")


def test_tiny_prediction_advantage_cannot_erase_established_fit():
    model = Learning(["room"], [UNIT])
    row = model.rows["room"]
    row.theta[1] = 0.1
    row.samples = 500
    row.counts = [100]
    row.covariance = [[0.001 if i == j else 0 for j in range(6)] for i in range(6)]
    model.regimes["room"]["challenger"].theta[1] = 0
    model.clock = 24 * 3600
    state = model.regimes["room"]
    state["blocks"] = [
        {
            "bucket": i,
            "seconds": 7200,
            "errors": {"active": 0.001 * 7200, "challenger": 0},
            "contexts": ["day", "night", f"{UNIT}:0", f"{UNIT}:1"],
        }
        for i in range(12)
    ]
    established = deepcopy(model.rows["room"])
    assert not model.choose_regime("room")
    assert model.rows["room"] == established


def test_legacy_load_recovers_archived_fit_only_with_observed_support():
    model = Learning(["room"], [UNIT], window_minutes=60)
    good = model.rows["room"]
    good.theta = [0, 0.1, 0, 0, 0, 0]
    good.samples = 500
    good.counts = [80]
    model.promote("room")
    model.rows["room"].theta[1] = -0.001
    model.clock = 4 * 3600
    state = model.regimes["room"]
    state["blocks"] = [
        {
            "bucket": i,
            "seconds": 7200,
            "errors": {"active": 720, "challenger": 720, "saved #1": 0},
            "contexts": [],
        }
        for i in range(2)
    ]
    legacy = model.to_dict()
    legacy.pop("learning_version")
    restored = Learning.from_dict(json.loads(json.dumps(legacy)))
    assert restored.rows["room"] == good
    assert Learning.from_dict(restored.to_dict()).to_dict() == restored.to_dict()
    legacy["regimes"]["room"]["blocks"] = []
    restored = Learning.from_dict(legacy)
    assert restored.rows["room"] == model.rows["room"]


def test_disturbance_evidence_round_trips_exactly():
    model = Learning(["room"], [UNIT])
    model.streaks["room"] = (-1, 3, 0)
    model.disturbed["room"] = "cooling"
    model.residuals["room"] = -0.25
    saved = model.to_dict()
    assert Learning.from_dict(json.loads(json.dumps(saved))).to_dict() == saved


def test_pending_command_withholds_old_mode_even_with_matching_action():
    tracker = EffortTracker()
    tracker.finish({UNIT: "heating"}, 0, modes={UNIT: "heat"})
    tracker.change(UNIT, None, 30)  # Mode service starts before any acknowledgement.
    tracker.change(UNIT, "heating", 60, mode="heat", commanded_mode="cool")
    efforts, _ = tracker.finish(
        {UNIT: "heating"}, 300, modes={UNIT: "heat"}, commanded_modes={UNIT: "cool"}
    )
    assert efforts[UNIT] is None
    assert tracker.finished_segments[UNIT] == [[0, 300, None]]

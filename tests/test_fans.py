"""Pure bandit, scheduling, quiet limits and simulated physical house."""

import json
from copy import deepcopy
from random import Random

import pytest

from custom_components.house_climate.const import DEFAULTS
from custom_components.house_climate.fans import (
    FanLearning,
    context,
    quiet_modes,
    reward,
)


def devices():
    return {"unit": {"fan_modes": ["a", "b", "c"], "fan_mode": "a", "running": True}}


def tick(model, systems, now, values=None, **kwargs):
    command = model.tick(
        now, "heat", 3, systems, values or {"one": 20, "two": 22}, {}, 21, **kwargs
    )
    if command:
        system, _, data = command
        systems[system].update(data)
    return command


def samples(model):
    return sum(
        v["samples"]
        for actions in model.arms.values()
        for buckets in actions.values()
        for arms in buckets.values()
        for v in arms.values()
    )


def test_neutral_start_explores_every_option():
    model = FanLearning(rng=Random(7))
    arms = model.options("unit", "fan", "heat|0|1", ["a", "b", "c"])
    assert list(arms.values()) == [{"mean_reward": 0, "samples": 0, "rooms": {}}] * 3
    assert model.confidence_percent == 0
    systems = devices()
    seen = set()
    for i in range(4):
        tick(model, systems, i * 1800)
        seen.add(systems["unit"]["fan_mode"])
    assert seen == {"a", "b", "c"}
    assert samples(model) == 3


def test_hold_and_one_change_across_systems_and_actions():
    model = FanLearning(rng=Random(3))
    systems = devices()
    systems["unit"].update(swing_modes=["x", "y"], swing_mode="x")
    systems["other"] = dict(systems["unit"])
    tick(model, systems, 0)
    for now in (1, 300, 900, 1799):
        assert tick(model, systems, now) is None
        assert samples(model) == 0
    tick(model, systems, 1800)
    assert model.window["action"] == "swing"
    assert samples(model) == 1
    assert tick(model, systems, 1801) is None
    tick(model, systems, 3600)
    assert model.window["system"] == "other"


@pytest.mark.parametrize(
    "reason", ["transient", "not_reporting", "setpoint", "mode", "disabled"]
)
def test_contaminated_windows_do_not_train(reason):
    model = FanLearning(rng=Random(1))
    systems = devices()
    tick(model, systems, 0)
    if reason in ("transient", "not_reporting", "mode", "setpoint"):
        model.invalidate()  # Event between scheduled passes, even if recovered.
    else:
        tick(model, systems, 300, enabled=False)
    assert tick(model, systems, 600) is None
    tick(model, systems, 1800)
    assert samples(model) == 0
    tick(model, systems, 3600)
    assert samples(model) == 1


def test_context_changes_and_setpoint_invalidate():
    model = FanLearning()
    systems = devices()
    tick(model, systems, 0)
    model.tick(1800, "cool", 12, systems, {"one": 21}, {}, 22)
    assert samples(model) == 0
    assert context("heat", -0.1, True) == "heat|-5|1"
    assert context("off", 3, True) is None
    assert context("heat", None, True) is None


def test_weighted_reward_and_tunable_mean_penalty():
    assert reward({"a": 20, "b": 24}, {"a": 3, "b": 1}, 21) == -3
    assert reward({"a": 22, "b": 24}, {}, 21, 2) == -9


def test_restart_keeps_learning_choices_but_restarts_hold():
    model = FanLearning(rng=Random(1))
    systems = devices()
    tick(model, systems, 0)
    tick(model, systems, 1800)
    saved = json.loads(json.dumps(model.to_dict()))
    restored = FanLearning(saved=saved, rng=Random(1))
    assert restored.arms == model.arms
    assert restored.chosen == model.chosen
    tick(restored, systems, 86400)
    assert samples(restored) == 1
    assert tick(restored, systems, 86401) is None
    tick(restored, systems, 88200)
    assert samples(restored) == 2


def test_outside_change_reasserts_and_discards_reward():
    model = FanLearning(rng=Random(1))
    systems = devices()
    tick(model, systems, 0)
    expected = systems["unit"]["fan_mode"]
    systems["unit"]["fan_mode"] = "outside"
    assert tick(model, systems, 600) == ("unit", "set_fan_mode", {"fan_mode": expected})
    assert tick(model, systems, 1800) is None
    assert samples(model) == 0


@pytest.mark.parametrize(
    "clock", ["00:00", "06:59", "07:00", "12:00", "22:00", "23:59"]
)
def test_quiet_off_restricts_nothing(clock):
    assert DEFAULTS["quiet_hours_enabled"] is False
    assert quiet_modes(["a", "b", "c", "auto"], "a", clock=clock) == [
        "a",
        "b",
        "c",
        "auto",
    ]


@pytest.mark.parametrize(
    "clock,limited",
    [
        ("00:00", True),
        ("06:59", True),
        ("07:00", False),
        ("21:59", False),
        ("22:00", True),
    ],
)
def test_quiet_cross_midnight(clock, limited):
    modes = ["unusual", "mediumish", "powerful", "auto"]
    assert quiet_modes(modes, "mediumish", True, clock) == (
        modes[:2] if limited else modes
    )


@pytest.mark.parametrize(
    "clock,limited",
    [("08:59", False), ("09:00", True), ("16:59", True), ("17:00", False)],
)
def test_quiet_daytime(clock, limited):
    assert quiet_modes(["a", "b"], "a", True, clock, "09:00", "17:00") == (
        ["a"] if limited else ["a", "b"]
    )


def simulate():
    model = FanLearning(rng=Random(412))
    systems = devices()
    rng = Random(731)
    temperature = [19.0, 23.0]
    choices, spreads, means = [], [], []
    # Five-minute physical steps, 600 half-hour experiments (~12.5 days).
    for step in range(3601):
        tick(model, systems, step * 300, dict(zip(("one", "two"), temperature)))
        fan = systems["unit"]["fan_mode"]
        equilibrium_half_spread = {"a": 2, "b": 0.3, "c": 1.2}[fan]
        mean = sum(temperature) / 2
        half_spread = (temperature[1] - temperature[0]) / 2
        mean += 0.4 * (21 - mean) + rng.gauss(0, 0.002)
        half_spread += 0.8 * (equilibrium_half_spread - half_spread)
        temperature = [mean - half_spread, mean + half_spread]
        choices.append(fan)
        spreads.append(2 * half_spread)
        means.append(mean)
    return model, choices, spreads, means


def test_simulated_house_converges_to_best_fan():
    model, choices, spreads, means = simulate()
    best = model.diagnostics()["systems"]["unit"]["fan"]["heat|0|1"]["best"]
    share = choices[-600:].count("b") / 600
    print(
        f"\nFan simulation: best={best}, final 100 windows best share={share:.1%}, mean spread={sum(spreads[-600:]) / 600:.3f} °C vs 4.000 baseline, maximum mean error={max(abs(m - 21) for m in means):.3f} °C"
    )
    assert best == "b"
    assert share > 0.9
    assert sum(spreads[-600:]) / 600 < 0.8
    assert max(abs(m - 21) for m in means) < 0.05


def test_reassertions_on_other_actions_wait_for_global_hold():
    model = FanLearning(rng=Random(3))
    systems = devices()
    systems["other"] = dict(systems["unit"])
    tick(model, systems, 0)
    tick(model, systems, 1800)
    systems["unit"]["fan_mode"] = "outside"
    assert tick(model, systems, 1900) is None
    assert tick(model, systems, 3600)[0] == "unit"
    assert tick(model, systems, 3601) is None


def test_quiet_restriction_interrupts_hold_immediately():
    model = FanLearning(rng=Random(5))
    systems = devices()
    systems["other"] = dict(systems["unit"])
    tick(model, systems, 0)
    assert systems["unit"]["fan_mode"] == "c"
    systems["unit"]["fan_modes"] = quiet_modes(["a", "b", "c"], "a", True, "22:00")
    assert tick(model, systems, 1799) == ("unit", "set_fan_mode", {"fan_mode": "a"})
    assert tick(model, systems, 1800) is None
    assert samples(model) == 0  # The allowed options changed during this window.


def test_diagnostics_best_only_uses_sampled_options_and_reconcile():
    model = FanLearning()
    arms = model.options("unit", "fan", "heat|0|1", ["a", "b"])
    arms["a"].update(samples=1, mean_reward=-3)
    assert model.diagnostics()["systems"]["unit"]["fan"]["heat|0|1"]["best"] == "a"
    model.reconcile([])
    assert model.arms == {}


@pytest.mark.parametrize("valid", [False, True])
def test_quiet_cap_without_experiment_or_outdoor_context(valid):
    model = FanLearning()
    systems = devices()
    systems["unit"].update(fan_modes=["a"], fan_mode="c", quiet_restricted=True)
    assert model.tick(0, "heat", None, systems, {}, {}, 21, valid=valid) == (
        "unit",
        "set_fan_mode",
        {"fan_mode": "a"},
    )
    assert model.window is None


@pytest.mark.parametrize(
    "action,modes,expected",
    [
        ("fan", ["low", "med", "high", "auto"], ["low", "med", "high"]),
        (
            "fan",
            ["superQuiet", "quiet", "low", "powerful", "superPowerful", "auto"],
            ["superQuiet", "quiet", "low", "powerful", "superPowerful"],
        ),
        (
            "swing",
            [
                "horizontal",
                "midhorizontal",
                "midpoint",
                "midvertical",
                "vertical",
                "auto",
                "swing",
            ],
            [
                "horizontal",
                "midhorizontal",
                "midpoint",
                "midvertical",
                "vertical",
                "swing",
            ],
        ),
    ],
)
def test_default_excludes_auto_from_runtime_arms(action, modes, expected):
    assert DEFAULTS["learn_exclude_modes"] == "auto"
    model = FanLearning()
    systems = {"unit": {f"{action}_modes": modes, f"{action}_mode": "auto"}}
    command = tick(model, systems, 0)
    assert list(model.arms["unit"][action]["heat|0|0"]) == expected
    assert command[1] == f"set_{action}_mode"
    assert systems["unit"][f"{action}_mode"] in expected
    assert tick(model, systems, 1) is None


@pytest.mark.parametrize(
    "excluded,expected",
    [
        ("auto", ["low", "HIGH", "swing"]),
        (" AUTO, high, , SWING ", ["low"]),
        ("", ["low", "HIGH", "Auto", "swing"]),
    ],
)
def test_case_insensitive_custom_exclusions(excluded, expected):
    model = FanLearning(learn_exclude_modes=excluded)
    for action in ("fan", "swing"):
        arms = model.options(
            "unit", action, "heat|0|1", ["low", "HIGH", "Auto", "swing"]
        )
        assert list(arms) == expected


def test_excluded_current_mode_gets_next_window_before_other_actions():
    model = FanLearning(rng=Random(1))
    systems = devices()
    systems["unit"].update(swing_modes=["vertical", "auto"], swing_mode="auto")
    assert tick(model, systems, 0) == (
        "unit",
        "set_swing_mode",
        {"swing_mode": "vertical"},
    )
    # A second excluded action waits for the global hold deadline.
    systems["unit"]["fan_mode"] = "auto"
    assert tick(model, systems, 1) is None
    assert tick(model, systems, 1800)[1] == "set_fan_mode"
    assert tick(model, systems, 1801) is None


@pytest.mark.parametrize("action", ["fan", "swing"])
def test_all_modes_excluded_leaves_action_alone(action):
    model = FanLearning(learn_exclude_modes="auto, low")
    systems = {"unit": {f"{action}_modes": ["Auto", "low"], f"{action}_mode": "Auto"}}
    assert tick(model, systems, 0) is None
    assert model.arms == {}
    assert model.window is None
    assert systems["unit"][f"{action}_mode"] == "Auto"


def test_saved_excluded_arms_choices_and_window_are_dropped_on_load():
    model = FanLearning(learn_exclude_modes="")
    for action in ("fan", "swing"):
        arms = model.options("unit", action, "heat|0|1", ["low", "AUTO"])
        arms["AUTO"].update(samples=10, mean_reward=1)
        arms["low"].update(samples=2, mean_reward=-1)
    model.chosen = {"unit": {"fan": "AUTO", "swing": "low"}}
    model._start(0, "unit", "fan", "heat|0|1", "AUTO", 21, -1)
    saved = json.loads(json.dumps(model.to_dict()))
    restored = FanLearning(saved=saved)
    for action in ("fan", "swing"):
        assert restored.arms["unit"][action]["heat|0|1"] == {
            "low": {"samples": 2, "mean_reward": -1, "rooms": {}}
        }
    assert restored.chosen == {"unit": {"swing": "low"}}
    assert restored.resume is None
    assert "AUTO" in saved["arms"]["unit"]["fan"]["heat|0|1"]


def test_quiet_cap_uses_only_list_order_for_auto():
    assert quiet_modes(["low", "Auto", "high"], "low", True, "23:00") == ["low"]
    assert quiet_modes(["Auto", "low", "high"], "low", True, "23:00") == ["Auto", "low"]


def test_room_moments_reweight_simulated_arms_without_relearning():
    model = FanLearning(hold_minutes=1, rng=Random(3))
    systems = {"unit": {"fan_modes": ["a", "b"], "fan_mode": "a"}}
    weights = {"A": 9, "B": 1}
    model.tick(0, "heat", 3, systems, {"A": 21, "B": 21}, weights, 21)
    # Deterministic rooms: a keeps A on target, b keeps B on target.
    for now in range(60, 6060, 60):
        choice = model.window["choice"]
        systems["unit"]["fan_mode"] = choice
        values = {"A": 21, "B": 24} if choice == "a" else {"A": 24, "B": 21}
        # Record the just-selected setting's response for the whole window.
        model.window["reward"] = {room: value - 21 for room, value in values.items()}
        model.tick(now, "heat", 3, systems, values, weights, 21)
    arms = model.arms["unit"]["fan"]["heat|0|0"]
    saved = deepcopy(model.to_dict())
    assert model.choose(arms)[0] == "a"
    assert arms["a"]["mean_reward"] == pytest.approx(-0.9)
    model.weights = {"A": 1, "B": 9}
    model.rescore()
    # Equalize UCB counts to isolate the measured preference.
    ranked = deepcopy(arms)
    for arm in ranked.values():
        arm["samples"] = 100
    assert model.choose(ranked)[0] == "b"
    assert arms["b"]["mean_reward"] == pytest.approx(-0.9)
    for name in arms:
        assert (
            arms[name]["rooms"]
            == saved["arms"]["unit"]["fan"]["heat|0|0"][name]["rooms"]
        )
        assert (
            arms[name]["samples"]
            == saved["arms"]["unit"]["fan"]["heat|0|0"][name]["samples"]
        )


def test_old_scalar_fan_arms_load_as_untried():
    saved = {
        "arms": {
            "unit": {"fan": {"heat|0|1": {"low": {"mean_reward": -3, "samples": 40}}}}
        }
    }
    restored = FanLearning(saved=saved)
    assert restored.arms["unit"]["fan"]["heat|0|1"]["low"] == {
        "rooms": {},
        "samples": 0,
        "mean_reward": 0,
    }
    assert saved["arms"]["unit"]["fan"]["heat|0|1"]["low"]["samples"] == 40


def test_fan_moments_time_weighting_penalty_and_removed_room():
    model = FanLearning(hold_minutes=1, penalty=2)
    systems = {"unit": {"fan_modes": ["a"], "fan_mode": "a"}}
    model.tick(0, "heat", 3, systems, {"A": 22, "B": 24}, {}, 21)
    model.tick(15, "heat", 3, systems, {"A": 24, "B": 22}, {}, 21)
    model.tick(60, "heat", 3, systems, {"A": 24, "B": 22}, {}, 21)
    arm = model.arms["unit"]["fan"]["heat|0|0"]["a"]
    assert arm["rooms"]["A"] == {
        "seconds": 60,
        "mean_error": 2.5,
        "mean_squared_error": 7,
    }
    assert arm["mean_reward"] == pytest.approx(-9)
    model.weights = {"A": 1}
    assert model.score(arm) == pytest.approx(-13.25)

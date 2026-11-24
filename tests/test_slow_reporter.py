"""Closed-loop simulation of a slow cloud reporter (v0.5.23).

Synthetic plant; no recorder data or house coefficients enter production code.
One heat pump heats six rooms through a first-order room loss. Its thermostat
latches on its own room (hysteresis 0.5 °C) and the compressor starts 2 min
after a call, never within 5 min of its last stop, runs a 6-min defrost every
60 min of running below 5 °C outdoor and leaves a 3-min warm tail after a
stop. The house sends the controller's binary commands: own ± 3 °C from the
device's *reported* temperature, and mode off for a 5-h span every morning.

The cloud reports the device every 120 min at :57 and right after each write.
Each report is the true state at its time, except 10% that are a frozen,
contradictory snapshot (the opposite action and the previous temperature).
That is the live 2026-11-21/22 pattern of the cloud-read heat pump.

HEAD learned from the last reported action. v0.5.23 labels effort from the
confirmed command with a transition model (`command_effort`,
`CommandTransitions`), cross-checks fresh reports (`CommandChecks`) and falls
back to a fresh report only when the command is ambiguous.
"""

from math import ceil, floor, pi, sin
from random import Random

import pytest

from custom_components.house_climate.latent import LatentEffort, room_terms
from custom_components.house_climate.learning import (
    CommandTransitions,
    EffortTracker,
    Learning,
    command_effort,
    own_estimate,
)
from custom_components.house_climate.reporting import CommandChecks

UNIT = "climate.unit"
RATES = {"r1": 1.0, "r2": 1.2, "r3": 1.5, "r4": 1.1, "r5": 1.3, "r6": 1.4}
LOSS = {"r1": 0.002, "r2": 0.0025, "r3": 0.003, "r4": 0.002, "r5": 0.0028, "r6": 0.0022}
THERMOSTAT = "r1"
HYSTERESIS = 0.5
START_DELAY, RESTART_GUARD, STOP_TAIL = 2, 5, 3
OFF_SPAN = (7 * 60, 12 * 60)  # local minutes of the daily commanded off span
LATENT_DELAY = 45  # block + longest response delay, minutes


def simulate(
    source="command",
    *,
    days=3,
    part_load=None,
    drift=0.0,
    seed=1122,
    lockout=None,
    modulating=False,
    latent=None,
):
    """Run the loop; return the model and the measured truth/coverage stats.

    `source` is "device" (HEAD: last reported action, mode-transition
    withholding and the set_hvac_mode pre-mark) or "command" (v0.5.23).
    `part_load` is a (low, high) range for a variable-speed unit's per-run
    capacity fraction; None is a single-speed unit. `lockout` is a (start, end)
    minute span in which the unit ignores its thermostat (a utility
    curtailment or lockout the house cannot see).

    `modulating` sends a modulating unit's on commands (own + 0.6–1.0 °C at a
    0.1 °C step, mostly ambiguous) after a 2-h clear warm-up following the daily
    off span. `latent` runs `LatentEffort` on the rooms: "apply" backfills its
    labels, "skip" estimates the same blocks but leaves them skipped.
    """
    rng = Random(seed)
    model = Learning(
        list(RATES),
        [UNIT],
        window_minutes=60,
        drift_noise=drift,
    )
    tracker = EffortTracker()
    labels = CommandTransitions(START_DELAY * 60, RESTART_GUARD * 60, STOP_TAIL * 60)
    checks = CommandChecks()
    temperatures = {r: 21.8 + rng.uniform(-0.4, 0.4) for r in RATES}

    def read():
        return {
            r: round((t + rng.gauss(0, 0.02)) / 0.1) * 0.1
            for r, t in temperatures.items()
        }

    def mean(values):
        return sum(values.values()) / len(values)

    device = {"mode": "heat", "target": 19.0}
    plant = {
        "call": False,
        "running": False,
        "start_due": None,
        "last_stop": -1e9,
        "tail_until": -1e9,
        "defrost_until": -1e9,
        "since_defrost": 0,
        "load": 1.0,
    }
    readings = read()
    report = {}

    def publish(minute):
        """One cloud report of the device at `minute`."""
        running = plant["running"]
        snapshot = {
            "time": minute,
            "mode": device["mode"],
            "target": device["target"],
            "own": round(temperatures[THERMOSTAT] / 0.1) * 0.1,
            "action": "heating" if running else "idle",
            "mean": mean(readings),
        }
        if report and rng.random() < 0.1:
            # Frozen snapshot: the old reading and the opposite action.
            snapshot["own"] = report["own"]
            snapshot["action"] = "idle" if running else "heating"
        if device["mode"] == "off":
            snapshot["action"] = "off"
        report.clear()
        report.update(snapshot)

    publish(0)
    command = {"mode": "heat", "target": device["target"], "sent_at": -1e9}
    on, since = False, -100
    stats = {"delivered": 0.0, "called": 0.0, "ticks": 0, "labelled": 0}
    stats.update(filled=0.0, latent_checks=0, latent_wrong=0)
    running = {}  # minute -> the compressor ran (truth for latent labels)
    estimator = LatentEffort() if latent else None
    off_ticks = off_accepted = 0

    def label(minute):
        """v0.5.23 learning action at `minute`: (value or action, trusted)."""
        fresh = minute - report["time"] <= 5
        own, correction, _ = own_estimate(report["own"], report["mean"], mean(readings))
        confirmed = minute - command["sent_at"] <= 2 or (
            report["mode"] == command["mode"]
            and abs(report["target"] - command["target"]) <= 0.25
        )
        kind, direction, _ = command_effort(
            command["mode"],
            command["target"],
            own,
            correction,
            active_mode="heat",
            confirmed=confirmed,
            hysteresis=HYSTERESIS,
        )
        current = labels.current(UNIT)
        cause = (
            command["sent_at"] * 60
            if current is None or command["sent_at"] * 60 > current["since"]
            else minute * 60
        )
        state = labels.observe(UNIT, kind, direction, minute * 60, cause)
        if fresh and state["label"] is not None:
            settled = (
                minute * 60 - state["start"] >= 5 * 60
                if state["label"] == "on"
                else minute * 60 - max(state["since"], state.get("tail_until", 0))
                >= 5 * 60
            )
            if settled:
                running = report["action"] in ("heating", "cooling")
                checks.check(UNIT, report["time"], running != (state["label"] == "on"))
        if checks.suspended(UNIT) or state["label"] is None:
            return (report["action"] if fresh else None), False
        return labels.value(UNIT, minute * 60), True

    def feed(minute):
        """Pass the current learning action to the tracker, like the coordinator."""
        if source == "device":
            tracker.change(
                UNIT,
                report["action"],
                minute * 60,
                mode=report["mode"],
                commanded_mode=command["mode"],
            )
            return report["action"], False
        last = tracker.times.get(UNIT, minute * 60)
        for t in labels.breakpoints(UNIT, last, minute * 60):
            tracker.change(UNIT, labels.value(UNIT, t), t, trusted=True)
        action, trusted = label(minute)
        tracker.change(
            UNIT,
            action,
            minute * 60,
            mode=report["mode"],
            commanded_mode=command["mode"],
            trusted=trusted,
        )
        return action, trusted

    def send(minute, mode, target):
        if source == "device" and mode != device["mode"]:
            tracker.change(UNIT, None, minute * 60)  # HEAD's set_hvac_mode pre-mark
        device["mode"], device["target"] = mode, target
        command.update(mode=mode, target=target, sent_at=minute)
        publish(minute)  # a write triggers a cloud refresh
        feed(minute)

    action, trusted = feed(0)
    tracker.finish({UNIT: action}, 0, trusted={UNIT} if trusted else ())
    outdoor = 4.0
    for step in range(1, days * 288 + 1):
        minute = step * 5
        before = readings
        for t in range(minute - 4, minute + 1):
            outdoor = 4 + 4 * sin(2 * pi * (t / 1440 - 0.375))
            own = temperatures[THERMOSTAT]
            if device["mode"] != "heat":
                plant["call"] = False
            elif not plant["call"] and device["target"] - own >= HYSTERESIS:
                plant["call"] = True
            elif plant["call"] and own >= device["target"]:
                plant["call"] = False
            locked = lockout and lockout[0] <= t < lockout[1]
            if locked and plant["running"]:
                plant.update(running=False, last_stop=t, tail_until=t)
            if plant["call"] and not plant["running"] and not locked:
                if plant["start_due"] is None:
                    plant["start_due"] = max(
                        t + START_DELAY, plant["last_stop"] + RESTART_GUARD
                    )
                if t >= plant["start_due"]:
                    plant.update(running=True, start_due=None, since_defrost=0)
                    if part_load:
                        plant["load"] = rng.uniform(*part_load)
            elif not plant["call"]:
                plant["start_due"] = None
                if plant["running"]:
                    plant.update(running=False, last_stop=t, tail_until=t + STOP_TAIL)
            delivered = 0.0
            if plant["running"]:
                if outdoor < 5:
                    plant["since_defrost"] += 1
                    if plant["since_defrost"] >= 60:
                        plant.update(defrost_until=t + 6, since_defrost=0)
                delivered = -0.3 if t < plant["defrost_until"] else plant["load"]
            elif t < plant["tail_until"]:
                delivered = 0.4
            stats["delivered"] += delivered
            running[t] = plant["running"]
            stats["called"] += plant["call"]
            for room in temperatures:
                temperatures[room] += (
                    LOSS[room] / 5 * (outdoor - temperatures[room])
                    + RATES[room] / 60 * delivered
                    + 0.004 / 5
                )
            if t % 120 == 57:
                readings = read()
                publish(t)
                feed(t)
        readings = read()
        action, trusted = feed(minute)
        efforts, seconds = tracker.finish(
            {UNIT: action},
            minute * 60,
            modes={UNIT: report["mode"]},
            commanded_modes={UNIT: command["mode"]},
            trusted={UNIT} if trusted else (),
        )
        stats["ticks"] += 1
        stats["labelled"] += efforts[UNIT] is not None
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
        if estimator:
            rooms = {}
            for room in RATES:
                value = model.temperature_filters[room].mean(
                    minute * 60 - 600, minute * 60
                )
                terms = room_terms(model, room, value, outdoor)
                if terms:
                    rooms[room] = (value, *terms)
            estimator.observe(minute * 60, rooms)
            for system, start, end, value in estimator.process(
                minute * 60,
                model.systems,
                {UNIT: {"direction": 1, "estimate": True}},
                lambda key, a, b: model.effort_span("system", key, a, b),
            ):
                if latent == "apply":
                    stats["filled"] += model.backfill_effort(system, start, end, value)
                ran = [running.get(t, False) for t in range(start // 60, end // 60)]
                stats["latent_checks"] += 1
                stats["latent_wrong"] += (sum(ran) / len(ran) > 0.5) != (value > 0)
            # Like the coordinator: windows wait for late labels once ready.
            ready = estimator.diagnostics(UNIT)["ready"]
            model.label_delay_minutes = LATENT_DELAY if ready else 0
        local = minute % 1440
        if OFF_SPAN[0] < local <= OFF_SPAN[1]:
            off_ticks += 1
            window = model.windows.get(THERMOSTAT)
            # Averaged windows need 60 + 10 min of labelled updates.
            off_accepted += bool(window and window.get("accepted"))
        # House controller on the reported (possibly stale) device temperature.
        house = mean(readings)
        if OFF_SPAN[0] <= local < OFF_SPAN[1]:
            if device["mode"] != "off":
                on, since = False, minute
                send(minute, "off", device["target"])
            continue
        if not on and house < 21.6 and minute - since >= 5:
            on, since = True, minute
        elif on and minute - since >= 10 and (house > 22.2 or minute - since >= 60):
            on, since = False, minute
        reported = report["own"]
        if modulating and OFF_SPAN[1] <= local < OFF_SPAN[1] + 120:
            on = True  # warm-up after the off span: a clear on
            own, _, _ = own_estimate(reported, report["mean"], house)
            target = ceil((own + 3) / 0.5) * 0.5
        elif modulating and on:
            if device["mode"] == "heat" and device["target"] - reported > 0.4:
                target = device["target"]
            else:
                target = round((reported + rng.uniform(0.6, 1.0)) / 0.1) * 0.1
        else:
            target = (
                ceil((reported + 3) / 0.5) * 0.5
                if on
                else floor((reported - 3) / 0.5) * 0.5
            )
        if device["mode"] != "heat" or device["target"] != target:
            send(minute, "heat", target)
    stats["coverage"] = (stats["labelled"] + stats["filled"] / 300) / stats["ticks"]
    stats["latent"] = estimator
    stats["off_windows"] = off_accepted / off_ticks
    stats["truth"] = {
        room: rate * stats["delivered"] / stats["called"]
        for room, rate in RATES.items()
    }
    stats["contradictions"] = checks.status(UNIT)
    stats["checks"] = checks
    return model, stats


def effects(model, outdoor=4.0, room=22.0):
    diagnostics = model.diagnostics(outdoor, dict.fromkeys(RATES, room))
    return {
        r: values["effective_effect"]
        for r, values in diagnostics["systems"][UNIT].items()
    }


def test_head_device_source_learns_almost_nothing():
    """Pinned failing: HEAD's stale action leaves every effect under half of truth."""
    model, stats = simulate("device")
    learned = effects(model)
    for room, truth in stats["truth"].items():
        assert learned[room] < 0.5 * truth


@pytest.mark.parametrize("drift", [0.0, 1.0])
def test_command_source_learns_commanded_effects(drift):
    model, stats = simulate("command", drift=drift)
    learned = effects(model)
    for room, truth in stats["truth"].items():
        assert learned[room] == pytest.approx(truth, rel=0.2), room
    assert stats["coverage"] >= 0.85
    # Frozen snapshots alone never suspend command labels.
    assert not stats["contradictions"]["suspended"]


@pytest.mark.parametrize("drift", [0.0, 1.0])
def test_variable_speed_learns_mean_delivered_per_commanded_hour(drift):
    model, stats = simulate("command", part_load=(0.35, 0.85), drift=drift)
    learned = effects(model)
    for room, truth in stats["truth"].items():
        assert learned[room] == pytest.approx(truth, rel=0.25), room
    assert stats["coverage"] >= 0.85


def test_long_commanded_off_spans_keep_windows():
    _, command = simulate("command")
    _, head = simulate("device")
    assert command["off_windows"] >= 0.9
    # HEAD lost about an hour of windows around each mode switch.
    assert head["off_windows"] < command["off_windows"]


def test_lockout_suspends_command_labels():
    """A unit that ignores its commands is caught by fresh-report cross-checks."""
    _, stats = simulate("command", lockout=(12 * 60, 3 * 1440))
    assert stats["contradictions"]["suspended"]
    assert stats["contradictions"]["rate"] > 0.3


@pytest.mark.parametrize("drift", [0.0, 1.0])
def test_latent_raises_coverage_without_raising_confidence(drift):
    """Modulating unit: most commands are ambiguous; latent labels fill them.

    The same replay with the latent spans skipped is the reference: latent
    labels add coverage and windows but no confidence (counts or the
    independent covariance), and the effects stay near truth.
    """
    skipped, before = simulate(
        "command", modulating=True, latent="skip", days=6, drift=drift
    )
    model, after = simulate(
        "command", modulating=True, latent="apply", days=6, drift=drift
    )
    assert after["latent"].diagnostics(UNIT)["ready"]
    # v0.5.25: suspensions clear after 4 agreeing checks, so the reference
    # keeps more command coverage (0.67 vs 0.30) and the gain is smaller.
    assert after["coverage"] - before["coverage"] >= 0.20
    # Latent labels never raise confidence (v0.5.25: with more command time
    # mixed in, windows holding latent time can lower it slightly).
    assert model.confidence_percent <= skipped.confidence_percent + 1.0
    for room in RATES:
        assert (
            100 * model.confidence(room, UNIT)
            <= 100 * skipped.confidence(room, UNIT) + 1.0
        ), room
        # Latent time never adds counts; a window mixing latent and command
        # time (more of them since v0.5.25's faster clearing) adds none.
        assert all(
            got <= ref + 1e-9
            for got, ref in zip(model.rows[room].counts, skipped.rows[room].counts)
        ), room
    learned = effects(model)
    for room, truth in after["truth"].items():
        assert learned[room] == pytest.approx(truth, rel=0.25), room


def test_latent_labels_match_the_true_run_state():
    """Every decisive latent label agrees with the plant in its block."""
    _, stats = simulate("command", modulating=True, latent="apply", days=6)
    assert stats["latent_checks"] >= 100
    assert stats["latent_wrong"] / stats["latent_checks"] <= 0.05

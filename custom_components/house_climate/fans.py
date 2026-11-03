"""Contextual UCB fan/vane learning and global experiment scheduling; no HA I/O."""

from copy import deepcopy
from math import floor, isfinite, log, sqrt
from random import Random


def context(mode, outdoor, running):
    if mode not in ("heat", "cool") or outdoor is None or not isfinite(outdoor):
        return None
    low = floor(outdoor / 5) * 5
    return f"{mode}|{low}|{int(running)}"


def quiet_modes(
    modes, maximum, enabled=False, clock="12:00", start="22:00", end="07:00"
):
    """List order defines the limit. End is exclusive."""
    inside = start <= clock < end if start < end else clock >= start or clock < end
    if not enabled or start == end or not inside or maximum not in modes:
        return list(modes)
    return list(modes[: modes.index(maximum) + 1])


def reward(values, weights, setpoint, penalty=1):
    pairs = [(v, weights.get(k, 1)) for k, v in values.items() if weights.get(k, 1) > 0]
    if not pairs:
        return None
    total = sum(w for _, w in pairs)
    mean = sum(v * w for v, w in pairs) / total
    return (
        -sum(w * (v - mean) ** 2 for v, w in pairs) / total
        - penalty * (mean - setpoint) ** 2
    )


class FanLearning:
    """Equal zero-count priors, random tie breaks, UCB exploration.

    Arms retain time-weighted per-room error moments over clean holds.
    Rewards are recomputed under current priorities; no thermal weights enter fits.
    A dirty window keeps its deadline but never trains. Restarts preserve arms
    and choices, then begin a fresh full hold without crediting downtime.
    """

    def __init__(
        self,
        hold_minutes=30,
        penalty=1,
        saved=None,
        rng=None,
        learn_exclude_modes="auto",
    ):
        if hold_minutes <= 0 or penalty < 0:
            raise ValueError("Invalid fan learning parameters")
        self.excluded = {
            mode.strip().casefold()
            for mode in learn_exclude_modes.split(",")
            if mode.strip()
        }
        self.hold = hold_minutes * 60
        self.penalty = penalty
        self.rng = rng or Random()
        saved = deepcopy(saved or {})
        self.arms = saved.get("arms", {})
        for actions in self.arms.values():
            for buckets in actions.values():
                for arms in buckets.values():
                    for arm in arms.values():
                        if "rooms" not in arm:
                            arm.update(rooms={}, samples=0, mean_reward=0.0)
        self.weights = {}
        self.chosen = saved.get("chosen", {})
        self.cursor = saved.get("cursor", 0)
        self.window = None
        self.resume = saved.get("window")
        self.last_decision = "Fan learning has not received its first control pass."
        self.wait_reason = None
        self.reconcile(self.arms.keys() | self.chosen.keys())

    def learnable_modes(self, modes):
        return [mode for mode in modes if mode.casefold() not in self.excluded]

    def reconcile(self, systems):
        """Drop removed systems and excluded modes from persisted experience."""
        self.arms = {s: a for s, a in self.arms.items() if s in systems}
        self.chosen = {s: a for s, a in self.chosen.items() if s in systems}
        for actions in self.arms.values():
            for buckets in actions.values():
                for bucket, arms in buckets.items():
                    buckets[bucket] = {
                        m: a
                        for m, a in arms.items()
                        if m.casefold() not in self.excluded
                    }
        for system, actions in self.chosen.items():
            self.chosen[system] = {
                a: m for a, m in actions.items() if m.casefold() not in self.excluded
            }
        if self.resume and (
            self.resume["system"] not in systems
            or self.resume["choice"].casefold() in self.excluded
        ):
            self.resume = None
        if self.window and (
            self.window["system"] not in systems
            or self.window["choice"].casefold() in self.excluded
        ):
            self.window = None

    def invalidate(self, reason="Fan experiment interrupted"):
        if self.window:
            self.window["dirty"] = True
            self.window["reason"] = reason

    def options(self, system, action, bucket, modes):
        arms = (
            self.arms.setdefault(system, {})
            .setdefault(action, {})
            .setdefault(bucket, {})
        )
        modes = self.learnable_modes(modes)
        for mode in modes:
            arms.setdefault(mode, {"mean_reward": 0.0, "samples": 0, "rooms": {}})
        return {m: arms[m] for m in modes}

    def score(self, arm):
        pairs = [
            (r, self.weights.get(k, 0) if self.weights else 1)
            for k, r in arm["rooms"].items()
            if (self.weights.get(k, 0) if self.weights else 1) > 0 and r["seconds"] > 0
        ]
        total = sum(w for _, w in pairs)
        if not total:
            return 0.0
        mean = sum(w * r["mean_error"] for r, w in pairs) / total
        square = sum(w * r["mean_squared_error"] for r, w in pairs) / total
        return -(square - mean**2) - self.penalty * mean**2

    def rescore(self):
        for actions in self.arms.values():
            for buckets in actions.values():
                for arms in buckets.values():
                    for arm in arms.values():
                        arm["mean_reward"] = self.score(arm)

    def choose(self, arms):
        unseen = [m for m, a in arms.items() if not a["samples"]]
        if unseen:
            return self.rng.choice(unseen), True
        total = sum(a["samples"] for a in arms.values())
        scores = {
            m: a["mean_reward"] + sqrt(2 * log(total + 1) / a["samples"])
            for m, a in arms.items()
        }
        best = max(scores.values())
        choice = self.rng.choice([m for m, score in scores.items() if score == best])
        return choice, choice != max(arms, key=lambda m: arms[m]["mean_reward"])

    def tick(
        self,
        now,
        mode,
        outdoor,
        systems,
        values,
        weights,
        setpoint,
        *,
        valid=True,
        enabled=True,
        invalid_reason=None,
    ):
        """Systems map IDs to available, running, fan/swing_modes and fan/swing_mode.

        Caller supplies already restricted option lists and clean filtered rooms.
        Returns at most one (system, service, data) command, only if different.
        """
        self.weights = dict(weights)
        self.rescore()
        systems = {
            system: {
                **device,
                **{
                    f"{action}_modes": self.learnable_modes(
                        device.get(f"{action}_modes", [])
                    )
                    for action in ("fan", "swing")
                },
            }
            for system, device in systems.items()
        }
        candidates = []
        for system, device in systems.items():
            bucket = context(mode, outdoor, device.get("running", False))
            if not device.get("available", True) or bucket is None:
                continue
            for action in ("fan", "swing"):
                modes = device.get(f"{action}_modes", [])
                if modes:
                    self.options(system, action, bucket, modes)
                    candidates.append((system, action, bucket, modes))
        if not enabled:
            reason = "fan control disabled or House mode off"
        elif not valid:
            reason = invalid_reason or "required room reading missing or not reporting"
        elif outdoor is None or not isfinite(outdoor):
            reason = "missing outdoor temperature reading"
        elif mode not in ("heat", "cool"):
            reason = f"HVAC mode {mode!r} has no heat/cool learning context"
        elif not candidates:
            unavailable = [
                s for s, d in systems.items() if not d.get("available", True)
            ]
            reason = (
                f"systems unavailable: {', '.join(unavailable)}"
                if unavailable
                else "no learnable fan/vane modes (missing options or all excluded)"
            )
        elif not values:
            reason = "no reporting room temperature readings"
        else:
            reason = None
        self.wait_reason = reason
        sample = (
            {k: v - setpoint for k, v in values.items()} if reason is None else None
        )
        if reason:
            self.invalidate(reason)
            self.last_decision = f"Fan learning paused: {reason}."
        # Quiet limits take effect on this pass, interrupting any global hold.
        if enabled:
            for system, device in systems.items():
                modes = device.get("fan_modes", [])
                chosen = self.chosen.get(system, {}).get("fan")
                restricted = device.get("quiet_restricted", False) or (
                    chosen is not None and chosen not in modes
                )
                if (
                    device.get("available", True)
                    and modes
                    and restricted
                    and device.get("fan_mode") not in modes
                ):
                    self.invalidate()
                    self.window = None
                    self.resume = None
                    choice = modes[0]
                    self.chosen.setdefault(system, {})["fan"] = choice
                    bucket = context(mode, outdoor, device.get("running", False))
                    if bucket is not None:
                        self.options(system, "fan", bucket, modes)
                    if bucket is not None and sample is not None:
                        self._start(
                            now, system, "fan", bucket, choice, setpoint, sample
                        )
                    self.last_decision = (
                        f"{system} fan capped at {choice} for quiet hours."
                        + (f" Fan learning paused: {reason}." if reason else "")
                    )
                    return self._command(system, "fan", choice)
        if not enabled:
            return None
        # Corrections do not depend on reward eligibility or outdoor context.
        for system, device in systems.items():
            if not device.get("available", True):
                continue
            for action in ("fan", "swing"):
                modes = device.get(f"{action}_modes", [])
                if not modes:
                    continue
                current = device.get(f"{action}_mode")
                chosen = self.chosen.get(system, {}).get(action)
                excluded = str(current).casefold() in self.excluded
                if not excluded and not (chosen in modes and current != chosen):
                    continue
                self.invalidate(f"{system} {action} setting changed")
                if (
                    self.window
                    and now - self.window["start"] < self.hold
                    and (system, action)
                    != (self.window["system"], self.window["action"])
                ):
                    self.last_decision = (
                        f"{system} {action} correction waits for the current fan hold deadline."
                        + (f" Fan learning paused: {reason}." if reason else "")
                    )
                    return None
                bucket = context(mode, outdoor, device.get("running", False))
                choice = chosen if chosen in modes else self.rng.choice(modes)
                self.chosen.setdefault(system, {})[action] = choice
                if bucket is not None:
                    self.options(system, action, bucket, modes)
                self.resume = None
                self._start(now, system, action, bucket, choice, setpoint, sample)
                self.last_decision = (
                    f"{system} {action} excluded mode {current}; trying {device.get('name', system)} "
                    f"{'vane' if action == 'swing' else 'fan'} {choice} to learn."
                    if excluded
                    else f"{system} {action} differs; re-asserting {choice}."
                ) + (f" Fan learning paused: {reason}." if reason else "")
                return self._command(system, action, choice)
        if reason:
            return None
        if self.resume:
            old = self.resume
            self.resume = None
            if any(
                (s, a, b) == (old["system"], old["action"], old["bucket"])
                and old["choice"] in modes
                for s, a, b, modes in candidates
            ):
                self._start(
                    now,
                    old["system"],
                    old["action"],
                    old["bucket"],
                    old["choice"],
                    setpoint,
                    sample,
                )
        if self.window:
            w = self.window
            if (
                w["bucket"] is None
                or mode != w["bucket"].split("|")[0]
                or setpoint != w["setpoint"]
            ):
                self.invalidate("HVAC mode or user setpoint changed")
            device = systems.get(w["system"], {})
            if (
                w["system"] not in systems
                or not device.get("available", True)
                or w["choice"] not in device.get(f"{w['action']}_modes", [])
            ):
                self.invalidate(
                    f"{w['system']} unavailable or selected {w['action']} mode no longer offered"
                )
            duration = max(0, now - w["last"])
            for room, error in w["reward"].items():
                stats = w["rooms"].setdefault(room, [0.0, 0.0, 0.0])
                stats[0] += duration
                stats[1] += duration * error
                stats[2] += duration * error**2
            w["last"], w["reward"] = now, sample
        if self.window and now - self.window["start"] < self.hold:
            w = self.window
            self.last_decision = (
                f"Fan window invalid: {w.get('reason', 'experiment interrupted')}; "
                "holding setting until the deadline."
                if w["dirty"]
                else f"Holding {w['system']} {w['action']} {w['choice']} for fan learning."
            )
            return None
        if self.window:
            w = self.window
            if not w["dirty"]:
                arm = self.arms[w["system"]][w["action"]][w["bucket"]][w["choice"]]
                arm["samples"] += 1
                for room, (seconds, error, squared) in w["rooms"].items():
                    if not seconds:
                        continue
                    stats = arm["rooms"].setdefault(
                        room, dict(seconds=0.0, mean_error=0.0, mean_squared_error=0.0)
                    )
                    total = stats["seconds"] + seconds
                    stats["mean_error"] += (
                        error - seconds * stats["mean_error"]
                    ) / total
                    stats["mean_squared_error"] += (
                        squared - seconds * stats["mean_squared_error"]
                    ) / total
                    stats["seconds"] = total
                arm["mean_reward"] = self.score(arm)
            self.window = None
        restricted = [
            c
            for c in candidates
            if (
                self.chosen.get(c[0], {}).get(c[1]) is not None
                and self.chosen[c[0]][c[1]] not in c[3]
            )
            or str(systems[c[0]].get(f"{c[1]}_mode", "")).casefold() in self.excluded
        ]
        system, action, bucket, modes = (
            restricted[0] if restricted else candidates[self.cursor % len(candidates)]
        )
        self.cursor += 1
        choice, exploring = self.choose(self.options(system, action, bucket, modes))
        self.chosen.setdefault(system, {})[action] = choice
        self._start(now, system, action, bucket, choice, setpoint, sample)
        label = "vane" if action == "swing" else "fan"
        heat, low, _ = bucket.split("|")
        name = systems[system].get("name", system)
        self.last_decision = (
            f"trying {name} {label} {choice} to learn"
            if exploring or not self.arms[system][action][bucket][choice]["samples"]
            else f"{name} {label} {choice} is best so far for {heat} at {low}–{int(low) + 5} °C outside"
        )
        if systems[system].get(f"{action}_mode") != choice:
            return self._command(system, action, choice)
        return None

    def _start(self, now, system, action, bucket, choice, setpoint, sample):
        self.window = dict(
            start=now,
            last=now,
            system=system,
            action=action,
            bucket=bucket,
            choice=choice,
            setpoint=setpoint,
            reward=sample or {},
            rooms={},
            dirty=sample is None or bucket is None,
            reason=self.wait_reason,
        )

    @staticmethod
    def _command(system, action, choice):
        return system, f"set_{action}_mode", {f"{action}_mode": choice}

    def diagnostics(self):
        self.rescore()
        return {
            "systems": {
                s: {
                    a: {
                        b: {
                            "best": max(
                                (m for m in arms if arms[m]["samples"]),
                                key=lambda m: arms[m]["mean_reward"],
                            )
                            if any(v["samples"] for v in arms.values())
                            else None,
                            "options": deepcopy(arms),
                        }
                        for b, arms in buckets.items()
                    }
                    for a, buckets in actions.items()
                }
                for s, actions in self.arms.items()
            },
            "last_decision": self.last_decision,
            "wait_reason": self.wait_reason,
            "window": deepcopy(self.window),
        }

    @property
    def confidence_percent(self):
        counts = [
            min(v["samples"] for v in arms.values())
            for actions in self.arms.values()
            for buckets in actions.values()
            for arms in buckets.values()
            if arms
        ]
        return 100 * sum(n / (n + 10) for n in counts) / len(counts) if counts else 0

    def to_dict(self):
        return deepcopy(
            dict(
                arms=self.arms,
                chosen=self.chosen,
                cursor=self.cursor,
                window=self.window,
            )
        )

"""Deterministic house control, with no Home Assistant dependencies."""

from copy import copy
from dataclasses import dataclass, field
from math import ceil, floor, isfinite


@dataclass
class Reading:
    value: float | None
    age: float = 0
    reporting: bool = True


@dataclass
class Device:
    mode: str = "off"
    temperature: float | None = None
    minimum: float = 7
    maximum: float = 35
    preset: str | None = None
    available: bool = True
    hvac_action: str | None = None
    current_temperature: float | None = None
    name: str | None = None
    step: float = 0.5


@dataclass
class ControlState:
    setpoint: float = 21
    mode: str = "off"
    active_mode: str = "off"
    last_switch: float = 0
    integrals: dict[str, float] = field(default_factory=dict)
    offsets: dict[str, float] = field(default_factory=dict)
    last_update: float | None = None
    last_run: dict[str, float] = field(default_factory=dict)
    # Legacy retry timestamps remain loadable; only steady evidence gates tests.
    last_excite: dict[str, float] = field(default_factory=dict)
    last_steady_test: dict[str, float] = field(default_factory=dict)
    steady_runs: dict[str, dict] = field(default_factory=dict)
    completed_runs: dict[str, dict] = field(default_factory=dict)
    test_observed_since: dict[str, float] = field(default_factory=dict)
    experiment_info: dict[str, dict] = field(default_factory=dict)
    action_times: dict[str, float] = field(default_factory=dict)
    actions: dict[str, str | None] = field(default_factory=dict)
    excitation: dict = field(default_factory=dict)
    efforts: dict[str, float] = field(default_factory=dict)
    target_adjustments: dict[str, float] = field(default_factory=dict)
    rounding_errors: dict[str, float] = field(default_factory=dict)
    drive_bias: dict[str, float] = field(default_factory=dict)
    requested_efforts: dict[str, float] = field(default_factory=dict)
    effort_switches: dict[str, float] = field(default_factory=dict)

    thermostats: dict[str, dict] = field(default_factory=dict)
    thermostat_points: dict[str, list[float]] = field(default_factory=dict)

    reported_actions: dict[str, str] = field(default_factory=dict)
    reported_efforts: dict[str, float] = field(default_factory=dict)
    reported_names: dict[str, str] = field(default_factory=dict)

    last_start: dict[str, float] = field(default_factory=dict)
    last_stop: dict[str, float] = field(default_factory=dict)
    starts: dict[str, list[float]] = field(default_factory=dict)
    reported_starts: dict[str, list[float]] = field(default_factory=dict)
    commanded: dict[str, bool] = field(default_factory=dict)
    command_start: dict[str, float] = field(default_factory=dict)
    command_stop: dict[str, float] = field(default_factory=dict)
    pending_start: dict[str, bool] = field(default_factory=dict)

    def demand_on(self, key):
        return self.commanded.get(key, self.actions.get(key) in ("heating", "cooling"))

    def command_demand(self, key, on, now):
        """Reserve a transition before sending it; failure retains a safe dwell."""
        previous = self.demand_on(key)
        self.commanded[key] = on
        if on != previous:
            if on:
                self.command_start[key] = now
                self.starts.setdefault(key, []).append(now)
                self.pending_start[key] = True
            else:
                self.command_stop[key] = now

    def observe_report(self, system, device, config):
        """Retain reported actuator input separately from unknown runtime."""
        key = system["entity_id"]
        if not device.available:
            return
        previous = self.thermostats.get(key, {})
        own = (
            device.current_temperature
            if device.current_temperature is not None
            else previous.get("own")
        )
        target = (
            device.temperature
            if device.temperature is not None
            else previous.get("target")
        )
        action = device.hvac_action
        mode = (
            device.mode
            if device.mode in ("heat", "cool", "off")
            else previous.get("mode", "off")
        )
        if action in ("heating", "cooling"):
            mode = "heat" if action == "heating" else "cool"
        if all(v is not None and isfinite(v) for v in (own, target)):
            direction = 1 if mode == "heat" else -1
            if (
                action in ("heating", "cooling")
                and previous.get("action") == "idle"
                and previous.get("target") == target
                and previous.get("mode") == mode
            ):
                gap = direction * (target - own)
                if 0 < gap <= 3:
                    points = self.thermostat_points.setdefault(key, [])
                    points.append(gap)
                    del points[:-32]
            self.thermostats[key] = {
                **previous,
                "own": own,
                "target": target,
                "mode": mode,
                "running": (
                    action in ("heating", "cooling")
                    if action is not None
                    else previous.get("running", False)
                ),
                "action": action,
                "time": None,
            }
        if not device.available or action not in (
            "heating",
            "cooling",
            "idle",
            "off",
            "fan",
            "drying",
        ):
            return
        key = system["entity_id"]
        effort = 0.0
        if action in ("heating", "cooling"):
            direction = 1 if action == "heating" else -1
            effort = 1.0
            if system.get("modulating", device.step < 0.5) and all(
                value is not None and isfinite(value)
                for value in (device.temperature, device.current_temperature)
            ):
                effort = clamp(
                    direction
                    * (device.temperature - device.current_temperature)
                    / config.get("run_margin", 1),
                    0,
                    1,
                )
            effort *= direction
        self.reported_actions[key] = action
        self.reported_efforts[key] = effort
        self.reported_names[key] = device.name or key

    def cycling_status(self, system, now):
        running = self.demand_on(system)
        clocks = (
            (self.last_start, self.command_start)
            if running
            else (self.last_stop, self.command_stop)
        )
        times = [clock[system] for clock in clocks if system in clock]
        since = max(times) if times else None
        return {
            "starts_last_hour": max(
                sum(now - 3600 < t <= now for t in clock.get(system, []))
                for clock in (self.starts, self.reported_starts)
            ),
            "run_minutes": max(0, now - since) / 60
            if running and since is not None
            else 0,
            "off_minutes": max(0, now - since) / 60
            if not running and since is not None
            else 0,
        }

    def observe_action(self, system, action, now):
        """Account only observed runtime, including transitions between ticks."""
        self.test_observed_since.setdefault(system, self.last_run.get(system, now))
        effort = self.reported_efforts.get(
            system, 1 if action == "heating" else -1 if action == "cooling" else 0
        )
        run = self.steady_runs.get(system)
        changed = run and (
            run["action"] != action or abs(run["effort"] - effort) > 0.05
        )
        if changed:
            self.completed_runs.setdefault(system, []).append(
                {"seconds": max(0, now - run["since"]), "ended": now}
            )
            self.completed_runs[system] = self.completed_runs[system][-64:]
        if action in ("heating", "cooling"):
            if not run or changed:
                self.steady_runs[system] = {
                    "action": action,
                    "effort": effort,
                    "since": now,
                }
        else:
            self.steady_runs.pop(system, None)
        previous = self.actions.get(system)
        elapsed = max(0, now - self.action_times.get(system, now))
        running = action in ("heating", "cooling")
        was_running = previous in ("heating", "cooling")
        known = action in ("heating", "cooling", "idle", "off", "fan", "drying")
        if known:
            if running and (not was_running or system not in self.last_start):
                self.last_start[system] = now
                self.reported_starts.setdefault(system, []).append(now)
                if not self.pending_start.pop(system, False):
                    self.starts.setdefault(system, []).append(now)
            elif not running and (
                was_running
                or self.last_start.get(system, float("-inf"))
                > self.last_stop.get(system, float("-inf"))
            ):
                self.last_stop[system] = now
            elif not running:
                self.last_stop.setdefault(system, now)
        self.starts[system] = [t for t in self.starts.get(system, []) if t > now - 3600]
        self.reported_starts[system] = [
            t for t in self.reported_starts.get(system, []) if t > now - 3600
        ]
        if running or previous in ("heating", "cooling"):
            self.last_run[system] = now
        else:
            self.last_run.setdefault(system, now)
        if self.excitation.get("system") == system and previous == {
            "heat": "heating",
            "cool": "cooling",
        }.get(self.excitation["mode"]):
            self.excitation["runtime"] += elapsed
        if self.excitation.get("system") == system and (
            changed
            or action
            != {"heat": "heating", "cool": "cooling"}.get(self.excitation["mode"])
        ):
            self.excitation["runtime"] = 0
        self.actions[system], self.action_times[system] = action, now


def cycling_hold(config, state, key, now):
    """Protect command transitions immediately and extend holds with reports."""
    settings = {
        **config,
        **next((s for s in config["systems"] if s["entity_id"] == key), {}),
    }
    running = state.demand_on(key)
    start = max(
        state.last_start.get(key, float("-inf")),
        state.command_start.get(key, float("-inf")),
    )
    stop = max(
        state.last_stop.get(key, float("-inf")),
        state.command_stop.get(key, float("-inf")),
    )
    # A real thermostat stop after acknowledgement is new evidence, unlike an
    # old idle report arriving while the commanded start is still pending.
    if not state.pending_start.get(key) and stop > start:
        running = False
    if running:
        if now - start < settings.get("min_run_minutes", 10) * 60:
            return True
    elif now - stop < settings.get("min_off_minutes", 5) * 60 or max(
        sum(now - 3600 < t <= now for t in clocks.get(key, []))
        for clocks in (state.starts, state.reported_starts)
    ) >= settings.get("max_starts_per_hour", 3):
        return False
    return None


def effort_limits(config, state, key, now, in_band):
    hold = cycling_hold(config, state, key, now)
    if hold is None and in_band:
        hold = state.demand_on(key)
    return (0.001, 1) if hold is True else (0, 0) if hold is False else (0, 1)


def excitation_scores(config, state, values, learning, outdoor=None):
    weights = {m["entity_id"]: m.get("weight", 1) for m in config["sensors"]}
    total = sum(weights.get(r, 0) for r in values)
    rooms = [
        r
        for r in values
        if r in learning.rows
        and total > 0
        and weights.get(r, 0) / total >= config.get("minor_weight_share", 0.05)
    ]
    scores = {}
    for key in (s["entity_id"] for s in config["systems"]):
        ranked = sorted(
            rooms,
            key=lambda r: weights[r] * learning.steady_std(r, key, outdoor, values[r]),
            reverse=True,
        )
        scores[key] = (
            sum(
                weights[r] * learning.steady_std(r, key, outdoor, values[r])
                for r in rooms
            ),
            ranked,
        )
    return scores


def excitation_target(
    config,
    state,
    devices,
    values,
    house,
    mode,
    now,
    learning,
    outdoor=None,
    solar=0,
    sources=None,
    allocation=None,
):
    """Select one steady, informative run within forecast comfort limits."""
    minutes = config.get("excite_minutes", 60)
    for key, runs in state.completed_runs.items():
        for run in runs:
            if run["seconds"] >= minutes * 60:
                state.last_steady_test[key] = max(
                    state.last_steady_test.get(key, float("-inf")), run["ended"]
                )
    state.completed_runs.clear()
    for key, run in state.steady_runs.items():
        if now - run["since"] >= minutes * 60:
            state.last_steady_test[key] = now
    if learning is None:
        state.excitation = {}
        return
    scores = excitation_scores(config, state, values, learning, outdoor)
    wait = config.get("excite_after_minutes", 120) * 60
    for key, (score, _) in scores.items():
        last = state.last_steady_test.get(key)
        # Only qualifying evidence postpones a test, never a failed attempt.
        eligible = (
            last if last is not None else state.test_observed_since.get(key, now)
        ) + wait
        state.experiment_info[key] = {
            "uncertainty_score": score,
            "last_steady_test": last,
            "next_eligible": eligible,
            "skip_reason": None,
            "blocked_by": None,
            "blocked_forecast": None,
            "blocked_ceiling": None,
        }
    helps = house is not None and (
        (mode == "heat" and house <= state.setpoint)
        or (mode == "cool" and house >= state.setpoint)
    )
    direction = 1 if mode == "heat" else -1

    def safe(key, remaining):
        efforts = dict(
            state.excitation.get(
                "others", allocation if allocation is not None else state.efforts
            )
        )
        for other, device in devices.items():
            if not device.available:
                efforts[other] = state.reported_efforts.get(other, 0)
        baseline_effort = (allocation if allocation is not None else state.efforts).get(
            key, 0
        )
        efforts[key] = direction
        systems = list(efforts)
        tested_index = systems.index(key)
        rooms = scores.get(key, (0, []))[1]
        limit = config.get("test_overshoot", 1.5)
        initial = state.excitation.get("initial_values", values)
        ceilings = {
            room: (
                direction * (initial.get(room, values[room]) - state.setpoint) + 0.3
                if direction * (initial.get(room, values[room]) - state.setpoint)
                > limit
                else limit
            )
            for room in rooms
        }
        # Account for the test's predicted contribution so far as well as its
        # remaining contribution. This keeps the 0.3 C allowance per test rather
        # than renewing it on every pass, while unrelated drift cancels out.
        elapsed_steps = max(
            0,
            ceil(
                (now - state.excitation.get("started", now))
                / (config.get("interval", 5) * 60)
            ),
        )
        steps = max(1, ceil(remaining / config.get("interval", 5)))
        for step in range(steps + 1):
            for room in rooms:
                base, effects = learning.forecast(
                    room, values[room], systems, step, outdoor, solar, sources
                )
                predicted = base + sum(b * u for b, u in zip(effects, efforts.values()))
                # Full-horizon effort coefficient retains passive decay and
                # delayed response across the elapsed/remaining boundary.
                _, test_effects = learning.forecast(
                    room,
                    initial.get(room, values[room]),
                    systems,
                    elapsed_steps + step,
                    outdoor,
                    solar,
                    sources,
                )
                increment = (
                    direction
                    * test_effects[tested_index]
                    * (direction - baseline_effort)
                )
                forecast = direction * (predicted - state.setpoint)
                without_test = forecast - increment
                if forecast > ceilings[room] and (
                    (
                        direction * (initial.get(room, values[room]) - state.setpoint)
                        <= limit
                        and without_test <= ceilings[room]
                    )
                    or increment > 0.3
                ):
                    state.experiment_info[key].update(
                        blocked_by=room,
                        blocked_forecast=predicted,
                        blocked_ceiling=state.setpoint + direction * ceilings[room],
                    )
                    return False
        return bool(rooms)

    threshold = (1 - config.get("explore_threshold", 0.8)) * 0.03
    candidates = []
    allocated = allocation if allocation is not None else state.efforts
    for key, (score, _) in scores.items():
        device = devices.get(key)
        reason = None
        if not device or not device.available:
            reason = "unavailable"
        elif device.current_temperature is None or not isfinite(
            device.current_temperature
        ):
            reason = "missing local temperature"
        elif house is None:
            reason = "missing house data"
        elif mode not in ("heat", "cool"):
            reason = "inactive mode"
        elif not helps:
            reason = (
                "house above setpoint" if mode == "heat" else "house below setpoint"
            )
        elif config.get("explore_threshold", 0.8) <= 0:
            reason = "exploration disabled"
        elif score <= threshold:
            reason = "not uncertain enough"
        elif cycling_hold(config, state, key, now) is False:
            reason = "cycling hold"
        elif now < state.experiment_info[key]["next_eligible"]:
            reason = "eligibility wait"
        elif state.excitation:
            reason = "another experiment running"
        elif not safe(key, minutes):
            room = state.experiment_info[key]["blocked_by"]
            reason = f"forecast above limit: {room}" if room else "no test rooms"
        state.experiment_info[key]["skip_reason"] = reason
        if reason is None:
            candidates.append(key)
    active = state.excitation
    if active:
        key = active["system"]
        device = devices.get(key)
        state.experiment_info[key]["skip_reason"] = None
        remaining = max(
            config.get("interval", 5), minutes - (now - active["started"]) / 60
        )
        unsafe = not safe(key, remaining)
        finished = (
            not helps
            or mode != active["mode"]
            or not device
            or not device.available
            or cycling_hold(config, state, key, now) is False
            or active["runtime"] >= minutes * 60
            or state.last_steady_test.get(key, float("-inf")) >= active["started"]
            or now - active["started"] >= 2 * minutes * 60
            or unsafe
        )
        if finished:
            # An abort remains due on the next pass. Only steady evidence earns
            # another full eligibility period.
            if active["runtime"] >= minutes * 60:
                state.last_steady_test[key] = now
                state.experiment_info[key]["last_steady_test"] = now
                state.experiment_info[key]["next_eligible"] = now + wait
            room = state.experiment_info[key]["blocked_by"]
            state.experiment_info[key]["skip_reason"] = (
                f"forecast above limit: {room}"
                if unsafe and room
                else "eligibility wait"
            )
            state.excitation = {}
        return
    # Prefer an idle unit when several are due, but a short normal allocation
    # cannot indefinitely replace the promised full steady test.
    for key in sorted(
        candidates,
        key=lambda k: (
            devices[k].hvac_action in ("heating", "cooling")
            or abs(allocated.get(k, 0)) > 0,
            -scores[k][0],
        ),
    ):
        device = devices[key]
        target = clamp(
            floor(
                (device.current_temperature + direction * config.get("run_margin", 1))
                / device.step
                + 0.5
            )
            * device.step,
            device.minimum,
            device.maximum,
        )
        if direction * (target - device.current_temperature) <= 0:
            state.experiment_info[key]["skip_reason"] = "target at device limit"
            continue
        state.excitation = {
            "system": key,
            "mode": mode,
            "started": now,
            "runtime": 0,
            "target": target,
            "rooms": scores[key][1][:2],
            "initial_values": dict(values),
            "others": {k: v for k, v in allocated.items() if k != key},
        }
        break


@dataclass
class Decision:
    temperature: float | None
    spread: float | None
    readings: dict[str, float]
    commands: list[tuple[str, str, dict]]
    explanation: str


def mean(values, members):
    """Weighted mean using only positive finite weights and valid readings."""
    pairs = [
        (values[m["entity_id"]], m.get("weight", 1))
        for m in members
        if m["entity_id"] in values
        and isfinite(m.get("weight", 1))
        and m.get("weight", 1) > 0
    ]
    return sum(v * w for v, w in pairs) / sum(w for _, w in pairs) if pairs else None


def clamp(value, low, high):
    return max(low, min(high, value))


def thermostat_effort(config, state, key, thermostat):
    """Apply the local thermostat's latched on/off thresholds."""
    mode = thermostat["mode"]
    if mode not in ("heat", "cool"):
        thermostat["running"] = False
        return 0.0
    settings = {**config, **next(s for s in config["systems"] if s["entity_id"] == key)}
    points = state.thermostat_points.get(key, [])
    hysteresis = (
        sum(points) / len(points)
        if points
        else settings.get("thermostat_hysteresis", 0.5)
    )
    direction = 1 if mode == "heat" else -1
    gap = direction * (thermostat["target"] - thermostat["own"])
    if gap <= 0:
        thermostat["running"] = False
    elif gap > hysteresis:
        thermostat["running"] = True
    return float(thermostat["running"]) * direction


def model_step(learning, sensor, temperature, efforts, outdoor, solar, sources):
    from .learning import PRIOR_B

    if learning and sensor in learning.rows:
        passive, coefficients = learning.forecast(
            sensor, temperature, list(efforts), 1, outdoor, solar, sources
        )
        return passive + sum(b * u for b, u in zip(coefficients, efforts.values()))
    return temperature + PRIOR_B * sum(efforts.values())


def outage_step(
    config, state, thermostats, efforts, learning, outdoor, solar, sources, fraction=1
):
    """Advance all own sensors simultaneously using the same joint inputs."""
    efforts = dict(efforts)
    for key, thermostat in thermostats.items():
        efforts[key] = thermostat_effort(config, state, key, thermostat)
    for key, thermostat in thermostats.items():
        sensor = thermostat.get("sensor", key)
        if learning and sensor not in learning.rows:
            sensor = key
        predicted = model_step(
            learning, sensor, thermostat["own"], efforts, outdoor, solar, sources
        )
        thermostat["own"] += fraction * (predicted - thermostat["own"])
    return efforts


def observe_thermostats(
    config, state, readings, devices, now, learning, outdoor, solar, sources
):
    """Advance saved outage state by elapsed time only, preferring real readings."""
    unavailable = {}
    for system in config["systems"]:
        key = system["entity_id"]
        device = devices.get(key, Device(available=False))
        thermostat = state.thermostats.get(key)
        if not thermostat:
            continue
        sensor = system.get("own_sensor", key)
        thermostat["sensor"] = sensor
        if device.available:
            thermostat["time"] = now
        else:
            unavailable[key] = thermostat
    if not unavailable:
        return
    if learning:
        learning = copy(learning)
        learning.effort_history = list(learning.effort_history)
    interval = config.get("interval", 5) * 60
    # Different last observations are possible after restore or event reports.
    remaining = {
        key: max(0, now - (t["time"] if t.get("time") is not None else now)) / interval
        for key, t in unavailable.items()
    }
    while any(v > 1e-9 for v in remaining.values()):
        fraction = min(1, max(remaining.values()))
        before = {key: t["own"] for key, t in unavailable.items()}
        joint = outage_step(
            config,
            state,
            unavailable,
            {
                s["entity_id"]: state.reported_efforts.get(s["entity_id"], 0)
                for s in config["systems"]
            },
            learning,
            outdoor,
            solar,
            sources,
            fraction,
        )
        if learning:
            learning.record_efforts(
                joint,
                sources or {},
                learning.effort_clock,
                learning.effort_clock + fraction * interval,
            )
        for key, thermostat in unavailable.items():
            share = min(fraction, remaining[key]) / fraction
            thermostat["own"] = before[key] + share * (thermostat["own"] - before[key])
            remaining[key] = max(0, remaining[key] - fraction)
    for key, thermostat in unavailable.items():
        reading = readings.get(thermostat["sensor"])
        real = devices.get(key, Device(available=False)).current_temperature
        if (
            reading
            and reading.reporting
            and reading.age <= config.get("stale_after", 720) * 60
        ):
            real = reading.value
        if real is not None and isfinite(real):
            thermostat["own"] = real
        thermostat["time"] = now
        thermostat_effort(config, state, key, thermostat)


def allocate(
    config,
    values,
    devices,
    mode,
    setpoint,
    outdoor=None,
    learning=None,
    solar=0,
    sources=None,
    state=None,
    now=0,
):
    """Bounded least squares for the joint horizon objective, without dependencies.

    Propagate the interval model as an affine function of constant signed effort.
    Coordinate candidates include discrete state changes and L1 movement kinks.
    Weights are normalized so lambda has the same meaning as the fan reward.
    """
    from .learning import PRIOR_B

    rooms = list(values)
    systems = [
        s["entity_id"]
        for s in config["systems"]
        if devices.get(s["entity_id"], Device(available=False)).available
    ]
    fixed = {
        s["entity_id"]: (
            0
            if s["entity_id"] in state.thermostats
            else state.reported_efforts.get(s["entity_id"], 0)
        )
        for s in config["systems"]
        if state and s["entity_id"] not in systems
    }
    forecast_systems = systems + list(fixed)
    weights_by_room = {m["entity_id"]: m.get("weight", 1) for m in config["sensors"]}
    total = sum(weights_by_room[r] for r in rooms)
    weights = [weights_by_room[r] / total for r in rooms]
    direction = 1 if mode == "heat" else -1
    steps = max(
        1, round(config.get("control_horizon_minutes", 45) / config.get("interval", 5))
    )
    base, effects = [], []
    for room in rooms:
        if learning and room in learning.rows:
            passive, coefficients = learning.forecast(
                room, values[room], forecast_systems, steps, outdoor, solar, sources
            )
        else:
            passive, coefficients = (
                values[room],
                [steps * PRIOR_B] * len(forecast_systems),
            )
        base.append(
            passive
            + sum(b * u for b, u in zip(coefficients[len(systems) :], fixed.values()))
        )
        effects.append([direction * b for b in coefficients[: len(systems)]])
    mean_base = sum(w * t for w, t in zip(weights, base))
    mean_effect = [
        sum(w * line[j] for w, line in zip(weights, effects))
        for j in range(len(systems))
    ]
    penalty = config.get("average_weight", 1.0)
    # Residual rows: weighted deviations about the predicted mean and mean error.
    residual = [t - mean_base for t in base] + [mean_base - setpoint]
    matrix = [[v - mean_effect[j] for j, v in enumerate(line)] for line in effects] + [
        mean_effect
    ]
    objective_weights = weights + [penalty]
    in_band = state is not None and all(
        abs(t - state.setpoint) <= config.get("comfort_band", 1) / 2
        for t in values.values()
    )
    bounds = [
        effort_limits(config, state, key, now, in_band) if state else (0, 1)
        for key in systems
    ]
    previous = [abs(state.efforts.get(key, 0)) if state else 0 for key in systems]
    on = [state.demand_on(key) if state else False for key in systems]
    switch_cost = config.get("switch_penalty", 0.05) if state else 0
    change_cost = config.get("effort_change_penalty", 0.005) if state else 0
    efforts = [clamp(u, low, high) for u, (low, high) in zip(previous, bounds)]
    residual = [
        r + sum(v * u for v, u in zip(line, efforts))
        for r, line in zip(residual, matrix)
    ]
    for _ in range(200):
        change = 0
        for j in range(len(systems)):
            curvature = sum(
                w * line[j] ** 2 for w, line in zip(objective_weights, matrix)
            )
            gradient = sum(
                w * line[j] * r
                for w, line, r in zip(objective_weights, matrix, residual)
            )
            low, high = bounds[j]
            # Exact coordinate candidates for quadratic + L1 effort movement +
            # the discrete cost at zero. Evaluate both sides of the L1 kink.
            candidates = [low, high, clamp(previous[j], low, high)]
            if curvature > 1e-12:
                for sign in (-1, 1):
                    candidates.append(
                        clamp(
                            efforts[j]
                            - (gradient + sign * change_cost / 2) / curvature,
                            max(low, 0.001),
                            max(high, max(low, 0.001)),
                        )
                    )
            candidates = [u for u in candidates if low <= u <= high]
            system = next(s for s in config["systems"] if s["entity_id"] == systems[j])
            if state and not system.get("modulating", devices[systems[j]].step < 0.5):
                candidates = [u for u in (0, 1) if low <= u <= high]

            def cost(u):
                delta = u - efforts[j]
                return (
                    curvature * delta * delta
                    + 2 * gradient * delta
                    + switch_cost * ((u > 0) != on[j])
                    + change_cost * abs(u - previous[j])
                )

            updated = min(candidates, key=cost)
            delta = updated - efforts[j]
            efforts[j] = updated
            residual = [r + line[j] * delta for r, line in zip(residual, matrix)]
            change = max(change, abs(delta))
        if change < 1e-9:
            break
    outages = (
        {key: dict(state.thermostats[key]) for key in fixed if key in state.thermostats}
        if state
        else {}
    )
    if outages:

        def simulate(candidate):
            thermostats = {key: dict(t) for key, t in outages.items()}
            temperatures = dict(values)
            inputs = {**fixed, **dict(zip(systems, (direction * u for u in candidate)))}
            rollout = copy(learning) if learning else None
            if rollout:
                rollout.effort_history = list(learning.effort_history)
            for _ in range(steps):
                joint = outage_step(
                    config,
                    state,
                    thermostats,
                    inputs,
                    rollout,
                    outdoor,
                    solar,
                    sources,
                )
                temperatures = {
                    room: model_step(rollout, room, t, joint, outdoor, solar, sources)
                    for room, t in temperatures.items()
                }
                if rollout:
                    rollout.record_efforts(
                        joint,
                        sources or {},
                        rollout.effort_clock,
                        rollout.effort_clock + rollout.interval_minutes * 60,
                    )
            average = sum(w * temperatures[r] for w, r in zip(weights, rooms))
            objective = sum(
                w * (temperatures[r] - average) ** 2 for w, r in zip(weights, rooms)
            )
            objective += penalty * (average - setpoint) ** 2
            objective += sum(
                switch_cost * ((u > 0) != running) + change_cost * abs(u - old)
                for u, running, old in zip(candidate, on, previous)
            )
            return objective, average

        # The thermostat makes the horizon piecewise rather than affine. Evaluate
        # each candidate's fixed outage trajectory with all candidate system inputs.
        for _ in range(20):
            before = efforts[:]
            for j, key in enumerate(systems):
                low, high = bounds[j]
                system = next(s for s in config["systems"] if s["entity_id"] == key)
                candidates = [u for u in (0, 1) if low <= u <= high]
                if system.get("modulating", devices[key].step < 0.5):
                    candidates = [low + (high - low) * k / 40 for k in range(41)]
                    candidates += [efforts[j], clamp(previous[j], low, high)]

                def cost(u):
                    candidate = efforts[:]
                    candidate[j] = u
                    return simulate(candidate)[0]

                best = min(candidates, key=cost)
                if system.get("modulating", devices[key].step < 0.5):
                    width = (high - low) / 40
                    for _ in range(18):
                        options = [
                            best,
                            max(low, best - width),
                            min(high, best + width),
                        ]
                        best = min(options, key=cost)
                        width /= 2
                efforts[j] = best
            if max((abs(a - b) for a, b in zip(before, efforts)), default=0) < 1e-8:
                break
        return dict(zip(systems, (direction * u for u in efforts))), simulate(efforts)[
            1
        ]
    predicted_mean = mean_base + sum(e * b for e, b in zip(efforts, mean_effect))
    return dict(zip(systems, (direction * u for u in efforts))), predicted_mean


def control(
    config,
    state,
    readings,
    devices,
    now,
    outdoor=None,
    enabled=True,
    learning=None,
    solar=0,
    sources=None,
    measured_efforts=None,
):
    """Allocate joint thermal effort, then command relative to each own sensor."""
    members = config["sensors"]
    room_names = {m["entity_id"]: m.get("name", m["entity_id"]) for m in members}
    stale_after = config.get("stale_after", 720) * 60
    notes = [
        f"{m['entity_id']}: stale reading older than {stale_after / 60:g} minutes; dropped."
        for m in members
        if (r := readings.get(m["entity_id"])) is not None
        and r.value is not None
        and isfinite(r.value)
        and r.age > stale_after
    ]
    values = {
        k: r.value
        for k, r in readings.items()
        if r.reporting
        and r.value is not None
        and isfinite(r.value)
        and r.age <= stale_after
    }
    values = {
        m["entity_id"]: values[m["entity_id"]]
        for m in members
        if m["entity_id"] in values
        and isfinite(m.get("weight", 1))
        and m.get("weight", 1) > 0
    }
    house = mean(values, members)
    spread = max(values.values()) - min(values.values()) if values else None
    interval = config.get("interval", 5) * 60
    elapsed = interval if state.last_update is None else max(0, now - state.last_update)
    dt = 1 if elapsed >= interval else 0
    mode = state.mode
    if mode == "heat_cool":
        mode = state.active_mode
        deadband = config.get("deadband", 1)
        if house is not None:
            wanted = (
                "heat"
                if house < state.setpoint - deadband
                else "cool"
                if house > state.setpoint + deadband
                else mode
            )
            if wanted != mode:
                if (
                    state.active_mode == "off"
                    or now - state.last_switch >= config.get("minimum_dwell", 60) * 60
                ):
                    mode = wanted
                else:
                    notes.append("Minimum mode dwell retained the current mode.")
    if outdoor is not None and (
        (mode == "cool" and outdoor < config.get("cool_outdoor_min", 10))
        or (mode == "heat" and outdoor > config.get("heat_outdoor_max", 24))
    ):
        mode = "off"
        notes.append("Outdoor temperature vetoed operation.")
    for system in config["systems"]:
        state.observe_report(
            system, devices.get(system["entity_id"], Device(available=False)), config
        )
    observe_thermostats(
        config, state, readings, devices, now, learning, outdoor, solar, sources
    )
    if not enabled:
        if state.excitation:
            state.last_excite[state.excitation["system"]] = now
            state.excitation = {}
        return Decision(
            house,
            spread,
            values,
            [],
            " ".join(["Control disabled; observing only.", *notes]),
        )
    if mode != state.active_mode:
        state.active_mode, state.last_switch = mode, now
        state.efforts.clear()
        state.effort_switches.clear()
        state.rounding_errors.clear()
        state.target_adjustments.clear()
    for key, device in devices.items():
        state.observe_action(key, device.hvac_action if device.available else None, now)
    commands = []
    allocation = {}
    if values and mode in ("heat", "cool"):
        integral = state.integrals.get("mean", 0)
        allocation, predicted = allocate(
            config,
            values,
            devices,
            mode,
            state.setpoint + integral,
            outdoor,
            learning,
            solar,
            sources,
            state,
            now,
        )
        error = state.setpoint - house
        # Integrate only when the model can reach the biased mean in this horizon.
        # This prevents an unavailable/ineffective system winding the mean bias up.
        # Scale gain by lambda/(1+lambda) so balance-first settings also weaken
        # mean correction rather than quietly undoing the allocator trade-off.
        average_weight = config.get("average_weight", 1.0)
        if dt and (state.setpoint + integral - predicted) * error <= 0.05:
            state.integrals["mean"] = clamp(
                integral
                + config.get("mean_integral_gain", 0.001)
                * average_weight
                / (1 + average_weight)
                * error,
                -1,
                1,
            )
    excitation_target(
        config,
        state,
        devices,
        values,
        house,
        mode,
        now,
        learning,
        outdoor,
        solar,
        sources,
        allocation,
    )
    if state.excitation:
        for key, effort in state.excitation.get("others", {}).items():
            allocation[key] = effort
    for system in config["systems"]:
        entity_id = system["entity_id"]
        device = devices.get(entity_id, Device(available=False))
        if not device.available:
            action = state.reported_actions.get(entity_id)
            name = state.reported_names.get(entity_id, device.name or entity_id)
            thermostat = state.thermostats.get(entity_id)
            if thermostat:
                effort = thermostat_effort(config, state, entity_id, thermostat)
                action = "heating" if effort > 0 else "cooling" if effort < 0 else "off"
                notes.append(
                    f"{name} unavailable; predicted own temperature {thermostat['own']:.1f} °C "
                    f"vs its target {thermostat['target']:g} °C, assuming {action}"
                    + (
                        " until it reaches it."
                        if effort
                        else " until its thermostat calls again."
                    )
                )
                continue
            notes.append(
                f"{name} unavailable; assuming it continues {action} as last reported."
                if action is not None
                else f"{name} unavailable; no reported action; assuming off."
            )
            continue
        target = state.setpoint
        if house is None:
            notes.append(f"{entity_id}: no usable room sensors; using house setpoint.")
        elif mode in ("heat", "cool"):
            direction = 1 if mode == "heat" else -1
            effort = abs(allocation.get(entity_id, 0))
            dither = (
                learning.dither(
                    entity_id,
                    now,
                    values.keys() & learning.rows.keys(),
                    config.get("explore_threshold", 0.8),
                )
                if learning and entity_id in learning.systems and not state.excitation
                else 0
            )
            if dt and measured_efforts is not None:
                measured = measured_efforts.get(entity_id)
                requested = state.requested_efforts.get(entity_id)
                if (
                    measured is not None
                    and isfinite(measured)
                    and requested is not None
                    and not state.excitation
                ):
                    state.drive_bias[entity_id] = clamp(
                        state.drive_bias.get(entity_id, 0)
                        + 0.1 * (requested - abs(measured)),
                        -0.5,
                        0.5,
                    )
            state.requested_efforts[entity_id] = effort
            # Actual action runtime closes the actuator loop for cycling units.
            # Endpoints always remain full on/off relative to the local sensor.
            if 0 < effort < 1:
                effort = clamp(effort + state.drive_bias.get(entity_id, 0), 0.001, 1)
            margin = config.get("run_margin", 1)
            if dither:
                if effort > 0:
                    effort = clamp(effort + direction * dither / (2 * margin), 0.001, 1)
                notes.append(f"{entity_id}: exploring ({dither:+.2f} °C dither).")
            previous = (
                abs(state.efforts[entity_id]) if entity_id in state.efforts else None
            )
            if (
                not state.excitation
                and previous is not None
                and (effort > 0) != (previous > 0)
            ):
                if (
                    0 < effort < config.get("effort_hysteresis", 0.05)
                    or now - state.effort_switches.get(entity_id, float("-inf"))
                    < config.get("effort_dwell_minutes", 5) * 60
                ):
                    effort = previous
            if previous is None or (effort > 0) != (previous > 0):
                state.effort_switches[entity_id] = now
            in_band = all(
                abs(t - state.setpoint) <= config.get("comfort_band", 1) / 2
                for t in values.values()
            )
            low, high = effort_limits(
                config, state, entity_id, now, in_band and not state.excitation
            )
            if state.excitation.get("system") == entity_id:
                effort = 1
            effort = clamp(effort, low, high)
            state.efforts[entity_id] = direction * effort
            if state.excitation:
                state.requested_efforts[entity_id] = effort
            own = device.current_temperature
            if own is None or not isfinite(own):
                own = house
                notes.append(
                    f"{device.name or entity_id}: own sensor missing; using house mean as target reference."
                )
            target = own + direction * margin * (effort if effort > 0 else -1)
            if state.excitation.get("system") == entity_id and high > 0:
                target = (
                    max(state.excitation["target"], target)
                    if direction > 0
                    else min(state.excitation["target"], target)
                )
                notes.append(
                    f"testing {device.name or entity_id} for {config.get('excite_minutes', 60):g} min to learn its effect on "
                    + ", ".join(
                        room_names.get(r, r) for r in state.excitation.get("rooms", [])
                    )
                )
            # Explain room-level tradeoffs with learned reach, never room mappings.
            reach = {
                r: max(0, learning.blended(r, entity_id, outdoor, values[r]))
                if learning and r in learning.rows and entity_id in learning.systems
                else 0.03
                for r in values
            }
            weights = {m["entity_id"]: m.get("weight", 1) for m in members}
            total = sum(weights[r] for r in values)
            named = [
                r
                for r in values
                if weights[r] / total >= config.get("minor_weight_share", 0.05)
            ]
            helped = sorted(
                (r for r in named if direction * (house - values[r]) > 0),
                key=lambda r: weights[r] * reach[r] * abs(house - values[r]),
                reverse=True,
            )
            above = max(
                named,
                key=lambda r: weights[r] * direction * (values[r] - house),
                default=None,
            )
            action = "on" if effort > 0 else "low"
            notes.append(
                f"{device.name or entity_id} {action} (effort {effort:.2f}): "
                + (
                    f"{'lifts' if mode == 'heat' else 'cools'} {', '.join(room_names[r] for r in helped)} toward the average; "
                    if helped and effort > 0
                    else f"limits {'heat' if mode == 'heat' else 'cooling'} into {room_names[above]}; "
                    if above is not None
                    else "balances the house average. "
                )
                + (
                    f"{room_names[above]} is {abs(values[above] - house):.2f} °C {'above' if mode == 'heat' else 'below'} the average."
                    if above is not None
                    else ""
                )
            )
        if house is None or mode == "off":
            state.rounding_errors.pop(entity_id, None)
            state.target_adjustments.pop(entity_id, None)
        raw_target = clamp(target, device.minimum, device.maximum)
        if dt:
            state.target_adjustments[entity_id] = state.rounding_errors.get(
                entity_id, 0
            )
        held = state.excitation.get("system") == entity_id
        adjusted = raw_target + (
            0 if held else state.target_adjustments.get(entity_id, 0)
        )
        target = clamp(
            floor(adjusted / device.step + 0.5) * device.step,
            device.minimum,
            device.maximum,
        )
        if dt and not held:
            state.rounding_errors[entity_id] = clamp(
                adjusted - target, -device.step / 2, device.step / 2
            )
        command_mode = mode
        keep_target = False
        if mode in ("heat", "cool"):
            direction = 1 if mode == "heat" else -1
            own = device.current_temperature
            if own is None or not isfinite(own):
                own = house
            hold = cycling_hold(config, state, entity_id, now)
            in_band = bool(values) and all(
                abs(t - state.setpoint) <= config.get("comfort_band", 1) / 2
                for t in values.values()
            )
            required = hold
            if required is None and in_band and not state.excitation:
                required = state.demand_on(entity_id)
            if required is None and house is not None:
                required = abs(state.efforts.get(entity_id, 0)) > 0
            if own is not None and required is not None:
                was_on = direction * (target - own) > 0
                if required:
                    edge = (
                        (floor(own / device.step) + 1) * device.step
                        if direction == 1
                        else (ceil(own / device.step) - 1) * device.step
                    )
                    target = max(target, edge) if direction == 1 else min(target, edge)
                else:
                    edge = (
                        (ceil(own / device.step) - 1) * device.step
                        if direction == 1
                        else (floor(own / device.step) + 1) * device.step
                    )
                    target = min(target, edge) if direction == 1 else max(target, edge)
                target = clamp(target, device.minimum, device.maximum)
                if hold is False and direction * (target - own) > 0:
                    command_mode = "off"
                if hold is not None and was_on != hold:
                    notes.append(
                        f"{device.name or entity_id}: held {'on' if hold else 'off'} for cycling protection."
                    )
                elif hold is not None:
                    notes.append(
                        f"{device.name or entity_id}: held {'on' if hold else 'off'} for cycling protection (reported action constraint)."
                    )
            elif hold is not None:
                notes.append(
                    f"{device.name or entity_id}: held {'on' if hold else 'off'} for cycling protection; no temperature reference."
                )
                if hold:
                    keep_target = True
                    if device.temperature is not None:
                        target = device.temperature
                else:
                    command_mode = "off"
        offset = target - state.setpoint
        if command_mode == "off":
            state.command_demand(entity_id, False, now)
        elif mode in ("heat", "cool") and own is not None:
            state.command_demand(entity_id, direction * (target - own) > 0, now)
        state.offsets[entity_id] = offset
        if device.mode != command_mode:
            notes.append(f"{entity_id}: mode differs; re-asserting {command_mode}.")
            commands.append((entity_id, "set_hvac_mode", {"hvac_mode": command_mode}))
        if command_mode != "off":
            preset = system.get("preset")
            if preset and device.preset != preset:
                commands.append((entity_id, "set_preset_mode", {"preset_mode": preset}))
            if device.temperature != target and not keep_target:
                notes.append(
                    f"{entity_id}: target differs; setting {target:.1f} °C (offset {offset:+.2f})."
                )
                commands.append((entity_id, "set_temperature", {"temperature": target}))
    if state.last_update is None or elapsed >= interval:
        state.last_update = now
    notes.insert(
        0,
        f"House {house:.2f} °C; selected {mode}."
        if house is not None
        else f"No house sensors; selected {mode}, zero offsets.",
    )
    return Decision(house, spread, values, commands, " ".join(notes))

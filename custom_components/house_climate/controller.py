"""Deterministic house control, with no Home Assistant dependencies."""

from dataclasses import dataclass, field
from math import floor, isfinite


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
    last_excite: dict[str, float] = field(default_factory=dict)
    action_times: dict[str, float] = field(default_factory=dict)
    actions: dict[str, str | None] = field(default_factory=dict)
    excitation: dict = field(default_factory=dict)
    efforts: dict[str, float] = field(default_factory=dict)
    target_adjustments: dict[str, float] = field(default_factory=dict)
    rounding_errors: dict[str, float] = field(default_factory=dict)
    drive_bias: dict[str, float] = field(default_factory=dict)
    requested_efforts: dict[str, float] = field(default_factory=dict)
    effort_switches: dict[str, float] = field(default_factory=dict)

    def observe_action(self, system, action, now):
        """Account only observed runtime, including transitions between ticks."""
        previous = self.actions.get(system)
        elapsed = max(0, now - self.action_times.get(system, now))
        running = action in ("heating", "cooling")
        if running or previous in ("heating", "cooling"):
            self.last_run[system] = now
        else:
            self.last_run.setdefault(system, now)
        if self.excitation.get("system") == system and previous == {
            "heat": "heating",
            "cool": "cooling",
        }.get(self.excitation["mode"]):
            self.excitation["runtime"] += elapsed
        self.actions[system], self.action_times[system] = action, now


def excitation_target(config, state, devices, values, house, mode, now, learning):
    """Select one bounded, held experiment; never infer runtime from a target."""
    helps = house is not None and (
        (mode == "heat" and house < state.setpoint)
        or (mode == "cool" and house > state.setpoint)
    )
    active = state.excitation
    if active:
        device = devices.get(active["system"])
        finished = (
            not helps
            or mode != active["mode"]
            or not device
            or not device.available
            or active["runtime"] >= config.get("excite_minutes", 30) * 60
            or now - active["started"] >= 3600
        )
        if finished:
            state.last_excite[active["system"]] = now
            state.excitation = {}
        return
    if not helps or learning is None:
        return
    sensors = values.keys() & learning.rows.keys()
    for system in config["systems"]:
        key = system["entity_id"]
        device = devices.get(key)
        if (
            not device
            or not device.available
            or device.current_temperature is None
            or not isfinite(device.current_temperature)
            or device.hvac_action in ("heating", "cooling")
            or not sensors
            or learning.system_confidence(key, sensors)
            >= config.get("explore_threshold", 0.8)
            or now - max(state.last_run[key], state.last_excite.get(key, float("-inf")))
            < config.get("excite_after_minutes", 120) * 60
        ):
            continue
        low, high = device.minimum, device.maximum
        if low > high:
            continue
        target = clamp(
            floor(
                (
                    device.current_temperature
                    + config.get("run_margin", 1) * (1 if mode == "heat" else -1)
                )
                / device.step
                + 0.5
            )
            * device.step,
            low,
            high,
        )
        state.excitation = {
            "system": key,
            "mode": mode,
            "started": now,
            "runtime": 0,
            "target": target,
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
):
    """Bounded least squares for the joint horizon objective, without dependencies.

    Propagate the interval model as an affine function of constant signed effort.
    Cyclic exact coordinate minimization solves the small convex box problem.
    Weights are normalized so lambda has the same meaning as the fan reward.
    """
    from .learning import PRIOR_B

    rooms = list(values)
    systems = [
        s["entity_id"]
        for s in config["systems"]
        if devices.get(s["entity_id"], Device(available=False)).available
    ]
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
                room, values[room], systems, steps, outdoor, solar, sources
            )
        else:
            passive, coefficients = values[room], [steps * PRIOR_B] * len(systems)
        base.append(passive)
        effects.append([direction * b for b in coefficients])
    mean_base = sum(w * t for w, t in zip(weights, base))
    mean_effect = [
        sum(w * line[j] for w, line in zip(weights, effects))
        for j in range(len(systems))
    ]
    penalty = config.get("fan_reward_lambda", 1)
    # Residual rows: weighted deviations about the predicted mean and mean error.
    residual = [t - mean_base for t in base] + [mean_base - setpoint]
    matrix = [[v - mean_effect[j] for j, v in enumerate(line)] for line in effects] + [
        mean_effect
    ]
    objective_weights = weights + [penalty]
    efforts = [0.0] * len(systems)
    for _ in range(200):
        change = 0
        for j in range(len(systems)):
            curvature = sum(
                w * line[j] ** 2 for w, line in zip(objective_weights, matrix)
            )
            if curvature < 1e-12:
                continue
            gradient = sum(
                w * line[j] * r
                for w, line, r in zip(objective_weights, matrix, residual)
            )
            updated = clamp(efforts[j] - gradient / curvature, 0, 1)
            delta = updated - efforts[j]
            efforts[j] = updated
            residual = [r + line[j] * delta for r, line in zip(residual, matrix)]
            change = max(change, abs(delta))
        if change < 1e-9:
            break
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
    excitation_target(config, state, devices, values, house, mode, now, learning)
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
        )
        error = state.setpoint - house
        # Integrate only when the model can reach the biased mean in this horizon.
        # This prevents an unavailable/ineffective system winding the mean bias up.
        if dt and (state.setpoint + integral - predicted) * error <= 0.05:
            state.integrals["mean"] = clamp(
                integral + config.get("mean_integral_gain", 0.001) * error, -1, 1
            )
    for system in config["systems"]:
        entity_id = system["entity_id"]
        device = devices.get(entity_id, Device(available=False))
        if not device.available:
            notes.append(f"{entity_id} unavailable; skipped.")
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
                if learning and entity_id in learning.systems
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
                effort = clamp(effort + state.drive_bias.get(entity_id, 0), 0, 1)
            margin = config.get("run_margin", 1)
            if dither:
                effort = clamp(effort + direction * dither / (2 * margin), 0, 1)
                notes.append(f"{entity_id}: exploring ({dither:+.2f} °C dither).")
            previous = (
                abs(state.efforts[entity_id]) if entity_id in state.efforts else None
            )
            if previous is not None and (effort > 0.5) != (previous > 0.5):
                if (
                    abs(effort - 0.5) < config.get("effort_hysteresis", 0.05)
                    or now - state.effort_switches.get(entity_id, float("-inf"))
                    < config.get("effort_dwell_minutes", 5) * 60
                ):
                    effort = previous
            if previous is None or (effort > 0.5) != (previous > 0.5):
                state.effort_switches[entity_id] = now
            state.efforts[entity_id] = direction * effort
            own = device.current_temperature
            if own is None or not isfinite(own):
                own = house
                notes.append(
                    f"{device.name or entity_id}: own sensor missing; using house mean as target reference."
                )
            target = own + direction * margin * (2 * effort - 1)
            if state.excitation.get("system") == entity_id:
                target = state.excitation["target"]
                notes.append(
                    f"exploring: running {device.name or entity_id} to learn which rooms it reaches"
                )
            # Explain room-level tradeoffs with learned reach, never room mappings.
            reach = {
                r: max(0, learning.blended(r, entity_id))
                if learning and r in learning.rows and entity_id in learning.systems
                else 0.03
                for r in values
            }
            helped = sorted(
                (r for r in values if direction * (house - values[r]) > 0),
                key=lambda r: reach[r] * abs(house - values[r]),
                reverse=True,
            )
            above = max(values, key=lambda r: direction * (values[r] - house))
            action = "on" if effort > 0.5 else "low"
            notes.append(
                f"{device.name or entity_id} {action} (effort {effort:.2f}): "
                + (
                    f"{'lifts' if mode == 'heat' else 'cools'} {', '.join(room_names[r] for r in helped)} toward the average; "
                    if helped and effort > 0.5
                    else f"limits {'heat' if mode == 'heat' else 'cooling'} into {room_names[above]}; "
                )
                + f"{room_names[above]} is {abs(values[above] - house):.2f} °C {'above' if mode == 'heat' else 'below'} the average."
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
        offset = target - state.setpoint
        state.offsets[entity_id] = offset
        if device.mode != mode:
            notes.append(f"{entity_id}: mode differs; re-asserting {mode}.")
            commands.append((entity_id, "set_hvac_mode", {"hvac_mode": mode}))
        if mode != "off":
            preset = system.get("preset")
            if preset and device.preset != preset:
                commands.append((entity_id, "set_preset_mode", {"preset_mode": preset}))
            if device.temperature != target:
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

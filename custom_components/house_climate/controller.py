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


@dataclass
class ControlState:
    setpoint: float = 21
    mode: str = "off"
    active_mode: str = "off"
    last_switch: float = 0
    integrals: dict[str, float] = field(default_factory=dict)
    offsets: dict[str, float] = field(default_factory=dict)
    last_update: float | None = None


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


def control(
    config, state, readings, devices, now, outdoor=None, enabled=True, learning=None
):
    """Update PI state and return changed commands. Time and ages are seconds.

    Positive offset means a higher physical target in both heat and cool modes.
    Conditional integration prevents windup at the clamp or slew limit. Immediate
    user requests do not buy additional slew allowance between timed intervals.
    """
    members = config["sensors"]
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
        return Decision(
            house,
            spread,
            values,
            [],
            " ".join(["Control disabled; observing only.", *notes]),
        )
    if mode != state.active_mode:
        state.active_mode, state.last_switch = mode, now
    commands = []
    for system in config["systems"]:
        entity_id = system["entity_id"]
        device = devices.get(entity_id, Device(available=False))
        if not device.available:
            notes.append(f"{entity_id} unavailable; skipped.")
            continue
        influence = (
            learning.influences(entity_id, values.keys() & learning.rows.keys())
            if learning
            else {}
        )
        affected = [
            {**m, "weight": m.get("weight", 1) * influence.get(m["entity_id"], 0)}
            for m in members
        ]
        local = mean(values, affected) if influence else house
        if local is None:
            local = house
            affected = members
        offset = state.offsets.get(entity_id, 0)
        if house is None or local is None:
            offset = 0
            state.integrals[entity_id] = 0
            notes.append(f"{entity_id}: no usable room sensors; using house setpoint.")
        elif mode in ("heat", "cool"):
            error = state.setpoint - local
            drivers = affected if influence else members
            contributions = [
                (
                    m["entity_id"],
                    m.get("weight", 1) * (state.setpoint - values[m["entity_id"]]),
                )
                for m in drivers
                if m["entity_id"] in values and m.get("weight", 1) > 0
            ]
            contributions.sort(key=lambda item: abs(item[1]), reverse=True)
            notes.append(
                f"{entity_id}: error {error:+.2f} °C driven by "
                + ", ".join(s for s, _ in contributions)
                + "."
            )
            integral = state.integrals.get(entity_id, 0)
            candidate = integral + config.get("ki", 0.1) * error * dt
            raw = config.get("kp", 1) * error + candidate
            dither = (
                learning.dither(entity_id, now, values.keys() & learning.rows.keys())
                if learning
                else 0
            )
            if dither:
                notes.append(f"{entity_id}: exploring ({dither:+.2f} °C dither).")
            raw += dither
            limit = config.get("offset_clamp", 3)
            slew = config.get("max_offset_change", 0.5) * dt
            bounded = clamp(clamp(raw, -limit, limit), offset - slew, offset + slew)
            # Do not integrate farther into either saturation boundary.
            device_limited = (
                state.setpoint
                + bounded
                - clamp(state.setpoint + bounded, device.minimum, device.maximum)
            )
            if (raw - bounded) * error <= 0 and device_limited * error <= 0:
                state.integrals[entity_id] = candidate
            offset = clamp(floor(bounded * 2 + 0.5) / 2, -limit, limit)
        state.offsets[entity_id] = offset
        target = clamp(
            floor((state.setpoint + offset) * 2 + 0.5) / 2,
            device.minimum,
            device.maximum,
        )
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

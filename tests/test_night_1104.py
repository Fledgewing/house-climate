"""Recorder regression; no room names or coefficients enter production code."""

from pathlib import Path

import pytest

from custom_components.house_climate.controller import (
    ControlState,
    Device,
    Reading,
    control,
)
from custom_components.house_climate.filtering import SensorFilter
from custom_components.house_climate.learning import Learning

ROOMS = ["liv", "down", "guest", "parent", "annabelle", "wesley_gabriel"]
NAMES = ["zone", *ROOMS]
DATA = [
    [float(x) for x in line.split()]
    for line in Path(__file__)
    .with_name("data")
    .joinpath("night-1104.txt")
    .read_text()
    .splitlines()
    if line and not line.startswith("#")
]


def solve(x, y):
    """Independent ordinary least squares, three columns, no RLS or priors."""
    a = [
        [sum(row[i] * row[j] for row in x) for j in range(3)]
        + [sum(row[i] * target for row, target in zip(x, y))]
        for i in range(3)
    ]
    for i in range(3):
        pivot = max(range(i, 3), key=lambda k: abs(a[k][i]))
        a[i], a[pivot] = a[pivot], a[i]
        scale = a[i][i]
        a[i] = [v / scale for v in a[i]]
        for k in range(3):
            if k != i:
                scale = a[k][i]
                a[k] = [v - scale * b for v, b in zip(a[k], a[i])]
    return [row[-1] for row in a]


def independent():
    result = {}
    for j, room in enumerate(NAMES):
        x, y = [], []
        for i in range(len(DATA) - 12):
            window = DATA[i + 1 : i + 13]
            x.append(
                [
                    sum(a[12] / 10 for a in window) / 12,
                    sum(a[15] / 10 for a in window) / 12,
                    sum((a[7] - a[j]) / 10 for a in window) / 12,
                ]
            )
            y.append((DATA[i + 12][j] - DATA[i][j]) / 10 / 0.8)
        result[room] = solve(x, y)[0]
    return result


def replay(small=False, window=48):
    references = independent()
    model = Learning(
        NAMES,
        ["B", "M"],
        interval_minutes=4,
        window_minutes=window,
        sources=["soleia", "papa_power", "server", "papa", "mummy"],
    )
    if small:
        saved = model.to_dict()
        saved.pop("integrated_version")
        for row in saved["rows"].values():
            row["theta"][1] = 0.004
            row["counts"][0] = 100
            row["covariance"] = [
                [0.001 if i == j else 0 for j in range(len(row["theta"]))]
                for i in range(len(row["theta"]))
            ]
        model = Learning.from_dict(saved)
    previous = {}
    filters = {room: SensorFilter() for room in NAMES}
    for i, row in enumerate(DATA):
        values = {}
        for j, room in enumerate(NAMES):
            f = filters[room]
            f.observe(row[j] / 10, i * 240)
            filtered = f.value(i * 240)
            if not f.blocked and not f.contaminated:
                values[room] = row[j] / 10 if window else filtered
        if i:
            model.update(
                previous,
                values,
                DATA[i - 1][7] / 10,
                {"B": row[12] / 10, "M": row[15] / 10},
                now=i * 240,
                solar=0,
                sources={
                    "soleia": row[8] / 100,
                    "papa_power": row[9] / 100,
                    "server": row[10] / 100,
                    "papa": int(row[11]) // 10,
                    "mummy": int(row[11]) % 10,
                },
            )
        previous = values
        for j, room in enumerate(ROOMS, 1):
            effect = model.blended(room, "B", 11, row[j] / 10) * 15
            if window and abs(effect - references[room]) > 0.35 * references[room]:
                assert model.confidence(room, "B") < 0.2
        for f in filters.values():
            f.finish_interval()
    return model


@pytest.mark.parametrize("small", [False, True])
def test_night_identifies_heat_from_fresh_and_confidently_small_prior(small):
    estimates = independent()
    baseline = replay(small, window=0)
    model = replay(small)
    for j, room in enumerate(ROOMS, 1):
        effect = model.blended(room, "B", 11, DATA[-1][j] / 10) * 15
        print(
            room,
            "independent",
            estimates[room],
            "integrated",
            effect,
            "before",
            baseline.blended(room, "B", 11, DATA[-1][j] / 10) * 15,
            "confidence",
            model.confidence(room, "B"),
        )
        assert effect > 0
        assert effect == pytest.approx(estimates[room], rel=0.35)
        assert model.confidence(room, "B") < 0.2
    assert not model.disturbed
    for room in ("down", "guest"):
        assert baseline.confidence(room, "B") > 0.7
        assert baseline.blended(room, "B", 11, 21) * 15 < estimates[room] / 2


def test_night_one_pass_report_lag_obeys_command_and_report_limits():
    estimates = independent()
    model = replay()
    config = {
        "interval": 4,
        "systems": [{"entity_id": "B"}],
        "sensors": [{"entity_id": room} for room in ROOMS],
        "explore_threshold": 0,
        "excite_after_minutes": 1e9,
    }
    state = ControlState(mode="heat", active_mode="heat", setpoint=22)
    device = Device(mode="heat", hvac_action="idle", current_temperature=23.5)
    state.observe_action("B", "idle", 0)
    on, since = False, 0
    starts, runs, offs = [], [], []
    errors, spreads, baseline_errors, baseline_spreads = [], [], [], []
    correction = dict.fromkeys(ROOMS, 0.0)
    for i in range(37, len(DATA)):
        row = DATA[i]
        now = i * 240
        values = {
            room: row[j] / 10 + correction[room] for j, room in enumerate(ROOMS, 1)
        }
        device.current_temperature = row[0] / 10
        # The report seen by this decision describes the preceding command.
        device.hvac_action = "heating" if on else "idle"
        decision = control(
            config,
            state,
            {k: Reading(v) for k, v in values.items()},
            {"B": device},
            now,
            outdoor=row[7] / 10,
            learning=model,
            solar=0,
            sources={
                "soleia": row[8] / 100,
                "papa_power": row[9] / 100,
                "server": row[10] / 100,
                "papa": int(row[11]) // 10,
                "mummy": int(row[11]) % 10,
            },
        )
        for _, service, data in decision.commands:
            if service == "set_temperature":
                device.temperature = data["temperature"]
            elif service == "set_hvac_mode":
                device.mode = data["hvac_mode"]
        next_on = (
            device.mode == "heat" and device.temperature > device.current_temperature
        )
        if next_on != on:
            (runs if on else offs).append((now - since) / 60)
            if next_on:
                starts.append(now)
            since, on = now, next_on
        for room in ROOMS:
            correction[room] = (
                0.995 * correction[room] + estimates[room] * (on - row[12] / 10) / 15
            )
        errors.append(abs(sum(values.values()) / len(ROOMS) - 22))
        spreads.append(max(values.values()) - min(values.values()))
        raw = [row[j] / 10 for j in range(1, 7)]
        baseline_errors.append(abs(sum(raw) / 6 - 22))
        baseline_spreads.append(max(raw) - min(raw))
    print(
        "runs",
        runs,
        "offs",
        offs,
        "starts",
        len(starts),
        "comfort",
        sum(errors) / len(errors),
        sum(baseline_errors) / len(errors),
        sum(spreads) / len(spreads),
        sum(baseline_spreads) / len(spreads),
    )
    assert min(runs) >= 10
    assert min(offs[1:]) >= 5
    assert max(sum(t - 3600 < s <= t for s in starts) for t in starts) <= 3
    assert sum(errors) / len(errors) <= sum(baseline_errors) / len(errors) + 0.2
    assert sum(spreads) / len(spreads) <= sum(baseline_spreads) / len(spreads) + 0.2


def test_integrated_window_discards_missing_rows_and_restart_gaps():
    model = Learning(["room"], ["unit"], interval_minutes=4, window_minutes=48)
    for i in range(11):
        model.update({"room": 20}, {"room": 20.01}, 10, {"unit": 1})
    assert model.rows["room"].samples == 0
    model.update({}, {}, 10, {"unit": 1})
    assert not model.windows
    for i in range(11):
        model.update({"room": 20}, {"room": 20.01}, 10, {"unit": 1})
    assert model.rows["room"].samples == 0
    restored = Learning.from_dict(model.to_dict())
    assert restored.window_minutes == 48
    assert not restored.windows
    assert restored.rows["room"] == model.rows["room"]


def test_integrated_fit_keeps_delayed_system_and_measured_source_effects():
    from random import Random

    rng = Random(1104)
    model = Learning(
        ["room"],
        ["unit"],
        sources=["source"],
        window_minutes=48,
        disturbance_sigma=0,
        regime_hours=1e9,
    )
    history = [0.0] * 8
    for step in range(12000):
        effort, source = float(rng.random() < 0.5), rng.random()
        temperature, outdoor = rng.uniform(18, 23), rng.uniform(5, 15)
        response = 0.01 * (outdoor - temperature) + 0.025 * history[-8] + 0.04 * source
        model.update(
            {"room": temperature},
            {"room": temperature + response},
            outdoor,
            {"unit": effort},
            sources={"source": source},
        )
        history.append(effort)
    assert model.blended("room", "unit", 10, 20) * 12 == pytest.approx(0.3, abs=0.06)
    assert model.source_effect("room", "source", 10, 20) == pytest.approx(
        0.04, abs=0.008
    )

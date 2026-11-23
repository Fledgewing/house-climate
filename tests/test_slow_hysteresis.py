"""Slow reporter hysteresis and command margin (v0.5.24).

Live 2026-11-22 18:09–21:54 PT on v0.5.23: the cloud-read Bryant was commanded
clearly on, but its command label turned ambiguous about 30 min after each
report, so no 70-min learning window completed. Two causes: the hysteresis
came from thermostat points learned from transitions seen up to 2 h late
(inflated, ≈ 1.5 °C), and the house-mean correction was counted twice (in
`own_estimate` and again as the full margin in `command_effort`).
"""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import CoreState
from homeassistant.util.unit_system import METRIC_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.house_climate.const import DEFAULTS, DOMAIN
from custom_components.house_climate.controller import (
    ControlState,
    Device,
    thermostat_effort,
    thermostat_hysteresis,
)
from custom_components.house_climate.learning import command_effort

CLOUD = "climate.cloud"
FETCH = "sensor.cloud_fetch"
INFLATED = [1.5, 1.6, 1.4]
# 17:55 PT on 11-22 (PST, UTC−8).
BASE = datetime(2026, 11, 23, 1, 55, tzinfo=UTC)


def pt(hour, minute, second=0):
    """A wall-clock PT time of the evening as an aware UTC datetime."""
    return BASE + timedelta(hours=hour - 17, minutes=minute - 55, seconds=second)


def config():
    return {
        "systems": [
            {"entity_id": CLOUD, "report_trust": "slow"},
            {"entity_id": "climate.split", "report_trust": "fresh"},
        ]
    }


def report(action, target=24.5, own=21.5, mode="heat"):
    return Device(
        mode=mode, hvac_action=action, temperature=target, current_temperature=own
    )


# --- clean transitions -------------------------------------------------------


def test_clean_transition_records_a_fresh_point():
    state = ControlState()
    state.observe_fresh_report(CLOUD, report("idle", own=22.0), 1000, True, 300)
    state.observe_fresh_report(CLOUD, report("heating", own=23.9), 1240, True, 300)
    assert state.fresh_thermostat_points[CLOUD] == [pytest.approx(0.6)]
    # The legacy list is never touched by this path.
    assert state.thermostat_points == {}


@pytest.mark.parametrize(
    ("idle", "heating"),
    [
        # Two hours apart: the transition is somewhere in between (live 20:07).
        ((1000, True), (8200, True)),
        # Either side stale.
        ((1000, False), (1240, True)),
        ((1000, True), (1240, False)),
        # Same report (an echo of one fetch).
        ((1000, True), (1000, True)),
        # Unknown report time.
        ((None, True), (1240, True)),
    ],
)
def test_unclean_transitions_record_nothing(idle, heating):
    state = ControlState()
    state.observe_fresh_report(CLOUD, report("idle", own=22.0), *idle, 300)
    state.observe_fresh_report(CLOUD, report("heating", own=23.9), *heating, 300)
    assert not state.fresh_thermostat_points.get(CLOUD)


def test_command_echo_is_never_the_idle_side():
    """An echo republishes the old fetch with the new target: not a report of it."""
    state = ControlState()
    state.observe_fresh_report(CLOUD, report("idle", target=20), 1000, True, 300)
    state.observe_fresh_report(CLOUD, report("idle", target=24.5), 1000, True, 300)
    state.observe_fresh_report(CLOUD, report("heating", own=21.6), 1180, True, 300)
    assert not state.fresh_thermostat_points.get(CLOUD)
    # A real idle report at that target, then heating, counts.
    state.observe_fresh_report(CLOUD, report("idle", own=24.4), 2000, True, 300)
    state.observe_fresh_report(CLOUD, report("heating", own=24.0), 2200, True, 300)
    assert state.fresh_thermostat_points[CLOUD] == [pytest.approx(0.5)]


def test_slow_hysteresis_uses_only_enough_fresh_points():
    state = ControlState(thermostat_points={CLOUD: list(INFLATED)})
    assert thermostat_hysteresis(config(), state, CLOUD) == (
        pytest.approx(1.5),
        "points",
    )
    state.slow_reporters.add(CLOUD)
    assert thermostat_hysteresis(config(), state, CLOUD) == (0.5, "default")
    state.fresh_thermostat_points[CLOUD] = [0.6, 0.7, 0.8]
    assert thermostat_hysteresis(config(), state, CLOUD) == (0.5, "default")
    state.fresh_thermostat_points[CLOUD].append(0.9)
    assert thermostat_hysteresis(config(), state, CLOUD) == (
        pytest.approx(0.75),
        "fresh_points",
    )


def test_control_latch_for_a_slow_unit_ignores_inflated_points():
    state = ControlState(thermostat_points={CLOUD: list(INFLATED)})
    thermostat = {"mode": "heat", "target": 24.5, "own": 23.8, "running": False}
    # Not slow: today's behaviour (gap 0.7 < learned 1.5, stays off).
    assert thermostat_effort(config(), state, CLOUD, dict(thermostat)) == 0
    state.slow_reporters.add(CLOUD)
    assert thermostat_effort(config(), state, CLOUD, dict(thermostat)) == 1


def test_live_1839_numbers_old_and_new_classification():
    """18:39 PT live: est 21.83 (correction 0.33), target 24.5, gap 2.67."""
    old = command_effort(
        "heat",
        24.5,
        21.83,
        0.33,
        hysteresis=sum(INFLATED) / 3,
        margin_factor=1,
        margin_cap=float("inf"),
    )
    assert old[0] == "ambiguous"
    assert command_effort("heat", 24.5, 21.83, 0.33, hysteresis=0.5)[0] == "on"


def test_live_1914_warming_with_the_command_adds_no_margin():
    """19:14 PT live: est 22.53 (correction 1.03 since 18:07), gap 1.97."""
    assert command_effort("heat", 24.5, 22.53, 1.03, drift=1.03)[0] == "on"
    # Unsigned (or against the command) it would need 2.0.
    assert command_effort("heat", 24.5, 22.53, 1.03)[0] == "ambiguous"
    assert command_effort("heat", 24.5, 22.53, 1.03, drift=-1.03)[0] == "ambiguous"


# --- the live evening, end to end --------------------------------------------


@pytest.fixture
def settings():
    data = deepcopy(DEFAULTS)
    data["learning_window_minutes"] = 60
    data["outdoor_sensor"] = "sensor.outdoor"
    data["sensors"] = [{"entity_id": "sensor.room", "weight": 1, "zone": ""}]
    data["systems"] = [
        {
            "entity_id": CLOUD,
            "preset": "",
            "report_trust": "slow",
            "report_timestamp_sensor": FETCH,
        },
        {"entity_id": "climate.split", "preset": "", "report_trust": "fresh"},
    ]
    return data


def at(when):
    return patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=when,
    )


async def start(hass, settings):
    hass.set_state(CoreState.running)
    hass.config.units = METRIC_SYSTEM
    with at(pt(17, 55)):
        hass.states.async_set("sensor.room", "21.5", {"unit_of_measurement": "°C"})
        hass.states.async_set("sensor.outdoor", "5")
        hass.states.async_set(
            CLOUD,
            "heat",
            {"hvac_action": "idle", "temperature": 20, "current_temperature": 21.4},
        )
        hass.states.async_set("climate.split", "off", {"hvac_action": "off"})
        hass.states.async_set(FETCH, pt(17, 55).isoformat())
        entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data=settings)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    c = entry.runtime_data
    assert not c.enabled  # disagreeing modes: observing only, never commands
    return entry, c


async def device(c, when, fetched=False, **changes):
    """A cloud fetch (`fetched`) or a command echo (same fetch time)."""
    with at(when):
        if fetched:
            c.hass.states.async_set(FETCH, when.isoformat())
        state = c.hass.states.get(CLOUD)
        c.hass.states.async_set(
            CLOUD, changes.pop("mode", state.state), {**state.attributes, **changes}
        )
        await c.hass.async_block_till_done()


def send(c, when, mode, target):
    c.command_settings[CLOUD] = {
        "mode": mode,
        "target": target,
        "sent_at": when.timestamp(),
    }


# The live warming since the 18:07 report: est 21.83 / 22.17 / 22.53 at
# 18:39 / 18:59 / 19:14 (+1.03 °C in 67 min, ≈ 0.077 °C per 5 min), then on.
WARMING = [(pt(18, 9, 10), 0.0), (pt(18, 39, 10), 0.33), (pt(18, 59, 10), 0.67)]
WARMING.append((pt(19, 14, 10), 1.03))
RATE = 0.077 / 300  # °C per second after 19:14


def room(when):
    """House mean: flat at 21.5, then the live warming until 21:15."""
    when = min(when, pt(21, 15))
    if when <= WARMING[0][0]:
        return 21.5
    for (a, x), (b, y) in zip(WARMING, WARMING[1:]):
        if when <= b:
            return 21.5 + x + (y - x) * (when - a) / (b - a)
    return 21.5 + WARMING[-1][1] + RATE * (when - WARMING[-1][0]).total_seconds()


def live_events(c, order="timestamp_first"):
    """The live evening's reports and commands (fetches in `order`)."""
    return {
        pt(18, 7): lambda t: fetch(c, t, order, current_temperature=21.5),
        pt(18, 9): lambda t: send(c, t, "heat", 24.5) or device(c, t, temperature=24.5),
        # The thermostat heats with its target only 0.5 above its reading.
        pt(20, 7): lambda t: fetch(
            c, t, order, hvac_action="heating", current_temperature=24.0
        ),
        pt(20, 9): lambda t: send(c, t, "heat", 27) or device(c, t, temperature=27),
        pt(21, 15): lambda t: (
            send(c, t, "off", 27) or device(c, t, True, mode="off", hvac_action="off")
        ),
    }


def first_events(c):
    """The live evening up to the 18:09 command only (no later fetch)."""
    events = live_events(c)
    return {t: events[t] for t in (pt(18, 7), pt(18, 9))}


async def evening(hass, settings, events=live_events, house=room, extra=None):
    """Replay 17:55–21:44 PT; return per-tick (time, info, effort)."""
    entry, c = await start(hass, settings)
    c.state.thermostat_points[CLOUD] = list(INFLATED)
    events = {**events(c), **(extra(c) if extra else {})}
    ticks = []
    windows = False
    when = pt(17, 59, 10)
    while when <= pt(21, 44, 10):
        for event_time in sorted(events):
            if when - timedelta(minutes=5) < event_time <= when:
                await events[event_time](event_time)
        with at(when):
            hass.states.async_set(
                "sensor.room", f"{house(when):.2f}", {"unit_of_measurement": "°C"}
            )
            await hass.async_block_till_done()
            await c.async_tick()
        info = deepcopy(c.action_sources[CLOUD])
        info["windows_accepted"] = c.windows_accepted()
        ticks.append((when, info, c.samples[-1]["efforts"][CLOUD]))
        windows = windows or c.windows_accepted()
        when += timedelta(minutes=5)
    return c, ticks, windows


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_live_evening_stays_on_and_completes_a_window(hass, settings):
    c, ticks, windows = await evening(hass, settings)
    for when, info, effort in ticks:
        command = info.get("command", {})
        if pt(18, 14) <= when < pt(21, 15):
            # v0.5.23: ambiguous from 18:44 (and 21:04) PT, learning skipped.
            assert (command.get("class"), command.get("transition")) == (
                "on",
                "on",
            ), when
            assert info["source"] == "command", when
            # 18:14 holds the 2-min start delay (18:09–18:11) of its interval.
            assert effort == (1 if when >= pt(18, 19) else pytest.approx(190 / 300))
        elif pt(21, 19) <= when:
            assert command["class"] == "off", when
            assert command["transition"] == "off", when
            if when >= pt(21, 24):
                assert effort == 0, when
    assert windows, "no 70-min learning window completed"
    estimates = {when: info["command"].get("own_est") for when, info, _ in ticks}
    assert estimates[pt(18, 39, 10)] == pytest.approx(21.83, abs=0.01)
    assert estimates[pt(18, 59, 10)] == pytest.approx(22.17, abs=0.01)
    assert estimates[pt(19, 14, 10)] == pytest.approx(22.53, abs=0.01)
    # 19:49–20:04 stay on only through the hold, while the house warms.
    held = [when for when, info, _ in ticks if info.get("command", {}).get("held")]
    assert pt(19, 49, 10) in held and pt(20, 4, 10) in held
    assert all(pt(19, 49) <= when < pt(20, 7) for when in held)
    on = next(info for when, info, _ in ticks if when == pt(21, 4, 10))
    assert on["command"]["hysteresis"] == 0.5
    assert on["command"]["hysteresis_source"] == "default"
    # 20:07 was seen two hours after the last idle report: the legacy list
    # still learns its 0.5, the clean list does not.
    assert c.state.thermostat_points[CLOUD] == [*INFLATED, 0.5]
    assert not c.state.fresh_thermostat_points.get(CLOUD)


# --- storage, both directions ------------------------------------------------


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_fresh_points_have_their_own_store_key(hass, settings):
    entry, c = await start(hass, settings)
    c.state.fresh_thermostat_points[CLOUD] = [0.6, 0.7]
    with at(pt(18, 0)):
        await c.async_tick()
    saved = await c.store.async_load()
    # v0.5.23 builds ControlState(**saved["state"]): no new keys in it.
    assert set(saved["state"]) == set(ControlState.__dataclass_fields__)
    assert not {"fresh_thermostat_points", "fresh_reports", "slow_reporters"} & set(
        saved["state"]
    )
    assert saved["fresh_thermostat_points"] == {CLOUD: [0.6, 0.7]}
    assert await hass.config_entries.async_unload(entry.entry_id)
    with at(pt(18, 1)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.runtime_data.state.fresh_thermostat_points == {CLOUD: [0.6, 0.7]}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_v0523_store_loads_without_fresh_points(hass, settings):
    entry, c = await start(hass, settings)
    c.state.thermostat_points[CLOUD] = list(INFLATED)
    with at(pt(18, 0)):
        await c.async_tick()
    saved = await c.store.async_load()
    saved.pop("fresh_thermostat_points")
    await c.store.async_save(saved)
    assert await hass.config_entries.async_unload(entry.entry_id)
    with at(pt(18, 1)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    c = entry.runtime_data
    assert c.state.fresh_thermostat_points == {}
    assert c.state.thermostat_points[CLOUD] == INFLATED


# --- attributes are snapshots ------------------------------------------------


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_nested_attribute_change_is_written(hass, settings):
    """HA skips a write whose attributes equal the previous state's."""
    entry, c = await start(hass, settings)
    with at(pt(18, 0)):
        await c.async_tick()
    before = hass.states.get("climate.house")
    sources = before.attributes["action_sources"]
    assert sources is not c.action_sources
    assert sources[CLOUD] == c.action_sources[CLOUD]
    c.action_sources[CLOUD]["source"] = "changed in place"
    c.async_update_listeners()
    await hass.async_block_till_done()
    after = hass.states.get("climate.house")
    assert after.attributes["action_sources"][CLOUD]["source"] == "changed in place"
    assert before.attributes["action_sources"][CLOUD]["source"] != "changed in place"
    assert after.attributes != before.attributes
    # The learning sensor's attributes are snapshots too.
    entity = next(
        e
        for e in hass.data["entity_components"]["sensor"].entities
        if e.entity_id == "sensor.house_learning"
    )
    first = entity.extra_state_attributes
    second = entity.extra_state_attributes
    assert first.keys() == second.keys()
    assert all(first[k] is not second[k] for k in first if isinstance(first[k], dict))


# --- the hold ends with the run ----------------------------------------------


def satisfied_at(end):
    """The live warming, then flat from `end` (the thermostat is satisfied)."""
    return lambda when: room(min(when, end))


async def fetch(c, when, order, **changes):
    """A cloud fetch: timestamp sensor first, or the climate entity first."""
    with at(when):
        state = c.hass.states.get(CLOUD)
        attributes = {**state.attributes, **changes}
        if order == "timestamp_first":
            c.hass.states.async_set(FETCH, when.isoformat())
            await c.hass.async_block_till_done()
        c.hass.states.async_set(CLOUD, state.state, attributes)
        await c.hass.async_block_till_done()
        if order == "entity_first":
            c.hass.states.async_set(FETCH, when.isoformat())
            await c.hass.async_block_till_done()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_hold_ends_when_the_house_stops_warming(hass, settings):
    """Review repro: satisfied at 20:20, next fetch 21:30 idle at 24.5.

    The estimate (23.54) lags the thermostat's real reading. With the hold
    alone the label stayed on (effort 1) for 70 min and nothing contradicted
    it. Now the flat house ends the hold and the idle fetch labels off.
    """
    c, ticks, _ = await evening(
        hass,
        settings,
        events=first_events,
        house=satisfied_at(pt(20, 20)),
        extra=lambda c: {
            pt(21, 30): lambda t: fetch(
                c, t, "entity_first", hvac_action="idle", current_temperature=24.5
            )
        },
    )
    info = {when: (i["command"], effort) for when, i, effort in ticks}
    # Held (below the clear gap) while the house still warms.
    command, effort = info[pt(19, 54, 10)]
    assert command["class"] == "on" and command["held"] and effort == 1
    assert command["trend"] > 0.05
    # Flat from 20:20: the hold ends within one trend window.
    assert info[pt(20, 34, 10)][0]["class"] == "ambiguous"
    for when, (command, effort) in info.items():
        if pt(20, 39) <= when < pt(21, 34):
            assert command["class"] == "ambiguous", when
            assert effort is None, when
    command, _ = info[pt(21, 34, 10)]
    assert command["class"] == "off"
    assert command["reason"] == "fresh report idle"


@pytest.mark.parametrize("order", ["timestamp_first", "entity_first"])
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_fresh_idle_report_contradicts_a_held_label(hass, settings, order):
    """The cross-check uses the label in effect at the report time."""
    c, ticks, _ = await evening(
        hass,
        settings,
        events=first_events,
        extra=lambda c: {
            pt(19, 55): lambda t: fetch(
                c, t, order, hvac_action="idle", current_temperature=24.5
            )
        },
    )
    info = {when: i["command"] for when, i, _ in ticks}
    assert info[pt(19, 54, 10)]["held"]
    checks = c.reporting.checks.status(CLOUD)
    assert checks["checks"] == 1
    assert checks["rate"] == 1.0
    command = info[pt(19, 59, 10)]
    assert command["class"] == "off"
    assert command["reason"] == "fresh report idle"


@pytest.mark.parametrize(("offset", "trusted"), [(-60, False), (60, True)])
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_reports_before_the_command_settle_nothing(
    hass, settings, offset, trusted
):
    """A fresh report made before the command was sent says nothing about it."""
    entry, c = await start(hass, settings)
    sent = pt(18, 9)
    # Target 1.0 above the reading: an unclear (ambiguous) command.
    send(c, sent, "heat", 22.5)
    await device(c, sent, temperature=22.5)
    await fetch(
        c,
        sent + timedelta(seconds=offset),
        "timestamp_first",
        hvac_action="heating",
        current_temperature=21.5,
    )
    with at(sent + timedelta(seconds=180)):
        await c.async_tick()
    command = c.action_sources[CLOUD]["command"]
    if trusted:
        assert (command["class"], command["reason"]) == ("on", "fresh report running")
    else:
        assert command["class"] == "ambiguous"
        assert command.get("reason") != "fresh report running"


# --- a fetch whose content arrives before its timestamp ----------------------


@pytest.mark.parametrize("order", ["timestamp_first", "entity_first"])
@pytest.mark.parametrize("settle", [None, -1])
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_fetch_order_never_breaks_the_label(hass, settings, order, settle):
    """The 20:07 fetch in either update order: no ambiguous label, no reset.

    Entity first, the action-change tick saw the new reading (24.0) with the
    18:07 report's mark (+1.4 °C since): est 25.4, ambiguous, effort None, and
    the averaged windows restarted. `settle` -1 disables the v0.5.24 hold.
    """
    if settle is not None:
        settings["report_settle_minutes"] = settle
    labels = []
    c, ticks, _ = await evening(
        hass,
        settings,
        events=lambda c: {
            **live_events(c, order),
            # Every label change between ticks (incl. the action-change tick).
            pt(20, 7, 1): lambda t: (
                labels.extend(list(c.command_labels.history[CLOUD])) or asyncio.sleep(0)
            ),
        },
    )
    history = [
        state
        for state in c.command_labels.history[CLOUD] + labels
        if pt(18, 11).timestamp() <= state["since"] < pt(21, 15).timestamp()
    ]
    broken = any(state["label"] is None for state in history) or any(
        not info["windows_accepted"]
        for when, info, _ in ticks
        if pt(19, 30) <= when < pt(21, 15)
    )
    assert broken == (order == "entity_first" and settle == -1)
    if order == "entity_first" and settle is None:
        info = {when: i["command"] for when, i, _ in ticks}
        # The next tick re-keys the mark with the mean from 20:07 (the 18:07
        # mark would give about 25.5).
        assert info[pt(20, 9, 10)]["own_est"] == pytest.approx(24.0, abs=0.1)

"""Slow reporters and command-derived learning effort (v0.5.23).

Unit tests for the pure pieces (`command_effort`, `CommandTransitions`,
`own_estimate`, `ReportCadence`, `CommandChecks`, trusted `EffortTracker`
labels) and Home Assistant tests for the coordinator's source chain.
"""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import CoreState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.house_climate.const import DEFAULTS, DOMAIN
from custom_components.house_climate.learning import (
    CommandTransitions,
    EffortTracker,
    command_effort,
    own_estimate,
)
from custom_components.house_climate.reporting import (
    CommandChecks,
    ReportCadence,
    Reporting,
)

CLOUD = "climate.cloud"
FETCH = "sensor.cloud_fetch"


@pytest.mark.parametrize(
    ("args", "kwargs", "expected"),
    [
        (("off", 16, 20), {}, "off"),
        (("fan_only", 16, 20), {}, "off"),
        (("heat", 23, 20), {}, "on"),
        (("cool", 17, 20), {}, "on"),
        (("heat", 17, 20), {}, "off"),
        (("cool", 23, 20), {}, "off"),
        # Small gaps are left to the thermostat's own hysteresis: unknown.
        (("heat", 21, 20), {}, "ambiguous"),
        (("heat", 19.5, 20), {}, "ambiguous"),
        # A stale reading's correction widens the band.
        (("heat", 22, 20), {}, "on"),
        (("heat", 22, 20, 0.8), {}, "ambiguous"),
        (("heat", 18.5, 20, 0.8), {}, "ambiguous"),
        # A learned wide hysteresis needs a wider on gap.
        (("heat", 21.6, 20), {"hysteresis": 1.0}, "ambiguous"),
        # Unconfirmed (echo missing, pending, outside change) is never a label.
        (("heat", 25, 20), {"confirmed": False}, "ambiguous"),
        (("off", 25, 20), {"confirmed": False}, "ambiguous"),
        # heat_cool/auto: the controller's selected direction decides.
        (("heat_cool", 25, 20), {"active_mode": "heat"}, "on"),
        (("heat_cool", 25, 20), {"active_mode": "cool"}, "off"),
        (("auto", 15, 20), {"active_mode": "cool"}, "on"),
        (("heat_cool", 25, 20), {"active_mode": "off"}, "ambiguous"),
        (("heat", 25, None), {}, "ambiguous"),
    ],
)
def test_command_effort_table(args, kwargs, expected):
    assert command_effort(*args, **kwargs)[0] == expected


def test_command_effort_direction_and_gap():
    assert command_effort("heat", 23, 20) == ("on", 1, 3)
    assert command_effort("cool", 17, 20) == ("on", -1, 3)


def test_own_estimate_prefers_room_sensor_then_corrects_with_house_mean():
    assert own_estimate(20, 21, 21.5, sensor=19.7) == (19.7, 0.0, "own_sensor")
    own, correction, source = own_estimate(20, 21, 21.5)
    assert own == pytest.approx(20.5)
    assert correction == pytest.approx(0.5)
    assert source == "reported+house"
    assert own_estimate(20, None, 21.5) == (20, 0.0, "reported")
    assert own_estimate(None, 21, 21.5) == (None, None, None)


def test_transitions_start_delay_restart_guard_and_stop_tail():
    labels = CommandTransitions(120, 300, 180)
    labels.observe(CLOUD, "on", 1, 0)
    assert labels.value(CLOUD, 60) == 0.0
    assert labels.value(CLOUD, 120) == 1.0
    labels.observe(CLOUD, "off", 0, 1000)
    assert labels.value(CLOUD, 1100) == 0.5
    assert labels.value(CLOUD, 1180) == 0.0
    # Restart guard: a new call 60 s after the stop waits until stop + 300 s.
    labels.observe(CLOUD, "on", 1, 1060)
    assert labels.value(CLOUD, 1200) == 0.0
    assert labels.value(CLOUD, 1300) == 1.0
    assert labels.breakpoints(CLOUD, 0, 1400) == [120, 1000, 1060, 1180, 1300]
    # An on that never started leaves no tail and no restart guard.
    labels = CommandTransitions(120, 300, 180)
    labels.observe(CLOUD, "on", 1, 0)
    labels.observe(CLOUD, "off", 0, 60)
    assert labels.value(CLOUD, 61) == 0.0
    labels.observe(CLOUD, "on", -1, 100)
    assert labels.value(CLOUD, 220) == -1.0
    # Ambiguous is no effort; a cause in the past starts the delay from it.
    labels.observe(CLOUD, "ambiguous", 0, 400)
    assert labels.value(CLOUD, 500) is None
    labels.observe(CLOUD, "on", 1, 1000, cause=0)
    assert labels.value(CLOUD, 1000) == 1.0


def test_trusted_label_keeps_a_known_mode_switch():
    for trusted, expected in ((False, None), (True, 0.4)):
        tracker = EffortTracker()
        tracker.finish({CLOUD: 0.0}, 0, modes={CLOUD: "off"}, trusted={CLOUD})
        tracker.change(CLOUD, 0.0, 60, mode="heat", trusted=trusted)
        tracker.change(CLOUD, 1.0, 180, mode="heat", trusted=trusted)
        efforts, _ = tracker.finish(
            {CLOUD: 1.0},
            300,
            modes={CLOUD: "heat"},
            commanded_modes={CLOUD: "heat"},
            trusted={CLOUD} if trusted else (),
        )
        assert efforts[CLOUD] == (
            expected if expected is None else pytest.approx(expected)
        )


def test_report_cadence_median_bootstrap_and_forced():
    cadence = ReportCadence()
    for t in range(0, 6 * 60, 60):
        cadence.observe(CLOUD, t)
        # Repeats and older times (command echoes) add no gap.
        cadence.observe(CLOUD, t)
        cadence.observe(CLOUD, t - 30)
    slow, reason = cadence.status(CLOUD, "auto", 5)
    assert slow and reason["bootstrap"] and reason["gaps"] == 5
    cadence.observe(CLOUD, 360)
    assert cadence.status(CLOUD, "auto", 5)[0] is False
    assert cadence.status(CLOUD, "slow", 5) == (
        True,
        {"mode": "forced slow", "gaps": 6, "median_gap_s": 60},
    )
    for t in range(1, 10):
        cadence.observe(CLOUD, 360 + t * 7200)
    assert cadence.status(CLOUD, "auto", 5)[0] is True
    assert cadence.status(CLOUD, "fresh", 5)[0] is False
    assert len(cadence.systems[CLOUD]["gaps"]) == 15
    restored = Reporting(Reporting({"cadence": cadence.to_dict()}).to_dict())
    assert restored.cadence.status(CLOUD, "auto", 5) == cadence.status(CLOUD, "auto", 5)
    # Stored data from v0.5.22 loads with empty cadence and checks.
    legacy = Reporting({"sensors": {}, "intervals": []})
    assert legacy.cadence.systems == {} and legacy.checks.systems == {}


def test_command_checks_suspend_and_clear():
    checks = CommandChecks()
    for t in range(9):
        checks.check(CLOUD, t, True)
    assert not checks.suspended(CLOUD)  # too few checks yet
    checks.check(CLOUD, 9, True)
    checks.check(CLOUD, 9, False)  # the same report counts once
    assert checks.suspended(CLOUD)
    assert checks.status(CLOUD) == {"checks": 10, "rate": 1.0, "suspended": True}
    for t in range(10, 28):
        checks.check(CLOUD, t, False)
    assert checks.suspended(CLOUD)  # 2 of 20 is not below 10%
    checks.check(CLOUD, 28, False)
    assert not checks.suspended(CLOUD)
    assert CommandChecks(checks.to_dict()).status(CLOUD) == checks.status(CLOUD)


@pytest.fixture
def settings():
    data = deepcopy(DEFAULTS)
    data["learning_window_minutes"] = 0
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


async def setup(hass, settings, start, *, fetched=None, action="heating"):
    hass.set_state(CoreState.running)
    hass.config.units = METRIC_SYSTEM
    hass.states.async_set("sensor.room", "20", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.outdoor", "5")
    hass.states.async_set(
        CLOUD,
        "heat",
        {"hvac_action": action, "temperature": 16, "current_temperature": 20},
    )
    hass.states.async_set("climate.split", "off", {"hvac_action": "off"})
    hass.states.async_set(FETCH, (fetched or start - timedelta(hours=1)).isoformat())
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data=settings)
    entry.add_to_hass(hass)
    with at(start):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    c = entry.runtime_data
    assert not c.enabled  # disagreeing modes: observing only, no commands
    return entry, c


async def tick_at(c, when, room=None, fetched=None, **attributes):
    if room is not None:
        c.hass.states.async_set("sensor.room", room, {"unit_of_measurement": "°C"})
    with at(when):
        if fetched is not None:
            c.hass.states.async_set(FETCH, fetched.isoformat())
        if attributes:
            state = c.hass.states.get(CLOUD)
            c.hass.states.async_set(
                CLOUD,
                attributes.pop("mode", state.state),
                {**state.attributes, **attributes},
            )
        await c.hass.async_block_till_done()
        await c.async_tick()
    return c.samples[-1]["efforts"][CLOUD]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_clear_off_command_beats_stale_heating_report(hass, settings):
    start = dt_util.utcnow()
    entry, c = await setup(hass, settings, start)
    c.command_settings[CLOUD] = {
        "mode": "heat",
        "target": 16.0,
        "sent_at": (start - timedelta(hours=1)).timestamp(),
    }
    await tick_at(c, start)
    own_samples = c.learning.rows[CLOUD].samples
    room_samples = c.learning.rows["sensor.room"].samples
    # HEAD learned "heating" here from a fetch an hour old.
    assert await tick_at(c, start + timedelta(seconds=300), "20.1") == 0
    info = hass.states.get("climate.house").attributes["action_sources"][CLOUD]
    assert info["source"] == "command"
    assert info["slow"] is True
    assert info["slow_reason"]["mode"] == "forced slow"
    assert info["report_fresh"] is False
    assert info["report_age_s"] == 3900
    assert info["command"]["class"] == "off"
    assert info["command"]["transition"] == "off"
    assert info["command"]["own_source"] == "reported+house"
    assert info["command"]["gap"] == pytest.approx(-4.1)
    assert info["coverage_24h"]["command"] == 1.0
    assert c.learning.rows["sensor.room"].samples == room_samples + 1
    # The slow unit's own (stale) row is kept but not learned from.
    assert c.learning.rows[CLOUD].samples == own_samples
    assert CLOUD in c.learning.rows
    # Control does not see the stale action either.
    assert c.state.actions.get(CLOUD) is None


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_fresh_report_then_skip_without_a_command(hass, settings):
    start = dt_util.utcnow()
    entry, c = await setup(hass, settings, start, fetched=start)
    await tick_at(c, start)
    assert await tick_at(c, start + timedelta(seconds=240), "20.1") == 1
    assert c.action_sources[CLOUD]["source"] == "report"
    assert c.action_sources[CLOUD]["command"]["reason"] == "no command"
    # The same fetch is stale 9 minutes later: no evidence at all.
    assert await tick_at(c, start + timedelta(seconds=540), "20.2") is None
    assert c.action_sources[CLOUD]["source"] is None
    assert c.action_sources[CLOUD]["coverage_24h"]["skipped"] > 0
    # A new fetch is fact again.
    later = start + timedelta(seconds=840)
    assert await tick_at(c, later, "20.3", fetched=later, hvac_action="idle") is None
    assert await tick_at(c, later + timedelta(seconds=120), "20.3") == 0


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_mode_switch_under_command_keeps_learning_with_start_delay(
    hass, settings
):
    start = dt_util.utcnow()
    entry, c = await setup(hass, settings, start)
    c.command_settings[CLOUD] = {
        "mode": "off",
        "target": 16.0,
        "sent_at": (start - timedelta(hours=1)).timestamp(),
    }
    await tick_at(c, start, mode="off", hvac_action="off")
    assert await tick_at(c, start + timedelta(seconds=300), "20.1") == 0
    assert c.command_labelled(CLOUD)
    # The house switches the unit to heat at a clearly-on target. The echo
    # carries a stale "idle"; the label starts after the 2-min start delay.
    switch = start + timedelta(seconds=360)
    c.command_settings[CLOUD] = {
        "mode": "heat",
        "target": 24.0,
        "sent_at": switch.timestamp(),
    }
    with at(switch):
        hass.states.async_set(
            CLOUD,
            "heat",
            {"hvac_action": "idle", "temperature": 24, "current_temperature": 20},
        )
        await hass.async_block_till_done()
    effort = await tick_at(c, start + timedelta(seconds=600), "20.2")
    # 60 s off, 120 s start delay, 120 s on of 300 s; HEAD withheld it (None).
    assert effort == pytest.approx(0.4)
    assert c.action_sources[CLOUD]["command"]["transition"] == "on"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_contradicted_commands_raise_and_clear_a_repair_issue(hass, settings):
    start = dt_util.utcnow()
    entry, c = await setup(hass, settings, start, action="idle")
    c.command_settings[CLOUD] = {
        "mode": "heat",
        "target": 24.0,
        "sent_at": (start - timedelta(hours=1)).timestamp(),
    }
    hass.states.async_set(
        CLOUD,
        "heat",
        {"hvac_action": "idle", "temperature": 24, "current_temperature": 20},
    )
    issue_id = f"{entry.entry_id}_{CLOUD}_command_contradicted"
    when = start
    # A unit that never runs while clearly commanded on (a lockout). The label
    # starts with this test's command (2-min delay), so the first fetch is not
    # yet a settled cross-check.
    for _ in range(11):
        when += timedelta(seconds=300)
        await tick_at(c, when, fetched=when)
    assert c.reporting.checks.suspended(CLOUD)
    info = c.action_sources[CLOUD]
    assert info["problem"] == "command_contradicted"
    assert info["contradictions"]["suspended"] is True
    # Suspended: the fresh report decides instead, and is still learned from.
    assert info["source"] == "report"
    when += timedelta(seconds=300)
    assert await tick_at(c, when, fetched=when) == 0
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue.translation_key == "command_contradicted"
    # The unit runs again: agreeing reports clear the suspension and issue.
    for _ in range(20):
        when += timedelta(seconds=300)
        await tick_at(c, when, fetched=when, hvac_action="heating")
    assert not c.reporting.checks.suspended(CLOUD)
    assert c.action_sources[CLOUD]["source"] == "command"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_hand_control_still_forces_zero(hass, settings):
    start = dt_util.utcnow()
    entry, c = await setup(hass, settings, start)
    c.system_control[CLOUD] = False
    await tick_at(c, start)
    assert await tick_at(c, start + timedelta(seconds=300), "20.1") == 0


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_command_state_persists_and_options_keep_learning(hass, settings):
    settings["learning_window_minutes"] = 60
    start = dt_util.utcnow()
    entry, c = await setup(hass, settings, start, fetched=start)
    c.command_settings[CLOUD] = {"mode": "heat", "target": 16.0, "sent_at": 1.0}
    await tick_at(c, start)
    saved = await c.store.async_load()
    assert saved["command_settings"][CLOUD]["target"] == 16.0
    assert saved["report_marks"][CLOUD]["reported"] == start.timestamp()
    assert "cadence" in saved["reporting"] and "checks" in saved["reporting"]
    c.learning.window_minutes = 0
    for step in range(3):
        c.learning.update(
            {"sensor.room": 19 + step / 10},
            {"sensor.room": 19.1 + step / 10},
            5,
            {CLOUD: 1, "climate.split": 0},
        )
    c.learning.window_minutes = 60
    with at(start + timedelta(seconds=60)):
        await c.async_tick(record=False)  # persist the fitted rows
    learned = c.learning.to_dict()

    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(
        result["flow_id"],
        {"sensors": ["sensor.room"], "systems": [CLOUD, "climate.split"]},
    )
    result = await flow.async_configure(result["flow_id"], {"weight": 1})
    assert result["step_id"] == "system"
    fields = {field.schema for field in result["data_schema"].schema}
    assert {
        "report_trust",
        "report_timestamp_sensor",
        "report_fresh_minutes",
        "command_effort",
    } <= fields
    result = await flow.async_configure(
        result["flow_id"],
        {"report_trust": "auto", "report_fresh_minutes": 10, "command_effort": False},
    )
    result = await flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "tunables"
    inputs = {
        k: v
        for k, v in entry.options.items()
        if k
        not in (
            "sensors",
            "systems",
            "extra_heat_sources",
            "priority_modes",
            "outdoor_sensor",
            "weather_entity",
            "solar_sensor",
            "occupancy_sensor",
        )
        and v not in ("", None)
    }
    inputs["outdoor_sensor"] = "sensor.outdoor"
    result = await flow.async_configure(result["flow_id"], inputs)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    system = entry.options["systems"][0]
    assert system["report_trust"] == "auto"
    assert system["report_fresh_minutes"] == 10
    assert system["command_effort"] is False
    # An empty timestamp sensor clears it; the split keeps its settings.
    assert "report_timestamp_sensor" not in system
    assert entry.options["systems"][1]["report_trust"] == "fresh"
    c = entry.runtime_data
    learned_after = c.learning.to_dict()
    assert learned_after["rows"] == learned["rows"]
    assert c.command_settings[CLOUD]["target"] == 16.0

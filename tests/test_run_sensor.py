"""Local run sensor as the truth for a unit's learning action (v0.5.22).

The 11/21 evidence: a cloud-read Bryant reported `hvac_action: heating` for 76+
minutes while its own snapshot said the indoor unit was off, and `idle` while
rooms warmed. Fed to learning, heating looked like it cooled the house.
"""

from copy import deepcopy
from datetime import timedelta
from math import pi, sin
from unittest.mock import patch

import pytest
from homeassistant.core import CoreState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.house_climate.const import DEFAULTS, DOMAIN
from custom_components.house_climate.learning import (
    EffortTracker,
    Learning,
    run_action,
)

UNIT = "climate.home_zone_1"
RATES = {"downstairs": 1.16, "guest": 1.47, "parents": 1.06, "office": 0.7}


@pytest.mark.parametrize(
    ("running", "mode", "active", "expected"),
    [
        (True, "heat", "off", "heating"),
        (False, "heat", "heat", "idle"),
        (True, "cool", "off", "cooling"),
        (False, "cool", "cool", "idle"),
        # heat_cool/auto: the controller's selected direction decides the sign.
        (True, "heat_cool", "heat", "heating"),
        (True, "heat_cool", "cool", "cooling"),
        (True, "auto", "cool", "cooling"),
        (True, "heat_cool", "off", None),
        # Running with no heat/cool direction contradicts the device: uncertain.
        (True, "off", "heat", None),
        (True, "fan_only", "heat", None),
        (False, "off", "heat", "idle"),
        (None, "heat", "heat", None),
    ],
)
def test_run_action_direction(running, mode, active, expected):
    assert run_action(running, mode, active) == expected


def replay_1121(*, run_sensor, days=3):
    """Real heating alternates hour on/hour off; the cloud reports the opposite.

    Assumptions: first-order rooms, smooth 5–11 °C weather, supplied Bryant
    rates, five-minute intervals, no learning window. The cloud's frozen
    snapshot says heating exactly while the unit is really off, and idle while
    it really runs (rooms fell while it said heating, rose while it said idle).
    """
    model = Learning(list(RATES), [UNIT])
    tracker = EffortTracker()
    values = dict.fromkeys(RATES, 20.0)
    tracker.finish({UNIT: "idle"}, 0, modes={UNIT: "heat"})
    for minute in range(5, days * 1440 + 1, 5):
        previous = minute - 5
        actual = previous % 120 < 60
        reported = "idle" if actual else "heating"
        action = run_action(actual, "heat", "heat") if run_sensor else reported
        outdoor = 8 + 3 * sin(2 * pi * previous / 1440)
        before = dict(values)
        for room, rate in RATES.items():
            values[room] += 0.002 * (outdoor - values[room]) + rate / 12 * actual
        tracker.change(UNIT, action, previous * 60, mode="heat")
        efforts, seconds = tracker.finish(
            {UNIT: action}, minute * 60, modes={UNIT: "heat"}
        )
        model.update(before, values, outdoor, efforts, duration=seconds / 300)
    return model


def test_1121_replay_with_run_sensor_learns_positive_heating():
    model = replay_1121(run_sensor=True)
    for room, rate in RATES.items():
        assert model.blended(room, UNIT) * 12 == pytest.approx(rate, rel=0.2)


def test_1121_replay_without_run_sensor_is_unchanged():
    # Today's behaviour: the contradictory cloud action teaches heating cools.
    model = replay_1121(run_sensor=False)
    assert all(model.blended(room, UNIT) <= 0 for room in RATES)


@pytest.fixture
def settings():
    data = deepcopy(DEFAULTS)
    data["learning_window_minutes"] = 0
    data["outdoor_sensor"] = "sensor.outdoor"
    data["sensors"] = [{"entity_id": "sensor.room", "weight": 1, "zone": ""}]
    data["systems"] = [
        {
            "entity_id": "climate.ducted",
            "preset": "",
            "run_sensor": "binary_sensor.ducted_run",
        },
        {"entity_id": "climate.split", "preset": ""},
    ]
    return data


async def setup(hass, settings, run_state="off"):
    hass.set_state(CoreState.running)
    hass.config.units = METRIC_SYSTEM
    hass.states.async_set("sensor.room", "19", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.outdoor", "5")
    hass.states.async_set("climate.ducted", "heat", {"hvac_action": "heating"})
    hass.states.async_set("climate.split", "off", {"hvac_action": "off"})
    run = settings["systems"][0].get("run_sensor")
    if run:
        hass.states.async_set(run, run_state)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data=settings)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    c = entry.runtime_data
    # Disagreeing modes leave control observing only: no commands interfere.
    assert not c.enabled
    return entry, c


def at(when):
    return patch(
        "custom_components.house_climate.coordinator.dt_util.utcnow",
        return_value=when,
    )


async def set_at(hass, when, entity_id, state, attributes=None):
    with at(when):
        hass.states.async_set(entity_id, state, attributes or {})
        await hass.async_block_till_done()


async def tick_at(c, when, room=None):
    if room is not None:
        c.hass.states.async_set("sensor.room", room, {"unit_of_measurement": "°C"})
        await c.hass.async_block_till_done()
    with at(when):
        await c.async_tick()
    return c.samples[-1]["efforts"]["climate.ducted"]


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize(("mode", "sign"), [("heat", 1), ("cool", -1)])
async def test_binary_run_sensor_overrides_contradictory_device_action(
    hass, settings, mode, sign
):
    entry, c = await setup(hass, settings)
    start = dt_util.utcnow()
    contradiction = "heating" if mode == "heat" else "cooling"
    await set_at(hass, start, "climate.ducted", mode, {"hvac_action": contradiction})
    await tick_at(c, start)
    samples = c.learning.rows["sensor.room"].samples
    # Device says it runs the whole interval; the local sensor says off.
    assert await tick_at(c, start + timedelta(seconds=300), "19.1") == 0
    source = hass.states.get("climate.house").attributes["action_sources"]
    assert source["climate.ducted"]["source"] == "run_sensor"
    assert source["climate.ducted"]["running"] is False
    assert source["climate.ducted"]["device_action"] == contradiction
    assert source["climate.split"]["source"] == "device"
    # Device says idle; the run sensor turns on for 4 of 5 minutes.
    await set_at(
        hass,
        start + timedelta(seconds=300),
        "climate.ducted",
        mode,
        {"hvac_action": "idle"},
    )
    await set_at(hass, start + timedelta(seconds=360), "binary_sensor.ducted_run", "on")
    effort = await tick_at(c, start + timedelta(seconds=600), "19.2")
    assert effort == pytest.approx(sign * 0.8)
    assert c.action_sources["climate.ducted"]["action"] == contradiction
    assert c.learning.rows["sensor.room"].samples == samples + 2


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_power_run_sensor_threshold(hass, settings):
    settings["systems"][0]["run_sensor"] = "sensor.ducted_power"
    settings["systems"][0]["run_threshold"] = 50
    entry, c = await setup(hass, settings, run_state="12")
    start = dt_util.utcnow()
    await set_at(hass, start, "climate.ducted", "heat", {"hvac_action": "heating"})
    await tick_at(c, start)
    assert await tick_at(c, start + timedelta(seconds=300)) == 0
    await set_at(hass, start + timedelta(seconds=300), "sensor.ducted_power", "640")
    assert await tick_at(c, start + timedelta(seconds=600)) == 1
    assert c.action_sources["climate.ducted"]["running"] is True


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_unavailable_run_sensor_feeds_no_evidence(hass, settings):
    entry, c = await setup(hass, settings, run_state="on")
    start = dt_util.utcnow()
    await set_at(hass, start, "climate.ducted", "heat", {"hvac_action": "heating"})
    await tick_at(c, start)
    assert await tick_at(c, start + timedelta(seconds=300), "19.1") == 1
    samples = c.learning.rows["sensor.room"].samples
    await set_at(
        hass, start + timedelta(seconds=400), "binary_sensor.ducted_run", "unavailable"
    )
    # No silent fallback to the device's "heating".
    assert await tick_at(c, start + timedelta(seconds=600), "19.2") is None
    assert c.learning.rows["sensor.room"].samples == samples
    assert c.action_sources["climate.ducted"]["problem"] == "unavailable"
    # Still unavailable for a whole interval: withheld again.
    assert await tick_at(c, start + timedelta(seconds=900), "19.3") is None
    assert c.learning.rows["sensor.room"].samples == samples


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_stale_run_sensor_feeds_no_evidence(hass, settings):
    entry, c = await setup(hass, settings, run_state="on")
    start = dt_util.utcnow()
    await set_at(hass, start, "climate.ducted", "heat", {"hvac_action": "heating"})
    await tick_at(c, start)
    assert await tick_at(c, start + timedelta(seconds=300), "19.1") == 1
    # Same last report, but now older than stale_after: no evidence.
    c.config["stale_after"] = 6
    assert await tick_at(c, start + timedelta(seconds=600), "19.2") is None
    assert c.action_sources["climate.ducted"]["problem"] == "stale"
    # A fresh report restores evidence from the next interval. (HA's state clock
    # is real time here, so restore the default window rather than age it.)
    c.config["stale_after"] = 720
    await set_at(
        hass, start + timedelta(seconds=600), "binary_sensor.ducted_run", "off"
    )
    assert await tick_at(c, start + timedelta(seconds=900), "19.3") == 0


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_without_run_sensor_device_action_is_used(hass, settings):
    settings["systems"][0].pop("run_sensor")
    entry, c = await setup(hass, settings)
    start = dt_util.utcnow()
    await tick_at(c, start)
    assert await tick_at(c, start + timedelta(seconds=300), "19.1") == 1
    assert c.action_sources["climate.ducted"] == {
        "source": "device",
        "action": "heating",
    }


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_set_and_clear_run_sensor_keep_learning(hass, settings):
    settings["systems"][0].pop("run_sensor")
    settings["learning_window_minutes"] = 60
    entry, c = await setup(hass, settings)
    # Fit rows directly (per-interval form); the options flow keeps window 60.
    c.learning.window_minutes = 0
    for step in range(3):
        c.learning.update(
            {"sensor.room": 19 + step / 10},
            {"sensor.room": 19.1 + step / 10},
            5,
            {"climate.ducted": 1, "climate.split": 0},
        )
    c.learning.window_minutes = 60
    await c.async_tick(record=False)
    learned = c.learning.to_dict()
    assert c.learning.rows["sensor.room"].samples == 3

    async def configure(system_inputs):
        flow = hass.config_entries.options
        result = await flow.async_init(entry.entry_id)
        result = await flow.async_configure(
            result["flow_id"],
            {
                "sensors": ["sensor.room"],
                "systems": ["climate.ducted", "climate.split"],
            },
        )
        result = await flow.async_configure(result["flow_id"], {"weight": 1})
        assert result["step_id"] == "system"
        result = await flow.async_configure(result["flow_id"], system_inputs)
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
        return entry.runtime_data

    c = await configure({"run_sensor": "sensor.ducted_power", "run_threshold": 0.4})
    assert entry.options["systems"][0]["run_sensor"] == "sensor.ducted_power"
    assert entry.options["systems"][0]["run_threshold"] == 0.4
    assert "run_sensor" not in entry.options["systems"][1]
    assert c.learning.to_dict() == learned
    hass.states.async_set("sensor.ducted_power", "1.2", {"unit_of_measurement": "A"})
    later = dt_util.utcnow()
    await tick_at(c, later)
    assert await tick_at(c, later + timedelta(seconds=300), "19.4") == 1
    assert c.action_sources["climate.ducted"]["source"] == "run_sensor"
    learned = c.learning.to_dict()

    c = await configure({})
    assert "run_sensor" not in entry.options["systems"][0]
    assert "run_threshold" not in entry.options["systems"][0]
    assert c.learning.to_dict() == learned

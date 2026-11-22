"""Duct supply − return ΔT as a unit's signed learning effort (v0.5.22).

The 11/21 Bryant reported a frozen cloud `hvac_action`, and its blower runs
almost continuously, so neither the cloud nor "blower on" says whether it heats.
Two duct probes do: ΔT sign is the direction and its size the effort.
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
    delta_effort,
)

UNIT = "climate.home_zone_1"
RATES = {"downstairs": 1.16, "guest": 1.47, "parents": 1.06, "office": 0.7}
SUPPLY, RETURN = "sensor.supply_air", "sensor.return_air"


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (0, 0),
        (1.9, 0),
        (-2, 0),  # blower only / probe offset: dead band
        (2.1, 2.1 / 15),
        (7.5, 0.5),  # heat scale 15
        (15, 1),
        (24, 1),  # aux strips: clamped, effort is per full unit capacity
        (-5, -0.5),  # cool scale 10; also a defrost in heat mode
        (-10, -1),
        (-14, -1),
        (None, None),
        (float("nan"), None),
    ],
)
def test_delta_effort_table(delta, expected):
    result = delta_effort(delta)
    assert result == (expected if expected is None else pytest.approx(expected))


def test_delta_effort_custom_scales():
    assert delta_effort(3, deadband=4) == 0
    assert delta_effort(10, 2, 20, 10) == pytest.approx(0.5)
    assert delta_effort(-4, 2, 20, 8) == pytest.approx(-0.5)


def replay(*, capacity, days=4, binary=False):
    """Real heating alternates hour on/hour off; the cloud says the opposite.

    Assumptions: first-order rooms, smooth 5–11 °C weather, supplied Bryant
    rates per full capacity, constant airflow so ΔT = capacity × 14 °C, probes
    sampled every minute, five-minute intervals. A short defrost every
    90 minutes of running gives a negative ΔT and removes heat. `capacity`
    maps the minute into the unit's running fraction (0 = off).
    """
    model = Learning(list(RATES), [UNIT])
    tracker = EffortTracker()
    values = dict.fromkeys(RATES, 20.0)
    tracker.finish({UNIT: 0}, 0, modes={UNIT: "heat"})
    before = dict(values)
    for minute in range(days * 1440):
        load = capacity(minute)
        if load and minute % 90 == 45:
            load = -0.3  # defrost: reversing valve, really removing heat
        outdoor = 8 + 3 * sin(2 * pi * minute / 1440)
        effort = delta_effort(load * 14)
        if binary:  # a run sensor: on/off only, no part-load size
            effort = float(load > 0) - float(load < 0)
        tracker.change(UNIT, effort, minute * 60, mode="heat")
        for room, rate in RATES.items():
            values[room] += (
                0.002 / 5 * (outdoor - values[room]) + rate / 60 * load * 14 / 15
            )
        if (minute + 1) % 5 == 0:
            efforts, seconds = tracker.finish(
                {UNIT: effort}, (minute + 1) * 60, modes={UNIT: "heat"}
            )
            model.update(before, values, outdoor, efforts, duration=seconds / 300)
            before = dict(values)
    return model


def test_1121_replay_with_probes_learns_positive_bryant_effects():
    model = replay(capacity=lambda minute: float(minute % 120 < 60))
    for room, rate in RATES.items():
        # Effect per full effort (ΔT 15 °C); true per-full rate is `rate`.
        assert model.blended(room, UNIT) * 12 == pytest.approx(rate, rel=0.2)


def test_variable_speed_part_load_learns_per_full_effort():
    stages = [0.35, 0.6, 0.85, 0.5]

    def capacity(minute):
        if minute % 120 >= 60:
            return 0.0
        return stages[(minute // 120) % len(stages)]

    model = replay(capacity=capacity)
    for room, rate in RATES.items():
        assert model.blended(room, UNIT) * 12 == pytest.approx(rate, rel=0.2)
    # On/off evidence alone counts part load as full and learns ~60% of truth.
    binary = replay(capacity=capacity, binary=True)
    for room, rate in RATES.items():
        assert binary.blended(room, UNIT) * 12 < 0.75 * rate


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
            "supply_sensor": SUPPLY,
            "return_sensor": RETURN,
        },
        {"entity_id": "climate.split", "preset": ""},
    ]
    return data


async def setup(hass, settings, supply="20", ret="20", run=None):
    hass.set_state(CoreState.running)
    hass.config.units = METRIC_SYSTEM
    hass.states.async_set("sensor.room", "19", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.outdoor", "5")
    hass.states.async_set(
        "climate.ducted",
        "heat",
        {"hvac_action": "heating", "current_temperature": 20},
    )
    hass.states.async_set("climate.split", "off", {"hvac_action": "off"})
    hass.states.async_set(SUPPLY, supply)
    hass.states.async_set(RETURN, ret)
    if run is not None:
        hass.states.async_set("binary_sensor.ducted_run", run)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data=settings)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    c = entry.runtime_data
    assert not c.enabled  # disagreeing modes: observe only, no commands
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


def source(c):
    return c.action_sources["climate.ducted"]


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize(
    ("mode", "action", "supply", "expected"),
    [
        # Stale cloud says cool/cooling; the air is being heated.
        ("cool", "cooling", "32", 0.8),
        # Heat mode says heating; a defrost really removes heat.
        ("heat", "heating", "15", -0.5),
        # Cloud says heating; blower only.
        ("heat", "heating", "21", 0),
        # Device reports off; air still heated (cloud lag): ΔT wins.
        ("off", "off", "35", 1),
    ],
)
async def test_delta_t_overrides_device_action_and_mode(
    hass, settings, mode, action, supply, expected
):
    entry, c = await setup(hass, settings)
    start = dt_util.utcnow()
    await set_at(
        hass,
        start,
        "climate.ducted",
        mode,
        {"hvac_action": action, "current_temperature": 20},
    )
    await set_at(hass, start, SUPPLY, supply)
    await tick_at(c, start)
    samples = c.learning.rows["sensor.room"].samples
    assert await tick_at(c, start + timedelta(seconds=300), "19.1") == pytest.approx(
        expected
    )
    assert c.learning.rows["sensor.room"].samples == samples + 1
    attrs = hass.states.get("climate.house").attributes["action_sources"]
    assert attrs["climate.ducted"]["source"] == "delta_t"
    assert attrs["climate.ducted"]["supply"] == float(supply)
    assert attrs["climate.ducted"]["return"] == 20
    assert attrs["climate.ducted"]["delta_t"] == float(supply) - 20
    assert attrs["climate.ducted"]["effort"] == pytest.approx(expected)
    assert attrs["climate.ducted"]["device_action"] == action
    assert attrs["climate.split"]["source"] == "device"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_delta_t_integrates_probe_updates_within_interval(hass, settings):
    entry, c = await setup(hass, settings)
    start = dt_util.utcnow()
    await tick_at(c, start)
    # 0 for 60 s, +1 (ΔT 15) for 120 s, −0.5 (defrost) for 60 s, +0.5 for 60 s.
    await set_at(hass, start + timedelta(seconds=60), SUPPLY, "35")
    await set_at(hass, start + timedelta(seconds=180), SUPPLY, "15")
    await set_at(hass, start + timedelta(seconds=240), RETURN, "7.5")
    await set_at(hass, start + timedelta(seconds=240), SUPPLY, "15")
    effort = await tick_at(c, start + timedelta(seconds=300), "19.1")
    assert effort == pytest.approx((120 * 1 - 60 * 0.5 + 60 * 0.5) / 300)


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize(
    ("run", "supply", "expected", "disagreement"),
    [
        ("on", "32", 0.8, False),  # agree
        ("off", "20", 0, False),  # agree: idle
        ("off", "38", 1, True),  # strips on another circuit: trust ΔT
        ("on", "21", 0, True),  # compressor drawing, no ΔT yet: trust ΔT
    ],
)
async def test_run_sensor_cross_checks_delta_t(
    hass, settings, run, supply, expected, disagreement
):
    settings["systems"][0]["run_sensor"] = "binary_sensor.ducted_run"
    entry, c = await setup(hass, settings, supply=supply, run=run)
    start = dt_util.utcnow()
    await tick_at(c, start)
    assert await tick_at(c, start + timedelta(seconds=300), "19.1") == pytest.approx(
        expected
    )
    assert source(c)["source"] == "delta_t"
    assert source(c)["running"] is (run == "on")
    assert source(c)["disagreement"] is disagreement


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_unavailable_probe_falls_back_to_trusted_run_sensor(hass, settings):
    settings["systems"][0]["run_sensor"] = "binary_sensor.ducted_run"
    entry, c = await setup(hass, settings, supply="32", run="on")
    start = dt_util.utcnow()
    await tick_at(c, start)
    assert await tick_at(c, start + timedelta(seconds=300), "19.1") == 0.8
    await set_at(hass, start + timedelta(seconds=300), SUPPLY, "unavailable")
    # Run sensor on, device in heat: +1 from the run sensor's existing rule.
    assert await tick_at(c, start + timedelta(seconds=600), "19.2") == 1
    assert source(c)["source"] == "run_sensor"
    assert source(c)["delta_problem"] == "supply unavailable"
    assert source(c)["problem"] is None
    # Both untrusted: no evidence, no fallback to the device's "heating".
    samples = c.learning.rows["sensor.room"].samples
    await set_at(
        hass, start + timedelta(seconds=700), "binary_sensor.ducted_run", "unavailable"
    )
    assert await tick_at(c, start + timedelta(seconds=900), "19.3") is None
    assert source(c)["problem"] == "supply unavailable"
    assert c.learning.rows["sensor.room"].samples == samples


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize("entity_id", [SUPPLY, RETURN])
async def test_unavailable_probe_without_run_sensor_feeds_no_evidence(
    hass, settings, entity_id
):
    entry, c = await setup(hass, settings, supply="32")
    start = dt_util.utcnow()
    await tick_at(c, start)
    assert await tick_at(c, start + timedelta(seconds=300), "19.1") == 0.8
    samples = c.learning.rows["sensor.room"].samples
    await set_at(hass, start + timedelta(seconds=400), entity_id, "unavailable")
    assert await tick_at(c, start + timedelta(seconds=600), "19.2") is None
    assert await tick_at(c, start + timedelta(seconds=900), "19.3") is None
    assert c.learning.rows["sensor.room"].samples == samples
    name = "supply" if entity_id == SUPPLY else "return"
    assert source(c)["problem"] == f"{name} unavailable"
    assert source(c)["action"] is None


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_stale_probe_feeds_no_evidence(hass, settings):
    entry, c = await setup(hass, settings, supply="32")
    start = dt_util.utcnow()
    await tick_at(c, start)
    assert await tick_at(c, start + timedelta(seconds=300), "19.1") == 0.8
    samples = c.learning.rows["sensor.room"].samples
    c.config["stale_after"] = 6  # same last report, now older than stale_after
    assert await tick_at(c, start + timedelta(seconds=600), "19.2") is None
    assert source(c)["problem"] == "supply stale"
    assert c.learning.rows["sensor.room"].samples == samples


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_return_falls_back_to_system_current_temperature(hass, settings):
    settings["systems"][0].pop("return_sensor")
    entry, c = await setup(hass, settings, supply="27.5")
    start = dt_util.utcnow()
    await tick_at(c, start)
    # Device current_temperature 20 → ΔT 7.5 → 0.5.
    assert await tick_at(c, start + timedelta(seconds=300), "19.1") == 0.5
    assert source(c)["return_sensor"] == "climate.ducted.current_temperature"
    assert source(c)["return"] == 20
    # The fallback is sampled on device updates too.
    await set_at(
        hass,
        start + timedelta(seconds=300),
        "climate.ducted",
        "heat",
        {"hvac_action": "heating", "current_temperature": 12.5},
    )
    assert await tick_at(c, start + timedelta(seconds=600), "19.2") == 1
    await set_at(
        hass,
        start + timedelta(seconds=600),
        "climate.ducted",
        "heat",
        {"hvac_action": "heating"},
    )
    assert await tick_at(c, start + timedelta(seconds=900), "19.3") is None
    assert source(c)["problem"] == "return unavailable"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_set_and_clear_probes_keep_learning(hass, settings):
    for key in ("supply_sensor", "return_sensor"):
        settings["systems"][0].pop(key)
    settings["learning_window_minutes"] = 60
    entry, c = await setup(hass, settings)
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

    c = await configure(
        {"supply_sensor": SUPPLY, "return_sensor": RETURN, "delta_full_heat": 12}
    )
    system = entry.options["systems"][0]
    assert system["supply_sensor"] == SUPPLY
    assert system["return_sensor"] == RETURN
    assert system["delta_deadband"] == 2
    assert system["delta_full_heat"] == 12
    assert system["delta_full_cool"] == 10
    assert "supply_sensor" not in entry.options["systems"][1]
    assert c.learning.to_dict() == learned
    hass.states.async_set(SUPPLY, "26")
    later = dt_util.utcnow()
    await tick_at(c, later)
    assert await tick_at(c, later + timedelta(seconds=300), "19.4") == 0.5
    assert source(c)["source"] == "delta_t"
    learned = c.learning.to_dict()

    # Supply only: return falls back; then clear everything.
    c = await configure({"supply_sensor": SUPPLY})
    assert "return_sensor" not in entry.options["systems"][0]
    assert entry.options["systems"][0]["delta_full_heat"] == 12
    assert c.learning.to_dict() == learned
    c = await configure({})
    assert not {
        "supply_sensor",
        "return_sensor",
        "delta_deadband",
        "delta_full_heat",
        "delta_full_cool",
    } & set(entry.options["systems"][0])
    assert c.learning.to_dict() == learned

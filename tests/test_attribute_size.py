"""Entity attributes stay under the recorder's 16 KB limit (v0.5.24).

Live 2026-11-22: "State attributes for sensor.house_learning exceed maximum
size of 16384 bytes" every 5 min (113 times); the recorder dropped them. The
live house has 8 room sensors, 2 systems and several heat sources.
"""

import json
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import CoreState
from homeassistant.helpers.json import JSONEncoder
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.house_climate.const import DEFAULTS, DOMAIN

ROOMS = [f"sensor.room_{n}" for n in range(8)]
SYSTEMS = ["climate.bryant", "climate.livingroom"]
SOURCES = [
    {"entity_id": "sensor.office_heat_pad_power", "kind": "power", "threshold": 10},
    {"entity_id": "sensor.server_power", "kind": "power", "threshold": 10},
    {"entity_id": "switch.fire", "kind": "binary"},
    {"entity_id": "switch.oven", "kind": "binary"},
]
LIMIT = 12 * 1024
ENTITIES = ["sensor.house_learning", "sensor.house_fan_learning", "climate.house"]


def size(state):
    return len(json.dumps(dict(state.attributes), cls=JSONEncoder))


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_attributes_fit_for_eight_rooms_two_systems_four_sources(hass):
    hass.set_state(CoreState.running)
    hass.config.units = METRIC_SYSTEM
    settings = deepcopy(DEFAULTS)
    settings["outdoor_sensor"] = "sensor.outdoor"
    settings["sensors"] = [
        {"entity_id": room, "weight": 1, "zone": "", "name": f"Room {n}"}
        for n, room in enumerate(ROOMS)
    ]
    settings["systems"] = [
        {"entity_id": SYSTEMS[0], "preset": "", "report_trust": "slow"},
        {"entity_id": SYSTEMS[1], "preset": "", "report_trust": "fresh"},
    ]
    settings["extra_heat_sources"] = SOURCES
    hass.states.async_set("sensor.outdoor", "5")
    for n, room in enumerate(ROOMS):
        hass.states.async_set(room, str(20 + n / 10), {"unit_of_measurement": "°C"})
    for system in SYSTEMS:
        hass.states.async_set(
            system,
            "heat",
            {"hvac_action": "heating", "temperature": 23, "current_temperature": 20},
        )
    hass.states.async_set(
        "sensor.office_heat_pad_power", "40", {"unit_of_measurement": "W"}
    )
    hass.states.async_set("sensor.server_power", "120", {"unit_of_measurement": "W"})
    hass.states.async_set("switch.fire", "on")
    hass.states.async_set("switch.oven", "off")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data=settings)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    c = entry.runtime_data
    # Fill every row with full-precision numbers, like a learned house.
    for row in c.learning.rows.values():
        row.theta = [0.0123456789 * (i + 1) for i in range(len(row.theta))]
        row.counts = [123.456789] * len(row.counts)
        row.samples = 4321
    # Three saved regime models per row (their coefficients are not exposed).
    for sensor, state in c.learning.regimes.items():
        state["saved"] = [
            {"id": f"saved {n}", "last_fit": 1795400000.123 + n, "row": deepcopy(row)}
            for n, row in enumerate([c.learning.rows[sensor]] * 3)
        ]
    start = dt_util.utcnow()
    for step in range(4):
        with patch(
            "custom_components.house_climate.coordinator.dt_util.utcnow",
            return_value=start + timedelta(minutes=5 * step),
        ):
            for n, room in enumerate(ROOMS):
                hass.states.async_set(
                    room,
                    f"{20 + n / 10 + step / 37:.3f}",
                    {"unit_of_measurement": "°C"},
                )
            await hass.async_block_till_done()
            await c.async_tick()
    sizes = {entity: size(hass.states.get(entity)) for entity in ENTITIES}
    assert all(value < LIMIT for value in sizes.values()), sizes
    attrs = hass.states.get("sensor.house_learning").attributes
    cell = attrs["systems"][SYSTEMS[0]][ROOMS[0]]
    assert set(cell) == {"b", "effective_effect", "confidence", "effective_samples"}
    assert cell["b"] == pytest.approx(0.0123456789 * 2 * 12, rel=1e-3)
    saved = attrs["sensors"][ROOMS[0]]["saved_models"][0]
    assert saved == {"model": "saved 0", "last_fit": 1795400000.123}
    # The model itself keeps every field.
    full = c.learning.diagnostics()
    assert {"g", "h", "steady_std", "blended_b", "influence"} <= set(
        full["systems"][SYSTEMS[0]][ROOMS[0]]
    )

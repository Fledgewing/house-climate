"""v0.5.25: heat_cool coasts in its mode instead of flipping on a small overshoot.

Live 11-23: at 23.1 °C (setpoint 22, dead band 1) with 10.6 °C outdoors the
house switched the heat pump to cool for 7.5 h (it never cooled), and at
06:07 with 9.4 °C outdoors (cool vetoed) it switched it off for 5.5 h; both
times the house drifted to 21.0 °C before heat came back.
"""

import pytest

from custom_components.house_climate.controller import (
    ControlState,
    Device,
    Reading,
    coast_reason,
    control,
)


@pytest.fixture
def config():
    return {
        "sensors": [{"entity_id": "sensor.a", "weight": 1}],
        "systems": [{"entity_id": "climate.a", "preset": "manual"}],
    }


def step(config, state, house, outdoor, now=10000, mode="heat"):
    return control(
        config,
        state,
        {"sensor.a": Reading(house)},
        {"climate.a": Device(mode=mode, temperature=22, current_temperature=house)},
        now,
        outdoor=outdoor,
    )


def auto(active, setpoint=22):
    return ControlState(
        mode="heat_cool", active_mode=active, setpoint=setpoint, last_switch=0
    )


def test_evening_overshoot_coasts_in_heat(config):
    state = auto("heat")
    result = step(config, state, 23.1, 10.6)
    assert state.active_mode == "heat"
    assert "Coasting in heat" in result.explanation
    assert not any(
        command[2].get("hvac_mode") in ("cool", "off") for command in result.commands
    )


def test_morning_overshoot_with_cool_vetoed_stays_in_heat_not_off(config):
    state = auto("heat")
    result = step(config, state, 23.1, 9.4)
    assert state.active_mode == "heat"
    assert "rules out cool" in result.explanation
    assert "vetoed operation" not in result.explanation
    # Even far past the band cooling stays ruled out; heat idles instead of off.
    step(config, state, 25.5, 9.4, now=20000)
    assert state.active_mode == "heat"


def test_real_overheating_still_switches_to_cool(config):
    state = auto("heat")
    step(config, state, 24.1, 10.6)
    assert state.active_mode == "cool"


def test_warm_outdoors_does_not_coast(config):
    # Outdoor air warmer than the house cannot bring it back: cool as before.
    state = auto("heat")
    step(config, state, 23.1, 23.5)
    assert state.active_mode == "cool"


def test_unknown_outdoor_keeps_old_switch(config):
    state = auto("heat")
    step(config, state, 23.1, None)
    assert state.active_mode == "cool"


def test_summer_undershoot_coasts_in_cool(config):
    state = auto("cool", setpoint=24)
    step(config, state, 22.8, 23.5, mode="cool")
    assert state.active_mode == "cool"
    step(config, state, 21.9, 23.5, now=20000, mode="cool")
    assert state.active_mode == "heat"


def test_current_mode_vetoed_still_goes_off(config):
    # Cool already running when the night gets cold: the veto still turns it off.
    state = auto("cool")
    result = step(config, state, 22.5, 8, mode="cool")
    assert state.active_mode == "off"
    assert "vetoed operation" in result.explanation


def test_back_below_setpoint_heats_without_waiting_for_the_band(config):
    state = auto("heat")
    step(config, state, 23.1, 10.6)
    step(config, state, 21.6, 10.6, now=20000)
    assert state.active_mode == "heat"


@pytest.mark.parametrize(
    ("mode", "wanted", "house", "outdoor", "coasts"),
    [
        ("heat", "cool", 23.9, 10.6, True),
        ("heat", "cool", 24.0, 10.6, False),
        ("heat", "cool", 30.0, 5.0, True),  # cool ruled out
        ("heat", "cool", 23.5, None, False),
        ("cool", "heat", 20.1, 23, True),
        ("cool", "heat", 20.5, 19, False),  # outdoor colder than the house
        ("heat", "heat", 23.5, 10, False),
    ],
)
def test_coast_reason(mode, wanted, house, outdoor, coasts):
    config = {"deadband": 1, "coast_margin": 1}
    assert bool(coast_reason(config, mode, wanted, house, 22, outdoor)) is coasts


def test_coast_margin_zero_restores_band_switch(config):
    config["coast_margin"] = 0
    state = auto("heat")
    step(config, state, 23.1, 10.6)
    assert state.active_mode == "cool"

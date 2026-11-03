"""Priority reconciliation and unchanged control when upgrading legacy weights."""

from copy import deepcopy

from custom_components.house_climate.controller import (
    ControlState,
    Device,
    Reading,
    control,
)
from custom_components.house_climate.priority import members, modes


def test_balance_preserves_legacy_control_and_reconciles_members():
    config = {
        "sensors": [{"entity_id": "a", "weight": 3}, {"entity_id": "b", "weight": 1}],
        "systems": [{"entity_id": "unit"}],
    }
    readings = {"a": Reading(19), "b": Reading(23)}
    before = control(config, ControlState(mode="heat"), readings, {"unit": Device()}, 0)
    upgraded = deepcopy(config)
    upgraded["priority_modes"] = modes(config)
    assert upgraded["priority_modes"] == {"balance": {"a": 3, "b": 1}}
    after = control(
        {**upgraded, "sensors": members(upgraded, "balance")},
        ControlState(mode="heat"),
        readings,
        {"unit": Device()},
        0,
    )
    assert after == before
    upgraded["priority_modes"]["night"] = {"a": 8, "b": 2}
    upgraded["sensors"] = [{"entity_id": "a"}, {"entity_id": "new", "weight": 99}]
    assert modes(upgraded) == {
        "balance": {"a": 3, "new": 1},
        "night": {"a": 8, "new": 1},
    }

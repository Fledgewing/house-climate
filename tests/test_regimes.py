"""Evidence-based room regimes, with deterministic physical simulations."""

from copy import deepcopy
from math import pi, sin
from random import Random

import pytest

from custom_components.house_climate.learning import Learning


def simulate(kind):
    rng = Random(51)
    model = Learning(["room"], ["a", "b"])
    temperature = 20.0
    switches = []
    original = None
    preserved = None
    ac = None
    for step in range(21 * 288):
        hour = step / 12
        phase = hour % 24
        changed = hour >= 48
        cooling = (48 <= hour < 216 or hour >= 384) and kind == "ac"
        burst = 48 <= hour < 50 and kind == "heater"
        stove = changed and 18 <= phase < 22 and kind == "stove"
        loss = 0.045 if changed and kind == "window" else 0.012
        outdoor = 8 + 6 * sin(2 * pi * phase / 24)
        solar = max(0, sin(2 * pi * (phase - 6) / 24)) * 3
        efforts = {
            s: rng.uniform(0.2, 1) if rng.random() < 0.7 else 0 for s in ["a", "b"]
        }
        before = temperature
        temperature += (
            loss * (outdoor - temperature)
            + 0.35 * efforts["a"]
            + 0.25 * efforts["b"]
            + 0.02 * solar
            - 0.2 * cooling
            + 0.9 * (burst or stove)
            + rng.gauss(0, 0.001)
        )
        previous = model.regimes["room"]["active"]
        model.update({"room": before}, {"room": temperature}, outdoor, efforts, solar)
        if model.regimes["room"]["active"] != previous:
            switches.append(
                (
                    hour + 1 / 12,
                    model.regimes["room"]["active"],
                    list(model.rows["room"].theta),
                )
            )
            if len(switches) == 1:
                original = deepcopy(model.regimes["room"]["saved"][0]["row"])
            if len(switches) == 2:
                preserved = deepcopy(model.rows["room"])
                ac = deepcopy(model.regimes["room"]["saved"][0]["row"])
            if len(switches) == 3:
                assert model.rows["room"].theta == ac.theta
    print(f"\n{kind}: switches={switches}")
    return model, switches, original, preserved


@pytest.mark.parametrize("kind", ["heater", "stove", "window", "ac"])
def test_physical_regimes(kind):
    model, switches, original, preserved = simulate(kind)
    if kind in ("heater", "stove"):
        assert switches == []
    elif kind == "window":
        assert len(switches) == 1
        assert 72 <= switches[0][0] <= 84
        assert switches[0][2][0] == pytest.approx(0.045, abs=0.004)
        assert model.rows["room"].theta[0] == pytest.approx(0.045, abs=0.002)
    else:
        assert len(switches) == 3
        assert 72 <= switches[0][0] <= 84
        assert 220 <= switches[1][0] <= 224
        assert original == preserved
        assert 388 <= switches[2][0] <= 392
        assert switches[1][1].startswith("saved #")
        assert switches[2][1].startswith("saved #")


def test_persistence_reconciliation_and_controls():
    model = Learning(["one", "two"], ["a", "b"], sources=["stove"])
    model.rows["one"].theta = [0.02, 0.1, 0.2, 0.3, 0.04, 0.01]
    model.promote("one")
    saved = deepcopy(model.regimes["one"]["saved"][0]["row"])
    model.set_temporary("one", True, 2)
    snapshot = deepcopy(model.to_dict())
    restored = Learning.from_dict(snapshot)
    assert restored.to_dict() == snapshot
    restored.update(
        {"one": 20, "two": 20},
        {"one": 22, "two": 20},
        10,
        {"a": 1, "b": 0},
        sources={"stove": 0},
    )
    assert restored.rows["one"] == model.rows["one"]
    assert restored.regimes["one"]["challenger"] == model.regimes["one"]["challenger"]
    restored.relearn("one")
    assert restored.regimes["one"]["saved"][0]["row"] == saved
    restored.reconcile(["one"], ["b", "a"], [])
    assert set(restored.regimes) == {"one"}
    assert restored.regimes["one"]["saved"][0]["row"].theta == [
        0.02,
        0.2,
        0.1,
        0.04,
        0.01,
    ]
    restored.update({"one": 20}, {"one": 20}, 10, {"a": 1, "b": 0}, now=7201)
    assert not restored.regimes["one"]["temporary"]


def test_library_eviction_and_frozen_rows():
    model = Learning(["room"], ["a"])
    for i in range(3):
        model.clock = 100 + i
        model.promote("room")
    model.regimes["room"]["saved"][0]["last_fit"] = 999
    model.promote("room")
    assert [s["id"] for s in model.regimes["room"]["saved"]] == [
        "saved #1",
        "saved #3",
        "saved #4",
    ]
    frozen = deepcopy(model.regimes["room"]["saved"])
    model.update({"room": 20}, {"room": 21}, 10, {"a": 1})
    for before, after in zip(frozen, model.regimes["room"]["saved"]):
        assert before["row"] == after["row"]


@pytest.mark.parametrize(
    "missing",
    ["day", "night", "a:0", "a:1", "b:0", "b:1", "coverage", "gap", "margin", "wins"],
)
def test_promotion_requires_complete_varied_evidence(missing):
    model = Learning(["room"], ["a", "b"])
    model.clock = 24 * 3600
    contexts = ["day", "night", "a:0", "a:1", "b:0", "b:1"]
    model.regimes["room"]["blocks"] = [
        {
            "bucket": i,
            "seconds": 7200,
            "errors": {"active": 720, "challenger": 360},
            "contexts": contexts.copy(),
        }
        for i in range(12)
    ]
    assert model.evidence("room", "challenger", 24, True)
    blocks = model.regimes["room"]["blocks"]
    if missing in contexts:
        for block in blocks:
            block["contexts"].remove(missing)
    elif missing == "coverage":
        blocks[0]["seconds"] = 300
    elif missing == "gap":
        blocks.pop(0)
    elif missing == "margin":
        for block in blocks:
            block["errors"]["challenger"] = 505
    else:
        for block in blocks[:3]:
            block["errors"]["challenger"] = 1000
    assert not model.evidence("room", "challenger", 24, True)


def test_old_store_loads_neutral_challengers_and_independent_rooms():
    old = Learning(["one", "two"], ["a"]).to_dict()
    old.pop("regimes")
    restored = Learning.from_dict(old)
    assert restored.regimes["one"]["challenger"].samples == 0
    restored.rows["one"].counts = [100]
    restored.rows["one"].samples = 100
    restored.rows["one"].covariance = [
        [0.001 if i == j else 0 for j in range(4)] for i in range(4)
    ]
    restored.rows["one"].theta = [0, 0.03, 0, 0]
    for _ in range(5):
        restored.update({"one": 20, "two": 20}, {"one": 21, "two": 20.03}, 20, {"a": 1})
    assert restored.disturbed == {"one": "heat"}
    assert restored.regimes["one"]["challenger"].samples == 5
    two = deepcopy(restored.rows["two"])
    restored.promote("one")
    assert "one" not in restored.disturbed
    assert restored.rows["two"] == two
    restored.relearn("one")
    assert restored.rows["two"] == two


def test_boundary_scores_are_pre_update_and_duration_weighted():
    model = Learning(["room"], ["a"])
    model.rows["room"].theta = [0, 0.1, 0, 0]
    model.regimes["room"]["challenger"].theta = [0, 0.2, 0, 0]
    model.update({"room": 20}, {"room": 20.2}, 20, {"a": 1}, now=7300, local_hour=8)
    blocks = model.regimes["room"]["blocks"]
    assert [b["seconds"] for b in blocks] == [200, 100]
    assert [b["errors"]["active"] for b in blocks] == pytest.approx([20, 10])
    assert [b["errors"]["challenger"] for b in blocks] == pytest.approx(
        [0, 0], abs=1e-10
    )
    assert model.rows["room"].theta[1] != 0.1


def test_temporary_expiry_does_not_train_partial_interval():
    model = Learning(["room"], ["a"])
    model.set_temporary("room", True, 1)
    before = deepcopy(model.rows["room"])
    model.update({"room": 20}, {"room": 22}, 5, {"a": 1}, now=3601)
    assert not model.regimes["room"]["temporary"]
    assert model.rows["room"] == before
    assert model.regimes["room"]["challenger"].samples == 0
    assert not model.regimes["room"]["blocks"]

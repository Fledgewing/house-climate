"""Latent effort from the rooms' response (v0.5.23 part B).

Unit tests for the learning pieces (window advance over a missing effort,
`backfill_effort`, `effort_span`, count weights and the independent
covariance) and for `LatentEffort` on a synthetic six-room house, plus Home
Assistant tests for the coordinator wiring. The closed-loop latent
simulation is in tests/test_slow_reporter.py.
"""

import json
from copy import deepcopy
from datetime import timedelta
from random import Random

import pytest
import test_command_effort
from homeassistant.util import dt as dt_util
from test_command_effort import CLOUD, at, setup, tick_at

from custom_components.house_climate.const import DEFAULTS
from custom_components.house_climate.latent import DELAYS, LatentEffort
from custom_components.house_climate.learning import Learning, Row

# --- Learning: window advance, backfill, count weights ----------------------


def averaged(**kwargs):
    return Learning(["room"], ["unit"], window_minutes=60, **kwargs)


def feed(model, steps, effort, start=0, rise=0.01):
    for step in range(start, start + steps):
        model.update(
            {"room": 20 + rise * step},
            {"room": 20 + rise * (step + 1)},
            10,
            {"unit": effort},
            now=(step + 1) * 300,
        )


def test_missing_effort_advances_the_window_instead_of_clearing_it():
    model = averaged()
    feed(model, 10, 1)
    start = model.windows["room"]["start"]
    feed(model, 1, None, start=10)
    # HEAD cleared every window here and needed 70 minutes to refill.
    assert model.windows["room"]["start"] == start
    assert model.windows["room"]["accepted"] is False
    assert model.effort_span("system", "unit", 3000, 3300)[1] == 1.0
    samples = model.rows["room"].samples
    feed(model, 10, 1, start=11)
    # The gap is still inside every 60-minute window: nothing is accepted.
    assert model.rows["room"].samples == samples
    assert not model.windows["room"]["accepted"]


def test_backfill_completes_windows_that_end_after_it():
    plain, filled = averaged(), averaged()
    for model in (plain, filled):
        feed(model, 10, 1)
        feed(model, 1, None, start=10)
        feed(model, 3, 1, start=11)
    assert filled.backfill_effort("unit", 3000, 3300, 1.0) == 300
    for model in (plain, filled):
        feed(model, 2, 1, start=14)
    assert plain.rows["room"].samples == 0
    assert filled.rows["room"].samples == 2
    assert filled.windows["room"]["accepted"]


def test_backfill_fills_only_missing_effort_and_flags_it_latent():
    model = averaged()
    model.effort_history = [
        [0, 300, {"system:unit": 1.0}],
        [300, 900, {"system:unit": None}],
        [900, 1200, {"system:unit": 0.0}],
    ]
    assert model.backfill_effort("unit", 0, 600, 0.0) == 300
    assert model.effort_history == [
        [0, 300, {"system:unit": 1.0}],
        [300, 600, {"system:unit": 0.0, "latent:system:unit": True}],
        [600, 900, {"system:unit": None}],
        [900, 1200, {"system:unit": 0.0}],
    ]
    # (known mean, unknown share, constant, latent share)
    assert model.effort_span("system", "unit", 0, 1200) == (0.25, 0.25, None, 0.25)
    assert model.effort_span("system", "unit", 900, 1200) == (0.0, 0.0, 0.0, 0.0)
    assert model.effort_span("system", "unit", 300, 600) == (0.0, 0.0, 0.0, 1.0)
    assert model.latent_share("system", "unit", 0, 600) == 0.5
    assert model.backfill_effort("unit", 0, 1200, None) == 0
    # Stored history with the flag round-trips; removing the system drops it.
    restored = Learning.from_dict(model.to_dict())
    assert restored.effort_history == model.effort_history
    restored.reconcile(["room"], [])
    assert all(values == {} for _, _, values in restored.effort_history)


def test_latent_window_adds_no_count_and_leaves_confidence_covariance():
    model = averaged()
    row = model.rows["room"]
    x = [1.0] * len(row.theta)
    Learning.fit_row(row, x, 0.1, [1.0], 1, 0.99, evidence_weight=0.5)
    counts, covariance = list(row.counts), deepcopy(row.covariance)
    assert row.independent is None  # no latent window yet: same as covariance
    Learning.fit_row(
        row, x, 0.1, [1.0], 1, 0.99, evidence_weight=0.5, count_weights=[0.0]
    )
    assert row.counts == counts  # neither added nor aged
    assert row.independent == covariance  # snapshot before the latent window
    assert row.covariance != covariance  # the coefficients still learn
    Learning.fit_row(
        row, x, 0.1, [1.0], 1, 0.99, evidence_weight=0.5, count_weights=[1.0]
    )
    assert row.counts[0] == pytest.approx(0.99 * counts[0] + 0.5)
    assert row.independent != covariance  # independent windows update it
    # Without count weights the update is exactly the pre-v0.5.23 one.
    old, new = deepcopy(model.neutral_row()), deepcopy(model.neutral_row())
    Learning.fit_row(old, x, 0.2, [1.0], 1, 0.99, evidence_weight=0.5)
    Learning.fit_row(
        new, x, 0.2, [1.0], 1, 0.99, evidence_weight=0.5, count_weights=[1.0]
    )
    assert old == new


def test_confidence_uses_the_independent_covariance():
    model = averaged()
    row = model.rows["room"]
    row.counts = [1000.0]
    row.theta[1] = 0.1
    row.independent = deepcopy(row.covariance)
    tight = deepcopy(row.covariance)
    for i in range(len(tight)):
        tight[i][i] = 1e-8
    row.covariance = tight
    loose = model.confidence("room", "unit")
    row.independent = None
    assert model.confidence("room", "unit") > loose + 0.5


def test_stored_rows_without_new_fields_load_unchanged():
    model = averaged()
    feed(model, 20, 1)
    saved = model.to_dict()
    saved.pop("independent_covariances")
    for item in saved["effort_history"]:
        assert not any(key.startswith("latent:") for key in item[2])
    restored = Learning.from_dict(saved)
    assert restored.rows["room"].independent is None
    assert restored.rows["room"].theta == model.rows["room"].theta
    assert restored.rows["room"].counts == model.rows["room"].counts


V0522_ROW_FIELDS = {"theta", "covariance", "counts", "samples", "squared_error"}


def marked(model, value):
    return [
        [value] * len(model.neutral_row().covariance)
        for _ in model.neutral_row().covariance
    ]


def test_row_dicts_keep_the_v0522_fields_for_rollback():
    """v0.5.22 builds `Row(**data)`: a stored row must have exactly its fields."""
    model = averaged()
    feed(model, 20, 1)
    state = model.regimes["room"]
    model.rows["room"].independent = marked(model, 1.0)
    state["challenger"].independent = marked(model, 2.0)
    state["saved"] = [
        {"id": "saved #1", "row": deepcopy(model.rows["room"]), "last_fit": 0}
    ]
    state["saved"][0]["row"].independent = marked(model, 3.0)
    saved = model.to_dict()
    assert set(saved["rows"]["room"]) == V0522_ROW_FIELDS
    assert set(saved["regimes"]["room"]["challenger"]) == V0522_ROW_FIELDS
    assert set(saved["regimes"]["room"]["saved"][0]["row"]) == V0522_ROW_FIELDS
    Row(**saved["rows"]["room"])  # what v0.5.22's from_dict does
    restored = Learning.from_dict(json.loads(json.dumps(saved)))
    assert restored.rows["room"].independent == marked(model, 1.0)
    assert restored.regimes["room"]["challenger"].independent == marked(model, 2.0)
    assert restored.regimes["room"]["saved"][0]["row"].independent == marked(model, 3.0)
    # A store written with the field inside the row (86b7e6e) loads too.
    saved.pop("independent_covariances")
    saved["rows"]["room"]["independent"] = marked(model, 4.0)
    legacy = Learning.from_dict(saved)
    assert legacy.rows["room"].independent == marked(model, 4.0)


def test_reconcile_keeps_and_remaps_the_independent_covariance():
    model = averaged()
    model.rows["room"].independent = marked(model, 9.0)
    model.reconcile(["room"], ["unit"])
    assert model.rows["room"].independent == marked(model, 9.0)
    model.rows["room"].independent[1][1] = 5.0  # the unit's b variance
    model.reconcile(["room"], ["unit", "split"])
    independent = model.rows["room"].independent
    assert independent[1][1] == 5.0  # unit's column kept in place
    assert independent[2][2] == model.neutral_row().covariance[2][2]  # new: prior


def test_challenger_reset_also_resets_its_independent_covariance():
    model = Learning(["room"], ["unit"])
    challenger = model.regimes["room"]["challenger"]
    challenger.samples = 30
    challenger.independent = marked(model, 9.0)
    model.regimes["room"]["surprises"] = 1
    model.update({"room": 20}, {"room": 25}, 10, {"unit": 0})
    assert model.regimes["room"]["challenger"].independent is None


def test_latent_lag_span_adds_no_count():
    """A window whose lag regressor reads a latent span counts nothing."""
    model = averaged()
    feed(model, 10, 1)
    feed(model, 1, None, start=10)
    model.backfill_effort("unit", 3000, 3300, 1.0)
    # Windows end at most at 10200 s: the gap at 3000-3300 has left every
    # window by 7200 s but stays in their lag hour.
    feed(model, 23, 1, start=11)
    assert model.rows["room"].samples > 0
    assert model.rows["room"].counts == [0.0]
    feed(model, 16, 1, start=34)
    assert model.rows["room"].counts[0] > 0


def test_plain_fit_lag_span_latent_adds_no_count():
    model = Learning(["room"], ["unit"])
    feed(model, 3, 1)
    model.effort_history[-1][2]["latent:system:unit"] = True
    feed(model, 1, 1, start=3)
    assert model.rows["room"].counts == pytest.approx([3 * 0.998**2 + 0.0], rel=0.01)


def test_label_delay_evaluates_windows_later():
    prompt, delayed = averaged(), averaged(label_delay_minutes=45)
    for model in (prompt, delayed):
        feed(model, 20, 1)
    fitted = prompt.rows["room"].samples
    assert fitted > 0
    assert delayed.rows["room"].samples == 0
    feed(delayed, 9, 1, start=20)  # 45 minutes later
    assert delayed.rows["room"].samples == fitted


# --- LatentEffort on a synthetic six-room house -----------------------------

ROOMS = {"a": 1.0, "b": 1.4, "c": 0.8, "d": 1.2, "e": 0.6, "f": 1.1}
OFFSETS = {"a": -0.3, "b": -0.2, "c": -0.4, "d": -0.25, "e": -0.1, "f": -0.35}
UNIT, OTHER = "climate.unit", "climate.other"


def house(truth, known, *, delay=5, spike=None, other=None, seed=7, **options):
    """Run `LatentEffort` over 15-min blocks.

    Returns (estimator, decisions {block: label or None}, estimates {block: û}).

    `truth` is the unit's real effort per block, `known` whether its label is
    known (an anchor) or missing (to estimate). Rooms respond `delay` minutes
    late; `spike` adds 1 °C/h to room "a" in that block's response span.
    `other` makes a second system's label missing in that block.
    """
    rng = Random(seed)
    estimator = LatentEffort(**options)
    learning = Learning(["a"], [UNIT, OTHER])
    learning.effort_history = [
        [
            b * 900,
            (b + 1) * 900,
            {
                f"system:{UNIT}": float(u) if known[b] else None,
                f"system:{OTHER}": None if b == other else 0.0,
            },
        ]
        for b, u in enumerate(truth)
    ]
    temperatures = dict.fromkeys(ROOMS, 20.0)
    decisions, estimates = {}, {}
    for tick in range(1, (len(truth) + 5) * 3):
        now = tick * 300
        cause = (now - 300 - delay * 60) // 900
        effort = truth[cause] if 0 <= cause < len(truth) else 0
        for room, share in ROOMS.items():
            rate = share * effort + OFFSETS[room]
            if spike is not None and (now - 300 - delay * 60) // 900 == spike:
                rate += room == "a"
            temperatures[room] += rate / 12
        estimator.observe(
            now,
            {
                room: (value + rng.gauss(0, 0.02), 0.0, {})
                for room, value in temperatures.items()
            },
        )
        before = dict(estimator.status.get(UNIT, {}))
        for system, start, end, value in estimator.process(
            now,
            [UNIT, OTHER],
            {UNIT: {"direction": 1, "estimate": True}},
            lambda key, a, b: learning.effort_span("system", key, a, b),
        ):
            decisions[start // 900] = value
        status = estimator.status.get(UNIT, {})
        if status and status != before and "u_hat" in status:
            decisions.setdefault(status["block"] // 900, None)
            estimates[status["block"] // 900] = status["u_hat"]
    return estimator, decisions, estimates


def pattern(blocks, seed=3):
    rng = Random(seed)
    return [int(rng.random() < 0.5) for _ in range(blocks)]


def test_latent_recovers_on_and_off_after_anchors():
    truth = pattern(80)
    known = [b < 50 for b in range(80)]
    estimator, decisions, _ = house(truth, known)
    status = estimator.diagnostics(UNIT)
    assert status["ready"] and status["delay_min"] == 5 and status["rooms"] == 6
    estimated = range(51, 80)  # block 50's response mixes in the anchors
    decided = [b for b in estimated if decisions.get(b) is not None]
    assert len(decided) >= 0.8 * len(estimated)
    assert all(decisions[b] == truth[b] for b in decided)


def test_latent_refuses_before_twelve_on_and_off_anchors():
    truth = [0] + [1] * 11 + [0] * 20 + [1] * 10  # block 0 is never processed
    known = [b < 32 for b in range(42)]
    estimator, decisions, _ = house(truth, known)
    status = estimator.diagnostics(UNIT)
    assert (status["anchors_on"], status["anchors_off"]) == (11, 20)
    assert not status["ready"]
    assert not any(v is not None for v in decisions.values())


def test_one_room_sun_spike_fails_chi2():
    truth = pattern(70)
    known = [b < 50 for b in range(70)]
    _, clean, _ = house(truth, known)
    estimator, spiked, _ = house(truth, known, spike=60)
    assert clean[60] == truth[60]
    assert spiked[60] is None
    assert estimator.status[UNIT]["block"] > 60 * 900  # later blocks still decide
    assert spiked[62] == truth[62]


def test_latent_scale_on_held_out_certain_blocks():
    truth = pattern(90, seed=11)
    known = [b < 50 or b % 2 for b in range(90)]
    _, _, estimates = house(truth, known)
    held_on = [u for b, u in estimates.items() if b >= 52 and not b % 2 and truth[b]]
    assert len(held_on) >= 5
    assert sum(held_on) / len(held_on) == pytest.approx(1.0, abs=0.15)


def test_two_unlabelled_systems_are_not_identifiable():
    truth = pattern(70)
    known = [b < 50 for b in range(70)]
    _, decisions, _ = house(truth, known, other=60)
    assert decisions.get(60) is None
    assert decisions.get(62) == truth[62]


def test_latent_state_round_trips():
    truth = pattern(60)
    estimator, _, _ = house(truth, [b < 50 for b in range(60)])
    restored = LatentEffort(estimator.to_dict())
    assert restored.to_dict() == estimator.to_dict()
    assert restored.fit(UNIT) == estimator.fit(UNIT)
    assert restored.diagnostics(UNIT) == estimator.diagnostics(UNIT)
    assert restored.samples == []  # a restart loses only the last hour of ticks
    assert LatentEffort(None).to_dict() == {
        "stats": {},
        "anchors": {},
        "processed": None,
        "clock": None,
        "engaged": False,
        "status": {},
    }


# --- Coordinator wiring ------------------------------------------------------

EXTRA = ("sensor.room2", "sensor.room3")


def ready_estimator(now):
    """An estimator already calibrated: every room heats 1 °C/h when on."""
    estimator = LatentEffort(min_rooms=3)
    rng = Random(5)
    for block in range(30):
        value = float(block % 2)
        start = now - (40 - block) * 900
        estimator.add_anchor(
            CLOUD,
            start,
            value,
            {
                delay: {
                    room: value + rng.gauss(0, 0.05) for room in ("sensor.room", *EXTRA)
                }
                for delay in DELAYS
            },
        )
    return estimator


# The slow cloud unit (forced slow, stale fetch) and a fresh split, as in
# tests/test_command_effort.py.
settings = pytest.fixture(test_command_effort.settings.__wrapped__)


@pytest.fixture
def rooms(settings):
    settings["sensors"] += [
        {"entity_id": key, "weight": 1, "zone": ""} for key in EXTRA
    ]
    return settings


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_latent_backfills_unlabelled_heating(hass, rooms):
    start = dt_util.utcnow().replace(second=0, microsecond=0)
    for key in EXTRA:
        hass.states.async_set(key, "20", {"unit_of_measurement": "°C"})
    entry, c = await setup(hass, rooms, start)
    c.latent = ready_estimator(start.timestamp())
    assert c.learning.label_delay_minutes == 0
    for step in range(19):
        when = start + timedelta(minutes=5 * step)
        value = f"{20 + step / 12:.3f}"
        for key in EXTRA:
            hass.states.async_set(key, value, {"unit_of_measurement": "°C"})
        # No command and a stale fetch: nothing labels the unit right now.
        assert await tick_at(c, when, value) is None
    info = hass.states.get("climate.house").attributes["action_sources"][CLOUD]
    assert info["source"] == "latent"
    assert info["action"] is None
    assert info["latent"]["ready"] is True
    assert info["latent"]["label"] == 1.0
    assert info["latent"]["u_hat"] == pytest.approx(1.0, abs=0.15)
    assert info["coverage_24h"]["latent"] > 0
    flagged = [
        values
        for _, _, values in c.learning.effort_history
        if values.get(f"latent:system:{CLOUD}")
    ]
    assert flagged and all(v[f"system:{CLOUD}"] == 1.0 for v in flagged)
    # Averaged learning windows now wait for late labels.
    assert c.learning.label_delay_minutes == 45
    saved = await c.store.async_load()
    assert saved["latent"]["anchors"][CLOUD]
    assert saved["latent"]["status"][CLOUD]["label"] == 1.0


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_latent_option_off_keeps_the_span_skipped(hass, rooms):
    rooms["systems"][0]["latent_effort"] = False
    start = dt_util.utcnow().replace(second=0, microsecond=0)
    for key in EXTRA:
        hass.states.async_set(key, "20", {"unit_of_measurement": "°C"})
    entry, c = await setup(hass, rooms, start)
    c.latent = ready_estimator(start.timestamp())
    for step in range(19):
        value = f"{20 + step / 12:.3f}"
        for key in EXTRA:
            hass.states.async_set(key, value, {"unit_of_measurement": "°C"})
        await tick_at(c, start + timedelta(minutes=5 * step), value)
    info = c.action_sources[CLOUD]
    assert info["source"] is None
    assert "latent" not in info
    assert "latent" not in info["coverage_24h"]
    assert not any(
        values.get(f"latent:system:{CLOUD}")
        for _, _, values in c.learning.effort_history
    )
    assert c.learning.label_delay_minutes == 0


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_store_without_latent_state_loads_and_keeps_learning(hass, settings):
    start = dt_util.utcnow()
    entry, c = await setup(hass, settings, start)
    await tick_at(c, start)
    saved = await c.store.async_load()
    legacy = deepcopy(saved)
    legacy.pop("latent")
    legacy["learning"].pop("independent_covariances")
    await c.store.async_save(legacy)
    with at(start):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    restored = entry.runtime_data
    assert restored.latent.to_dict()["anchors"] == {}
    assert restored.learning.rows.keys() == c.learning.rows.keys()
    for key, row in restored.learning.rows.items():
        assert row.theta == c.learning.rows[key].theta
        assert row.counts == c.learning.rows[key].counts
    assert DEFAULTS["learning_window_minutes"] == 60


def test_anchors_expire_with_time():
    estimator = LatentEffort()
    for block in range(30):
        estimator.add_anchor(UNIT, block * 900, float(block % 2), {5: {"a": 0.0}})
    estimator.observe(30 * 900, {})
    assert estimator.anchor_counts(UNIT) == (15, 15)
    estimator.observe(8 * 86400, {})
    assert estimator.anchor_counts(UNIT) == (0, 0)
    assert not estimator.diagnostics(UNIT)["ready"]
    assert LatentEffort(estimator.to_dict()).clock == 8 * 86400


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_label_delay_holds_once_engaged(hass, rooms):
    rooms["learning_window_minutes"] = 60
    start = dt_util.utcnow().replace(second=0, microsecond=0)
    for key in EXTRA:
        hass.states.async_set(key, "20", {"unit_of_measurement": "°C"})
    entry, c = await setup(hass, rooms, start)
    c.latent = ready_estimator(start.timestamp())
    await tick_at(c, start)
    assert c.latent.engaged and c.learning.label_delay_minutes == 45
    # Calibration goes stale (no longer ready): the delay is held anyway.
    c.latent.anchors = {}
    await tick_at(c, start + timedelta(minutes=5))
    assert not c.latent.diagnostics(CLOUD)["ready"]
    assert c.learning.label_delay_minutes == 45
    saved = await c.store.async_load()
    assert saved["latent"]["engaged"] is True
    # Latent off for every system releases it.
    for system in c.config["systems"]:
        system["latent_effort"] = False
    await tick_at(c, start + timedelta(minutes=10))
    assert c.learning.label_delay_minutes == 0

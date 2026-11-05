"""Averaged learning uses the same physical clock for every input."""

import pytest

from custom_components.house_climate.filtering import SensorFilter
from custom_components.house_climate.learning import Learning


@pytest.mark.parametrize("excursion_minutes,completed", [(10, True), (30, False)])
def test_excursion_masks_own_span(excursion_minutes, completed):
    f = SensorFilter(history_minutes=80)
    model = Learning(["room"], ["unit"], interval_minutes=2, window_minutes=60)
    previous = 20
    # At 80 min the entire excursion is inside the completed 60-min window.
    for minute in range(0, 81, 2):
        value = 23 if 30 <= minute < 30 + excursion_minutes else 20
        f.observe(value, minute * 60)
        f.value(minute * 60)
        if minute:
            model.update(
                {"room": previous},
                {"room": value},
                10,
                {"unit": 1},
                now=minute * 60,
                sensor_filters={"room": f},
            )
        previous = value
        f.finish_interval()
    assert f.excursions[0][:2] == [1800, (30 + excursion_minutes) * 60]
    assert f.mean(20 * 60, 70 * 60) == 20
    assert bool(model.rows["room"].samples) is completed
    if completed:
        assert model.residuals["room"] == pytest.approx(0, abs=0.01)
        assert model.rows["room"].counts[0] > 0


def test_regressors_shift_by_half_average_span():
    f = SensorFilter(history_minutes=80, max_rate=100)
    model = Learning(
        ["room"], ["unit"], sources=["source"], interval_minutes=2, window_minutes=60
    )
    for minute in range(0, 81, 2):
        f.observe(20, minute * 60)
        if minute:
            model.update(
                {"room": 20},
                {"room": 20},
                minute,
                {"unit": minute},
                solar=2 * minute,
                sources={"source": 3 * minute},
                now=minute * 60,
                sensor_filters={"room": f},
            )
    result = model.averaged_window("room", f, 80 * 60, 80 * 60, 20, 20)
    x, _, activity, _ = result
    # Physical interval [15,75] rather than the unaligned [20,80].
    assert activity == pytest.approx([46, 138])
    assert x[0] == pytest.approx(26)
    assert x[-2] == pytest.approx(92)

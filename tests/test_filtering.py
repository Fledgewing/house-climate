"""Duration weighting, generic transients, restart continuity and slow trends."""

import pytest

from custom_components.house_climate.filtering import SensorFilter


@pytest.mark.parametrize("sign", [-1, 1])
def test_short_excursion_between_passes_and_restart(sign):
    f = SensorFilter()
    f.observe(22, 0)
    f.observe(22 + sign * 4, 60)
    assert f.blocked
    assert f.value(120) == 22
    f = SensorFilter(saved=f.to_dict())
    f.observe(22, 180)
    assert f.value(300) == 22
    assert f.blocked
    assert f.rejected == [[60, 180]]
    f.finish_interval()
    assert not f.blocked
    assert f.value(2000) == 22
    assert not f.rejected
    assert len(f.points) == 1


def test_time_weighting_not_sample_count_and_missing_values():
    f = SensorFilter(max_rate=100)
    f.observe(20, 0)
    for second in range(600, 610):
        f.observe(24, second)
    assert f.value(610) == 20
    f.observe(None, 620)
    f.observe(25, 1500)
    assert f.value(1600) == 25


def test_fast_permanent_step_is_admitted_after_window():
    f = SensorFilter()
    f.observe(22, 0)
    f.observe(18, 60)
    assert f.value(900) == 22
    assert f.blocked
    assert f.value(961) == 18
    f.finish_interval()
    assert not f.blocked


def test_slow_drop_tracks_with_less_than_fifteen_minutes_lag():
    f = SensorFilter()
    for minute in range(61):
        value = 24 - minute * 0.1
        f.observe(value, minute * 60)
        filtered = f.value(minute * 60)
        assert value <= filtered <= value + 1.5
        assert not f.blocked
    assert filtered == pytest.approx(18.8)


def test_small_fast_step_does_not_confirm_itself_on_unchanged_reports():
    f = SensorFilter()
    f.observe(22, 0)
    f.observe(22.1, 1)
    f.observe(22.1, 60)
    assert f.pending
    assert not f.rejected
    assert f.value(60) == 22
    f.observe(22, 120)
    assert not f.pending
    assert f.rejected == [[1, 120]]

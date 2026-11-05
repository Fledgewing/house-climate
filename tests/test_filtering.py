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
    assert f.value(960) == 18
    assert not f.blocked
    assert f.contaminated
    f.finish_interval()
    assert not f.contaminated


def test_slow_drop_tracks_with_less_than_fifteen_minutes_lag():
    f = SensorFilter()
    for minute in range(61):
        value = 24 - minute * 0.1
        f.observe(value, minute * 60)
        filtered = f.value(minute * 60)
        assert value <= filtered <= value + 1.5
        assert not f.blocked
    assert filtered == pytest.approx(18.8)


def test_small_report_burst_uses_one_minute_movement_allowance():
    f = SensorFilter()
    f.observe(22, 0)
    f.observe(22.1, 1)
    f.observe(22.1, 60)
    assert not f.pending
    assert not f.rejected
    assert f.value(60) == 22.1
    f.observe(22, 120)
    assert not f.pending
    assert not f.rejected


def test_cumulative_fast_steps_cannot_evade_rate_filter():
    f = SensorFilter()
    f.observe(22, 0)
    for second in range(1, 8):
        f.observe(22 + second * 0.1, second)
    assert f.pending
    assert 22 <= f.value(10) <= 22.5


def test_report_on_change_step_expires_on_passes_without_reports():
    f = SensorFilter()
    f.observe(16.9, 0)
    f.observe(20.6, 60)
    for now in (300, 600, 900):
        assert f.value(now) == 16.9
        assert f.blocked
        f.finish_interval()
    assert f.value(960) == 20.6
    assert not f.blocked
    assert len(f.points) == 1

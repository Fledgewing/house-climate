"""Frozen clocks and activity windows do not depend on HA state metadata."""

from custom_components.house_climate.reporting import Reporting

HOUR = 3600


def test_activity_union_threshold_and_cooling():
    tracker = Reporting()
    tracker.observe("room", 19, 0)
    tracker.activity(0, {"heat": True, "cool": True}, 4 * HOUR)
    tracker.activity(30 * 60, {"heat": False, "cool": False}, 4 * HOUR)
    tracker.activity(4 * HOUR, {}, 4 * HOUR)
    # Two systems running together for 30 minutes are not one hour of activity.
    assert not tracker.frozen("room", 4 * HOUR, 4 * HOUR)
    tracker.activity(4 * HOUR, {"cool": True}, 4 * HOUR)
    tracker.activity(5 * HOUR, {"cool": False}, 4 * HOUR)
    assert tracker.frozen("room", 5 * HOUR, 4 * HOUR)
    # A frozen room stays excluded even after the runtime ages out.
    tracker.activity(10 * HOUR, {}, 4 * HOUR)
    assert tracker.frozen("room", 10 * HOUR, 4 * HOUR)
    tracker.observe("room", None, 10 * HOUR)
    tracker.observe("room", 19.0, 10 * HOUR)
    assert tracker.frozen("room", 10 * HOUR, 4 * HOUR)
    tracker.observe("room", 19.1, 10 * HOUR)
    assert not tracker.frozen("room", 10 * HOUR, 4 * HOUR)


def test_restored_activity_does_not_invent_runtime_across_downtime():
    tracker = Reporting()
    tracker.observe("room", 19, 0)
    tracker.activity(0, {"system": True}, 4 * HOUR)
    tracker.activity(30 * 60, {}, 4 * HOUR)
    restored = Reporting(tracker.to_dict())
    restored.activity(5 * HOUR, {"system": True}, 4 * HOUR)
    assert not restored.frozen("room", 5 * HOUR, 4 * HOUR)
    assert restored.sensors["room"]["changed"] == 0
    restored.activity(6 * HOUR, {"system": False}, 4 * HOUR)
    assert restored.frozen("room", 6 * HOUR, 4 * HOUR)


def test_activity_before_value_change_does_not_freeze_sensor():
    tracker = Reporting()
    tracker.activity(0, {"system": True}, 4 * HOUR)
    tracker.activity(HOUR, {"system": False}, 4 * HOUR)
    tracker.observe("room", 19, HOUR)
    tracker.activity(5 * HOUR, {}, 4 * HOUR)
    assert not tracker.frozen("room", 5 * HOUR, 4 * HOUR)

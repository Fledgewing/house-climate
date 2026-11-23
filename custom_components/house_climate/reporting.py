"""Value-change and actual HVAC activity tracking, independent of HA metadata."""

from statistics import median

from .const import (
    CADENCE_GAPS,
    CADENCE_MIN_GAPS,
    CADENCE_SLOW_INTERVALS,
    COMMAND_DEFAULTS,
)


class Reporting:
    """Keep numeric changes and the union of observed heating/cooling intervals.

    Frozen status latches until a value change. Unknown states and repeated reports
    do not reset the clock. Downtime is never counted as observed HVAC runtime.
    """

    def __init__(self, saved=None):
        saved = saved or {}
        self.sensors = saved.get("sensors", {})
        self.intervals = saved.get("intervals", [])
        self.actions = {}
        self.observed_at = None
        self.cadence = ReportCadence(saved.get("cadence"))
        self.checks = CommandChecks(saved.get("checks"))

    def activity(self, now, actions, window):
        if self.observed_at is not None and any(self.actions.values()):
            self.add_interval(self.observed_at, now)
        self.observed_at = now
        self.actions.update(actions)
        self.intervals = [
            [max(start, now - window), end]
            for start, end in self.intervals
            if end > now - window
        ]

    def add_interval(self, start, end):
        if end <= start:
            return
        intervals = sorted([*self.intervals, [start, end]])
        self.intervals = []
        for start, end in intervals:
            if self.intervals and start <= self.intervals[-1][1]:
                self.intervals[-1][1] = max(end, self.intervals[-1][1])
            else:
                self.intervals.append([start, end])

    def observe(self, sensor, value, now):
        if value is None:
            return
        previous = self.sensors.get(sensor)
        if previous is None or previous["value"] != value:
            self.sensors[sensor] = {"value": value, "changed": now, "frozen": False}

    def frozen(self, sensor, now, window):
        if not (state := self.sensors.get(sensor)):
            return False
        start = max(state["changed"], now - window)
        runtime = sum(max(0, end - max(begin, start)) for begin, end in self.intervals)
        if now - state["changed"] >= window and runtime >= 3600:
            state["frozen"] = True
        return state["frozen"]

    def to_dict(self):
        return {
            "sensors": self.sensors,
            "intervals": self.intervals,
            "cadence": self.cadence.to_dict(),
            "checks": self.checks.to_dict(),
        }


class ReportCadence:
    """Per-system gaps between successive device reports (v0.5.23).

    A report time is the device's own fetch timestamp when one is configured,
    else the climate entity's `last_reported`. Only a later time adds a gap, so
    repeated writes of one fetch (command echoes) never shorten the cadence.
    """

    def __init__(self, saved=None):
        self.systems = {
            key: {"last": value.get("last"), "gaps": list(value.get("gaps", []))}
            for key, value in (saved or {}).items()
        }

    def observe(self, key, reported):
        if reported is None:
            return
        state = self.systems.setdefault(key, {"last": None, "gaps": []})
        last = state["last"]
        if last is not None and reported > last:
            state["gaps"] = [*state["gaps"], reported - last][-CADENCE_GAPS:]
        if last is None or reported > last:
            state["last"] = reported

    def status(self, key, trust, interval_minutes):
        """(slow, reason): forced by `trust`, else the median gap decides.

        Fewer than CADENCE_MIN_GAPS gaps count as slow: trusting an unknown
        cloud device is what cost two days of learning in November 2026.
        """
        gaps = self.systems.get(key, {}).get("gaps", [])
        reason = {"mode": trust, "gaps": len(gaps)}
        if gaps:
            reason["median_gap_s"] = round(median(gaps))
        if trust in ("slow", "fresh"):
            return trust == "slow", {**reason, "mode": f"forced {trust}"}
        if len(gaps) < CADENCE_MIN_GAPS:
            return True, {**reason, "bootstrap": True}
        return median(gaps) > CADENCE_SLOW_INTERVALS * interval_minutes * 60, reason

    def to_dict(self):
        return self.systems


class CommandChecks:
    """Fresh reports cross-checking command labels; suspends contradicted labels.

    Each distinct fresh report is one check. Above the suspend rate over the
    last checks (with a minimum count) command labels are suspended until the
    rate falls below the clear rate.
    """

    def __init__(self, saved=None):
        self.systems = {
            key: {
                "results": list(value.get("results", [])),
                "suspended": bool(value.get("suspended", False)),
                "checked": value.get("checked"),
            }
            for key, value in (saved or {}).items()
        }

    def check(self, key, reported, contradicted, settings=None):
        """Record one cross-check of the report at `reported`; once per report."""
        settings = {**COMMAND_DEFAULTS, **(settings or {})}
        state = self.systems.setdefault(
            key, {"results": [], "suspended": False, "checked": None}
        )
        if state["checked"] is not None and reported <= state["checked"]:
            return
        state["checked"] = reported
        state["results"] = [*state["results"], bool(contradicted)][
            -settings["command_checks"] :
        ]
        rate = self.rate(key)
        if (
            len(state["results"]) >= settings["command_min_checks"]
            and rate > settings["command_suspend_rate"]
        ):
            state["suspended"] = True
        elif rate < settings["command_clear_rate"]:
            state["suspended"] = False

    def rate(self, key):
        results = self.systems.get(key, {}).get("results", [])
        return sum(results) / len(results) if results else 0.0

    def suspended(self, key):
        return self.systems.get(key, {}).get("suspended", False)

    def status(self, key):
        results = self.systems.get(key, {}).get("results", [])
        return {
            "checks": len(results),
            "rate": round(self.rate(key), 3),
            "suspended": self.suspended(key),
        }

    def to_dict(self):
        return self.systems

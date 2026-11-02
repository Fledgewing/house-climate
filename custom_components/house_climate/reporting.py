"""Value-change and actual HVAC activity tracking, independent of HA metadata."""


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
        return {"sensors": self.sensors, "intervals": self.intervals}

"""Bounded time-weighted room filtering, independent of sensor/device type."""


class SensorFilter:
    """Hold fast excursions pending return; retain only the rolling window.

    Return means within max_rate degrees of the pre-excursion value (one
    minute's allowed movement), or crossing it. Unconfirmed steps are admitted
    after the window expires. Pending intervals never train the thermal model.
    """

    def __init__(self, minutes=15, max_rate=0.5, saved=None):
        self.window = minutes * 60
        self.max_rate = max_rate
        saved = saved or {}
        self.points = [list(p) for p in saved.get("points", [])]
        self.rejected = [list(p) for p in saved.get("rejected", [])]
        self.pending = saved.get("pending")
        self.dirty = False
        self.contaminated = False

    def observe(self, value, now):
        self._expire(now)
        if self.points and now < self.points[-1][0]:
            return
        previous = self.points[-1] if self.points else None
        if self.pending and value is not None:
            start, anchor, direction = self.pending
            if (
                previous
                and previous[1] is not None
                and direction * (value - previous[1]) < 0
                and direction * (value - anchor) <= self.max_rate
            ):
                self.rejected.append([start, now])
                self.pending = None
                self.dirty = True
                self.contaminated = True
        elif previous and value is not None and previous[1] is not None:
            elapsed = now - previous[0]
            delta = value - previous[1]
            if elapsed > 0 and abs(delta) * 60 > self.max_rate * elapsed:
                self.pending = [now, previous[1], 1 if delta > 0 else -1]
                self.contaminated = True
        self.points.append([now, value])
        self._prune(now)

    def _expire(self, now):
        if self.pending and now - self.pending[0] >= self.window:
            self.pending = None

    def _prune(self, now):
        cutoff = now - self.window
        while len(self.points) > 1 and self.points[1][0] <= cutoff:
            self.points.pop(0)
        self.rejected = [p for p in self.rejected if p[1] > cutoff]

    def value(self, now):
        self._expire(now)
        self._prune(now)
        weights = {}
        excluded = self.rejected + ([[self.pending[0], now]] if self.pending else [])
        for index, (start, value) in enumerate(self.points):
            end = self.points[index + 1][0] if index + 1 < len(self.points) else now
            start, end = max(start, now - self.window), min(end, now)
            duration = max(0, end - start)
            for left, right in excluded:
                duration -= max(0, min(end, right) - max(start, left))
            if value is not None and duration > 0:
                weights[value] = weights.get(value, 0) + duration
        if not weights:
            # At startup there is no elapsed duration yet. During a pending
            # excursion hold its anchor if the entire window is quarantined.
            return (
                self.pending[1]
                if self.pending
                else (self.points[-1][1] if self.points else None)
            )
        halfway = sum(weights.values()) / 2
        cumulative = 0
        for value, weight in sorted(weights.items()):
            cumulative += weight
            if cumulative >= halfway:
                return value

    @property
    def blocked(self):
        return self.dirty or self.pending is not None

    def finish_interval(self):
        self.dirty = False
        self.contaminated = False

    def to_dict(self):
        return {
            "points": self.points,
            "rejected": self.rejected,
            "pending": self.pending,
        }

"""Bounded time-weighted room filtering, independent of sensor/device type."""

from statistics import median


class SensorFilter:
    """Hold fast excursions pending return; retain only the rolling window.

    Four robust jitter scales distinguish excursions from ordinary report noise.
    Control holds excursions until return or expiry; learning retains their exact
    spans separately, even after the control quarantine expires.
    """

    def __init__(self, minutes=15, max_rate=0.5, saved=None, history_minutes=None):
        self.window = minutes * 60
        self.retention = max(minutes, history_minutes or minutes) * 60
        self.max_rate = max_rate
        saved = saved or {}
        self.points = [list(p) for p in saved.get("points", [])]
        self.rejected = [list(p) for p in saved.get("rejected", [])]
        self.pending = saved.get("pending")
        self.jitter = saved.get("jitter", 0.4)
        self.changes = saved.get("changes", [])
        self.excursions = [list(p) for p in saved.get("excursions", [])]
        if "excursions" not in saved:
            self.excursions = [[a, b, 0, 0] for a, b in self.rejected]
            if self.pending:
                a, anchor, direction = self.pending
                self.excursions.append([a, None, anchor, direction])
        self.dirty = False
        self.contaminated = False

    def observe(self, value, now):
        self._expire(now)
        if self.points and now < self.points[-1][0]:
            return
        previous = self.points[-1] if self.points else None
        limit = max(self.max_rate, 4 * self.jitter)
        returned = False
        if self.excursions and self.excursions[-1][1] is None and value is not None:
            excursion = self.excursions[-1]
            if excursion[3] * (value - excursion[2]) <= limit:
                excursion[1] = now
                returned = True
            elif now - excursion[0] >= self.retention:
                excursion[1] = now
        if (
            previous
            and previous[1] is not None
            and value is not None
            and not self.pending
        ):
            change = value - previous[1]
            # Robust absolute short-term deviations about the rolling median.
            # Clip influence and adapt only 2% per report, never during excursions.
            if now - previous[0] <= 300 and abs(change) <= limit:
                levels = [v for t, v in self.points if now - t <= 600 and v is not None]
                center = median([*levels, value])
                self.changes = (self.changes + [abs(value - center)])[-31:]
                scale = max(0.4, median(self.changes))
                self.jitter += 0.02 * (scale - self.jitter)
        if self.pending and value is not None:
            start, anchor, direction = self.pending
            if (
                previous
                and previous[1] is not None
                and direction * (value - previous[1]) < 0
                and direction * (value - anchor) <= limit
            ):
                self.rejected.append([start, now])
                self.pending = None
                self.dirty = True
                self.contaminated = True
        elif (
            not returned and previous and value is not None and previous[1] is not None
        ):
            # Report timing is not measurement timing: tiny quantized steps can
            # arrive seconds apart. Allow one minute of movement, but compare
            # against a minute-old anchor so many small steps cannot hide a jump.
            anchor = self.points[0]
            for point in reversed(self.points):
                anchor = point
                if now - point[0] >= 60:
                    break
            if anchor[1] is None:
                anchor = previous
            elapsed = max(60, now - anchor[0])
            delta = value - anchor[1]
            if elapsed > 0 and abs(delta) > max(
                limit, self.max_rate * min(elapsed, 120) / 60
            ):
                self.pending = [now, anchor[1], 1 if delta > 0 else -1]
                if not self.excursions or self.excursions[-1][1] is not None:
                    self.excursions.append([now, None, anchor[1], self.pending[2]])
                self.contaminated = True
        self.points.append([now, value])
        self._prune(now)

    def _expire(self, now):
        if self.excursions and self.excursions[-1][1] is None:
            excursion = self.excursions[-1]
            if now - excursion[0] >= self.retention:
                excursion[1] = excursion[0] + self.retention
        if self.pending and now - self.pending[0] >= self.window:
            self.pending = None

    def _prune(self, now):
        cutoff = now - self.retention
        while len(self.points) > 1 and self.points[1][0] <= cutoff:
            self.points.pop(0)
        self.rejected = [p for p in self.rejected if p[1] > now - self.window]
        self.excursions = [p for p in self.excursions if p[1] is None or p[1] > cutoff]

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

    def exclusions(self, now):
        """Learning masks survive control quarantine expiry and interval resets."""
        return [(a, now if b is None else b) for a, b, _, _ in self.excursions]

    def mean(self, start, end):
        """Time-weighted last value, excluding only the excursion's own span."""
        total = weight = 0.0
        if not self.points or self.points[0][0] > start:
            return None
        for index, (left, value) in enumerate(self.points):
            right = self.points[index + 1][0] if index + 1 < len(self.points) else end
            left, right = max(left, start), min(right, end)
            seconds = max(0, right - left)
            for a, b in self.exclusions(end):
                seconds -= max(0, min(right, b) - max(left, a))
            if value is None and seconds > 0:
                return None
            if seconds > 0:
                total += value * seconds
                weight += seconds
        return total / weight if weight > 0 else None

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
            "jitter": self.jitter,
            "changes": self.changes,
            "excursions": self.excursions,
        }

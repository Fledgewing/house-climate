"""Online interval thermal models and runtime integration; no HA dependencies."""

from dataclasses import asdict, dataclass
from math import isfinite, sqrt

PRIOR_B = 0.03


@dataclass
class Row:
    theta: list[float]
    covariance: list[list[float]]
    counts: list[float]
    samples: int = 0
    squared_error: float = 0


class Learning:
    """RLS on [outdoor - room, signed efforts..., source fractions..., solar, 1].

    Coefficients describe one configured control interval. Covariance 100 gives
    weak, identical regularization for all coefficients; only b has nonzero mean.
    Counts forget on every valid row sample, including idle intervals.
    """

    def __init__(
        self,
        sensors,
        systems,
        forgetting=0.998,
        n0=24,
        sources=(),
        disturbance_sigma=3,
        disturbance_intervals=2,
        disturbance_clear_intervals=3,
    ):
        if not 0 < forgetting <= 1 or n0 <= 0:
            raise ValueError("Invalid learning parameters")
        self.systems = list(systems)
        self.sources = list(sources)
        self.disturbance_sigma = disturbance_sigma
        self.disturbance_intervals = disturbance_intervals
        self.disturbance_clear_intervals = disturbance_clear_intervals
        self.residuals = {}
        self.streaks = {}
        self.disturbed = {}
        self.forgetting = forgetting
        self.n0 = n0
        self.rows = {}
        self.reconcile(sensors, systems, sources)

    def reconcile(self, sensors, systems, sources=()):
        """Preserve matching rows/columns, start new members at neutral priors."""
        systems = list(systems)
        sources = list(sources)
        previous = [("system", s) for s in self.systems] + [
            ("source", s) for s in self.sources
        ]
        inputs = [("system", s) for s in systems] + [("source", s) for s in sources]
        size = len(inputs) + 3
        mapping = [
            0,
            *[previous.index(s) + 1 if s in previous else None for s in inputs],
            len(previous) + 1,
            len(previous) + 2,
        ]
        rows = {}
        for sensor in sensors:
            row = Row(
                [0, *([PRIOR_B] * len(systems)), *([0.0] * len(sources)), 0, 0],
                [[100.0 if i == j else 0.0 for j in range(size)] for i in range(size)],
                [0.0] * len(inputs),
            )
            if old := self.rows.get(sensor):
                for i, source in enumerate(mapping):
                    if source is not None:
                        row.theta[i] = old.theta[source]
                        for j, other in enumerate(mapping):
                            if other is not None:
                                row.covariance[i][j] = old.covariance[source][other]
                row.counts = [
                    old.counts[previous.index(s)] if s in previous else 0
                    for s in inputs
                ]
                row.samples, row.squared_error = old.samples, old.squared_error
            rows[sensor] = row
        self.systems, self.sources, self.rows = systems, sources, rows
        for evidence in (self.residuals, self.streaks, self.disturbed):
            for sensor in list(evidence):
                if sensor not in rows:
                    del evidence[sensor]

    def update(
        self, before, after, outdoor, efforts, solar=0, duration=1, sources=None
    ):
        """Fit available rows; duration is elapsed/configured interval.

        Missing context/effort invalidates the interval, missing temperatures only
        invalidate their row. Scaling regressors supports slightly delayed ticks.
        """
        sources = sources or {}
        activity = [efforts.get(s) for s in self.systems] + [
            sources.get(s) for s in self.sources
        ]
        context = [outdoor, solar, duration, *activity]
        if any(v is None or not isfinite(v) for v in context) or duration <= 0:
            self.streaks.clear()
            return
        for sensor, row in self.rows.items():
            start, end = before.get(sensor), after.get(sensor)
            if start is None or end is None or not isfinite(start) or not isfinite(end):
                self.streaks.pop(sensor, None)
                continue
            x = [
                duration * v
                for v in [
                    outdoor - start,
                    *activity,
                    solar,
                    1,
                ]
            ]
            error = end - start - sum(a * b for a, b in zip(row.theta, x))
            self.residuals[sensor] = error / duration
            limit = max(0.3, self.disturbance_sigma * self.fit_error(sensor))
            if self.disturbance_sigma > 0:
                direction = (
                    (1 if error > 0 else -1) if abs(error) > limit * duration else 0
                )
                # Confidence describes runtime evidence, not sample count alone.
                confident = self.sensor_confidence(sensor) >= 0.5 and all(
                    row.counts[len(self.systems) + k] >= self.n0
                    for k, source in enumerate(self.sources)
                    if sources[source] > 0
                )
                previous, count, clear = self.streaks.get(sensor, (0, 0, 0))
                count = (
                    count + 1
                    if direction and direction == previous
                    else int(bool(direction))
                )
                clear = clear + 1 if not direction else 0
                self.streaks[sensor] = (direction, count, clear)
                if not confident:
                    self.disturbed.pop(sensor, None)
                    self.streaks.pop(sensor, None)
                elif direction and count >= self.disturbance_intervals:
                    self.disturbed[sensor] = "heat" if direction > 0 else "cooling"
                elif clear >= self.disturbance_clear_intervals:
                    self.disturbed.pop(sensor, None)
                if sensor in self.disturbed:
                    continue
                error = max(-limit * duration, min(limit * duration, error))
            p = row.covariance
            px = [sum(a * b for a, b in zip(line, x)) for line in p]
            denominator = self.forgetting + sum(a * b for a, b in zip(x, px))
            gain = [v / denominator for v in px]
            row.theta = [v + g * error for v, g in zip(row.theta, gain)]
            # Symmetric rank-one update preserves covariance symmetry.
            row.covariance = [
                [
                    (v - px[i] * px[j] / denominator) / self.forgetting
                    for j, v in enumerate(line)
                ]
                for i, line in enumerate(p)
            ]
            row.counts = [
                self.forgetting * n + (abs(value) > 1e-9)
                for value, n in zip(activity, row.counts)
            ]
            row.squared_error = (
                self.forgetting * row.squared_error + (error / duration) ** 2
            )
            row.samples += 1

    def fit_error(self, sensor):
        row = self.rows[sensor]
        weight = (
            (1 - self.forgetting**row.samples) / (1 - self.forgetting)
            if self.forgetting < 1
            else row.samples
        )
        return sqrt(row.squared_error / weight) if weight else 0

    def sensor_confidence(self, sensor):
        values = [self.confidence(sensor, s) for s in self.systems]
        return sum(values) / len(values) if values else 0

    def confidence(self, sensor, system):
        n = self.rows[sensor].counts[self.systems.index(system)]
        return n / (n + self.n0)

    def system_confidence(self, system, sensors=None):
        return min(
            (
                self.confidence(s, system)
                for s in (self.rows if sensors is None else sensors)
            ),
            default=0,
        )

    @property
    def confidence_percent(self):
        pairs = [self.confidence(s, j) for s in self.rows for j in self.systems]
        return 100 * sum(pairs) / len(pairs) if pairs else 0

    def system_prior(self, system):
        """Unobserved rooms inherit this system's measured reach, not weak reach."""
        index = self.systems.index(system) + 1
        pairs = [
            (self.confidence(s, system), row.theta[index])
            for s, row in self.rows.items()
            if self.confidence(s, system) > 0
        ]
        total = sum(c for c, _ in pairs)
        return sum(c * b for c, b in pairs) / total if total else PRIOR_B

    def blended(self, sensor, system):
        confidence = self.confidence(sensor, system)
        b = self.rows[sensor].theta[self.systems.index(system) + 1]
        return confidence * b + (1 - confidence) * self.system_prior(system)

    def influences(self, system, sensors=None):
        positive = {
            s: max(0, self.blended(s, system))
            for s in (self.rows if sensors is None else sensors)
        }
        total = sum(positive.values())
        return {s: b / total for s, b in positive.items()} if total > 1e-9 else {}

    def dither(self, system, now, sensors=None, threshold=0.8):
        confidence = self.system_confidence(system, sensors)
        if confidence >= threshold:
            return 0
        phase = (now / 7200 + self.systems.index(system) / len(self.systems)) % 1
        return (0.5 if phase < 0.5 else -0.5) * (1 - confidence / threshold)

    def diagnostics(self):
        return {
            "systems": {
                j: {
                    s: {
                        "b": row.theta[k + 1],
                        "blended_b": self.blended(s, j),
                        "influence": self.influences(j).get(s, 0),
                        "confidence": 100 * self.confidence(s, j),
                        "effective_samples": row.counts[k],
                    }
                    for s, row in self.rows.items()
                }
                for k, j in enumerate(self.systems)
            },
            "sources": {
                source: {
                    sensor: {
                        "e": row.theta[1 + len(self.systems) + k],
                        "confidence": 100
                        * row.counts[len(self.systems) + k]
                        / (row.counts[len(self.systems) + k] + self.n0),
                        "effective_samples": row.counts[len(self.systems) + k],
                    }
                    for sensor, row in self.rows.items()
                }
                for k, source in enumerate(self.sources)
            },
            "disturbed": dict(self.disturbed),
            "sensors": {
                s: {
                    "a": row.theta[0],
                    "c": row.theta[-2],
                    "d": row.theta[-1],
                    "fit_error": self.fit_error(s) if row.samples else None,
                    "samples": row.samples,
                    "residual": self.residuals.get(s),
                    "disturbed": self.disturbed.get(s),
                    "confidence": 100 * self.sensor_confidence(s),
                }
                for s, row in self.rows.items()
            },
        }

    def to_dict(self):
        return {
            "systems": self.systems,
            "sources": self.sources,
            "disturbance_sigma": self.disturbance_sigma,
            "disturbance_intervals": self.disturbance_intervals,
            "disturbance_clear_intervals": self.disturbance_clear_intervals,
            "forgetting": self.forgetting,
            "n0": self.n0,
            "rows": {s: asdict(row) for s, row in self.rows.items()},
        }

    @classmethod
    def from_dict(cls, saved):
        model = cls(
            saved["rows"],
            saved["systems"],
            saved["forgetting"],
            saved["n0"],
            saved.get("sources", ()),
            saved.get("disturbance_sigma", 3),
            saved.get("disturbance_intervals", 2),
            saved.get("disturbance_clear_intervals", 3),
        )
        model.rows = {s: Row(**row) for s, row in saved["rows"].items()}
        return model


class EffortTracker:
    """Integrate action transitions, including changes between controller passes.

    Unknown/unavailable action makes that system's whole interval missing.
    A restart deliberately starts a new interval rather than guessing downtime.
    """

    def __init__(self):
        self.actions = {}
        self.times = {}
        self.totals = {}
        self.missing = set()
        self.started = None

    def change(self, system, action, now):
        if system in self.times:
            elapsed = max(0, now - self.times[system])
            previous = self.actions[system]
            if previous is None and elapsed > 0:
                self.missing.add(system)
            self.totals[system] = self.totals.get(system, 0) + (previous or 0) * elapsed
        self.actions[system] = {
            "heating": 1,
            "cooling": -1,
            "idle": 0,
            "off": 0,
            "fan": 0,
            "drying": 0,
        }.get(action)
        self.times[system] = now

    def finish(self, actions, now):
        for system, action in actions.items():
            self.change(system, action, now)
        duration = now - self.started if self.started is not None else 0
        result = {
            s: self.totals.get(s, 0) / duration
            if duration > 0 and s not in self.missing
            else None
            for s in actions
        }
        self.started = now
        self.totals = {}
        self.missing.clear()
        return result, duration

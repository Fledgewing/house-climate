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
    """RLS on [outdoor - room, signed efforts..., solar kW, constant].

    Coefficients describe one configured control interval. Covariance 100 gives
    weak, identical regularization for all coefficients; only b has nonzero mean.
    Counts forget on every valid row sample, including idle intervals.
    """

    def __init__(self, sensors, systems, forgetting=0.998, n0=24):
        if not 0 < forgetting <= 1 or n0 <= 0:
            raise ValueError("Invalid learning parameters")
        self.systems = list(systems)
        self.forgetting = forgetting
        self.n0 = n0
        self.rows = {}
        self.reconcile(sensors, systems)

    def reconcile(self, sensors, systems):
        """Preserve matching rows/columns, start new members at neutral priors."""
        systems = list(systems)
        previous = self.systems
        size = len(systems) + 3
        mapping = [
            0,
            *[previous.index(s) + 1 if s in previous else None for s in systems],
            len(previous) + 1,
            len(previous) + 2,
        ]
        rows = {}
        for sensor in sensors:
            row = Row(
                [0, *([PRIOR_B] * len(systems)), 0, 0],
                [[100.0 if i == j else 0.0 for j in range(size)] for i in range(size)],
                [0.0] * len(systems),
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
                    for s in systems
                ]
                row.samples, row.squared_error = old.samples, old.squared_error
            rows[sensor] = row
        self.systems, self.rows = systems, rows

    def update(self, before, after, outdoor, efforts, solar=0, duration=1):
        """Fit available rows; duration is elapsed/configured interval.

        Missing context/effort invalidates the interval, missing temperatures only
        invalidate their row. Scaling regressors supports slightly delayed ticks.
        """
        context = [outdoor, solar, duration, *[efforts.get(s) for s in self.systems]]
        if any(v is None or not isfinite(v) for v in context) or duration <= 0:
            return
        for sensor, row in self.rows.items():
            start, end = before.get(sensor), after.get(sensor)
            if start is None or end is None or not isfinite(start) or not isfinite(end):
                continue
            x = [
                duration * v
                for v in [
                    outdoor - start,
                    *[efforts[s] for s in self.systems],
                    solar,
                    1,
                ]
            ]
            p = row.covariance
            px = [sum(a * b for a, b in zip(line, x)) for line in p]
            denominator = self.forgetting + sum(a * b for a, b in zip(x, px))
            gain = [v / denominator for v in px]
            error = end - start - sum(a * b for a, b in zip(row.theta, x))
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
                self.forgetting * n + (abs(efforts[s]) > 1e-9)
                for s, n in zip(self.systems, row.counts)
            ]
            row.squared_error = self.forgetting * row.squared_error + error * error
            row.samples += 1

    def confidence(self, sensor, system):
        n = self.rows[sensor].counts[self.systems.index(system)]
        return n / (n + self.n0)

    def system_confidence(self, system):
        return min((self.confidence(s, system) for s in self.rows), default=0)

    @property
    def confidence_percent(self):
        pairs = [self.confidence(s, j) for s in self.rows for j in self.systems]
        return 100 * sum(pairs) / len(pairs) if pairs else 0

    def blended(self, sensor, system):
        confidence = self.confidence(sensor, system)
        b = self.rows[sensor].theta[self.systems.index(system) + 1]
        return confidence * b + (1 - confidence) * PRIOR_B

    def influences(self, system):
        positive = {s: max(0, self.blended(s, system)) for s in self.rows}
        total = sum(positive.values())
        return {s: b / total for s, b in positive.items()} if total > 1e-9 else {}

    def dither(self, system, now):
        confidence = self.system_confidence(system)
        if confidence >= 0.8:
            return 0
        phase = (now / 7200 + self.systems.index(system) / len(self.systems)) % 1
        return (0.5 if phase < 0.5 else -0.5) * (1 - confidence / 0.8)

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
            "sensors": {
                s: {
                    "a": row.theta[0],
                    "c": row.theta[-2],
                    "d": row.theta[-1],
                    "fit_error": sqrt(
                        row.squared_error
                        / (
                            (1 - self.forgetting**row.samples) / (1 - self.forgetting)
                            if self.forgetting < 1
                            else row.samples
                        )
                    )
                    if row.samples
                    else None,
                    "samples": row.samples,
                }
                for s, row in self.rows.items()
            },
        }

    def to_dict(self):
        return {
            "systems": self.systems,
            "forgetting": self.forgetting,
            "n0": self.n0,
            "rows": {s: asdict(row) for s, row in self.rows.items()},
        }

    @classmethod
    def from_dict(cls, saved):
        model = cls(saved["rows"], saved["systems"], saved["forgetting"], saved["n0"])
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

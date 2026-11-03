"""Online interval thermal models and runtime integration; no HA dependencies."""

from copy import deepcopy
from dataclasses import asdict, dataclass
from math import ceil, exp, isfinite, sqrt

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
        regime_margin=0.3,
        regime_hours=24,
        regime_return_hours=4,
        interval_minutes=5,
    ):
        if not 0 < forgetting <= 1 or n0 <= 0:
            raise ValueError("Invalid learning parameters")
        self.regime_margin = regime_margin
        self.regime_hours = regime_hours
        self.regime_return_hours = regime_return_hours
        self.interval_minutes = interval_minutes
        self.clock = 0
        self.regimes = {}
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

        def remap(old=None):
            row = Row(
                [0, *([PRIOR_B] * len(systems)), *([0.0] * len(sources)), 0, 0],
                [[100.0 if i == j else 0.0 for j in range(size)] for i in range(size)],
                [0.0] * len(inputs),
            )
            if old:
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
            return row

        for sensor in sensors:
            row = remap(self.rows.get(sensor))
            if sensor in self.regimes:
                state = self.regimes[sensor]
                state["challenger"] = remap(state["challenger"])
                for saved in state["saved"]:
                    saved["row"] = remap(saved["row"])
                if previous != inputs:
                    state["blocks"] = []
            else:
                self.regimes[sensor] = {
                    "challenger": remap(),
                    "saved": [],
                    "blocks": [],
                    "active": "main",
                    "serial": 0,
                    "temporary": False,
                    "temporary_until": None,
                    "disturbed_since": None,
                }
            rows[sensor] = row
        self.systems, self.sources, self.rows = systems, sources, rows
        for evidence in (self.residuals, self.streaks, self.disturbed, self.regimes):
            for sensor in list(evidence):
                if sensor not in rows:
                    del evidence[sensor]

    def update(
        self,
        before,
        after,
        outdoor,
        efforts,
        solar=0,
        duration=1,
        sources=None,
        now=None,
        local_hour=None,
    ):
        """Fit available rows; duration is elapsed/configured interval.

        Missing context/effort invalidates the interval, missing temperatures only
        invalidate their row. Scaling regressors supports slightly delayed ticks.
        """
        timestamp = (
            self.clock + duration * self.interval_minutes * 60 if now is None else now
        )
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
            self.clock = timestamp
            state = self.regimes[sensor]
            if (
                state["temporary"]
                and state["temporary_until"] is not None
                and self.clock >= state["temporary_until"]
            ):
                self.set_temporary(sensor, False)
                continue
            if state["temporary"]:
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
            self.score_regimes(sensor, x, end - start, activity, duration, local_hour)
            challenger = state["challenger"]
            innovation = end - start - sum(a * b for a, b in zip(challenger.theta, x))
            # A repeated large miss signals lost confidence in the fast fit.
            # Retain its coefficients, but let new observations identify a jump.
            surprise = abs(innovation / duration) > max(0.1, 6 * self.fit_error(sensor))
            state["surprises"] = state.get("surprises", 0) + 1 if surprise else 0
            if state["surprises"] >= 2 and challenger.samples >= 24:
                challenger.covariance = self.neutral_row().covariance
                state["surprises"] = 0
                state["identified_since"] = self.clock
            self.fit_row(
                challenger,
                x,
                innovation,
                activity,
                duration,
                exp(-duration * self.interval_minutes / 1440),
            )
            if self.choose_regime(sensor):
                continue
            self.residuals[sensor] = error / duration
            # Better-established models can detect smaller persistent changes,
            # preserving a useful original for later returns without an age gate.
            floor = max(0.1, 0.3 * (1 - self.sensor_confidence(sensor)))
            if any(
                sources[source] > 0 and row.counts[len(self.systems) + k] < self.n0
                for k, source in enumerate(self.sources)
            ):
                floor = 0.3
            limit = max(floor, self.disturbance_sigma * self.fit_error(sensor))
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
                    if state["disturbed_since"] is None:
                        state["disturbed_since"] = self.clock
                    continue
                state["disturbed_since"] = None
                error = max(-limit * duration, min(limit * duration, error))
            self.fit_row(row, x, error, activity, duration, self.forgetting)

    @staticmethod
    def fit_row(row, x, error, activity, duration, forgetting):
        p = row.covariance
        px = [sum(a * b for a, b in zip(line, x)) for line in p]
        denominator = forgetting + sum(a * b for a, b in zip(x, px))
        gain = [v / denominator for v in px]
        row.theta = [v + g * error for v, g in zip(row.theta, gain)]
        # Symmetric rank-one update preserves covariance symmetry.
        row.covariance = [
            [(v - px[i] * px[j] / denominator) / forgetting for j, v in enumerate(line)]
            for i, line in enumerate(p)
        ]
        row.counts = [
            forgetting * n + (abs(value) > 1e-9)
            for value, n in zip(activity, row.counts)
        ]
        row.squared_error = forgetting * row.squared_error + (error / duration) ** 2
        row.samples += 1

    def score_regimes(self, sensor, x, target, activity, duration, local_hour):
        """Pre-update absolute errors, in fixed two-hour observed-time blocks."""
        state = self.regimes[sensor]
        candidates = {"active": self.rows[sensor], "challenger": state["challenger"]}
        candidates.update({saved["id"]: saved["row"] for saved in state["saved"]})
        errors = {
            key: abs(target - sum(a * b for a, b in zip(row.theta, x))) / duration
            for key, row in candidates.items()
        }
        seconds = duration * self.interval_minutes * 60
        interval_start = self.clock - seconds
        first = int(interval_start // 7200)
        last = int((self.clock - 1e-6) // 7200)
        # Allocate an interval that crosses a boundary to both blocks. Keep only
        # the bounded evidence horizon even after unusually delayed observations.
        horizon = ceil(max(self.regime_hours, self.regime_return_hours) / 2) + 1
        blocks = state["blocks"]
        for bucket in range(max(first, last - horizon), last + 1):
            start = max(interval_start, bucket * 7200)
            end = min(self.clock, (bucket + 1) * 7200)
            weight = end - start
            if not blocks or blocks[-1]["bucket"] != bucket:
                blocks.append(
                    {"bucket": bucket, "seconds": 0, "errors": {}, "contexts": []}
                )
            block = blocks[-1]
            hour = (self.clock / 3600) % 24 if local_hour is None else local_hour
            hour = (hour - (self.clock - (start + end) / 2) / 3600) % 24
            contexts = ["day" if 6 <= hour < 18 else "night"]
            contexts += [
                f"{system}:{int(abs(value) > 1e-9)}"
                for system, value in zip(self.systems, activity)
            ]
            block["contexts"] = sorted(set(block["contexts"]) | set(contexts))
            block["seconds"] += weight
            for key, error in errors.items():
                block["errors"][key] = block["errors"].get(key, 0) + error * weight
        if errors["active"] <= max(0.01, min(errors.values()) * 1.1):
            state["active_last_fit"] = self.clock
        for saved in state["saved"]:
            if errors[saved["id"]] <= max(0.01, min(errors.values()) * 1.1):
                saved["last_fit"] = self.clock
        state["blocks"] = [b for b in blocks if b["bucket"] >= last - horizon]

    def evidence(self, sensor, candidate, hours, variety):
        state = self.regimes[sensor]
        count = max(1, ceil(hours / 2))
        current = int(self.clock // 7200)
        blocks = [
            b for b in state["blocks"] if current - count <= b["bucket"] < current
        ]
        if len(blocks) != count or any(b["seconds"] < 0.9 * 7200 for b in blocks):
            return False
        wins = [
            b
            for b in blocks
            if b["errors"].get(candidate, float("inf"))
            < (1 - self.regime_margin) * b["errors"]["active"]
            and b["errors"]["active"] > 1e-5 * b["seconds"]
        ]
        if len(wins) < 0.8 * count:
            return False
        if variety:
            contexts = set().union(*(set(b["contexts"]) for b in wins))
            required = {"day", "night"} | {
                f"{s}:{running}" for s in self.systems for running in (0, 1)
            }
            if not required <= contexts:
                return False
            for context in required:
                relevant = [b for b in blocks if context in b["contexts"]]
                if sum(b in wins for b in relevant) < 0.8 * len(relevant):
                    return False
        return True

    def choose_regime(self, sensor):
        state = self.regimes[sensor]
        for saved in sorted(state["saved"], key=lambda s: s["last_fit"], reverse=True):
            if self.evidence(sensor, saved["id"], self.regime_return_hours, False):
                self.promote(sensor, saved["id"])
                return True
        stable = (
            self.clock - state.get("identified_since", 0) >= self.regime_hours * 3600
        )
        if stable and self.evidence(sensor, "challenger", self.regime_hours, True):
            self.promote(sensor)
            return True
        return False

    def neutral_row(self):
        size = len(self.systems) + len(self.sources) + 3
        return Row(
            [0, *([PRIOR_B] * len(self.systems)), *([0.0] * len(self.sources)), 0, 0],
            [[100.0 if i == j else 0.0 for j in range(size)] for i in range(size)],
            [0.0] * (size - 3),
        )

    def clear_regime_evidence(self, sensor):
        state = self.regimes[sensor]
        state["blocks"] = []
        state["disturbed_since"] = None
        self.disturbed.pop(sensor, None)
        self.streaks.pop(sensor, None)
        self.residuals.pop(sensor, None)

    def promote(self, sensor, saved_id=None):
        """Archive the active row; frozen library rows never train in the background."""
        state = self.regimes[sensor]
        chosen = next((s for s in state["saved"] if s["id"] == saved_id), None)
        replacement = chosen["row"] if chosen else state["challenger"]
        if chosen:
            state["saved"].remove(chosen)
        state["serial"] += 1
        state["saved"].append(
            {
                "id": f"saved #{state['serial']}",
                "row": self.rows[sensor],
                "last_fit": state.get("active_last_fit", self.clock),
                "origin": state["active"],
            }
        )
        if len(state["saved"]) > 3:
            state["saved"].remove(min(state["saved"], key=lambda s: s["last_fit"]))
        self.rows[sensor] = replacement
        state["active_last_fit"] = chosen["last_fit"] if chosen else self.clock
        state["active"] = saved_id if chosen else f"promoted at {self.clock}"
        state["switched_at"] = self.clock
        state["challenger"] = self.neutral_row()
        state["identified_since"] = self.clock
        state["surprises"] = 0
        self.clear_regime_evidence(sensor)

    def relearn(self, sensor):
        """Explicitly reset this room while preserving its model library."""
        self.rows[sensor] = self.neutral_row()
        self.regimes[sensor]["challenger"] = self.neutral_row()
        self.regimes[sensor]["active"] = "main"
        self.regimes[sensor]["identified_since"] = self.clock
        self.regimes[sensor]["surprises"] = 0
        self.regimes[sensor].pop("active_last_fit", None)
        self.clear_regime_evidence(sensor)

    def set_temporary(self, sensor, enabled, hours=0):
        state = self.regimes[sensor]
        state["surprises"] = 0
        state["temporary"] = enabled
        state["temporary_until"] = (
            self.clock + hours * 3600 if enabled and hours else None
        )
        self.clear_regime_evidence(sensor)

    def regime_diagnostics(self, sensor):
        state = self.regimes[sensor]
        active = sum(b["errors"]["active"] for b in state["blocks"])
        challenger = sum(b["errors"]["challenger"] for b in state["blocks"])
        return {
            "active_model": state["active"],
            "saved_models": [
                {
                    "model": s["id"],
                    "last_fit": s["last_fit"],
                    "coefficients": list(s["row"].theta),
                }
                for s in state["saved"]
            ],
            "challenger_advantage": 100 * (1 - challenger / active)
            if active > 1e-12
            else 0,
            "temporary": state["temporary"],
            "temporary_until": state["temporary_until"],
            "disturbed_since": state["disturbed_since"],
        }

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

    def forecast(
        self, sensor, temperature, systems, steps, outdoor=None, solar=0, sources=None
    ):
        """Affine horizon prediction for constant efforts and current context.

        Uncertain effects retain the optimistic system-mean blend. Passive
        losses are propagated each interval, rather than extrapolated linearly.
        Missing outdoor context omits its term; missing solar/sources use zero.
        """
        row = self.rows[sensor]
        a = row.theta[0] if outdoor is not None else 0
        background = a * outdoor if outdoor is not None else 0
        background += row.theta[-2] * (solar or 0) + row.theta[-1]
        background += sum(
            row.theta[1 + len(self.systems) + k] * (sources or {}).get(source, 0)
            for k, source in enumerate(self.sources)
        )
        retention = 1 - a
        gain = sum(retention**k for k in range(steps))
        return (
            retention**steps * temperature + gain * background,
            [
                gain
                * max(
                    0,
                    self.blended(sensor, system) if system in self.systems else PRIOR_B,
                )
                for system in systems
            ],
        )

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
                    **self.regime_diagnostics(s),
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
            "regime_margin": self.regime_margin,
            "regime_hours": self.regime_hours,
            "regime_return_hours": self.regime_return_hours,
            "interval_minutes": self.interval_minutes,
            "clock": self.clock,
            "regimes": {
                s: {
                    **deepcopy(state),
                    "challenger": asdict(state["challenger"]),
                    "saved": [
                        {**saved, "row": asdict(saved["row"])}
                        for saved in state["saved"]
                    ],
                }
                for s, state in self.regimes.items()
            },
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
        for key in (
            "regime_margin",
            "regime_hours",
            "regime_return_hours",
            "interval_minutes",
            "clock",
        ):
            if key in saved:
                setattr(model, key, saved[key])
        for sensor, state in deepcopy(saved.get("regimes", {})).items():
            state["challenger"] = Row(**state["challenger"])
            for item in state["saved"]:
                item["row"] = Row(**item["row"])
            model.regimes[sensor] = state
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

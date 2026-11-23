"""Online interval thermal models and runtime integration; no HA dependencies."""

from copy import deepcopy
from dataclasses import asdict, dataclass
from math import ceil, exp, isfinite, sqrt

from .filtering import SensorFilter

PRIOR_B = 0.03
INTERACTION_PRIOR = 0.0001
LAG_PRIOR = 0.01
BACKGROUND_PRIOR = 0.0001
SOURCE_PRIOR = 0.01
# Integrated rows may treat background drift as a local level (random walk):
# per learning window its variance grows by this many RLS units. Zero keeps a
# constant background, as before v0.5.21.
DRIFT_NOISE = 0.0


@dataclass
class Row:
    theta: list[float]
    covariance: list[list[float]]
    counts: list[float]
    samples: int = 0
    squared_error: float = 0
    # v0.5.23: covariance from windows without latent labels, for confidence.
    # None until a latent window is fitted (then it equals `covariance`).
    independent: list[list[float]] | None = None


class Learning:
    """RLS on [delta, inputs..., inputs * delta..., lagged inputs..., solar, 1].

    Coefficients describe one configured control interval. Covariance 100 gives
    weak regularization for fixed effects; interactions have a tighter zero prior.
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
        interaction_prior=INTERACTION_PRIOR,
        lag_minutes=60,
        lag_prior=LAG_PRIOR,
        window_minutes=0,
        average_minutes=10,
        drift_noise=DRIFT_NOISE,
        label_delay_minutes=0,
    ):
        if not 0 < forgetting <= 1 or n0 <= 0:
            raise ValueError("Invalid learning parameters")
        if not isfinite(interaction_prior) or interaction_prior <= 0:
            raise ValueError("Invalid interaction prior")
        if not isfinite(lag_prior) or lag_prior <= 0 or lag_minutes <= 0:
            raise ValueError("Invalid lag parameters")
        if not isfinite(window_minutes) or window_minutes < 0:
            raise ValueError("Invalid learning window")
        if not isfinite(average_minutes) or average_minutes < 0:
            raise ValueError("Invalid learning average")
        if not isfinite(drift_noise) or drift_noise < 0:
            raise ValueError("Invalid background drift")
        self.drift_noise = drift_noise
        # v0.5.23: averaged windows are evaluated this much later, so late
        # (latent) labels can complete them. Zero is the pre-v0.5.23 timing.
        self.label_delay_minutes = label_delay_minutes
        self.lag_minutes = lag_minutes
        self.window_minutes = window_minutes
        self.average_minutes = average_minutes
        self.context_history = []
        self.temperature_filters = {}
        self.windows = {}
        self.lag_prior = lag_prior
        self.effort_history = []
        self.effort_clock = 0
        self.interaction_prior = interaction_prior
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
        mapping = [
            0,
            *[previous.index(s) + 1 if s in previous else None for s in inputs],
            *[
                len(previous) + previous.index(s) + 1 if s in previous else None
                for s in inputs
            ],
            *[
                2 * len(previous) + previous.index(s) + 1 if s in previous else None
                for s in inputs
            ],
            3 * len(previous) + 1,
            3 * len(previous) + 2,
        ]
        if previous != inputs:
            removed = set(previous) - set(inputs)
            for _, _, values in self.effort_history:
                for kind, key in removed:
                    values.pop(f"{kind}:{key}", None)
                    values.pop(f"latent:{kind}:{key}", None)
        rows = {}

        def remap(old=None):
            row = self.neutral_row(systems, sources)
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
        observed_history=None,
        sensor_filters=None,
    ):
        """Fit available rows; duration is elapsed/configured interval.

        Missing context/effort invalidates the interval, missing temperatures only
        invalidate their row. Scaling regressors supports slightly delayed ticks.
        """
        timestamp = (
            self.effort_clock + duration * self.interval_minutes * 60
            if now is None
            else now
        )
        sources = sources or {}
        activity = [efforts.get(s) for s in self.systems] + [
            sources.get(s) for s in self.sources
        ]
        start_time = timestamp - duration * self.interval_minutes * 60
        lagged = [
            self.past_effort(kind, key, start_time)
            for kind, keys in (("system", self.systems), ("source", self.sources))
            for key in keys
        ]
        self.record_efforts(efforts, sources, start_time, timestamp, observed_history)
        context = [outdoor, solar, duration, *activity]
        averaged = self.window_minutes and self.average_minutes
        sensor_filters = sensor_filters or {}
        known = [outdoor, solar, duration]
        if any(v is None or not isfinite(v) for v in known) or duration <= 0:
            self.streaks.clear()
            self.windows.clear()
            return
        if any(v is None or not isfinite(v) for v in context) and not averaged:
            self.streaks.clear()
            self.windows.clear()
            return
        if averaged:
            self.context_history.append((start_time, timestamp, outdoor, solar))
            cutoff = timestamp - (self.history_minutes - 5) * 60
            self.context_history = [v for v in self.context_history if v[1] > cutoff]
        if any(v is None or not isfinite(v) for v in context):
            # v0.5.23: a missing effort advances each averaged window instead of
            # clearing it. The gap is in `effort_history`, which `averaged_window`
            # rejects until `backfill_effort` fills it (a late latent label).
            self.streaks.clear()
            for sensor in self.rows:
                start, end = before.get(sensor), after.get(sensor)
                if (
                    start is None
                    or end is None
                    or not isfinite(start)
                    or not isfinite(end)
                ):
                    self.windows.pop(sensor, None)
                    continue
                if sensor_filters.get(sensor) is None:
                    sensor_filter = self.internal_filter(sensor)
                    if not sensor_filter.points:
                        sensor_filter.observe(start, start_time)
                    sensor_filter.observe(end, timestamp)
                window = self.windows.get(sensor)
                if window is None or abs(window["end"] - start_time) > 1e-6:
                    window = {"start": start_time}
                window["end"] = timestamp
                window["accepted"] = False
                self.windows[sensor] = window
            return
        interval_duration = duration
        interval_activity = activity
        for sensor, row in self.rows.items():
            duration = interval_duration
            activity = interval_activity
            start, end = before.get(sensor), after.get(sensor)
            if start is None or end is None or not isfinite(start) or not isfinite(end):
                self.streaks.pop(sensor, None)
                self.windows.pop(sensor, None)
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
            count_weights = None
            x = [
                duration * v
                for v in [
                    outdoor - start,
                    *activity,
                    *[value * (outdoor - start) for value in activity],
                    *[value or 0 for value in lagged],
                    solar,
                    1,
                ]
            ]
            frozen = [
                self.lag_index(k) for k, value in enumerate(lagged) if value is None
            ]
            if averaged:
                integrated = self.averaged_window(
                    sensor,
                    sensor_filters.get(sensor),
                    start_time,
                    timestamp,
                    start,
                    end,
                )
                if integrated is None:
                    continue
                x, target, activity, frozen, latent = integrated
                # A window holding any latent-labelled time adds no counts and
                # ages none (no self-confirmation): confidence stays what the
                # same replay gives with the latent spans skipped.
                count_weights = [0.0 if share > 1e-9 else 1.0 for share in latent]
                start, end, duration = 0.0, target, 1
            elif self.window_minutes:
                window = self.windows.get(sensor)
                if window is None or abs(window["end"] - start_time) > 1e-6:
                    window = {"segments": []}
                window["segments"].append((x, end - start, duration, activity, frozen))
                window["end"] = timestamp
                self.windows[sensor] = window
                segments = window["segments"]
                while (
                    sum(s[2] for s in segments[1:]) * self.interval_minutes
                    >= self.window_minutes
                ):
                    segments.pop(0)
                duration = sum(s[2] for s in segments)
                if duration * self.interval_minutes < self.window_minutes:
                    continue
                x = [sum(s[0][k] for s in segments) / duration for k in range(len(x))]
                end, start = sum(s[1] for s in segments) / duration, 0.0
                activity = [
                    sum(s[3][k] * s[2] for s in segments) / duration
                    for k in range(len(activity))
                ]
                frozen = set().union(*(s[4] for s in segments))
                duration = 1
            error = end - start - sum(a * b for a, b in zip(row.theta, x))
            evidence_weight = (
                min(1, self.interval_minutes * interval_duration / self.window_minutes)
                if self.window_minutes
                else 1
            )
            self.score_regimes(sensor, x, end - start, activity, duration, local_hour)
            challenger = state["challenger"]
            innovation = end - start - sum(a * b for a, b in zip(challenger.theta, x))
            # A repeated large miss signals lost confidence in the fast fit.
            # Retain its coefficients, but let new observations identify a jump.
            surprise = abs(innovation / duration) > max(0.1, 6 * self.fit_error(sensor))
            state["surprises"] = state.get("surprises", 0) + 1 if surprise else 0
            if state["surprises"] >= 2 and challenger.samples >= 24:
                challenger.covariance = self.neutral_row().covariance
                # Scores earned before reopening uncertainty cannot qualify the
                # newly unidentified candidate for promotion.
                state["blocks"] = []
                state["surprises"] = 0
                state["identified_since"] = self.clock
            self.fit_row(
                challenger,
                x,
                innovation,
                activity,
                duration,
                exp(-duration * self.interval_minutes / 1440),
                frozen,
                evidence_weight,
                self.background_drift,
                count_weights,
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
            self.fit_row(
                row,
                x,
                error,
                activity,
                duration,
                self.forgetting**evidence_weight,
                frozen,
                evidence_weight,
                self.background_drift,
                count_weights,
            )

    @property
    def history_minutes(self):
        """Temperature history an averaged window needs, with its label delay."""
        return self.window_minutes + self.average_minutes + self.label_delay_minutes + 5

    def internal_filter(self, sensor):
        sensor_filter = self.temperature_filters.get(sensor)
        if sensor_filter is None:
            sensor_filter = self.temperature_filters[sensor] = SensorFilter(
                history_minutes=self.history_minutes
            )
        else:
            sensor_filter.retention = max(
                sensor_filter.retention, self.history_minutes * 60
            )
        return sensor_filter

    def averaged_window(self, sensor, sensor_filter, start, end, before, after):
        """Integrate clean spans on the trailing mean's half-span delayed clock."""
        span = self.average_minutes * 60
        lag = span / 2
        width = self.window_minutes * 60
        if sensor_filter is None:
            sensor_filter = self.internal_filter(sensor)
            if not sensor_filter.points:
                sensor_filter.observe(before, start)
            sensor_filter.observe(after, end)
        window = self.windows.get(sensor)
        if window is None or abs(window["end"] - start) > 1e-6:
            window = {"start": start}
        window["end"] = end
        window["accepted"] = False
        self.windows[sensor] = window
        delay = self.label_delay_minutes * 60
        if end - window["start"] < width + span + delay:
            return None
        left, right = end - lag - delay - width, end - lag - delay
        excluded = [
            (max(a, left), min(b, right))
            for a, b in sensor_filter.exclusions(end)
            if a < right and b > left
        ]
        if sum(b - a for a, b in excluded) > width / 4:
            return None
        runs = []
        cursor = left
        for a, b in excluded:
            if a > cursor:
                runs.append((cursor, a))
            cursor = max(cursor, b)
        if cursor < right:
            runs.append((cursor, right))
        count = len(self.systems) + len(self.sources)
        x = [0.0] * (3 * count + 3)
        latent = [0.0] * count
        target = seconds = 0.0
        frozen = set()
        keys = [
            (kind, key)
            for kind, keys in (("system", self.systems), ("source", self.sources))
            for key in keys
        ]
        for a, b in runs:
            first = sensor_filter.mean(a + lag - span, a + lag)
            last = sensor_filter.mean(b + lag - span, b + lag)
            if first is None or last is None:
                return None
            target += last - first
            boundaries = sorted(
                {a, b}
                | {t for row in self.context_history for t in row[:2] if a < t < b}
                | {t for row in self.effort_history for t in row[:2] if a < t < b}
            )
            for lo, hi in zip(boundaries, boundaries[1:]):
                context = next(
                    (v for v in self.context_history if v[0] <= lo and v[1] >= hi), None
                )
                if context is None:
                    return None
                activity = [self.past_effort(kind, key, hi, lo) for kind, key in keys]
                if any(v is None for v in activity):
                    return None
                temperature = sensor_filter.mean(lo + lag - span, lo + lag)
                if temperature is None:
                    return None
                delta = context[2] - temperature
                lagged = [self.past_effort(kind, key, lo) for kind, key in keys]
                frozen.update(
                    self.lag_index(k) for k, v in enumerate(lagged) if v is None
                )
                values = [
                    delta,
                    *activity,
                    *[v * delta for v in activity],
                    *[v or 0 for v in lagged],
                    context[3],
                    1,
                ]
                for k, v in enumerate(values):
                    x[k] += v * (hi - lo)
                for k, (kind, key) in enumerate(keys):
                    latent[k] += self.latent_share(kind, key, lo, hi) * (hi - lo)
                seconds += hi - lo
        if not seconds:
            return None
        window["accepted"] = True
        # Same per-control-interval units as the unsmoothed integrated model.
        return (
            [v / seconds for v in x],
            target * self.interval_minutes * 60 / seconds,
            [v / seconds for v in x[1 : count + 1]],
            frozen,
            [v / seconds for v in latent],
        )

    @property
    def background_drift(self):
        """Background random-walk variance per learning window (integrated rows)."""
        return self.drift_noise if self.window_minutes else 0

    @staticmethod
    def fit_row(
        row,
        x,
        error,
        activity,
        duration,
        forgetting,
        frozen=(),
        evidence_weight=1,
        drift=0,
        count_weights=None,
    ):
        """RLS step; `count_weights` scale each input's count update (v0.5.23).

        A weight of 0 (a window holding latent-labelled time) neither adds to
        a count nor ages it, and leaves `independent` (the covariance used by
        confidence) untouched, so confidence comes only from windows labelled
        by independent sources. The coefficients and their covariance learn
        from every window.
        """

        def updated(p, gain=None):
            raw_px = [sum(a * b for a, b in zip(line, x)) for line in p]
            px = [0 if i in frozen else v for i, v in enumerate(raw_px)]
            denominator = forgetting + sum(a * b for a, b in zip(x, px))
            if gain is not None:
                gain.extend(v / denominator for v in px)

            def covariance(i, j, value):
                if i in frozen and j in frozen:
                    return value
                if i in frozen or j in frozen:
                    # A zero-gain (Joseph) update keeps the missing coefficient's
                    # mean/variance intact, while preserving a positive covariance.
                    return (value - raw_px[i] * raw_px[j] / denominator) / sqrt(
                        forgetting
                    )
                return (value - px[i] * px[j] / denominator) / forgetting

            result = [
                [covariance(i, j, v) for j, v in enumerate(line)]
                for i, line in enumerate(p)
            ]
            # A random-walk background tracks slow unmodeled heat or loss. When
            # the controller reacts to that drift, a constant background leaves
            # it to the effort, interaction and lag columns (closed-loop bias).
            result[-1][-1] += drift * evidence_weight
            return result

        latent = count_weights is not None and any(w < 1 for w in count_weights)
        if latent and row.independent is None:
            row.independent = deepcopy(row.covariance)
        elif not latent and row.independent is not None:
            row.independent = updated(row.independent)
        gain = []
        row.covariance = updated(row.covariance, gain)
        row.theta = [v + g * error for v, g in zip(row.theta, gain)]
        weights = count_weights or [1] * len(row.counts)
        row.counts = [
            forgetting**weight * n + evidence_weight * weight * (abs(value) > 1e-9)
            for value, n, weight in zip(activity, row.counts, weights)
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
            # Tiny correlated window/rounding errors cannot justify replacing
            # an established fit with a fast, less certain model.
            and b["errors"]["active"] - b["errors"].get(candidate, float("inf"))
            > 0.001 * self.interval_minutes * b["seconds"]
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

    def neutral_row(self, systems=None, sources=None):
        systems = self.systems if systems is None else systems
        sources = self.sources if sources is None else sources
        count = len(systems) + len(sources)
        size = 3 * count + 3
        return Row(
            [
                0,
                *([PRIOR_B] * len(systems)),
                *([0.0] * len(sources)),
                *([0.0] * (2 * count)),
                0,
                0,
            ],
            [
                [
                    (
                        self.lag_prior
                        if 2 * count < i <= 3 * count
                        else self.interaction_prior
                        if count < i <= 2 * count
                        else BACKGROUND_PRIOR
                        if self.window_minutes and i == size - 1
                        else SOURCE_PRIOR
                        if self.window_minutes and len(systems) < i <= count
                        else 100.0
                    )
                    if i == j
                    else 0.0
                    for j in range(size)
                ]
                for i in range(size)
            ],
            [0.0] * count,
        )

    def interaction_index(self, index):
        """Column for the interaction of a zero-based system/source input."""
        return 1 + len(self.systems) + len(self.sources) + index

    def lag_index(self, index):
        return 1 + 2 * (len(self.systems) + len(self.sources)) + index

    def record_efforts(self, efforts, sources, start, end, observed_history=None):
        """Retain bounded observed intervals, never fill gaps across restarts."""
        self.effort_clock = max(self.effort_clock, end)
        if end <= start:
            return
        values = {f"system:{k}": efforts.get(k) for k in self.systems}
        values.update({f"source:{k}": sources.get(k) for k in self.sources})
        if observed_history is None:
            self.effort_history.append([start, end, values])
        else:
            # Retain transition boundaries: a lag window can cut through a pass.
            boundaries = sorted(
                {start, end}
                | {
                    max(start, min(end, t))
                    for segments in observed_history.values()
                    for left, right, _ in segments
                    for t in (left, right)
                }
            )
            for left, right in zip(boundaries, boundaries[1:]):
                segment_values = {
                    key: next(
                        (
                            value
                            for a, b, value in observed_history.get(key, [])
                            if a <= left and b >= right
                        ),
                        None,
                    )
                    for key in values
                }
                self.effort_history.append([left, right, segment_values])
        self.effort_history = [
            item
            for item in self.effort_history
            if item[1]
            > end
            - (
                self.lag_minutes
                + self.window_minutes
                + self.average_minutes
                + self.label_delay_minutes
            )
            * 60
            - (end - start)
        ]
        self.effort_clock = max(self.effort_clock, end)

    def latent_share(self, kind, key, start, end):
        """Share of [start, end] whose effort is a backfilled latent label."""
        if end <= start:
            return 0.0
        flag = f"latent:{kind}:{key}"
        seconds = 0.0
        for left, right, values in self.effort_history:
            if values.get(flag):
                seconds += max(0, min(right, end) - max(left, start))
        return min(1.0, seconds / (end - start))

    def effort_span(self, kind, key, start, end):
        """Labels over [start, end]: (known mean, unknown share, constant, latent).

        `known mean` is the time-weighted effort with unknown time counted as 0;
        `constant` is the single value when the whole span is known and constant
        (else None); `latent` is the share labelled by a latent backfill.
        """
        if end <= start:
            return 0.0, 1.0, None, 0.0
        total = known = 0.0
        values_seen = set()
        flag = f"latent:{kind}:{key}"
        latent = 0.0
        for left, right, values in self.effort_history:
            seconds = min(right, end) - max(left, start)
            if seconds <= 0:
                continue
            value = values.get(f"{kind}:{key}")
            if value is None or not isfinite(value):
                continue
            known += seconds
            total += seconds * value
            values_seen.add(round(value, 6))
            if values.get(flag):
                latent += seconds
        span = end - start
        unknown = max(0.0, 1 - known / span)
        if unknown < 1e-6:
            unknown = 0.0
        constant = (
            next(iter(values_seen)) if not unknown and len(values_seen) == 1 else None
        )
        return total / span, unknown, constant, latent / span

    def backfill_effort(self, system, start, end, value, latent=True):
        """Fill missing effort for `system` within [start, end]; return seconds.

        Only segments whose effort is missing change; known labels are never
        overwritten. Windows ending after the backfill use the span. A latent
        label is flagged (`latent:system:<key>`) so it adds no counts.
        """
        if value is None or not isfinite(value) or end <= start:
            return 0.0
        key = f"system:{system}"
        flag = f"latent:{key}"
        filled = 0.0
        history = []
        for left, right, values in self.effort_history:
            a, b = max(left, start), min(right, end)
            if b <= a or values.get(key) is not None:
                history.append([left, right, values])
                continue
            if left < a:
                history.append([left, a, dict(values)])
            inside = {**values, key: value}
            if latent:
                inside[flag] = True
            history.append([a, b, inside])
            filled += b - a
            if b < right:
                history.append([b, right, dict(values)])
        self.effort_history = history
        return filled

    def past_effort(self, kind, key, end, start=None):
        start = end - self.lag_minutes * 60 if start is None else start
        covered, total = 0, 0
        cursor = start
        for left, right, values in self.effort_history:
            left, right = max(left, start), min(right, end)
            if right <= left:
                continue
            value = values.get(f"{kind}:{key}")
            if left > cursor + 1e-6 or value is None or not isfinite(value):
                return None
            left = max(left, cursor)
            total += (right - left) * value
            covered += right - left
            cursor = right
        return (
            total / (end - start)
            if covered >= end - start - 1e-6 and end > start
            else None
        )

    def steady_std(
        self, sensor, system, outdoor=None, temperature=None, independent=False
    ):
        """Posterior standard deviation of raw b + g*delta + h.

        `independent` uses the covariance of windows without latent labels.
        """
        k = self.systems.index(system)
        delta = (
            outdoor - temperature
            if outdoor is not None and temperature is not None
            else 0
        )
        terms = [(1 + k, 1), (self.interaction_index(k), delta), (self.lag_index(k), 1)]
        row = self.rows[sensor]
        p = row.independent if independent and row.independent else row.covariance
        return sqrt(max(0, sum(a * b * p[i][j] for i, a in terms for j, b in terms)))

    def source_effect(self, sensor, source, outdoor=None, temperature=None):
        index = len(self.systems) + self.sources.index(source)
        row = self.rows[sensor]
        delta = (
            outdoor - temperature
            if outdoor is not None and temperature is not None
            else 0
        )
        return (
            row.theta[1 + index]
            + row.theta[self.interaction_index(index)] * delta
            + row.theta[self.lag_index(index)]
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
        confidence = n / (n + self.n0)
        if self.window_minutes:
            row = self.rows[sensor]
            k = self.systems.index(system)
            effect = row.theta[1 + k] + row.theta[self.lag_index(k)]
            confidence = min(
                confidence,
                effect**2
                / (
                    effect**2
                    + self.steady_std(sensor, system, independent=True) ** 2
                    + 1e-12
                ),
            )
        return confidence

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

    def blended(self, sensor, system, outdoor=None, temperature=None):
        confidence = self.confidence(sensor, system)
        b = self.rows[sensor].theta[self.systems.index(system) + 1]
        if self.window_minutes and self.rows[sensor].samples:
            # Integrated observations already regularize toward the row prior.
            # Do not mix a measured room with another room's thermostat response.
            confidence = 1
        delta = (
            outdoor - temperature
            if outdoor is not None and temperature is not None
            else 0
        )
        g = self.rows[sensor].theta[self.interaction_index(self.systems.index(system))]
        return (
            confidence * b
            + (1 - confidence) * self.system_prior(system)
            + g * delta
            + self.rows[sensor].theta[self.lag_index(self.systems.index(system))]
        )

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
        retention = 1 - a
        gain = sum(retention**k for k in range(steps))
        background += sum(
            (
                self.source_effect(sensor, key, outdoor, temperature)
                - row.theta[self.lag_index(len(self.systems) + k)]
            )
            * (sources or {}).get(key, 0)
            for k, key in enumerate(self.sources)
        )
        base = retention**steps * temperature + gain * background
        coefficients = [
            gain
            * (
                max(0, self.blended(sensor, key, outdoor, temperature))
                - row.theta[self.lag_index(self.systems.index(key))]
            )
            if key in self.systems
            else gain * PRIOR_B
            for key in systems
        ]
        window = self.lag_minutes * 60
        for step in range(steps):
            t = step * self.interval_minutes * 60
            fraction = min(1, t / window)
            weight = retention ** (steps - 1 - step)
            for k, (kind, key) in enumerate(
                [("system", key) for key in self.systems]
                + [("source", key) for key in self.sources]
            ):
                h = row.theta[self.lag_index(k)]
                if h == 0:
                    continue
                known = 0
                if fraction < 1:
                    value = self.past_effort(
                        kind, key, self.effort_clock, self.effort_clock + t - window
                    )
                    known = (value or 0) * (1 - fraction)
                base += weight * h * known
                if kind == "source":
                    base += weight * h * fraction * (sources or {}).get(key, 0)
                elif key in systems:
                    coefficients[systems.index(key)] += weight * h * fraction
        return base, coefficients

    def influences(self, system, sensors=None, outdoor=None, temperatures=None):
        positive = {
            s: max(0, self.blended(s, system, outdoor, (temperatures or {}).get(s)))
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

    def diagnostics(self, outdoor=None, temperatures=None):
        """Report hourly effects without changing the interval-based model."""
        per_hour = 60 / self.interval_minutes
        return {
            "units": {"effects": "°C per hour of full effort", "confidence": "%"},
            "systems": {
                j: {
                    s: {
                        "b": per_hour * row.theta[k + 1],
                        "g": per_hour * row.theta[self.interaction_index(k)],
                        "h": per_hour * row.theta[self.lag_index(k)],
                        "steady_std": per_hour
                        * self.steady_std(s, j, outdoor, (temperatures or {}).get(s)),
                        "blended_b": per_hour
                        * self.blended(s, j, outdoor, (temperatures or {}).get(s)),
                        "effective_effect": per_hour
                        * max(
                            0, self.blended(s, j, outdoor, (temperatures or {}).get(s))
                        ),
                        "influence": self.influences(
                            j, outdoor=outdoor, temperatures=temperatures
                        ).get(s, 0),
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
                        "e": per_hour * row.theta[1 + len(self.systems) + k],
                        "h": per_hour
                        * row.theta[self.lag_index(len(self.systems) + k)],
                        "g": per_hour
                        * row.theta[self.interaction_index(len(self.systems) + k)],
                        "effective_effect": per_hour
                        * self.source_effect(
                            sensor, source, outdoor, (temperatures or {}).get(sensor)
                        ),
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
            "learning_version": 2,
            "residuals": dict(self.residuals),
            "streaks": {s: list(v) for s, v in self.streaks.items()},
            "disturbed": dict(self.disturbed),
            "interaction_prior": self.interaction_prior,
            "lag_prior": self.lag_prior,
            "lag_minutes": self.lag_minutes,
            "window_minutes": self.window_minutes,
            "average_minutes": self.average_minutes,
            "drift_noise": self.drift_noise,
            "integrated_version": int(bool(self.window_minutes)),
            "effort_history": deepcopy(self.effort_history),
            "effort_clock": self.effort_clock,
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
            interaction_prior=saved.get("interaction_prior", INTERACTION_PRIOR),
            lag_prior=saved.get("lag_prior", LAG_PRIOR),
            lag_minutes=saved.get("lag_minutes", 60),
            window_minutes=saved.get("window_minutes", 0),
            average_minutes=saved.get("average_minutes", 10),
            drift_noise=saved.get("drift_noise", DRIFT_NOISE),
        )

        def load_row(data):
            row = Row(**deepcopy(data))
            count = len(model.systems) + len(model.sources)
            if len(row.theta) != 3 * count + 3:
                # Insert missing interaction/lag columns, preserving all old covariance.
                old_groups = (len(row.theta) - 3) // count if count else 3
                prior = model.neutral_row()
                mapping = [
                    *range(old_groups * count + 1),
                    *([None] * ((3 - old_groups) * count)),
                    old_groups * count + 1,
                    old_groups * count + 2,
                ]
                for i, old_i in enumerate(mapping):
                    if old_i is not None:
                        prior.theta[i] = row.theta[old_i]
                        for j, old_j in enumerate(mapping):
                            if old_j is not None:
                                prior.covariance[i][j] = row.covariance[old_i][old_j]
                prior.counts = row.counts
                prior.samples, prior.squared_error = row.samples, row.squared_error
                row = prior
            if model.window_minutes and not saved.get("integrated_version"):
                # Short, correlated samples were not independent identification
                # evidence. Retain physical means but reopen their uncertainty.
                row.covariance = model.neutral_row().covariance
                row.independent = None
                row.theta[-1] = 0.0
                row.counts = [0.0] * count
            return row

        model.effort_history = deepcopy(saved.get("effort_history", []))
        model.effort_clock = saved.get("effort_clock", saved.get("clock", 0))
        model.rows = {s: load_row(row) for s, row in saved["rows"].items()}
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
            state["challenger"] = load_row(state["challenger"])
            for item in state["saved"]:
                item["row"] = load_row(item["row"])
            model.regimes[sensor] = state
        model.residuals = dict(saved.get("residuals", {}))
        model.streaks = {s: tuple(v) for s, v in saved.get("streaks", {}).items()}
        model.disturbed = dict(saved.get("disturbed", {}))
        if saved.get("learning_version", 0) < 2:
            for sensor, state in model.regimes.items():
                # An archived fit is recoverable only when retained observations
                # already establish its superiority. Never guess lost means from
                # the neutral prior or select a library row by coefficient size.
                for candidate in sorted(
                    state["saved"], key=lambda item: item["last_fit"], reverse=True
                ):
                    if model.evidence(
                        sensor, candidate["id"], model.regime_return_hours, False
                    ):
                        model.promote(sensor, candidate["id"])
                        break
                # Legacy challenger scores may predate covariance resets and
                # counted overlapping windows as independent evidence.
                state["challenger"] = deepcopy(model.rows[sensor])
                state["blocks"] = []
                state["identified_since"] = model.clock
                state["surprises"] = 0
        return model


def run_action(running, mode, active_mode=None):
    """Learning action from a local run sensor instead of a reported hvac_action.

    `running` is True/False, or None when the run sensor is unavailable or stale.
    Direction comes from the device's mode; in heat_cool/auto it comes from the
    house controller's selected direction (`ControlState.active_mode`), which is
    what the coordinator commands. Running with no known heat/cool direction, or
    any unknown run state, is uncertain (None) rather than a guess.
    """
    if running is None:
        return None
    if not running:
        return "idle"
    if mode in ("heat_cool", "auto"):
        mode = active_mode
    return {"heat": "heating", "cool": "cooling"}.get(mode)


def command_effort(
    mode,
    target,
    own,
    correction=0.0,
    *,
    active_mode=None,
    confirmed=True,
    clear_gap=1.5,
    off_gap=1.0,
    hysteresis=0.5,
):
    """Classify a confirmed thermostat setting as clear on, clear off or ambiguous.

    Returns `(kind, direction, gap)`: kind is "on", "off" or "ambiguous",
    direction +1 heat / −1 cool (0 when off or unknown), and gap the signed
    distance d·(target − own) in °C. `own` is the best estimate of the
    thermostat's own reading and `correction` its uncertainty, which widens the
    ambiguity band. An on needs the gap past both `clear_gap` and the
    thermostat's hysteresis plus `off_gap`; an off needs the target at least
    `off_gap` on the far side, since no thermostat runs with its target behind
    its reading. Mode off (and fan only) is a certain off. In heat_cool/auto the
    direction is the controller's `active_mode`, as in `run_action`.
    """
    if not confirmed:
        return "ambiguous", 0, None
    if mode in ("off", "fan_only"):
        return "off", 0, None
    if mode in ("heat_cool", "auto"):
        mode = active_mode
    direction = {"heat": 1, "cool": -1}.get(mode)
    if (
        direction is None
        or target is None
        or own is None
        or not isfinite(target)
        or not isfinite(own)
    ):
        return "ambiguous", 0, None
    gap = direction * (target - own)
    margin = abs(correction or 0)
    if gap >= max(clear_gap, (hysteresis or 0) + off_gap) + margin:
        return "on", direction, gap
    if gap <= -off_gap - margin:
        return "off", direction, gap
    return "ambiguous", direction, gap


def own_estimate(reported, mean_at_report, mean_now, sensor=None):
    """Best estimate of a slow thermostat's own reading: (own, correction, source).

    A trusted room sensor next to the thermostat wins (no correction). Else the
    last reported reading is moved by the house mean's change since that report,
    and the size of that move is the estimate's uncertainty. Without house means
    the reported reading stands uncorrected.
    """
    if sensor is not None and isfinite(sensor):
        return sensor, 0.0, "own_sensor"
    if reported is None or not isfinite(reported):
        return None, None, None
    if mean_at_report is None or mean_now is None:
        return reported, 0.0, "reported"
    correction = mean_now - mean_at_report
    return reported + correction, abs(correction), "reported+house"


class CommandTransitions:
    """Per-system command label over time with a thermostat transition model.

    Labels: "on" (±1 after a start delay, never before the restart guard has
    passed since the last run ended), "off" (half the last direction during the
    stop tail, then 0) and None (ambiguous: no effort). Times are seconds. The
    `cause` of a change is when it happened (the command's send time) and may
    precede the observation; values before the observation are never rewritten.
    """

    def __init__(self, start_delay=120, restart_guard=300, stop_tail=180):
        self.start_delay = start_delay
        self.restart_guard = restart_guard
        self.stop_tail = stop_tail
        self.history = {}
        self.on_end = {}

    def current(self, key):
        states = self.history.get(key)
        return states[-1] if states else None

    def observe(self, key, kind, direction, now, cause=None):
        label = kind if kind in ("on", "off") else None
        direction = direction if label == "on" else 0
        current = self.current(key)
        if (
            current is not None
            and current["label"] == label
            and current["direction"] == direction
        ):
            return current
        cause = now if cause is None else min(cause, now)
        state = {"since": now, "label": label, "direction": direction}
        if current is not None and current["label"] == "on":
            if cause > current["start"]:
                self.on_end[key] = cause
                if label == "off":
                    state["tail"] = 0.5 * current["direction"]
                    state["tail_until"] = cause + self.stop_tail
        if label == "on":
            start = cause + self.start_delay
            if key in self.on_end:
                start = max(start, self.on_end[key] + self.restart_guard)
            state["start"] = start
        states = self.history.setdefault(key, [])
        states.append(state)
        # Keep one state older than an hour as the base for value lookups.
        while len(states) > 2 and states[1]["since"] < now - 3600:
            states.pop(0)
        return state

    def state_at(self, key, when):
        found = None
        for state in self.history.get(key, []):
            if state["since"] <= when:
                found = state
        return found

    def value(self, key, when):
        """Effort at `when`: None when ambiguous or unknown."""
        state = self.state_at(key, when)
        if state is None or state["label"] is None:
            return None
        if state["label"] == "on":
            return 0.0 if when < state["start"] else float(state["direction"])
        if when < state.get("tail_until", float("-inf")):
            return state["tail"]
        return 0.0

    def breakpoints(self, key, start, end):
        """Times in (start, end) at which the modelled value changes."""
        times = set()
        for state in self.history.get(key, []):
            for name in ("since", "start", "tail_until"):
                if name in state and start < state[name] < end:
                    times.add(state[name])
        return sorted(times)


def delta_effort(delta, deadband=2, full_heat=15, full_cool=10):
    """Signed effort from duct supply − return ΔT (°C), clamped to [−1, 1].

    The sign is the air's: positive adds heat, negative removes it, whatever
    the device's mode says (a defrost in heat mode is negative). Inside the
    dead band (blower only, probe offsets) the effort is 0. Outside it the
    effort is proportional to ΔT, so a variable-speed unit at part load reports
    its part-load fraction of a full-capacity ΔT at roughly constant airflow.
    Above a full-capacity ΔT (aux strips) it saturates: effort is per full
    unit capacity, which the controller also caps at 1.
    """
    if delta is None or not isfinite(delta):
        return None
    if abs(delta) <= deadband:
        return 0.0
    scale = full_heat if delta > 0 else full_cool
    if not scale or scale <= 0:
        return None
    return max(-1.0, min(1.0, delta / scale))


class EffortTracker:
    """Integrate numeric efforts or action transitions between controller passes.

    Unknown/unavailable action makes that system's whole interval missing.
    A restart deliberately starts a new interval rather than guessing downtime.
    """

    def __init__(self):
        self.actions = {}
        self.times = {}
        self.totals = {}
        self.missing = set()
        self.started = None
        self.segments = {}
        self.finished_segments = {}
        self.modes = {}

    def change(
        self, system, action, now, *, mode=None, commanded_mode=None, trusted=False
    ):
        """Observe signed action, withholding mode transitions from learning.

        A `trusted` action (a command label with its own transition model)
        skips the withholding: the mode switch is known, not unknown.
        """
        if mode is not None and trusted:
            self.modes[system] = mode
        elif mode is not None:
            if system in self.modes and self.modes[system] != mode:
                self.missing.add(system)
            self.modes[system] = mode
            if (
                commanded_mode is not None
                and mode != commanded_mode
                or action == "heating"
                and mode == "cool"
                or action == "cooling"
                and mode == "heat"
            ):
                action = None
        if system in self.times:
            elapsed = max(0, now - self.times[system])
            previous = self.actions[system]
            if elapsed > 0:
                self.segments.setdefault(system, []).append(
                    [self.times[system], now, previous]
                )
            if previous is None and elapsed > 0:
                self.missing.add(system)
            self.totals[system] = self.totals.get(system, 0) + (previous or 0) * elapsed
        self.actions[system] = (
            (action if isfinite(action) else None)
            if isinstance(action, (int, float))
            else {
                "heating": 1,
                "cooling": -1,
                "idle": 0,
                "off": 0,
                "fan": 0,
                "drying": 0,
            }.get(action)
        )
        self.times[system] = now

    def invalidate(self, system):
        """Withhold this system's current interval, e.g. a stale run sensor."""
        self.missing.add(system)

    def finish(self, actions, now, *, modes=None, commanded_modes=None, trusted=()):
        for system, action in actions.items():
            self.change(
                system,
                action,
                now,
                mode=(modes or {}).get(system),
                commanded_mode=(commanded_modes or {}).get(system),
                trusted=system in trusted,
            )
        duration = now - self.started if self.started is not None else 0
        result = {
            s: self.totals.get(s, 0) / duration
            if duration > 0 and s not in self.missing
            else None
            for s in actions
        }
        self.started = now
        self.finished_segments = self.segments
        for system, value in result.items():
            if value is None and duration > 0:
                # Integrated/lagged windows must see the same invalid interval
                # as the immediate fit, including a mode change between ticks.
                self.finished_segments[system] = [[self.started - duration, now, None]]
        self.segments = {}
        self.totals = {}
        self.missing.clear()
        return result, duration

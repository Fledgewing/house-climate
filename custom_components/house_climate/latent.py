"""Latent effort from the rooms' response (v0.5.23 part B); no HA dependencies.

For a system whose effort label is missing (a slow reporter with an ambiguous
command), the rooms' temperature response over a block of B minutes says
whether it ran. Per system j and room i, with response delay δ:

    r_i = rate_i(t+δ) − passive_i(t+δ) − Σ_{k≠j} e_ik·u_k
    r_i = s_ij·u_j + c_ij + noise,  noise ~ N(0, σ_ij²)

Shares s, offsets c and noise σ are fitted only on *anchor* blocks: blocks
whose label for j is known for the whole block, constant, and not latent
(probe, run sensor, clear command, fresh report). An estimate is then

    û = Σ w s f (r − c − s·k) / Σ w (s f)²,  w = 1/σ²,
    se = 1/√(Σ w (s f)²),  χ² = Σ w (r − c − s·k − s f û)² / (n − 1)

where k is the block's known label mean and f its unknown share. Only a
decisive estimate becomes a label: 0 when |û| ≤ 0.25, the mode's direction d
when d·û ≥ 0.75, both with se ≤ 0.2 and χ² ≤ 3; else the span stays skipped.
Labels are quantised so a room's own noise does not flow back into its fit.
The label is backfilled into `Learning.effort_history` with a latent flag:
it fills learning windows but adds no confidence counts and is never an
anchor, so the estimate cannot confirm itself.
"""

from math import floor, isfinite, sqrt

DELAYS = (0, 5, 10, 15, 20, 30)  # minutes


class LatentEffort:
    """Anchor-calibrated inversion of room responses into on/off labels."""

    def __init__(
        self,
        saved=None,
        *,
        block_minutes=15,
        forgetting=0.998,
        min_anchors=12,
        min_rooms=3,
        anchor_days=7,
        off_limit=0.25,
        on_limit=0.75,
        max_se=0.2,
        max_chi2=3.0,
    ):
        saved = saved or {}
        self.block = block_minutes * 60
        self.forgetting = forgetting
        self.min_anchors = min_anchors
        self.min_rooms = min_rooms
        self.anchor_seconds = anchor_days * 86400
        self.off_limit, self.on_limit = off_limit, on_limit
        self.max_se, self.max_chi2 = max_se, max_chi2
        # Per tick: [time, {room: [temperature, passive °C/h, {system: °C/h}]}],
        # usable rooms only. Not persisted: a restart loses at most an hour.
        self.samples = []
        # {system: {delay: {room: [w, Σu, Σu², Σr, Σur, Σr²]}}}
        self.stats = {
            system: {
                int(delay): {room: list(v) for room, v in rooms.items()}
                for delay, rooms in delays.items()
            }
            for system, delays in saved.get("stats", {}).items()
        }
        # {system: [[block start, "on" | "off" | "partial"]]}
        self.anchors = {
            system: [list(a) for a in items]
            for system, items in saved.get("anchors", {}).items()
        }
        self.processed = saved.get("processed")
        self.status = {
            system: dict(value) for system, value in saved.get("status", {}).items()
        }

    def to_dict(self):
        return {
            "stats": {
                system: {
                    str(delay): {room: list(v) for room, v in rooms.items()}
                    for delay, rooms in delays.items()
                }
                for system, delays in self.stats.items()
            },
            "anchors": {s: [list(a) for a in v] for s, v in self.anchors.items()},
            "processed": self.processed,
            "status": {s: dict(v) for s, v in self.status.items()},
        }

    def observe(self, now, rooms):
        """One tick: {room: (temperature, passive °C/h, {system: effect °C/h})}."""
        if self.samples and now <= self.samples[-1][0]:
            return
        clean = {
            room: [temperature, passive, dict(effects)]
            for room, (temperature, passive, effects) in rooms.items()
            if temperature is not None
            and passive is not None
            and isfinite(temperature)
            and isfinite(passive)
        }
        self.samples.append([now, clean])
        horizon = 3 * self.block + max(DELAYS) * 60 + 3600
        self.samples = [s for s in self.samples if s[0] >= now - horizon]

    def span(self, room, start, end):
        """(T(start), T(end), mean passive, mean effects) or None.

        The room must be usable on every tick from the one at or before
        `start` to the one at or after `end`.
        """
        times = [s[0] for s in self.samples]
        first = next(
            (i for i in range(len(times) - 1, -1, -1) if times[i] <= start), None
        )
        last = next((i for i, t in enumerate(times) if t >= end), None)
        if first is None or last is None:
            return None
        chosen = self.samples[first : last + 1]
        if any(room not in values for _, values in chosen):
            return None

        def at(when):
            for (t0, v0), (t1, v1) in zip(chosen, chosen[1:]):
                if t0 <= when <= t1:
                    if t1 == t0:
                        return v0[room][0]
                    share = (when - t0) / (t1 - t0)
                    return v0[room][0] + share * (v1[room][0] - v0[room][0])
            return chosen[0][1][room][0] if chosen[0][0] == when else None

        low, high = at(start), at(end)
        if low is None or high is None:
            return None
        passive = sum(v[room][1] for _, v in chosen) / len(chosen)
        effects = {}
        for _, values in chosen:
            for system, value in values[room][2].items():
                effects[system] = effects.get(system, 0) + value / len(chosen)
        return low, high, passive, effects

    def responses(self, system, start, delay, systems, labels):
        """{room: r} for the block at `start` shifted by `delay`, or None.

        None when another system's effort is unknown in the shifted span: two
        unknown systems are not identifiable from one residual vector.
        """
        t1, t2 = start + delay * 60, start + self.block + delay * 60
        others = {}
        for other in systems:
            if other == system:
                continue
            known, unknown, _, _ = labels(other, t1, t2)
            if unknown > 0:
                return None
            others[other] = known
        result = {}
        hours = self.block / 3600
        for room in self.samples[-1][1] if self.samples else {}:
            values = self.span(room, t1, t2)
            if values is None:
                continue
            low, high, passive, effects = values
            result[room] = (
                (high - low) / hours
                - passive
                - sum(effects.get(k, 0) * u for k, u in others.items())
            )
        return result

    def fit(self, system):
        """Best delay and {room: (s, c, σ)} of rooms with |s| > 2σ."""
        best = None
        for delay, rooms in self.stats.get(system, {}).items():
            fitted, score = {}, 0.0
            for room, (w, su, suu, sr, sur, srr) in rooms.items():
                if w <= 2:
                    continue
                mu, mr = su / w, sr / w
                var_u = suu / w - mu * mu
                var_r = srr / w - mr * mr
                if var_u <= 1e-9 or var_r <= 1e-12:
                    continue
                cov = sur / w - mu * mr
                share = cov / var_u
                residual = max(var_r - share * cov, 1e-12) * w / (w - 2)
                sigma = sqrt(residual)
                score += cov * cov / (var_u * var_r)
                if abs(share) > 2 * sigma:
                    fitted[room] = (share, mr - share * mu, sigma)
            if best is None or score > best[2]:
                best = (delay, fitted, score)
        return (best[0], best[1]) if best else (None, {})

    def anchor_counts(self, system):
        items = self.anchors.get(system, [])
        return (
            sum(kind == "on" for _, kind in items),
            sum(kind == "off" for _, kind in items),
        )

    def add_anchor(self, system, start, value, responses_by_delay):
        delays = self.stats.setdefault(system, {})
        for delay, responses in responses_by_delay.items():
            rooms = delays.setdefault(delay, {})
            for stats in rooms.values():
                stats[:] = [self.forgetting * v for v in stats]
            for room, r in responses.items():
                w, su, suu, sr, sur, srr = rooms.get(room, [0.0] * 6)
                rooms[room] = [
                    w + 1,
                    su + value,
                    suu + value * value,
                    sr + r,
                    sur + value * r,
                    srr + r * r,
                ]
        kind = "off" if abs(value) < 1e-9 else "on" if abs(value) > 0.5 else "partial"
        items = self.anchors.setdefault(system, [])
        items.append([start, kind])
        self.anchors[system] = [a for a in items if a[0] > start - self.anchor_seconds]

    def estimate(self, system, start, known, unknown, direction, systems, labels):
        """Decide one block: (label or None, diagnostics)."""
        delay, rooms = self.fit(system)
        on, off = self.anchor_counts(system)
        info = {
            "block": start,
            "anchors_on": on,
            "anchors_off": off,
            "delay_min": delay,
            "rooms": len(rooms),
        }
        info["ready"] = (
            on >= self.min_anchors
            and off >= self.min_anchors
            and len(rooms) >= self.min_rooms
        )
        if not info["ready"]:
            return None, info
        responses = self.responses(system, start, delay, systems, labels)
        if responses is None:
            info["reason"] = "another system unlabelled"
            return None, info
        used = [
            (r - c - s * known, s * unknown, 1 / (sigma * sigma))
            for room, r in responses.items()
            if room in rooms
            for s, c, sigma in [rooms[room]]
        ]
        if len(used) < self.min_rooms:
            info["reason"] = "too few usable rooms"
            return None, info
        denominator = sum(w * a * a for _, a, w in used)
        if denominator <= 0:
            return None, info
        u_hat = sum(w * a * y for y, a, w in used) / denominator
        se = 1 / sqrt(denominator)
        chi2 = sum(w * (y - a * u_hat) ** 2 for y, a, w in used) / max(1, len(used) - 1)
        info.update(u_hat=round(u_hat, 3), se=round(se, 3), chi2=round(chi2, 2))
        label = None
        if se <= self.max_se and chi2 <= self.max_chi2:
            if abs(u_hat) <= self.off_limit:
                label = 0.0
            elif direction and direction * u_hat >= self.on_limit:
                label = float(direction)
        info["label"] = label
        return label, info

    def process(self, now, systems, candidates, labels):
        """Finish every block whose delayed response is in; return backfills.

        `systems` are all learning systems; `candidates` maps each system with
        latent effort enabled to {"direction": ±1 or None, "estimate": bool}.
        `labels(system, start, end)` is `Learning.effort_span` for systems.
        Returns [(system, start, end, value)] for decisive latent labels.
        """
        if not self.samples:
            return []
        latest = self.samples[-1][0]
        first = floor(self.samples[0][0] / self.block) * self.block + self.block
        start = self.processed if self.processed is not None else first
        start = max(start, first)
        backfills = []
        while start + self.block + max(DELAYS) * 60 <= latest:
            for system, options in candidates.items():
                end = start + self.block
                known, unknown, constant, latent = labels(system, start, end)
                if not unknown and not latent and constant is not None:
                    by_delay = {
                        delay: responses
                        for delay in DELAYS
                        if (
                            responses := self.responses(
                                system, start, delay, systems, labels
                            )
                        )
                    }
                    if by_delay:
                        self.add_anchor(system, start, constant, by_delay)
                elif unknown and options.get("estimate"):
                    label, info = self.estimate(
                        system,
                        start,
                        known,
                        unknown,
                        options.get("direction"),
                        systems,
                        labels,
                    )
                    self.status[system] = info
                    if label is not None:
                        backfills.append((system, start, end, label))
            start += self.block
        self.processed = start
        return backfills

    def diagnostics(self, system):
        """Latest estimate and readiness for one system."""
        delay, rooms = self.fit(system)
        on, off = self.anchor_counts(system)
        return {
            **self.status.get(system, {}),
            "anchors_on": on,
            "anchors_off": off,
            "delay_min": delay,
            "rooms": len(rooms),
            "ready": on >= self.min_anchors
            and off >= self.min_anchors
            and len(rooms) >= self.min_rooms,
        }


def room_terms(learning, sensor, temperature, outdoor, solar=0, sources=None):
    """(passive °C/h, {system: effect °C/h}) of one room from its learning row.

    Passive is the row's response with every system off: outdoor loss, solar,
    background and extra heat sources. Effects are the systems' blended hourly
    effects at this temperature, used to remove other labelled systems.
    """
    row = learning.rows.get(sensor)
    if row is None or temperature is None or outdoor is None:
        return None
    per_hour = 60 / learning.interval_minutes
    passive = (
        row.theta[0] * (outdoor - temperature)
        + row.theta[-2] * (solar or 0)
        + row.theta[-1]
        + sum(
            learning.source_effect(sensor, key, outdoor, temperature)
            * (sources or {}).get(key, 0)
            for key in learning.sources
            if (sources or {}).get(key) is not None
        )
    )
    effects = {
        system: per_hour * learning.blended(sensor, system, outdoor, temperature)
        for system in learning.systems
    }
    return per_hour * passive, effects

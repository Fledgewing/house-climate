"""Home Assistant I/O and persisted control/sample state."""

import asyncio
import logging
from dataclasses import asdict, replace
from datetime import timedelta
from math import floor, isfinite

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.recorder import DATA_INSTANCE, get_instance
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    COMMAND_DEFAULTS,
    DEFAULT_RUN_THRESHOLD,
    DEFAULTS,
    DELTA_DEFAULTS,
    DOMAIN,
    REPORT_DEFAULTS,
    fan_reward_weight,
)
from .controller import ControlState, Device, Reading, control, thermostat_hysteresis
from .fans import FanLearning, quiet_modes
from .filtering import SensorFilter
from .latent import DELAYS, LatentEffort, room_terms
from .learning import (
    CommandTransitions,
    EffortTracker,
    Learning,
    command_effort,
    delta_effort,
    own_estimate,
    run_action,
)
from .priority import members, modes
from .reporting import Reporting

_LOGGER = logging.getLogger(__name__)


class HouseCoordinator(DataUpdateCoordinator):
    """Serialize immediate requests and interval ticks; persist bounded history."""

    def __init__(self, hass, entry):
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry)
        self.config = dict(entry.options or entry.data)
        self.priority_mode = next(iter(modes(self.config)))
        self.state = ControlState()
        self.enabled = True
        self.system_control = {}
        self.fan_enabled = True
        self.learning_enabled = True
        self.learning_resume_at = None
        self.source_effort = EffortTracker()
        self.fans = FanLearning(
            self.config.get("fan_hold_minutes", 30),
            fan_reward_weight(self.config),
            learn_exclude_modes=self.config.get("learn_exclude_modes", "auto"),
        )
        self.restored = False
        self.initialized = False
        self.awaiting_mode = False
        self.samples = []
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self.lock = asyncio.Lock()
        self.last_commands = {}
        self.pending_commands = {}
        self.last_fan_commands = {}
        self.learning = self.new_learning()
        self.effort = EffortTracker()
        self.action_sources = {}
        self.learning_baseline = None
        self.reporting = Reporting()
        self.not_reporting = {}
        self.history_seeded = False
        self.latent = LatentEffort(
            block_minutes=self.command_setting("latent_block_minutes")
        )
        self.filters = {
            m["entity_id"]: SensorFilter(
                self.config.get("filter_minutes", 15),
                self.config.get("max_rate", 0.5),
                history_minutes=self.filter_history_minutes,
            )
            for m in self.config["sensors"]
        }
        self.raw_readings = {}
        self.current_temperatures = {}
        self.startup_sensors = set(self.filters)
        # Slow reporters and command-derived effort (v0.5.23).
        self.command_labels = CommandTransitions(
            self.command_setting("command_start_delay") * 60,
            self.command_setting("command_restart_guard") * 60,
            self.command_setting("command_stop_tail") * 60,
        )
        # Last command per system as a learning label: mode, target, sent_at (s).
        self.command_settings = {}
        # House mean when each system's current report was fetched.
        self.report_marks = {}
        # (time, house mean) samples for the command hold's trend (v0.5.24);
        # not persisted: after a restart no hold for one trend window.
        self.mean_history = []
        # Last seen report content (reading, action) per system, with the
        # report time and house mean when it changed (v0.5.24, not persisted).
        self.report_content = {}
        self.mark_taken = {}
        self.reports = {}
        self.outside_changes = {}
        self.command_fed = set()
        self.report_fed = {}
        self.coverage = {}

    def command_setting(self, key):
        return self.config.get(key, COMMAND_DEFAULTS[key])

    @property
    def latent_delay_minutes(self):
        """How late a latent label arrives: one block plus the longest delay."""
        return self.command_setting("latent_block_minutes") + max(DELAYS)

    @property
    def filter_history_minutes(self):
        """Room history for averaged windows, including a latent label delay."""
        window = self.config.get("learning_window_minutes", 60)
        average = self.config.get("learning_average_minutes", 10)
        delay = self.latent_delay_minutes if window and average else 0
        return window + average + delay + 5

    @staticmethod
    def report_setting(system, key):
        value = system.get(key)
        return REPORT_DEFAULTS[key] if value is None else value

    def active_systems(self, *, include_inactive=False):
        """Control selections exclude hand-controlled units; observations retain all."""
        return [
            system
            for system in self.config["systems"]
            if include_inactive or self.system_control.get(system["entity_id"], True)
        ]

    def active_devices(self, devices):
        return {s["entity_id"]: devices[s["entity_id"]] for s in self.active_systems()}

    async def async_system_control(self, entity_id, enabled):
        async with self.lock:
            if entity_id not in {
                s["entity_id"] for s in self.active_systems(include_inactive=True)
            }:
                raise ValueError("Unknown system")
            if self.system_control.get(entity_id, True) == enabled:
                return
            self.system_control[entity_id] = enabled
            if not enabled:
                now = dt_util.utcnow().timestamp()
                self.state.command_demand(entity_id, False, now)
                self.fans.finish_system(entity_id, now)
                self.state.efforts[entity_id] = 0
                self.state.requested_efforts[entity_id] = 0
                self.state.reported_efforts[entity_id] = 0
                self.state.experiment_info.pop(entity_id, None)
                self.state.steady_runs.pop(entity_id, None)
                self.state.completed_runs.pop(entity_id, None)
                if self.state.excitation.get("system") == entity_id:
                    self.state.last_excite[entity_id] = now
                    self.state.excitation = {}
                elif self.state.excitation:
                    self.state.excitation.get("others", {}).pop(entity_id, None)
                entity = self.hass.states.get(entity_id)
                if entity and entity.state in ("heat", "cool"):
                    await self.hass.services.async_call(
                        "climate",
                        "set_hvac_mode",
                        {"entity_id": entity_id, "hvac_mode": "off"},
                        blocking=True,
                    )
        await self.async_tick(record=False)

    @property
    def priority_weights(self):
        return modes(self.config)[self.priority_mode]

    @property
    def learning_sensors(self):
        return list(
            dict.fromkeys(
                [s["entity_id"] for s in self.config["sensors"]]
                + [s["entity_id"] for s in self.active_systems(include_inactive=True)]
            )
        )

    def own_sensor(self, system):
        """Prefer an explicit sensor, then a unique member on the same device."""
        if system.get("own_sensor"):
            return system["own_sensor"]
        registry = er.async_get(self.hass)
        unit = registry.async_get(system["entity_id"])
        if unit and unit.device_id:
            matches = [
                s["entity_id"]
                for s in self.config["sensors"]
                if (member := registry.async_get(s["entity_id"]))
                and member.device_id == unit.device_id
            ]
            if len(matches) == 1:
                return matches[0]
        return system["entity_id"]

    def new_learning(self):
        return Learning(
            self.learning_sensors,
            [s["entity_id"] for s in self.active_systems(include_inactive=True)],
            self.config.get("learning_forgetting", 0.998),
            self.config.get("learning_n0", 24),
            [s["entity_id"] for s in self.config.get("extra_heat_sources", [])],
            self.config.get("disturbance_sigma", 3),
            self.config.get("disturbance_intervals", 2),
            self.config.get("disturbance_clear_intervals", 3),
            self.config.get("regime_margin", 0.3),
            self.config.get("regime_hours", 24),
            self.config.get("regime_return_hours", 4),
            self.config.get("interval", 5),
            self.config.get("interaction_prior", 0.0001),
            self.config.get("lag_minutes", 60),
            self.config.get("lag_prior", 0.01),
            average_minutes=self.config.get("learning_average_minutes", 10),
            window_minutes=self.config.get(
                "learning_window_minutes", DEFAULTS["learning_window_minutes"]
            ),
            drift_noise=self.config.get("learning_drift", DEFAULTS["learning_drift"]),
        )

    async def async_load(self):
        if saved := await self.store.async_load():
            selected = saved.get("priority_mode")
            if selected not in modes(self.config):
                selected = self.config.get("priority_renames", {}).get(selected)
            if selected in modes(self.config):
                self.priority_mode = selected
            self.state = ControlState(**saved["state"])
            self.state.steady_runs.clear()
            # Retain inactivity/cooldown clocks, but never count downtime as runtime.
            self.state.action_times = {
                key: dt_util.utcnow().timestamp() for key in self.state.actions
            }
            if self.state.excitation:
                self.state.last_excite[self.state.excitation["system"]] = (
                    dt_util.utcnow().timestamp()
                )
                self.state.excitation = {}
            self.system_control = saved.get("system_control", {})
            self.enabled = saved.get("enabled", True)
            self.fan_enabled = saved.get("fan_enabled", True)
            self.learning_enabled = saved.get("learning_enabled", True)
            self.learning_resume_at = saved.get("learning_resume_at")
            self.fans = FanLearning(
                self.config.get("fan_hold_minutes", 30),
                fan_reward_weight(self.config),
                learn_exclude_modes=self.config.get("learn_exclude_modes", "auto"),
                saved=saved.get("fans"),
            )
            self.fans.reconcile(
                [s["entity_id"] for s in self.active_systems(include_inactive=True)]
            )
            self.samples = saved.get("samples", [])
            # Upgrade old Store data even when recorder is absent during an outage.
            for system in self.active_systems(include_inactive=True):
                key = system["entity_id"]
                if key in self.state.thermostats:
                    continue
                history = ControlState()
                for sample in self.samples:
                    report = sample.get("systems", {}).get(key, {})
                    if not report or not report.get("available", True):
                        continue
                    history.observe_report(
                        system,
                        Device(
                            **{
                                field: value
                                for field, value in report.items()
                                if field in Device.__dataclass_fields__
                            }
                        ),
                        self.config,
                    )
                    if key in history.thermostats:
                        history.thermostats[key]["time"] = sample["timestamp"]
                if key in history.thermostats:
                    self.state.thermostats[key] = history.thermostats[key]
                    self.state.thermostat_points[key] = history.thermostat_points.get(
                        key, []
                    )
            self.restored = True
            self.state.fresh_thermostat_points = {
                key: list(points)
                for key, points in saved.get("fresh_thermostat_points", {}).items()
            }
            self.reporting = Reporting(saved.get("reporting"))
            self.command_settings = {
                key: dict(value)
                for key, value in saved.get("command_settings", {}).items()
            }
            self.report_marks = {
                key: dict(value) for key, value in saved.get("report_marks", {}).items()
            }
            self.latent = LatentEffort(
                saved.get("latent"),
                block_minutes=self.command_setting("latent_block_minutes"),
            )
            for key in self.filters:
                self.filters[key] = SensorFilter(
                    self.config.get("filter_minutes", 15),
                    self.config.get("max_rate", 0.5),
                    saved.get("filters", {}).get(key),
                    history_minutes=self.filter_history_minutes,
                )
            if saved.get("learning"):
                self.learning = Learning.from_dict(
                    {
                        **saved["learning"],
                        "lag_minutes": self.config.get("lag_minutes", 60),
                        "average_minutes": self.config.get(
                            "learning_average_minutes", 10
                        ),
                        "lag_prior": self.config.get("lag_prior", 0.01),
                        "window_minutes": self.config.get(
                            "learning_window_minutes",
                            DEFAULTS["learning_window_minutes"],
                        ),
                        "interaction_prior": self.config.get(
                            "interaction_prior", 0.0001
                        ),
                        "drift_noise": self.config.get(
                            "learning_drift", DEFAULTS["learning_drift"]
                        ),
                    }
                )
                changed = {
                    source["entity_id"]
                    for source in self.config.get("extra_heat_sources", [])
                    if saved.get("source_responses", {}).get(
                        source["entity_id"], "on_off"
                    )
                    != source.get("response", "on_off")
                }
                if changed:
                    # Drop changed columns before restoring them with neutral priors,
                    # including challenger and saved regime rows/covariances.
                    self.learning.reconcile(
                        self.learning_sensors,
                        self.learning.systems,
                        [key for key in self.learning.sources if key not in changed],
                    )
                self.learning.reconcile(
                    self.learning_sensors,
                    [
                        s["entity_id"]
                        for s in self.active_systems(include_inactive=True)
                    ],
                    [s["entity_id"] for s in self.config.get("extra_heat_sources", [])],
                )
                self.learning.forgetting = self.config.get("learning_forgetting", 0.998)
                self.learning.n0 = self.config.get("learning_n0", 24)
                for key, default in (
                    ("regime_margin", 0.3),
                    ("regime_hours", 24),
                    ("regime_return_hours", 4),
                    ("disturbance_sigma", 3),
                    ("disturbance_intervals", 2),
                    ("disturbance_clear_intervals", 3),
                ):
                    setattr(self.learning, key, self.config.get(key, default))
                self.learning.interval_minutes = self.config.get("interval", 5)
            self.sync_label_delay()

    def start(self):
        cancel_timer = async_track_time_interval(
            self.hass,
            self.async_tick,
            timedelta(minutes=self.config.get("interval", 5)),
        )
        cancel_states = async_track_state_change_event(
            self.hass,
            [s["entity_id"] for s in self.active_systems(include_inactive=True)],
            self._system_changed,
        )

        cancel_sensors = async_track_state_change_event(
            self.hass,
            [s["entity_id"] for s in self.config["sensors"]],
            self._sensor_changed,
        )

        source_ids = [s["entity_id"] for s in self.config.get("extra_heat_sources", [])]
        cancel_sources = (
            async_track_state_change_event(self.hass, source_ids, self._source_changed)
            if source_ids
            else lambda: None
        )

        run_ids = list(
            dict.fromkeys(
                s[key]
                for s in self.active_systems(include_inactive=True)
                for key in (
                    "run_sensor",
                    "supply_sensor",
                    "return_sensor",
                    "report_timestamp_sensor",
                )
                if s.get(key)
            )
        )
        cancel_runs = (
            async_track_state_change_event(self.hass, run_ids, self._local_changed)
            if run_ids
            else lambda: None
        )

        @callback
        def cancel():
            cancel_timer()
            cancel_states()
            cancel_sensors()
            cancel_sources()
            cancel_runs()

        return cancel

    def source_action(self, source):
        entity = self.hass.states.get(source["entity_id"])
        if not entity or entity.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        if source.get("kind", "binary") == "power":
            value = self.number(source["entity_id"])
            factors = {"W": 1, "kW": 1000, "MW": 1000000}
            factor = factors.get(entity.attributes.get("unit_of_measurement"))
            if value is None or factor is None:
                return None
            on = value * factor > source.get("threshold", 10)
            if source.get("response", "on_off") == "proportional":
                return value * factor / 1000 if on else 0.0
        elif entity.state in ("on", "off"):
            on = entity.state == "on"
        else:
            return None
        return "heating" if on else "off"

    def system_config(self, entity_id):
        return next(
            s
            for s in self.active_systems(include_inactive=True)
            if s["entity_id"] == entity_id
        )

    def commanded_mode(self, entity_id):
        return self.pending_commands.get(entity_id, {}).get(
            "hvac_mode", self.last_commands.get(entity_id, (None,))[0]
        )

    def local_problem(self, entity, now):
        """None when a local sensor is trusted, else "unavailable"/"stale"."""
        if not entity or entity.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return "unavailable"
        reported = getattr(entity, "last_reported", None) or entity.last_updated
        if now - reported.timestamp() > self.config.get("stale_after", 720) * 60:
            return "stale"
        return None

    def run_state(self, system, now):
        """Local run sensor: (running, problem); running is None when untrusted."""
        sensor = system["run_sensor"]
        entity = self.hass.states.get(sensor)
        if problem := self.local_problem(entity, now):
            return None, problem
        if sensor.startswith("binary_sensor."):
            if entity.state not in ("on", "off"):
                return None, "unavailable"
            return entity.state == "on", None
        value = self.number(sensor)
        if value is None:
            return None, "unavailable"
        return value > system.get("run_threshold", DEFAULT_RUN_THRESHOLD), None

    def delta_state(self, system, now):
        """Duct probes: supply, return, ΔT and signed effort, or a problem.

        Without a return sensor the return is the system's own climate
        `current_temperature` (the thermostat's indoor reading).
        """
        result = {
            "supply_sensor": system["supply_sensor"],
            "return_sensor": system.get("return_sensor")
            or f"{system['entity_id']}.current_temperature",
            "supply": None,
            "return": None,
            "delta_t": None,
            "effort": None,
            "problem": None,
        }
        readings = [("supply", system["supply_sensor"], None)]
        readings.append(
            ("return", system["return_sensor"], None)
            if system.get("return_sensor")
            else ("return", system["entity_id"], "current_temperature")
        )
        for name, entity_id, attribute in readings:
            problem = self.local_problem(self.hass.states.get(entity_id), now)
            value = None if problem else self.number(entity_id, attribute)
            if value is None:
                result["problem"] = f"{name} {problem or 'unavailable'}"
                return result
            result[name] = value
        delta = result["supply"] - result["return"]
        result["delta_t"] = round(delta, 2)
        result["effort"] = delta_effort(
            delta,
            *(system.get(key, default) for key, default in DELTA_DEFAULTS.items()),
        )
        if result["effort"] is None:
            result["problem"] = "invalid scale"
        return result

    def report_time(self, system, entity):
        """Seconds of the device's last real report, or None when unknown.

        A configured `report_timestamp_sensor` (an entity whose state is the
        device's last data fetch time) wins; a command echo can rewrite the
        climate entity without a new fetch. Else the entity's `last_reported`.
        """
        sensor = self.report_setting(system, "report_timestamp_sensor")
        if sensor:
            state = self.hass.states.get(sensor)
            if not state or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                return None
            parsed = dt_util.parse_datetime(state.state)
            return dt_util.as_utc(parsed).timestamp() if parsed else None
        if not entity:
            return None
        reported = getattr(entity, "last_reported", None) or entity.last_updated
        return reported.timestamp()

    def house_mean(self):
        """Weighted mean of the counted room sensors' current values."""
        total = weight = 0.0
        for member in members(self.config, self.priority_mode):
            value = self.number(member["entity_id"])
            share = member.get("weight", 1)
            if value is None or not isfinite(share) or share <= 0:
                continue
            total += share * value
            weight += share
        return total / weight if weight else None

    def house_trend(self, now):
        """House-mean rate (°C per 5 min) over the last trend window, or None.

        Needs a sample at least `command_trend_minutes` old; keeps the newest
        such sample as the base and drops older ones.
        """
        mean = self.house_mean()
        window = self.command_setting("command_trend_minutes") * 60
        history = self.mean_history
        if mean is not None and (not history or history[-1][0] < now):
            history.append((now, mean))
        while len(history) > 1 and history[1][0] <= now - window:
            history.pop(0)
        if mean is None or not history or history[0][0] > now - window:
            return None
        base_time, base_mean = history[0]
        return (mean - base_mean) / (now - base_time) * 300

    def report_status(self, system, now):
        """Slow-reporter tag and report freshness for one system (v0.5.23)."""
        key = system["entity_id"]
        entity = self.hass.states.get(key)
        reported = self.report_time(system, entity)
        self.reporting.cadence.observe(key, reported)
        slow, reason = self.reporting.cadence.status(
            key,
            self.report_setting(system, "report_trust"),
            self.config.get("interval", 5),
        )
        age = now - reported if reported is not None else None
        fresh = age is not None and age <= (
            self.report_setting(system, "report_fresh_minutes") * 60
        )
        # The report's reading can arrive before its timestamp (the climate
        # entity updates before the timestamp sensor). Pairing the new reading
        # with the old report's mark gives a wrong estimate for a tick, so
        # remember when the reading changed and the house mean then. (The
        # action alone does not enter the estimate; a mode echo changes it.)
        content = entity.attributes.get("current_temperature") if entity else None
        seen = self.report_content.get(key)
        if seen is None or seen["content"] != content:
            self.report_content[key] = seen = {
                "content": content,
                "reported": reported,
                "at": now if seen is not None else None,
                "mean": self.house_mean(),
                # Changed without a new report time (yet): the current time
                # is not one first seen moments ago (timestamp first).
                "early": reported is not None
                and self.report_marks.get(key, {}).get("reported") == reported
                and now - self.mark_taken.get(key, float("-inf"))
                > self.command_setting("report_pair_seconds"),
            }
        settle = self.command_setting("report_settle_minutes") * 60
        recent = seen["at"] is not None and seen["early"] and now - seen["at"] <= settle
        pending = recent and seen["reported"] == reported
        if reported is not None and (
            self.report_marks.get(key, {}).get("reported") != reported
        ):
            # Content that arrived first carries its own (earlier) mean.
            arrived_first = recent and seen["reported"] != reported
            self.report_marks[key] = {
                "reported": reported,
                "mean": seen["mean"] if arrived_first else self.house_mean(),
            }
            self.mark_taken[key] = now
        status = {
            "slow": slow,
            "slow_reason": reason,
            "report_age_s": round(age) if age is not None else None,
            "report_fresh": fresh,
            "reported": reported,
        }
        if pending:
            status["report_pending"] = True
        self.reports[key] = status
        # Control reads the tag for the thermostat hysteresis (v0.5.24).
        if slow:
            self.state.slow_reporters.add(key)
        else:
            self.state.slow_reporters.discard(key)
        return status

    def report_usable(self, key):
        """A slow system's report is fact only while fresh; others always."""
        status = self.reports.get(key)
        return not status or not status["slow"] or status["report_fresh"]

    def command_action(self, system, mode, device_action, report, now):
        """Effort label from the confirmed command, or None; plus diagnostics."""
        key = system["entity_id"]
        command = self.command_settings.get(key)
        if not command:
            self.command_labels.observe(key, "ambiguous", 0, now)
            return None, {"class": "ambiguous", "reason": "no command"}
        sent = command["sent_at"]
        current = self.command_labels.current(key)
        if (
            report.get("report_pending")
            and current is not None
            and sent <= current["since"]
        ):
            # New reading without its report time yet: keep the label rather
            # than estimate from a mismatched mark (v0.5.24). A command sent
            # since the label is still classified (with the old mark).
            info = {
                "mode": command["mode"],
                "target": command["target"],
                "class": current["label"] or "ambiguous",
                "since": current["since"],
                "reason": "report time pending",
            }
            if self.reporting.checks.suspended(key):
                return None, info
            return self.command_labels.value(key, now), info
        target = self.number(key, "temperature")
        step = self.number(key, "target_temp_step") or 0.5
        echoed = mode == command["mode"] and (
            command["mode"] == "off"
            or target is not None
            and command["target"] is not None
            # A °F unit shown in °C rounds the echo (22.5 °C -> 72 °F ->
            # 22.22 °C), so allow at least 0.3 °C.
            and abs(target - command["target"]) <= max(step / 2, 0.3)
        )
        outside = self.outside_changes.get(key, float("-inf")) > sent
        confirmed = not outside and (
            echoed or now - sent <= self.command_setting("command_confirm_seconds")
        )
        # v0.5.25: a fresh report made after the command had time to land
        # shows whether the unit took it. The entity echoes a command at once
        # (optimistically), so for a slow unit only such a report confirms it.
        # A mode the unit refused (e.g. cool on a cold night) is then not
        # trusted until a later report shows a command in it taken.
        reported_at = report["reported"]
        landed = (
            report["report_fresh"]
            and reported_at is not None
            and reported_at >= sent + self.command_setting("command_confirm_seconds")
        )
        taken = echoed and not outside
        if landed:
            self.reporting.checks.verdict(key, command["mode"], reported_at, taken)
            if not taken:
                # The report time can arrive before the unit's own setting, so
                # an earlier pass may already have checked this report.
                self.reporting.checks.retract(key, reported_at)
        refused = report["slow"] and self.reporting.checks.is_rejected(
            key, command["mode"]
        )
        if refused:
            confirmed = False
        # Only an explicit room sensor: a sensor matched on the same device as
        # a slow reporter arrives through the same slow cloud fetch.
        sensor = system.get("own_sensor")
        own_value = None
        if (
            sensor
            and sensor != key
            and not self.local_problem(self.hass.states.get(sensor), now)
        ):
            own_value = self.number(sensor)
        reported_value = self.number(key, "current_temperature")
        own, correction, own_source = own_estimate(
            reported_value,
            self.report_marks.get(key, {}).get("mean"),
            self.house_mean(),
            own_value,
        )
        if report["slow"]:
            # v0.5.24: a slow unit's learned points come from transitions
            # seen up to hours late (inflated); only clean ones count.
            hysteresis, hysteresis_source = thermostat_hysteresis(
                self.config, self.state, key
            )
        else:
            points = self.state.thermostat_points.get(key)
            hysteresis, hysteresis_source = (
                (sum(points) / len(points), "points")
                if points
                else (self.config.get("thermostat_hysteresis", 0.5), "default")
            )
        # The label in effect when the report was made, for the cross-check
        # (v0.5.24): independent of which entity updated first, and never a
        # change observed at or after the report time (the report's own).
        at_report = (
            self.command_labels.state_before(key, reported_at)
            if reported_at is not None
            else None
        )
        trend = self.house_trend(now)
        kind, direction, gap = command_effort(
            command["mode"],
            command["target"],
            own,
            correction,
            active_mode=self.state.active_mode,
            confirmed=confirmed,
            clear_gap=self.command_setting("command_clear_gap"),
            off_gap=self.command_setting("command_off_gap"),
            hysteresis=hysteresis,
            margin_factor=self.command_setting("command_margin_factor"),
            margin_cap=self.command_setting("command_margin_cap"),
            # Signed house-mean move since the report (v0.5.24): a move with
            # the command adds no on margin.
            drift=own - reported_value if own_source == "reported+house" else None,
            running=current["direction"] if current and current["label"] == "on" else 0,
            hold_gap=self.command_setting("command_hold_gap"),
            trend=trend,
            hold_min_rate=self.command_setting("command_hold_min_rate"),
        )
        held = kind == "held"
        # A report made before this command was sent (an echo of an older
        # fetch) says nothing about it (v0.5.24).
        report_after_command = (
            report["report_fresh"] and reported_at is not None and reported_at >= sent
        )
        # v0.5.24: a fresh report of the unit running in the commanded
        # direction settles a small gap (its thermostat runs with the target
        # just past its reading). Without it the on label drops for that tick
        # and the next clear command restarts with a false start delay.
        reported_on = (
            kind in ("ambiguous", "held")
            and direction
            and report_after_command
            and device_action == ("heating" if direction > 0 else "cooling")
        )
        # Mirror: a fresh idle/off report settles an ambiguous or held label
        # as off from the report time (stop tail if it was on).
        reported_off = (
            kind in ("ambiguous", "held")
            and report_after_command
            and device_action in ("idle", "off")
        )
        if reported_on or held:
            kind = "on"
        elif reported_off:
            kind = "off"
        if reported_on and (current is None or current["label"] != "on"):
            # Seen running at the report: the start is no later than that.
            cause = reported_at - self.command_labels.start_delay
        elif reported_off:
            cause = reported_at
        else:
            cause = sent if current is None or sent > current["since"] else now
        state = self.command_labels.observe(key, kind, direction, now, cause)
        value = self.command_labels.value(key, now)
        transition = None
        if state["label"] == "on":
            transition = "start_delay" if now < state["start"] else "on"
        elif state["label"] == "off":
            transition = (
                "stop_tail" if now < state.get("tail_until", float("-inf")) else "off"
            )
        # A fresh report cross-checks the label settled at its report time,
        # once per report.
        if (
            report["report_fresh"]
            and at_report is not None
            and at_report["label"] is not None
            and device_action in ("heating", "cooling", "idle", "off", "fan", "drying")
        ):
            settled_at = (
                at_report["start"]
                if at_report["label"] == "on"
                else max(at_report["since"], at_report.get("tail_until", float("-inf")))
            )
            # A report within 5 min after this command reflects the change
            # itself (an echo fetch), not the settled label.
            # A report showing the command was not taken (v0.5.25) says the
            # unit refused it, not that the label's thermostat model is wrong.
            if (
                reported_at - settled_at >= 5 * 60
                and not (sent <= reported_at < sent + 5 * 60)
                and not (landed and not taken)
            ):
                self.reporting.checks.check(
                    key,
                    reported_at,
                    (device_action in ("heating", "cooling"))
                    != (at_report["label"] == "on"),
                    self.config,
                )
        info = {
            "mode": command["mode"],
            "target": command["target"],
            "own_est": round(own, 2) if own is not None else None,
            "own_source": own_source,
            "correction": round(correction, 2) if correction is not None else None,
            "gap": round(gap, 2) if gap is not None else None,
            "hysteresis": round(hysteresis, 2),
            "hysteresis_source": hysteresis_source,
            "class": kind,
            "since": state["since"],
        }
        if not confirmed:
            info["reason"] = (
                "changed outside house control"
                if outside
                else "unit refused this mode"
                if refused
                else "unconfirmed"
            )
        if transition:
            info["transition"] = transition
        if held:
            info["held"] = True
            info["trend"] = round(trend, 3)
        if reported_on:
            info["reason"] = "fresh report running"
        elif reported_off:
            info["reason"] = "fresh report idle"
        if self.reporting.checks.suspended(key):
            return None, info
        return value, info

    def latent_candidate(self, system):
        """Whether latent effort may label this system's unlabelled spans.

        A slow reporter without local sensors, or a system whose local run
        sensor/duct probes are untrusted right now (never the cloud action).
        """
        if not self.report_setting(system, "latent_effort"):
            return False
        key = system["entity_id"]
        if system.get("run_sensor") or system.get("supply_sensor"):
            info = self.action_sources.get(key, {})
            return bool(info.get("problem") or info.get("local_problem"))
        return bool(self.reports.get(key, {}).get("slow"))

    def latent_action(self, system, now):
        """Latent effort (rank 5): the estimate covering an unlabelled span.

        A block's room response is complete only a response delay after the
        block, so the present span always stays unlabelled (action None) and
        `LatentEffort` backfills it later with a decisive label (adds no
        learning counts). Returns the estimator's diagnostics when it is ready
        for this system, else None (skip).
        """
        if not self.report_setting(system, "latent_effort"):
            return None
        status = self.latent.diagnostics(system["entity_id"])
        return status if status["ready"] else None

    def learning_action(self, system, device_action, mode, now, available=True):
        """Action for effort/learning.

        Duct ΔT when its probes are trusted; else a trusted run sensor with the
        device's direction; else, when no local sensor is set, the device's
        reported action. A configured local sensor never falls back to the
        device: no trusted local evidence is no evidence (`problem` is set).

        v0.5.23: a slow reporter's action is fact only while its report is
        fresh. Its effort comes first from the confirmed command (on/off with
        a transition model), then a fresh report, else nothing. Untrusted local
        sensors fall back to a clear command, never to the report.
        """
        key = system["entity_id"]
        if not available:
            device_action = mode = None
        report = self.report_status(system, now)
        use_command = self.report_setting(
            system, "command_effort"
        ) and not self.reporting.checks.suspended(key)
        if not system.get("run_sensor") and not system.get("supply_sensor"):
            if not report["slow"]:
                self.action_sources[key] = {"source": "device", "action": device_action}
                return device_action
            info = {
                "device_action": device_action,
                "mode": mode,
                **{k: v for k, v in report.items() if k != "reported"},
            }
            value = None
            if self.report_setting(system, "command_effort"):
                value, info["command"] = self.command_action(
                    system, mode, device_action, report, now
                )
            if value is not None and use_command:
                info.update(source="command", action=value)
            elif report["report_fresh"]:
                info.update(source="report", action=device_action)
            elif (latent := self.latent_action(system, now)) is not None:
                # Unlabelled now; LatentEffort backfills the span later.
                info.update(source="latent", action=None, latent=latent)
            else:
                info.update(source=None, action=None)
                if self.report_setting(system, "latent_effort"):
                    info["latent"] = self.latent.diagnostics(key)
            self.add_check_info(key, info)
            self.action_sources[key] = info
            return info["action"]
        info = {"device_action": device_action, "mode": mode}
        run = None
        if system.get("run_sensor"):
            running, run_problem = self.run_state(system, now)
            run = run_action(running, mode, self.state.active_mode) if mode else None
            info.update(
                run_sensor=system["run_sensor"],
                running=running,
                run_problem=run_problem,
            )
        if system.get("supply_sensor"):
            delta = self.delta_state(system, now)
            info.update(
                {k: v for k, v in delta.items() if k != "problem"},
                delta_problem=delta["problem"],
            )
            if delta["problem"] is None:
                running = info.get("running")
                info.update(
                    source="delta_t",
                    action=delta["effort"],
                    # ΔT decides; a trusted run sensor only cross-checks it.
                    disagreement=running is not None
                    and running != (delta["effort"] != 0),
                    problem=None,
                )
            elif info.get("running") is not None:
                info.update(
                    source="run_sensor", action=run, disagreement=False, problem=None
                )
            else:
                info.update(
                    source="delta_t",
                    action=None,
                    disagreement=False,
                    problem=delta["problem"],
                )
        else:
            info.update(source="run_sensor", action=run, problem=info["run_problem"])
        if info["problem"] and use_command and self.command_settings.get(key):
            # Untrusted local sensors: a clear command, never the cloud action.
            value, command = self.command_action(
                system, mode, device_action, report, now
            )
            if value is not None:
                info.update(
                    source="command",
                    action=value,
                    command=command,
                    local_problem=info["problem"],
                    problem=None,
                )
                self.add_check_info(key, info)
        if info["problem"] and (latent := self.latent_action(system, now)) is not None:
            # Unlabelled now; LatentEffort backfills the span later.
            info.update(source="latent", action=None, latent=latent)
        self.action_sources[key] = info
        return info["action"]

    def windows_accepted(self):
        """Whether any learning row has a full integrated window now."""
        learning = self.learning
        if not learning.window_minutes:
            return True
        if learning.average_minutes:
            # A window advances over a missing effort (v0.5.23) but is accepted
            # only when every span in it is labelled.
            return any(w.get("accepted") for w in learning.windows.values())
        return any(
            sum(s[2] for s in w.get("segments", [])) * learning.interval_minutes * 60
            >= learning.window_minutes * 60
            for w in learning.windows.values()
        )

    def record_coverage(self, efforts, now, duration):
        """Last 24 h: share of time labelled by each source, skipped, windows."""
        if duration <= 0:
            return
        accepted = self.windows_accepted()
        for key, effort in efforts.items():
            source = (
                self.action_sources.get(key, {}).get("source") or "none"
                if effort is not None
                else "skipped"
            )
            spans = [
                span for span in self.coverage.get(key, []) if span[1] > now - 86400
            ]
            spans.append((now - duration, now, source, accepted))
            self.coverage[key] = spans
            self.coverage_info(key)

    def coverage_info(self, key):
        spans = self.coverage.get(key, [])
        total = sum(b - a for a, b, _, _ in spans)
        if total <= 0:
            return
        shares = {}
        for a, b, name, _ in spans:
            shares[name] = shares.get(name, 0) + (b - a) / total
        info = self.action_sources.get(key)
        if info is not None and (
            info.get("slow") is not None
            or info.get("source") == "command"
            or "latent" in shares
        ):
            info["coverage_24h"] = {
                **{name: round(v, 3) for name, v in shares.items()},
                "windows": round(sum(b - a for a, b, _, ok in spans if ok) / total, 3),
            }

    def relabel_coverage(self, key, start, end):
        """A latent backfill turns skipped time in [start, end] into latent."""
        spans = []
        for a, b, name, ok in self.coverage.get(key, []):
            lo, hi = max(a, start), min(b, end)
            if name != "skipped" or hi <= lo:
                spans.append((a, b, name, ok))
                continue
            if a < lo:
                spans.append((a, lo, name, ok))
            spans.append((lo, hi, "latent", ok))
            if hi < b:
                spans.append((hi, b, name, ok))
        self.coverage[key] = spans
        self.coverage_info(key)

    def latent_step(self, now, clean_values, outdoor, solar, sources, devices):
        """Feed the rooms' response to LatentEffort and backfill its labels.

        Rooms that are disturbed, temporary, not reporting or filter-blocked
        are left out. While a latent estimate is ready, averaged learning
        windows are evaluated `latent_delay_minutes` late so its labels can
        complete them.
        """
        span = self.config.get("learning_average_minutes", 10) * 60
        rooms = {}
        for member in self.config["sensors"]:
            key = member["entity_id"]
            if (
                key not in clean_values
                or key in self.learning.disturbed
                or self.learning.regimes.get(key, {}).get("temporary")
                or self.filters[key].blocked
            ):
                continue
            value = self.filters[key].mean(now - span, now) if span else None
            value = clean_values[key] if value is None else value
            terms = room_terms(self.learning, key, value, outdoor, solar, sources)
            if terms:
                rooms[key] = (value, *terms)
        self.latent.observe(now, rooms)
        candidates = {}
        for system in self.active_systems(include_inactive=True):
            key = system["entity_id"]
            if not self.report_setting(system, "latent_effort"):
                continue
            mode = devices[key].mode if key in devices else None
            if mode in ("heat_cool", "auto"):
                mode = self.state.active_mode
            candidates[key] = {
                "direction": {"heat": 1, "cool": -1}.get(mode),
                "estimate": self.latent_candidate(system),
            }
        for key, start, end, value in self.latent.process(
            now,
            self.learning.systems,
            candidates,
            lambda key, a, b: self.learning.effort_span("system", key, a, b),
        ):
            if self.learning.backfill_effort(key, start, end, value):
                self.relabel_coverage(key, start, end)
        if any(
            options["estimate"] and self.latent.diagnostics(key)["ready"]
            for key, options in candidates.items()
        ):
            self.latent.engaged = True
        # Held once engaged: flipping it with readiness would fit or skip the
        # same 45 minutes of windows twice. Off only with latent off everywhere.
        self.sync_label_delay()

    def sync_label_delay(self):
        """Averaged windows wait for late latent labels once latent is engaged."""
        enabled = any(
            self.report_setting(system, "latent_effort")
            for system in self.active_systems(include_inactive=True)
        )
        self.learning.label_delay_minutes = (
            self.latent_delay_minutes if self.latent.engaged and enabled else 0
        )

    def command_labelled(self, key):
        """Whether this system's effort comes (or can come) from its commands.

        Such a system is re-labelled right after a command instead of being
        marked unknown across a mode switch.
        """
        if self.action_sources.get(key, {}).get("source") == "command":
            return True
        system = next(
            (
                s
                for s in self.active_systems(include_inactive=True)
                if s["entity_id"] == key
            ),
            None,
        )
        return bool(
            system
            and self.reports.get(key, {}).get("slow")
            and not system.get("run_sensor")
            and not system.get("supply_sensor")
            and self.report_setting(system, "command_effort")
            and not self.reporting.checks.suspended(key)
        )

    def add_check_info(self, key, info):
        checks = self.reporting.checks.status(key)
        if checks["checks"] or checks.get("rejected_modes"):
            info["contradictions"] = checks
            if checks["suspended"]:
                info["problem"] = "command_contradicted"

    def label_until(self, key, now):
        """Fill the tracker up to `now` with known changes since its last sample.

        A command label's modelled transitions (start delay, stop tail), and a
        slow report's expiry: its action is no evidence once it is not fresh.
        """
        last = self.effort.times.get(key)
        if last is None:
            return
        if key in self.command_fed:
            for when in self.command_labels.breakpoints(key, last, now):
                self.effort.change(
                    key, self.command_labels.value(key, when), when, trusted=True
                )
        expiry = self.report_fed.get(key)
        if expiry is not None and last < expiry < now:
            self.effort.change(key, None, expiry)
            self.report_fed.pop(key)

    def feed_effort(self, key, action, now, mode):
        """Pass a learning action to the effort tracker.

        Command labels carry modelled transitions (start delay, stop tail)
        between samples; they are trusted, so a known mode switch is kept.
        """
        self.label_until(key, now)
        info = self.action_sources.get(key, {})
        trusted = info.get("source") == "command"
        if trusted:
            self.command_fed.add(key)
        else:
            self.command_fed.discard(key)
        self.report_fed.pop(key, None)
        if info.get("source") == "report" and info.get("slow"):
            status = self.reports.get(key, {})
            if status.get("reported") is not None:
                self.report_fed[key] = (
                    status["reported"]
                    + self.report_setting(
                        self.system_config(key), "report_fresh_minutes"
                    )
                    * 60
                )
        self.effort.change(
            key,
            action,
            now,
            mode=mode,
            commanded_mode=self.commanded_mode(key),
            trusted=trusted,
        )

    @callback
    def _local_changed(self, event):
        """A run sensor or duct probe changed: re-sample its systems' effort."""
        now = dt_util.utcnow().timestamp()
        changed = event.data["entity_id"]
        for system in self.active_systems(include_inactive=True):
            if changed not in (
                system.get("run_sensor"),
                system.get("supply_sensor"),
                system.get("return_sensor"),
                system.get("report_timestamp_sensor"),
            ):
                continue
            key = system["entity_id"]
            entity = self.hass.states.get(key)
            available = bool(
                entity and entity.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            )
            self.feed_effort(
                key,
                self.learning_action(
                    system,
                    entity.attributes.get("hvac_action") if available else None,
                    entity.state if available else None,
                    now,
                    available,
                ),
                now,
                entity.state if entity else None,
            )

    def source_value(self, source):
        """Current model effort: on fraction or kW; unknown stays missing."""
        action = self.source_action(source)
        if isinstance(action, (int, float)) or action is None:
            return action
        return float(action == "heating")

    @callback
    def _source_changed(self, event):
        key = event.data["entity_id"]
        source = next(
            s for s in self.config["extra_heat_sources"] if s["entity_id"] == key
        )
        self.source_effort.change(
            key, self.source_action(source), dt_util.utcnow().timestamp()
        )

    def reset_learning_interval(self):
        self.learning_baseline = None
        self.effort = EffortTracker()
        self.source_effort = EffortTracker()
        self.learning.streaks.clear()
        self.learning.windows.clear()
        self.learning.context_history.clear()
        self.learning.temperature_filters.clear()

    @property
    def frozen_window(self):
        return self.config.get("frozen_after", 4) * 3600

    def sensor_blocks_fans(self, entity_id):
        weights = {
            m["entity_id"]: max(0, m.get("weight", 1))
            for m in members(self.config, self.priority_mode)
        }
        total = sum(weights.values())
        return total > 0 and weights.get(entity_id, 0) / total >= self.config.get(
            "minor_weight_share", 0.05
        )

    def observe_sensor(self, entity_id, value, now):
        sensor_filter = self.filters[entity_id]
        if value is not None and entity_id in self.startup_sensors:
            self.startup_sensors.remove(entity_id)
            # Downtime/startup values give no evidence of a fast jump.
            if sensor_filter.points and sensor_filter.points[-1][1] != value:
                sensor_filter = self.filters[entity_id] = SensorFilter(
                    self.config.get("filter_minutes", 15),
                    self.config.get("max_rate", 0.5),
                    saved={
                        "jitter": sensor_filter.jitter,
                        "changes": sensor_filter.changes,
                    },
                    history_minutes=self.filter_history_minutes,
                )
        sensor_filter.observe(value, now)
        return sensor_filter

    @callback
    def _sensor_changed(self, event):
        entity_id = event.data["entity_id"]
        new = event.data.get("new_state")
        try:
            value = float(new.state) if new else None
        except TypeError, ValueError:
            value = None
        self.observe_sensor(
            entity_id,
            value if value is not None and isfinite(value) else None,
            dt_util.utcnow().timestamp(),
        )
        self.reporting.observe(
            entity_id,
            value if value is not None and isfinite(value) else None,
            dt_util.utcnow().timestamp(),
        )
        if self.sensor_blocks_fans(entity_id) and (
            value is None or not isfinite(value)
        ):
            self.fans.invalidate(f"{entity_id} missing or unavailable reading")
        # Recover control and repairs on the first numeric change, even between ticks.
        if entity_id in self.not_reporting:
            self.hass.async_create_task(self.async_tick(record=False))

    @callback
    def _system_changed(self, event):
        old, new = event.data.get("old_state"), event.data.get("new_state")
        # Runtime changes must be captured even during service calls under lock.
        entity_id = event.data["entity_id"]
        system = self.system_config(entity_id)
        # Control sees every report as in v0.5.22; freshness only decides which
        # transitions give a slow unit's thermostat points (v0.5.24).
        report = self.report_status(system, dt_util.utcnow().timestamp())
        if new and new.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            device = Device(
                mode=new.state,
                hvac_action=new.attributes.get("hvac_action"),
                temperature=self.number(entity_id, "temperature"),
                current_temperature=self.number(entity_id, "current_temperature"),
                step=self.number(entity_id, "target_temp_step") or 0.5,
                name=new.name,
            )
            # Before observe_report: both compare with the previous report.
            self.state.observe_fresh_report(
                entity_id,
                device,
                report["reported"],
                report["report_fresh"],
                self.report_setting(system, "report_fresh_minutes") * 60,
            )
            self.state.observe_report(
                next(
                    s
                    for s in self.active_systems(include_inactive=True)
                    if s["entity_id"] == entity_id
                ),
                device,
                self.config,
            )
        self.state.observe_action(
            entity_id,
            new.attributes.get("hvac_action")
            if new and new.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            else None,
            dt_util.utcnow().timestamp(),
        )
        if (
            new
            and new.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            and entity_id in self.state.thermostats
        ):
            self.state.thermostats[entity_id]["time"] = dt_util.utcnow().timestamp()
        self.reporting.activity(
            dt_util.utcnow().timestamp(),
            {
                entity_id: bool(
                    new
                    and new.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
                    and new.attributes.get("hvac_action") in ("heating", "cooling")
                )
            },
            self.frozen_window,
        )
        self.feed_effort(
            entity_id,
            self.learning_action(
                system,
                new.attributes.get("hvac_action") if new else None,
                new.state if new else None,
                dt_util.utcnow().timestamp(),
                bool(new and new.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)),
            ),
            dt_util.utcnow().timestamp(),
            new.state if new else None,
        )
        if not self.system_control.get(entity_id, True):
            self.state.reported_efforts[entity_id] = 0
            self.state.steady_runs.pop(entity_id, None)
            self.state.completed_runs.pop(entity_id, None)
            return
        if new:
            self.fans.running_changed(
                entity_id,
                dt_util.utcnow().timestamp(),
                new.attributes.get("hvac_action") in ("heating", "cooling"),
            )
        if old and new:
            commanded_target = self.last_commands.get(entity_id)
            pending_target = self.pending_commands.get(entity_id, {}).get("temperature")
            if old.attributes.get("temperature") != new.attributes.get(
                "temperature"
            ) and (
                new.attributes.get("temperature") != pending_target
                and (
                    not commanded_target
                    or new.attributes.get("temperature") != commanded_target[1]
                )
            ):
                self.fans.invalidate(f"{entity_id} user setpoint changed")
            commanded_mode = self.pending_commands.get(entity_id, {}).get(
                "hvac_mode", commanded_target[0] if commanded_target else None
            )
            if (old.state != new.state and new.state != commanded_mode) or any(
                old.attributes.get(f"{action}_mode")
                != new.attributes.get(f"{action}_mode")
                and new.attributes.get(f"{action}_mode")
                != self.fans.chosen.get(entity_id, {}).get(action)
                for action in ("fan", "swing")
            ):
                self.fans.invalidate(f"{entity_id} mode or fan/vane setting changed")
        if self.lock.locked() or self.data is None or old is None or new is None:
            return
        if entity_id in self.state.unrequested_starts and old.attributes.get(
            "hvac_action"
        ) != new.attributes.get("hvac_action"):
            self.hass.async_create_task(self.async_tick(record=False))
        for action in ("fan", "swing"):
            commanded = self.last_fan_commands.get((entity_id, action))
            if commanded and old.attributes.get(f"{action}_mode") != new.attributes.get(
                f"{action}_mode"
            ):
                choice, sent_at = commanded
                if (
                    new.attributes.get(f"{action}_mode") != choice
                    and (dt_util.utcnow() - sent_at).total_seconds() > 120
                ):
                    note = f" {entity_id} {action} changed outside house control; next interval will re-assert."
                    self.async_set_updated_data(
                        replace(self.data, explanation=self.data.explanation + note)
                    )
        if old.state != new.state or old.attributes.get(
            "temperature"
        ) != new.attributes.get("temperature"):
            commanded = self.last_commands.get(new.entity_id)
            if commanded is None:
                return
            mode, target, sent_at = commanded
            # Judge only changed fields. A mode acknowledgement can still
            # carry the previous mode's target until the target echo arrives.
            mode_changed = old.state != new.state
            outside_mode = mode_changed and new.state != mode
            outside_target = (
                not mode_changed
                and old.attributes.get("temperature")
                != new.attributes.get("temperature")
                and new.attributes.get("temperature") != target
            )
            if (dt_util.utcnow() - sent_at).total_seconds() <= 120 or not (
                outside_mode or outside_target
            ):
                return
            note = f" {new.entity_id} changed outside house control; next interval will re-assert."
            self.outside_changes[new.entity_id] = dt_util.utcnow().timestamp()
            _LOGGER.info("%s", note.strip())
            self.async_set_updated_data(
                replace(self.data, explanation=self.data.explanation + note)
            )

    def number(self, entity_id, attribute=None):
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            value = float(state.attributes.get(attribute) if attribute else state.state)
            return value if isfinite(value) else None
        except ValueError, TypeError:
            return None

    async def async_tick(self, _now=None, *, record=True):
        async with self.lock:
            now = dt_util.utcnow()
            if (
                not self.learning_enabled
                and self.learning_resume_at is not None
                and now.timestamp() >= self.learning_resume_at
            ):
                self.learning_enabled = True
                self.learning_resume_at = None
                self.reset_learning_interval()
                self.fans.window = self.fans.resume = None
            if not self.history_seeded:
                await self.async_seed_history(now)
                self.history_seeded = True
            self.reporting.activity(
                now.timestamp(),
                {
                    s["entity_id"]: bool(
                        (entity := self.hass.states.get(s["entity_id"]))
                        and entity.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
                        and entity.attributes.get("hvac_action")
                        in ("heating", "cooling")
                    )
                    for s in self.active_systems(include_inactive=True)
                },
                self.frozen_window,
            )
            self.not_reporting = {}
            readings = {}
            raw = {}
            for member in self.config["sensors"]:
                entity_id = member["entity_id"]
                entity = self.hass.states.get(entity_id)
                value = self.number(entity_id)
                # last_reported includes unchanged readings from reporting devices.
                reported = (
                    getattr(entity, "last_reported", None) or entity.last_updated
                    if entity
                    else None
                )
                age = (now - reported).total_seconds() if reported else 1e9
                self.reporting.observe(entity_id, value, now.timestamp())
                frozen = self.reporting.frozen(
                    entity_id, now.timestamp(), self.frozen_window
                )
                last_seen = self.device_last_seen(entity_id)
                stale_device = (
                    last_seen is not None
                    and (now - last_seen).total_seconds()
                    > self.config.get("stale_after", 720) * 60
                )
                issue_id = f"{self.config_entry.entry_id}_{entity_id}_not_reporting"
                if frozen or stale_device:
                    tracked = self.reporting.sensors.get(entity_id)
                    changed = (
                        dt_util.utc_from_timestamp(tracked["changed"]).isoformat()
                        if tracked
                        else "unknown"
                    )
                    reason = (
                        "value frozen during heating/cooling"
                        if frozen
                        else "device last seen is too old"
                    )
                    self.not_reporting[entity_id] = {
                        "last_change": changed,
                        "reason": reason,
                    }
                    ir.async_create_issue(
                        self.hass,
                        DOMAIN,
                        issue_id,
                        is_fixable=False,
                        severity=ir.IssueSeverity.WARNING,
                        translation_key="not_reporting",
                        translation_placeholders={
                            "sensor": entity_id,
                            "last_change": changed,
                        },
                    )
                else:
                    ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                sensor_filter = self.filters.setdefault(
                    entity_id,
                    SensorFilter(
                        self.config.get("filter_minutes", 15),
                        self.config.get("max_rate", 0.5),
                        history_minutes=self.filter_history_minutes,
                    ),
                )
                if (
                    entity_id in self.startup_sensors
                    or not sensor_filter.points
                    or sensor_filter.points[-1][1] != value
                ):
                    sensor_filter = self.observe_sensor(
                        entity_id, value, now.timestamp()
                    )
                # Resolve held excursions on every pass, even without a new report.
                filtered = sensor_filter.value(now.timestamp())
                readings[entity_id] = Reading(
                    filtered if value is not None else None,
                    age,
                    entity_id not in self.not_reporting,
                )
                raw[entity_id] = {
                    "value": value,
                    "age": age,
                    "state": entity.state if entity else None,
                    "not_reporting": entity_id in self.not_reporting,
                }
            self.raw_readings = raw
            if self.learning_baseline is not None:
                baseline, outdoor_before, solar_before = self.learning_baseline
                self.learning_baseline = (
                    {
                        key: value
                        for key, value in baseline.items()
                        if key not in self.not_reporting
                        and (
                            self.learning.window_minutes
                            or key not in self.filters
                            or not self.filters[key].blocked
                            and not self.filters[key].contaminated
                        )
                    },
                    outdoor_before,
                    solar_before,
                )
            devices = {}
            for system in self.active_systems(include_inactive=True):
                entity_id = system["entity_id"]
                self.report_status(system, now.timestamp())
                entity = self.hass.states.get(entity_id)
                if not entity or entity.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                    devices[entity_id] = Device(available=False)
                    continue
                devices[entity_id] = Device(
                    mode=entity.state,
                    temperature=self.number(entity_id, "temperature"),
                    minimum=self.number(entity_id, "min_temp") or 7,
                    maximum=self.number(entity_id, "max_temp") or 35,
                    preset=entity.attributes.get("preset_mode"),
                    hvac_action=entity.attributes.get("hvac_action"),
                    current_temperature=self.number(entity_id, "current_temperature"),
                    name=entity.name,
                    step=self.number(entity_id, "target_temp_step") or 0.5,
                )
            adoption = ""
            if not self.initialized:
                if not self.restored:
                    adoption = self.adopt_systems(self.active_devices(devices))
                self.initialized = True
            outdoor = self.number(self.config.get("outdoor_sensor"))
            if outdoor is None:
                outdoor = self.number(self.config.get("weather_entity"), "temperature")
            solar = self.solar_kw()
            # Fan rewards use the robust filter's value, including its held anchor.
            # Learning masks excursion spans; fan rewards retain the robust value.
            fan_problems = []
            clean_values = {}
            for key, reading in readings.items():
                reason = None
                if not reading.reporting:
                    reason = (
                        f"{key} not reporting ({self.not_reporting[key]['reason']})"
                    )
                elif reading.value is None:
                    reason = f"{key} missing or unavailable reading"
                elif reading.age > self.config.get("stale_after", 720) * 60:
                    reason = f"{key} stale reading"
                if reason is None:
                    clean_values[key] = reading.value
                elif self.sensor_blocks_fans(key):
                    fan_problems.append(reason)
            # Diagnostics cover every room and device row, including rooms with
            # zero priority weight and passes while learning is paused.
            self.current_temperatures = {
                **clean_values,
                **{
                    key: device.current_temperature
                    for key, device in devices.items()
                    if device.available and device.current_temperature is not None
                },
            }
            fan_clean = not fan_problems
            if not fan_clean:
                self.fans.invalidate("; ".join(fan_problems))
            if record:
                actions = {}
                for system in self.active_systems(include_inactive=True):
                    key, device = system["entity_id"], devices[system["entity_id"]]
                    actions[key] = self.learning_action(
                        system,
                        device.hvac_action,
                        device.mode,
                        now.timestamp(),
                        device.available,
                    )
                    if self.action_sources[key].get("problem") not in (
                        None,
                        "command_contradicted",
                    ):
                        # Untrusted local sensors never fall back to the device:
                        # the interval since the last good sample is unknown.
                        self.effort.invalidate(key)
                    self.label_until(key, now.timestamp())
                trusted = {
                    key
                    for key in actions
                    if self.action_sources[key].get("source") == "command"
                }
                self.command_fed = set(trusted)
                self.report_fed = {
                    key: self.reports[key]["reported"]
                    + self.report_setting(system, "report_fresh_minutes") * 60
                    for system in self.active_systems(include_inactive=True)
                    if (key := system["entity_id"]) in actions
                    and self.action_sources[key].get("source") == "report"
                    and self.action_sources[key].get("slow")
                }
                efforts, duration = self.effort.finish(
                    actions,
                    now.timestamp(),
                    modes={key: device.mode for key, device in devices.items()},
                    commanded_modes={
                        key: command[0] for key, command in self.last_commands.items()
                    },
                    trusted=trusted,
                )
                for key in efforts:
                    if not self.system_control.get(key, True):
                        efforts[key] = 0
                        self.effort.finished_segments[key] = [
                            (now.timestamp() - duration, now.timestamp(), 0)
                        ]
                source_fractions, _ = self.source_effort.finish(
                    {
                        source["entity_id"]: self.source_action(source)
                        for source in self.config.get("extra_heat_sources", [])
                    },
                    now.timestamp(),
                )
                observed_history = {
                    **{
                        f"system:{key}": self.effort.finished_segments.get(key, [])
                        for key in efforts
                    },
                    **{
                        f"source:{key}": self.source_effort.finished_segments.get(
                            key, []
                        )
                        for key in source_fractions
                    },
                }
                learning_values = {
                    key: raw[key]["value"]
                    for key, value in clean_values.items()
                    if self.learning.window_minutes
                    or not self.filters[key].blocked
                    and not self.filters[key].contaminated
                }
                learning_values.update(
                    {
                        key: device.current_temperature
                        for key, device in devices.items()
                        if device.available
                        and device.current_temperature is not None
                        # A slow reporter's own row learns only from fresh
                        # reports; the row itself is kept (v0.5.23).
                        and self.report_usable(key)
                    }
                )
                if (
                    self.learning_enabled
                    and self.learning_baseline is not None
                    and outdoor is not None
                    and solar is not None
                ):
                    before, previous_outdoor, previous_solar = self.learning_baseline
                    self.learning.update(
                        before,
                        learning_values,
                        previous_outdoor,
                        efforts,
                        previous_solar,
                        duration / (self.config.get("interval", 5) * 60),
                        sources=source_fractions,
                        observed_history=observed_history,
                        sensor_filters=self.filters,
                        now=now.timestamp(),
                        local_hour=dt_util.as_local(now).hour
                        + dt_util.as_local(now).minute / 60
                        + dt_util.as_local(now).second / 3600,
                    )
                else:
                    self.learning.record_efforts(
                        efforts,
                        source_fractions,
                        now.timestamp() - duration,
                        now.timestamp(),
                        observed_history,
                    )
                self.learning_baseline = (
                    (learning_values, outdoor, solar) if self.learning_enabled else None
                )
                self.record_coverage(efforts, now.timestamp(), duration)
                if self.learning_enabled and outdoor is not None:
                    self.latent_step(
                        now.timestamp(),
                        clean_values,
                        outdoor,
                        solar,
                        source_fractions,
                        devices,
                    )
                for sensor_filter in self.filters.values():
                    sensor_filter.finish_interval()
            for sensor, state in self.learning.regimes.items():
                if (
                    state["temporary"]
                    and state["temporary_until"] is not None
                    and now.timestamp() >= state["temporary_until"]
                ):
                    self.learning.set_temporary(sensor, False)
                    if self.learning_baseline is not None:
                        self.learning_baseline[0].pop(sensor, None)
            self.sync_regime_issues(now.timestamp())
            for key in self.learning.disturbed:
                clean_values.pop(key, None)
                if self.sensor_blocks_fans(key):
                    fan_problems.append(f"{key} unexplained temperature disturbance")
            for key, state in self.learning.regimes.items():
                if state["temporary"]:
                    clean_values.pop(key, None)
                    if self.sensor_blocks_fans(key):
                        fan_problems.append(f"{key} temporary change")
            if not self.learning_enabled:
                fan_problems.append("learning paused by switch")
            fan_clean = not fan_problems
            if not fan_clean:
                self.fans.invalidate("; ".join(fan_problems))
            control_systems = [
                {**system, "own_sensor": self.own_sensor(system)}
                for system in self.active_systems()
            ]
            for system in control_systems:
                sensor = system["own_sensor"]
                if sensor not in readings and sensor != system["entity_id"]:
                    entity = self.hass.states.get(sensor)
                    reported = (
                        (getattr(entity, "last_reported", None) or entity.last_updated)
                        if entity
                        else None
                    )
                    readings[sensor] = Reading(
                        self.number(sensor),
                        (now - reported).total_seconds() if reported else 1e9,
                    )
            decision = control(
                {
                    **self.config,
                    "systems": control_systems,
                    "sensors": [
                        {
                            **member,
                            "name": getattr(
                                self.hass.states.get(member["entity_id"]),
                                "name",
                                member["entity_id"],
                            ),
                        }
                        for member in members(self.config, self.priority_mode)
                    ],
                },
                self.state,
                readings,
                self.active_devices(devices),
                now.timestamp(),
                outdoor,
                self.enabled,
                self.learning,
                solar=solar,
                measured_efforts=efforts if record else None,
                sources={
                    source["entity_id"]: self.source_value(source) or 0
                    for source in self.config.get("extra_heat_sources", [])
                },
            )
            hand = [
                s["entity_id"]
                for s in self.active_systems(include_inactive=True)
                if not self.system_control.get(s["entity_id"], True)
            ]
            if hand:
                decision.explanation += f" Under hand control: {', '.join(hand)}."
                if not control_systems:
                    decision.explanation += " Every unit is under hand control."
            decision.explanation = (
                f"Priority mode {self.priority_mode}. " + decision.explanation
            )
            for entity_id, sensor_filter in self.filters.items():
                if (
                    sensor_filter.pending
                    or sensor_filter.rejected
                    or any(
                        b is None or b > now.timestamp() - sensor_filter.window
                        for _, b, _, _ in sensor_filter.excursions
                    )
                ):
                    entity = self.hass.states.get(entity_id)
                    name = entity.name if entity else entity_id
                    decision.explanation += f" {name} sensor transient ignored."
            for entity_id, details in self.not_reporting.items():
                decision.explanation += (
                    f" {entity_id}: not reporting ({details['reason']}); "
                    f"last real change {details['last_change']}; check its battery."
                )
            for key, direction in self.learning.disturbed.items():
                entity = self.hass.states.get(key)
                name = entity.name if entity else key
                decision.explanation += (
                    f" unexplained {direction} in {name}; not learning from it."
                )
            if not self.learning_enabled:
                decision.explanation += " Learning paused by switch."
            if adoption:
                decision.explanation = f"{adoption} {decision.explanation}"
            fan_systems = {}
            for entity_id, device in self.active_devices(devices).items():
                entity = self.hass.states.get(entity_id)
                attrs = entity.attributes if entity else {}
                fan_systems[entity_id] = {
                    "available": device.available,
                    "name": entity.name if entity else entity_id,
                    "running": device.hvac_action in ("heating", "cooling"),
                    "fan_mode": attrs.get("fan_mode"),
                    "swing_mode": attrs.get("swing_mode"),
                    "fan_modes": quiet_modes(
                        attrs.get("fan_modes") or [],
                        next(
                            s
                            for s in self.active_systems(include_inactive=True)
                            if s["entity_id"] == entity_id
                        ).get("quiet_max_fan"),
                        self.config.get("quiet_hours_enabled", False),
                        dt_util.as_local(now).strftime("%H:%M"),
                        self.config.get("quiet_start", "22:00")[:5],
                        self.config.get("quiet_end", "07:00")[:5],
                    ),
                    "swing_modes": attrs.get("swing_modes") or [],
                }
                fan_systems[entity_id]["quiet_restricted"] = fan_systems[entity_id][
                    "fan_modes"
                ] != list(attrs.get("fan_modes") or [])
            fan_command = self.fans.tick(
                now.timestamp(),
                self.state.active_mode,
                outdoor,
                fan_systems,
                clean_values,
                self.priority_weights,
                self.state.setpoint,
                valid=fan_clean,
                invalid_reason="; ".join(fan_problems),
                enabled=self.enabled and self.fan_enabled and self.state.mode != "off",
            )
            if fan_command:
                decision.commands.append(fan_command)
            if self.enabled and self.fan_enabled:
                decision.explanation += " " + self.fans.last_decision
            failures = set()
            for entity_id, service, data in decision.commands:
                if entity_id in failures:
                    continue
                previous_setting = self.command_settings.get(entity_id)
                try:
                    self.pending_commands[entity_id] = data
                    if service in ("set_hvac_mode", "set_temperature"):
                        # The command is the learning label from when it is
                        # sent; a failed call restores the previous one.
                        device = devices[entity_id]
                        self.command_settings[entity_id] = {
                            "mode": data.get(
                                "hvac_mode",
                                (previous_setting or {}).get("mode", device.mode),
                            ),
                            "target": data.get(
                                "temperature",
                                (previous_setting or {}).get(
                                    "target", device.temperature
                                ),
                            ),
                            "sent_at": dt_util.utcnow().timestamp(),
                        }
                    if service == "set_hvac_mode" and not self.command_labelled(
                        entity_id
                    ):
                        # Mark uncertainty before service callbacks can report
                        # either the old action or a delayed mode acknowledgement.
                        # A command-labelled system knows the switch instead.
                        self.effort.change(
                            entity_id, None, dt_util.utcnow().timestamp()
                        )
                    await self.hass.services.async_call(
                        "climate",
                        service,
                        {"entity_id": entity_id, **data},
                        blocking=True,
                    )
                    if service in ("set_fan_mode", "set_swing_mode"):
                        action = "fan" if service == "set_fan_mode" else "swing"
                        self.last_fan_commands[(entity_id, action)] = (
                            data[f"{action}_mode"],
                            dt_util.utcnow(),
                        )
                    if service in ("set_hvac_mode", "set_temperature"):
                        device = devices[entity_id]
                        mode, target, _ = self.last_commands.get(
                            entity_id, (device.mode, device.temperature, None)
                        )
                        self.last_commands[entity_id] = (
                            data.get("hvac_mode", mode),
                            data.get("temperature", target),
                            dt_util.utcnow(),
                        )
                except HomeAssistantError as err:
                    failures.add(entity_id)
                    if previous_setting is None:
                        self.command_settings.pop(entity_id, None)
                    else:
                        self.command_settings[entity_id] = previous_setting
                    if service in ("set_fan_mode", "set_swing_mode"):
                        self.fans.invalidate()
                    decision.explanation += f" {entity_id}: command failed ({err})."
                    _LOGGER.warning("Command to %s failed: %s", entity_id, err)
                finally:
                    self.pending_commands.pop(entity_id, None)
            for entity_id in {
                entity_id
                for entity_id, service, _ in decision.commands
                if service in ("set_hvac_mode", "set_temperature")
            }:
                # Re-label from the command just sent, at its send time.
                if (
                    system := next(
                        (
                            s
                            for s in self.active_systems(include_inactive=True)
                            if s["entity_id"] == entity_id
                        ),
                        None,
                    )
                ) is None or not self.command_labelled(entity_id):
                    continue
                entity = self.hass.states.get(entity_id)
                available = bool(
                    entity and entity.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
                )
                stamp = dt_util.utcnow().timestamp()
                self.feed_effort(
                    entity_id,
                    self.learning_action(
                        system,
                        entity.attributes.get("hvac_action") if available else None,
                        entity.state if available else None,
                        stamp,
                        available,
                    ),
                    stamp,
                    entity.state if entity else None,
                )
            self.sync_report_issues(now.timestamp())
            _LOGGER.debug("%s", decision.explanation)
            if record:
                occupancy = self.hass.states.get(
                    self.config.get("occupancy_sensor", "")
                )
                self.samples.append(
                    {
                        "timestamp": now.timestamp(),
                        "sensors": raw,
                        "outdoor": outdoor,
                        "solar": solar,
                        "efforts": efforts,
                        "extra_heat_sources": source_fractions,
                        "occupancy": occupancy.state if occupancy else None,
                        "systems": {
                            key: {
                                **asdict(value),
                                "offset": self.state.offsets.get(key, 0),
                            }
                            for key, value in devices.items()
                        },
                    }
                )
            cutoff = now.timestamp() - 30 * 86400
            self.samples = [s for s in self.samples if s["timestamp"] >= cutoff]
            await self.store.async_save(
                {
                    "priority_mode": self.priority_mode,
                    "state": asdict(self.state),
                    "system_control": self.system_control,
                    "enabled": self.enabled,
                    "fan_enabled": self.fan_enabled,
                    "learning_enabled": self.learning_enabled,
                    "learning_resume_at": self.learning_resume_at,
                    "fans": self.fans.to_dict(),
                    "samples": self.samples,
                    "learning": self.learning.to_dict(),
                    "source_responses": {
                        source["entity_id"]: source.get("response", "on_off")
                        for source in self.config.get("extra_heat_sources", [])
                    },
                    "reporting": self.reporting.to_dict(),
                    "command_settings": self.command_settings,
                    "report_marks": self.report_marks,
                    # v0.5.24: its own key, so v0.5.23 still loads "state".
                    "fresh_thermostat_points": self.state.fresh_thermostat_points,
                    "latent": self.latent.to_dict(),
                    "filters": {key: f.to_dict() for key, f in self.filters.items()},
                }
            )
            self.async_set_updated_data(decision)

    def device_last_seen(self, entity_id):
        """Resolve optional timestamp siblings through the device/entity registries."""
        registry = er.async_get(self.hass)
        member = registry.async_get(entity_id)
        if (
            not member
            or not member.device_id
            or not dr.async_get(self.hass).async_get(member.device_id)
        ):
            return None
        timestamps = []
        for sibling in er.async_entries_for_device(registry, member.device_id):
            entity = self.hass.states.get(sibling.entity_id)
            if (
                sibling.entity_id.endswith("_last_seen")
                and entity
                and entity.attributes.get("device_class") == "timestamp"
                and (parsed := dt_util.parse_datetime(entity.state)) is not None
            ):
                timestamps.append(dt_util.as_utc(parsed))
        return min(timestamps) if timestamps else None

    async def async_seed_history(self, now):
        """Seed real numeric changes and observed action runtime from recorder.

        Compare consecutive numeric values, ignoring restart/unavailable markers.
        A retained first observation is a conservative lower bound on value age.
        Store evidence wins unless recorder contains a later actual value change.
        """
        if DATA_INSTANCE not in self.hass.data:
            return
        from homeassistant.components.recorder.history import get_significant_states

        sensors = [s["entity_id"] for s in self.config["sensors"]]
        systems = [s["entity_id"] for s in self.active_systems(include_inactive=True)]

        def read():
            return (
                get_significant_states(
                    self.hass,
                    dt_util.utc_from_timestamp(0),
                    now,
                    sensors,
                    no_attributes=True,
                ),
                get_significant_states(
                    self.hass,
                    dt_util.utc_from_timestamp(0),
                    now,
                    systems,
                    significant_changes_only=False,
                ),
            )

        try:
            history, activity = await get_instance(self.hass).async_add_executor_job(
                read
            )
        except Exception:  # Recorder is optional; a failed query must not stop control.
            _LOGGER.warning(
                "Could not seed sensor reporting from recorder", exc_info=True
            )
            return
        for entity_id, states in history.items():
            value = None
            changed = None
            transitioned = False
            for state in states:
                try:
                    number = float(state.state)
                except TypeError, ValueError:
                    continue
                if isfinite(number) and number != value:
                    transitioned = value is not None
                    value, changed = number, state.last_updated.timestamp()
            if changed is None:
                continue
            previous = self.reporting.sensors.get(entity_id)
            if previous is None or (
                (previous["value"] != value or transitioned)
                and changed > previous["changed"]
            ):
                self.reporting.sensors[entity_id] = {
                    "value": value,
                    "changed": changed,
                    "frozen": False,
                }
        for entity_id, states in activity.items():
            last_run = None
            cycling = ControlState()
            for index, state in enumerate(states):
                if state.state not in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                ) and state.attributes.get("hvac_action") in ("heating", "cooling"):
                    end = (
                        states[index + 1].last_updated
                        if index + 1 < len(states)
                        else now
                    )
                    # With no following transition, only the retained observation
                    # is evidence; a recorder tail must not count HA downtime.
                    last_run = (
                        end.timestamp()
                        if index + 1 < len(states)
                        else state.last_updated.timestamp()
                    )
                    self.reporting.add_interval(
                        max(
                            state.last_updated.timestamp(),
                            now.timestamp() - self.frozen_window,
                        ),
                        end.timestamp(),
                    )

            if entity_id not in self.state.thermostats:
                system = next(
                    s
                    for s in self.active_systems(include_inactive=True)
                    if s["entity_id"] == entity_id
                )
                for recorded in sorted(states, key=lambda s: s.last_updated):
                    self.state.observe_report(
                        system,
                        Device(
                            available=recorded.state
                            not in (STATE_UNAVAILABLE, STATE_UNKNOWN),
                            mode=recorded.state,
                            temperature=recorded.attributes.get("temperature"),
                            current_temperature=recorded.attributes.get(
                                "current_temperature"
                            ),
                            hvac_action=recorded.attributes.get("hvac_action"),
                            name=recorded.name,
                        ),
                        self.config,
                    )
                    if (
                        recorded.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
                        and entity_id in self.state.thermostats
                    ):
                        self.state.thermostats[entity_id]["time"] = (
                            recorded.last_updated.timestamp()
                        )

            # Replay action transitions, including attribute-only changes. Merge
            # retained evidence without replacing newer Store/event observations.
            for recorded in sorted(states, key=lambda s: s.last_updated):
                action = (
                    recorded.attributes.get("hvac_action")
                    if recorded.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
                    else None
                )
                cycling.observe_action(
                    entity_id, action, recorded.last_updated.timestamp()
                )
            saved_transition = max(
                self.state.last_start.get(entity_id, float("-inf")),
                self.state.last_stop.get(entity_id, float("-inf")),
            )
            recorded_transition = max(
                cycling.last_start.get(entity_id, float("-inf")),
                cycling.last_stop.get(entity_id, float("-inf")),
            )
            for name in ("last_start", "last_stop"):
                evidence = getattr(cycling, name).get(entity_id)
                clocks = getattr(self.state, name)
                if evidence is not None:
                    clocks[entity_id] = max(
                        evidence, clocks.get(entity_id, float("-inf"))
                    )
            self.state.starts[entity_id] = sorted(
                {
                    t
                    for t in self.state.starts.get(entity_id, [])
                    + cycling.starts.get(entity_id, [])
                    if now.timestamp() - 3600 < t <= now.timestamp()
                }
            )
            # Seed the last known action so the first current observation does
            # not invent a fresh start for a compressor already running.
            if entity_id in cycling.actions and (
                entity_id not in self.state.actions
                or recorded_transition > saved_transition
            ):
                self.state.actions[entity_id] = cycling.actions[entity_id]
                self.state.action_times[entity_id] = now.timestamp()
            if last_run is not None:
                self.state.last_run.setdefault(entity_id, last_run)

    def solar_kw(self):
        entity_id = self.config.get("solar_sensor")
        if not entity_id:
            return 0.0
        value = self.number(entity_id)
        if value is None:
            return None
        unit = self.hass.states.get(entity_id).attributes.get("unit_of_measurement")
        factors = {"W": 0.001, "kW": 1, "MW": 1000}
        return value * factors[unit] if unit in factors else None

    def sync_report_issues(self, now):
        """Repair issue while a system's command labels are contradicted."""
        for system in self.active_systems(include_inactive=True):
            key = system["entity_id"]
            issue_id = f"{self.config_entry.entry_id}_{key}_command_contradicted"
            if self.reporting.checks.suspended(key):
                status = self.reporting.checks.status(key)
                entity = self.hass.states.get(key)
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="command_contradicted",
                    translation_placeholders={
                        "system": entity.name if entity else key,
                        "rate": f"{status['rate'] * 100:.0f}",
                    },
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def sync_regime_issues(self, now):
        for sensor, state in self.learning.regimes.items():
            issue_id = f"{self.config_entry.entry_id}_{sensor}_regime"
            since = state["disturbed_since"]
            if since is not None and now - since >= 12 * 3600:
                entity = self.hass.states.get(sensor)
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="regime",
                    translation_placeholders={
                        "room": entity.name if entity else sensor
                    },
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    async def async_room_learning(self, sensor, action, enabled=None):
        async with self.lock:
            self.learning.clock = dt_util.utcnow().timestamp()
            if action == "accept":
                self.learning.promote(sensor)
            elif action == "relearn":
                self.learning.relearn(sensor)
            elif action == "temporary":
                self.learning.set_temporary(
                    sensor, enabled, self.config.get("temporary_hours", 0)
                )
            else:
                raise ValueError("Unknown room learning action")
            if self.learning_baseline is not None:
                self.learning_baseline[0].pop(sensor, None)
            self.fans.invalidate("Room learning control changed")
        await self.async_tick(record=False)

    async def async_reset_learning(self):
        async with self.lock:
            self.learning = self.new_learning()
            self.sync_label_delay()
            self.fans = FanLearning(
                self.config.get("fan_hold_minutes", 30),
                fan_reward_weight(self.config),
                learn_exclude_modes=self.config.get("learn_exclude_modes", "auto"),
            )
            self.reset_learning_interval()
        await self.async_tick(record=False)

    def adopt_systems(self, devices):
        """Start fresh entries from current systems before issuing any commands."""
        available = [device for device in devices.values() if device.available]
        targets = [d.temperature for d in available if d.temperature is not None]
        target = sum(targets) / len(targets) if targets else 21
        self.state.setpoint = max(7, min(35, floor(target * 2 + 0.5) / 2))
        modes = {device.mode for device in available}
        if len(modes) == 1 and modes <= {"heat", "cool", "heat_cool"}:
            self.state.mode = modes.pop()
            # Auto mode retains the systems' current mode inside the deadband.
            self.state.active_mode = self.state.mode
            note = f"Adopted the systems' current {self.state.mode} mode."
        else:
            self.state.mode = "off"
            self.enabled = False
            self.awaiting_mode = True
            note = (
                "Systems disagree, are off, or have no available mode; "
                "control is off and observing only until you choose a House mode."
            )
        return f"{note} Starting House target is {self.state.setpoint:.1f} °C."

    async def async_change(
        self,
        *,
        setpoint=None,
        delta=None,
        mode=None,
        enabled=None,
        fan_enabled=None,
        priority_mode=None,
        learning_enabled=None,
    ):
        # Mutations share the lock with in-flight commands.
        async with self.lock:
            if any(
                v is not None for v in (delta, setpoint, mode, enabled, fan_enabled)
            ):
                self.fans.invalidate("House setpoint, mode or control switch changed")
            if learning_enabled is not None:
                self.learning_enabled = learning_enabled
                hours = self.config.get("learning_pause_hours", 0)
                self.learning_resume_at = (
                    dt_util.utcnow().timestamp() + hours * 3600
                    if not learning_enabled and hours
                    else None
                )
                self.reset_learning_interval()
                self.fans.invalidate("learning switch changed")
                self.fans.window = self.fans.resume = None
            if priority_mode is not None:
                if priority_mode not in modes(self.config):
                    raise ValueError("Unknown priority mode")
                self.priority_mode = priority_mode
            if fan_enabled is not None:
                self.fan_enabled = fan_enabled
            if delta is not None:
                setpoint = self.state.setpoint + delta
            if setpoint is not None:
                self.state.setpoint = max(7, min(35, round(setpoint * 2) / 2))
            if mode is not None:
                self.state.mode = mode
                if self.awaiting_mode:
                    self.enabled = True
                    self.awaiting_mode = False
            if enabled is not None:
                self.enabled = enabled
        await self.async_tick(record=False)

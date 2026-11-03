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

from .const import DOMAIN
from .controller import ControlState, Device, Reading, control
from .fans import FanLearning, quiet_modes
from .filtering import SensorFilter
from .learning import EffortTracker, Learning
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
        self.fan_enabled = True
        self.learning_enabled = True
        self.learning_resume_at = None
        self.source_effort = EffortTracker()
        self.fans = FanLearning(
            self.config.get("fan_hold_minutes", 30),
            self.config.get("fan_reward_lambda", 1),
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
        self.learning_baseline = None
        self.reporting = Reporting()
        self.not_reporting = {}
        self.history_seeded = False
        self.filters = {
            m["entity_id"]: SensorFilter(
                self.config.get("filter_minutes", 15), self.config.get("max_rate", 0.5)
            )
            for m in self.config["sensors"]
        }
        self.raw_readings = {}
        self.startup_sensors = set(self.filters)

    @property
    def priority_weights(self):
        return modes(self.config)[self.priority_mode]

    @property
    def learning_sensors(self):
        return list(
            dict.fromkeys(
                [s["entity_id"] for s in self.config["sensors"]]
                + [s["entity_id"] for s in self.config["systems"]]
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
            [s["entity_id"] for s in self.config["systems"]],
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
        )

    async def async_load(self):
        if saved := await self.store.async_load():
            selected = saved.get("priority_mode")
            if selected not in modes(self.config):
                selected = self.config.get("priority_renames", {}).get(selected)
            if selected in modes(self.config):
                self.priority_mode = selected
            self.state = ControlState(**saved["state"])
            # Retain inactivity/cooldown clocks, but never count downtime as runtime.
            self.state.action_times = {
                key: dt_util.utcnow().timestamp() for key in self.state.actions
            }
            if self.state.excitation:
                self.state.last_excite[self.state.excitation["system"]] = (
                    dt_util.utcnow().timestamp()
                )
                self.state.excitation = {}
            self.enabled = saved.get("enabled", True)
            self.fan_enabled = saved.get("fan_enabled", True)
            self.learning_enabled = saved.get("learning_enabled", True)
            self.learning_resume_at = saved.get("learning_resume_at")
            self.fans = FanLearning(
                self.config.get("fan_hold_minutes", 30),
                self.config.get("fan_reward_lambda", 1),
                learn_exclude_modes=self.config.get("learn_exclude_modes", "auto"),
                saved=saved.get("fans"),
            )
            self.fans.reconcile([s["entity_id"] for s in self.config["systems"]])
            self.samples = saved.get("samples", [])
            # Upgrade old Store data even when recorder is absent during an outage.
            for system in self.config["systems"]:
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
            self.reporting = Reporting(saved.get("reporting"))
            for key in self.filters:
                self.filters[key] = SensorFilter(
                    self.config.get("filter_minutes", 15),
                    self.config.get("max_rate", 0.5),
                    saved.get("filters", {}).get(key),
                )
            if saved.get("learning"):
                self.learning = Learning.from_dict(saved["learning"])
                self.learning.reconcile(
                    self.learning_sensors,
                    [s["entity_id"] for s in self.config["systems"]],
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

    def start(self):
        cancel_timer = async_track_time_interval(
            self.hass,
            self.async_tick,
            timedelta(minutes=self.config.get("interval", 5)),
        )
        cancel_states = async_track_state_change_event(
            self.hass,
            [s["entity_id"] for s in self.config["systems"]],
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

        @callback
        def cancel():
            cancel_timer()
            cancel_states()
            cancel_sensors()
            cancel_sources()

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
        elif entity.state in ("on", "off"):
            on = entity.state == "on"
        else:
            return None
        return "heating" if on else "off"

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
        self.state.observe_action(
            entity_id,
            new.attributes.get("hvac_action")
            if new and new.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            else None,
            dt_util.utcnow().timestamp(),
        )
        if new and new.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self.state.observe_report(
                next(s for s in self.config["systems"] if s["entity_id"] == entity_id),
                Device(
                    mode=new.state,
                    hvac_action=new.attributes.get("hvac_action"),
                    temperature=self.number(entity_id, "temperature"),
                    current_temperature=self.number(entity_id, "current_temperature"),
                    step=self.number(entity_id, "target_temp_step") or 0.5,
                    name=new.name,
                ),
                self.config,
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
        self.effort.change(
            entity_id,
            new.attributes.get("hvac_action")
            if new and new.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            else None,
            dt_util.utcnow().timestamp(),
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
            if old.state != new.state or any(
                old.attributes.get(f"{action}_mode")
                != new.attributes.get(f"{action}_mode")
                and new.attributes.get(f"{action}_mode")
                != self.fans.chosen.get(entity_id, {}).get(action)
                for action in ("fan", "swing")
            ):
                self.fans.invalidate(f"{entity_id} mode or fan/vane setting changed")
        if self.lock.locked() or self.data is None or old is None or new is None:
            return
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
            if (dt_util.utcnow() - sent_at).total_seconds() <= 120 or (
                new.state == mode and new.attributes.get("temperature") == target
            ):
                return
            note = f" {new.entity_id} changed outside house control; next interval will re-assert."
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
                    for s in self.config["systems"]
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
                            key not in self.filters
                            or (
                                not self.filters[key].blocked
                                and not self.filters[key].contaminated
                            )
                        )
                    },
                    outdoor_before,
                    solar_before,
                )
            devices = {}
            for system in self.config["systems"]:
                entity_id = system["entity_id"]
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
                    adoption = self.adopt_systems(devices)
                self.initialized = True
            outdoor = self.number(self.config.get("outdoor_sensor"))
            if outdoor is None:
                outdoor = self.number(self.config.get("weather_entity"), "temperature")
            solar = self.solar_kw()
            # Fan rewards use the robust filter's value, including its held anchor.
            # Pending/rejected excursions block thermal rows, not fan experiments.
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
            fan_clean = not fan_problems
            if not fan_clean:
                self.fans.invalidate("; ".join(fan_problems))
            if record:
                efforts, duration = self.effort.finish(
                    {
                        key: device.hvac_action if device.available else None
                        for key, device in devices.items()
                    },
                    now.timestamp(),
                )
                source_fractions, _ = self.source_effort.finish(
                    {
                        source["entity_id"]: self.source_action(source)
                        for source in self.config.get("extra_heat_sources", [])
                    },
                    now.timestamp(),
                )
                learning_values = {
                    key: value
                    for key, value in clean_values.items()
                    if not self.filters[key].blocked
                    and not self.filters[key].contaminated
                }
                learning_values.update(
                    {
                        key: device.current_temperature
                        for key, device in devices.items()
                        if device.available and device.current_temperature is not None
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
                        now=now.timestamp(),
                        local_hour=dt_util.as_local(now).hour
                        + dt_util.as_local(now).minute / 60
                        + dt_util.as_local(now).second / 3600,
                    )
                self.learning_baseline = (
                    (learning_values, outdoor, solar) if self.learning_enabled else None
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
                for system in self.config["systems"]
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
                devices,
                now.timestamp(),
                outdoor,
                self.enabled,
                self.learning,
                solar=solar,
                measured_efforts=efforts if record else None,
                sources={
                    source["entity_id"]: float(self.source_action(source) == "heating")
                    for source in self.config.get("extra_heat_sources", [])
                },
            )
            decision.explanation = (
                f"Priority mode {self.priority_mode}. " + decision.explanation
            )
            for entity_id, sensor_filter in self.filters.items():
                if sensor_filter.pending or sensor_filter.rejected:
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
            for entity_id, device in devices.items():
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
                            for s in self.config["systems"]
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
                try:
                    self.pending_commands[entity_id] = data
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
                    self.fans.invalidate()
                    decision.explanation += f" {entity_id}: command failed ({err})."
                    _LOGGER.warning("Command to %s failed: %s", entity_id, err)
                finally:
                    self.pending_commands.pop(entity_id, None)
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
                    "enabled": self.enabled,
                    "fan_enabled": self.fan_enabled,
                    "learning_enabled": self.learning_enabled,
                    "learning_resume_at": self.learning_resume_at,
                    "fans": self.fans.to_dict(),
                    "samples": self.samples,
                    "learning": self.learning.to_dict(),
                    "reporting": self.reporting.to_dict(),
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
        systems = [s["entity_id"] for s in self.config["systems"]]

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
                    s for s in self.config["systems"] if s["entity_id"] == entity_id
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
            self.fans = FanLearning(
                self.config.get("fan_hold_minutes", 30),
                self.config.get("fan_reward_lambda", 1),
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

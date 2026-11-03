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
from .reporting import Reporting

_LOGGER = logging.getLogger(__name__)


class HouseCoordinator(DataUpdateCoordinator):
    """Serialize immediate requests and interval ticks; persist bounded history."""

    def __init__(self, hass, entry):
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry)
        self.config = dict(entry.options or entry.data)
        self.state = ControlState()
        self.enabled = True
        self.fan_enabled = True
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

    def new_learning(self):
        return Learning(
            [s["entity_id"] for s in self.config["sensors"]],
            [s["entity_id"] for s in self.config["systems"]],
            self.config.get("learning_forgetting", 0.998),
            self.config.get("learning_n0", 24),
        )

    async def async_load(self):
        if saved := await self.store.async_load():
            self.state = ControlState(**saved["state"])
            self.enabled = saved.get("enabled", True)
            self.fan_enabled = saved.get("fan_enabled", True)
            self.fans = FanLearning(
                self.config.get("fan_hold_minutes", 30),
                self.config.get("fan_reward_lambda", 1),
                learn_exclude_modes=self.config.get("learn_exclude_modes", "auto"),
                saved=saved.get("fans"),
            )
            self.fans.reconcile([s["entity_id"] for s in self.config["systems"]])
            self.samples = saved.get("samples", [])
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
                    [s["entity_id"] for s in self.config["sensors"]],
                    [s["entity_id"] for s in self.config["systems"]],
                )
                self.learning.forgetting = self.config.get("learning_forgetting", 0.998)
                self.learning.n0 = self.config.get("learning_n0", 24)

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

        @callback
        def cancel():
            cancel_timer()
            cancel_states()
            cancel_sensors()

        return cancel

    @property
    def frozen_window(self):
        return self.config.get("frozen_after", 4) * 3600

    def sensor_blocks_fans(self, entity_id):
        weights = {
            m["entity_id"]: max(0, m.get("weight", 1)) for m in self.config["sensors"]
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
            value is None or not isfinite(value) or self.filters[entity_id].blocked
        ):
            self.fans.invalidate()
        # Recover control and repairs on the first numeric change, even between ticks.
        if entity_id in self.not_reporting:
            self.hass.async_create_task(self.async_tick(record=False))

    @callback
    def _system_changed(self, event):
        old, new = event.data.get("old_state"), event.data.get("new_state")
        # Runtime changes must be captured even during service calls under lock.
        entity_id = event.data["entity_id"]
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
            if old.attributes.get("temperature") != new.attributes.get(
                "temperature"
            ) and (
                not commanded_target
                or new.attributes.get("temperature") != commanded_target[1]
            ):
                self.fans.invalidate()
            if old.state != new.state or any(
                old.attributes.get(f"{action}_mode")
                != new.attributes.get(f"{action}_mode")
                and new.attributes.get(f"{action}_mode")
                != self.fans.chosen.get(entity_id, {}).get(action)
                for action in ("fan", "swing")
            ):
                self.fans.invalidate()
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
                        and not self.filters[key].blocked
                        and not self.filters[key].contaminated
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
            fan_clean = all(
                not self.filters[key].blocked
                and reading.reporting
                and reading.value is not None
                and reading.age <= self.config.get("stale_after", 720) * 60
                for key, reading in readings.items()
                if self.sensor_blocks_fans(key)
            )
            if not fan_clean:
                self.fans.invalidate()
            clean_values = {
                key: r.value
                for key, r in readings.items()
                if not self.filters[key].blocked
                and r.reporting
                and r.value is not None
                and r.age <= self.config.get("stale_after", 720) * 60
            }
            if record:
                efforts, duration = self.effort.finish(
                    {
                        key: device.hvac_action if device.available else None
                        for key, device in devices.items()
                    },
                    now.timestamp(),
                )
                learning_values = {
                    key: value
                    for key, value in clean_values.items()
                    if not self.filters[key].contaminated
                }
                if (
                    self.learning_baseline is not None
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
                    )
                self.learning_baseline = (learning_values, outdoor, solar)
                for sensor_filter in self.filters.values():
                    sensor_filter.finish_interval()
            decision = control(
                self.config,
                self.state,
                readings,
                devices,
                now.timestamp(),
                outdoor,
                self.enabled,
                self.learning,
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
                {m["entity_id"]: m.get("weight", 1) for m in self.config["sensors"]},
                self.state.setpoint,
                valid=fan_clean,
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
                    "state": asdict(self.state),
                    "enabled": self.enabled,
                    "fan_enabled": self.fan_enabled,
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
                    now - timedelta(seconds=self.frozen_window),
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
        for states in activity.values():
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
                    self.reporting.add_interval(
                        max(
                            state.last_updated.timestamp(),
                            now.timestamp() - self.frozen_window,
                        ),
                        end.timestamp(),
                    )

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

    async def async_reset_learning(self):
        async with self.lock:
            self.learning = self.new_learning()
            self.fans = FanLearning(
                self.config.get("fan_hold_minutes", 30),
                self.config.get("fan_reward_lambda", 1),
                learn_exclude_modes=self.config.get("learn_exclude_modes", "auto"),
            )
            self.learning_baseline = None
            self.effort = EffortTracker()
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
        self, *, setpoint=None, delta=None, mode=None, enabled=None, fan_enabled=None
    ):
        # Mutations share the lock with in-flight commands.
        async with self.lock:
            if any(
                v is not None for v in (delta, setpoint, mode, enabled, fan_enabled)
            ):
                self.fans.invalidate()
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

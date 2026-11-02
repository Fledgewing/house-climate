"""Home Assistant I/O and persisted control/sample state."""

import asyncio
import logging
from dataclasses import asdict, replace
from datetime import timedelta
from math import floor, isfinite

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .controller import ControlState, Device, Reading, control
from .learning import EffortTracker, Learning

_LOGGER = logging.getLogger(__name__)


class HouseCoordinator(DataUpdateCoordinator):
    """Serialize immediate requests and interval ticks; persist bounded history."""

    def __init__(self, hass, entry):
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry)
        self.config = dict(entry.options or entry.data)
        self.state = ControlState()
        self.enabled = True
        self.restored = False
        self.initialized = False
        self.awaiting_mode = False
        self.samples = []
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self.lock = asyncio.Lock()
        self.last_commands = {}
        self.learning = self.new_learning()
        self.effort = EffortTracker()
        self.learning_baseline = None

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
            self.samples = saved.get("samples", [])
            self.restored = True
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

        @callback
        def cancel():
            cancel_timer()
            cancel_states()

        return cancel

    @callback
    def _system_changed(self, event):
        old, new = event.data.get("old_state"), event.data.get("new_state")
        # Runtime changes must be captured even during service calls under lock.
        entity_id = event.data["entity_id"]
        self.effort.change(
            entity_id,
            new.attributes.get("hvac_action")
            if new and new.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            else None,
            dt_util.utcnow().timestamp(),
        )
        if self.lock.locked() or self.data is None or old is None or new is None:
            return
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
                readings[entity_id] = Reading(value, age)
                raw[entity_id] = {
                    "value": value,
                    "age": age,
                    "state": entity.state if entity else None,
                }
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
            if record:
                efforts, duration = self.effort.finish(
                    {
                        key: device.hvac_action if device.available else None
                        for key, device in devices.items()
                    },
                    now.timestamp(),
                )
                valid = {
                    key: r.value
                    for key, r in readings.items()
                    if r.value is not None
                    and r.age <= self.config.get("stale_after", 720) * 60
                }
                if (
                    self.learning_baseline is not None
                    and outdoor is not None
                    and solar is not None
                ):
                    before, previous_outdoor, previous_solar = self.learning_baseline
                    self.learning.update(
                        before,
                        valid,
                        previous_outdoor,
                        efforts,
                        previous_solar,
                        duration / (self.config.get("interval", 5) * 60),
                    )
                self.learning_baseline = (valid, outdoor, solar)
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
            if adoption:
                decision.explanation = f"{adoption} {decision.explanation}"
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
                    "samples": self.samples,
                    "learning": self.learning.to_dict(),
                }
            )
            self.async_set_updated_data(decision)

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

    async def async_change(self, *, setpoint=None, delta=None, mode=None, enabled=None):
        # Mutations share the lock with in-flight commands.
        async with self.lock:
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

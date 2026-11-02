"""Home Assistant I/O and persisted control/sample state."""

import asyncio
import logging
from dataclasses import asdict, replace
from datetime import timedelta
from math import isfinite

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

_LOGGER = logging.getLogger(__name__)


class HouseCoordinator(DataUpdateCoordinator):
    """Serialize immediate requests and interval ticks; persist bounded history."""

    def __init__(self, hass, entry):
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry)
        self.config = dict(entry.options or entry.data)
        self.state = ControlState()
        self.enabled = True
        self.restored = False
        self.samples = []
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self.lock = asyncio.Lock()

    async def async_load(self):
        if saved := await self.store.async_load():
            self.state = ControlState(**saved["state"])
            self.enabled = saved.get("enabled", True)
            self.samples = saved.get("samples", [])
            self.restored = True

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
        if self.lock.locked() or self.data is None or old is None or new is None:
            return
        if old.state != new.state or old.attributes.get(
            "temperature"
        ) != new.attributes.get("temperature"):
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
                age = (now - entity.last_reported).total_seconds() if entity else 1e9
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
            outdoor = self.number(self.config.get("outdoor_sensor"))
            if outdoor is None:
                outdoor = self.number(self.config.get("weather_entity"), "temperature")
            decision = control(
                self.config,
                self.state,
                readings,
                devices,
                now.timestamp(),
                outdoor,
                self.enabled,
            )
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
                        "solar": self.number(self.config.get("solar_sensor")),
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
                }
            )
            self.async_set_updated_data(decision)

    async def async_change(self, *, setpoint=None, delta=None, mode=None, enabled=None):
        # Mutations share the lock with in-flight commands.
        async with self.lock:
            if delta is not None:
                setpoint = self.state.setpoint + delta
            if setpoint is not None:
                self.state.setpoint = max(7, min(35, round(setpoint * 2) / 2))
            if mode is not None:
                self.state.mode = mode
            if enabled is not None:
                self.enabled = enabled
        await self.async_tick(record=False)

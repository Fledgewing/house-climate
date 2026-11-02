"""Selector-based configuration for arbitrary sensors and systems."""

from copy import deepcopy

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import DEFAULTS, DOMAIN


def entities(domain, multiple=False, device_class=None):
    config = {"domain": domain, "multiple": multiple}
    if device_class is not None:
        config["device_class"] = device_class
    return selector.EntitySelector(selector.EntitySelectorConfig(**config))


def number(low, high, step=0.1):
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=low, max=high, step=step, mode=selector.NumberSelectorMode.BOX
        )
    )


class HouseSteps:
    """Shared wizard; selecting a new list adds/removes members in options."""

    async def async_step_user(self, user_input=None):
        if not hasattr(self, "settings"):
            self.settings = deepcopy(DEFAULTS)
        errors = {}
        if user_input is not None:
            own_ids = {
                entity.entity_id
                for entity in er.async_get(self.hass).entities.values()
                if entity.platform == DOMAIN
            }
            if not user_input["sensors"] or not user_input["systems"]:
                errors["base"] = "empty_selection"
            elif own_ids.intersection(user_input["sensors"] + user_input["systems"]):
                errors["base"] = "self_reference"
            else:
                old = {s["entity_id"]: s for s in self.settings["sensors"]}
                self.settings["sensors"] = [
                    old.get(s, {"entity_id": s, "weight": 1.0, "zone": ""})
                    for s in dict.fromkeys(user_input["sensors"])
                ]
                old = {s["entity_id"]: s for s in self.settings["systems"]}
                self.settings["systems"] = [
                    old.get(s, {"entity_id": s, "preset": ""})
                    for s in dict.fromkeys(user_input["systems"])
                ]
                self.index = 0
                return await self.async_step_sensor()
        return self.async_show_form(
            step_id="user",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "sensors",
                        default=[s["entity_id"] for s in self.settings["sensors"]],
                    ): entities("sensor", True, "temperature"),
                    vol.Required(
                        "systems",
                        default=[s["entity_id"] for s in self.settings["systems"]],
                    ): entities("climate", True),
                }
            ),
        )

    async def async_step_sensor(self, user_input=None):
        member = self.settings["sensors"][self.index]
        if user_input is not None:
            member.update(user_input)
            self.index += 1
            if self.index == len(self.settings["sensors"]):
                self.index = 0
                return await self.async_step_system()
            return await self.async_step_sensor()
        return self.async_show_form(
            step_id="sensor",
            description_placeholders={"entity": member["entity_id"]},
            data_schema=vol.Schema(
                {
                    vol.Required("weight", default=member["weight"]): number(
                        0.01, 100, 0.01
                    ),
                    vol.Optional(
                        "zone", default=member.get("zone", "")
                    ): selector.TextSelector(),
                }
            ),
        )

    async def async_step_system(self, user_input=None):
        system = self.settings["systems"][self.index]
        if user_input is not None:
            system["preset"] = user_input.get("preset", "").strip()
            self.index += 1
            if self.index == len(self.settings["systems"]):
                return await self.async_step_tunables()
            return await self.async_step_system()
        return self.async_show_form(
            step_id="system",
            description_placeholders={"entity": system["entity_id"]},
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "preset",
                        description={"suggested_value": system.get("preset", "")},
                    ): selector.TextSelector(),
                }
            ),
        )

    async def async_step_tunables(self, user_input=None):
        if user_input is not None:
            self.settings.update(user_input)
            for key in (
                "outdoor_sensor",
                "weather_entity",
                "solar_sensor",
                "occupancy_sensor",
            ):
                self.settings[key] = user_input.get(key) or ""
            return self.async_create_entry(title="House Climate", data=self.settings)
        fields = {}
        for key, domain, device_class in [
            ("outdoor_sensor", "sensor", "temperature"),
            ("weather_entity", "weather", None),
            ("solar_sensor", "sensor", "power"),
            ("occupancy_sensor", "binary_sensor", "occupancy"),
        ]:
            fields[
                vol.Optional(
                    key,
                    description=(
                        {"suggested_value": self.settings[key]}
                        if self.settings.get(key)
                        else {}
                    ),
                )
            ] = entities(domain, device_class=device_class)
        for key, low, high, step in [
            ("interval", 1, 60, 1),
            ("stale_after", 1, 10080, 1),
            ("frozen_after", 1, 168, 0.5),
            ("deadband", 0.1, 10, 0.1),
            ("minimum_dwell", 0, 1440, 1),
            ("offset_clamp", 0, 10, 0.5),
            ("max_offset_change", 0.5, 5, 0.5),
            ("cool_outdoor_min", -40, 50, 0.5),
            ("heat_outdoor_max", -40, 50, 0.5),
            ("kp", 0, 10, 0.1),
            ("ki", 0, 10, 0.01),
            ("learning_forgetting", 0.9, 1, 0.001),
            ("learning_n0", 1, 1000, 1),
        ]:
            fields[vol.Required(key, default=self.settings[key])] = number(
                low, high, step
            )
        return self.async_show_form(step_id="tunables", data_schema=vol.Schema(fields))


class HouseConfigFlow(HouseSteps, config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return await super().async_step_user(user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HouseOptionsFlow()


class HouseOptionsFlow(HouseSteps, config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        self.settings = deepcopy(DEFAULTS)
        self.settings.update(
            deepcopy(dict(self.config_entry.options or self.config_entry.data))
        )
        return await self.async_step_user(user_input)

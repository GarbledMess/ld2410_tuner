from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .const import DOMAIN
from .history import location


class LD2410TunerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title="LD2410 Tuner", data={})
        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(_config_entry):
        return LD2410TunerOptionsFlow()


class LD2410TunerOptionsFlow(config_entries.OptionsFlow):
    """Where recorded history is stored. Saving reloads the integration, which moves it."""

    async def async_step_init(self, user_input=None):
        options = self.config_entry.options
        errors = {}
        if user_input is not None:
            try:
                chosen = await self.hass.async_add_executor_job(
                    location.check, self.hass, user_input.get(location.OPTION, "")
                )
            except location.LocationError as error:
                errors[location.OPTION] = error.reason
            else:
                return self.async_create_entry(
                    data={
                        location.OPTION: chosen,
                        location.MOVE_OPTION: bool(user_input.get(location.MOVE_OPTION, True)),
                    }
                )
        schema = vol.Schema(
            {
                vol.Optional(
                    location.OPTION,
                    description={"suggested_value": options.get(location.OPTION, "")},
                ): str,
                vol.Optional(
                    location.MOVE_OPTION, default=options.get(location.MOVE_OPTION, True)
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

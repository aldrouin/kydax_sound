"""Config flow for Kydax Symetrix."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)

from .const import DEFAULT_PORT, DOMAIN

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): TextSelector(),
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
            NumberSelector(
                NumberSelectorConfig(
                    min=1, max=65535, step=1, mode=NumberSelectorMode.BOX
                )
            ),
            vol.Coerce(int),
        ),
    }
)


class KydaxSymetrixConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup: appliance address. One entry per appliance."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_HOST])
            self._abort_if_unique_id_configured()
            # TODO: try connecting to the appliance before creating the entry
            # (errors["base"] = "cannot_connect" on failure).
            return self.async_create_entry(
                title=f"Symetrix {user_input[CONF_HOST]}",
                data={},
                options=dict(user_input),
            )

        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )

    # An options flow following the kydax_light menu pattern goes here once
    # there is configuration to manage (controller numbers, presets, ...).

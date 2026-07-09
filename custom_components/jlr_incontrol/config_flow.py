"""Config flow for Jaguar Land Rover InControl."""

from __future__ import annotations

import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import JlrApiError, JlrAuthError, JlrClient
from .const import (
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_PIN,
    CONF_USER_ID,
    CONF_USERNAME,
    DOMAIN,
)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_PIN): str,
    }
)


class JlrConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the JLR InControl config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate credentials by logging in and listing vehicles."""
        errors: dict[str, str] = {}
        if user_input is not None:
            device_id = str(uuid.uuid4())
            client = JlrClient(
                async_get_clientsession(self.hass),
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                device_id=device_id,
            )
            try:
                # login + register device + userId + list vehicles.
                await client.async_get_vehicles()
            except JlrAuthError:
                errors["base"] = "invalid_auth"
            except JlrApiError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001 - surface as a generic connection error
                errors["base"] = "cannot_connect"
            else:
                user_id = client.user_id
                await self.async_set_unique_id(user_id or user_input[CONF_USERNAME])
                self._abort_if_unique_id_configured()
                data = {
                    CONF_USERNAME: user_input[CONF_USERNAME],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_DEVICE_ID: device_id,
                    CONF_USER_ID: user_id,
                }
                if user_input.get(CONF_PIN):
                    data[CONF_PIN] = user_input[CONF_PIN]
                return self.async_create_entry(
                    title=user_input[CONF_USERNAME], data=data
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

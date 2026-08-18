"""Config flow for Jaguar Land Rover InControl."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import JlrClient
from .auth import JlrInvalidCode, JlrLogin, JlrLoginError
from .const import (
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_PIN,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    CONF_USERNAME,
    DISTANCE_UNIT_DEFAULT,
    DISTANCE_UNIT_KM,
    DISTANCE_UNIT_MILES,
    DOMAIN,
    OPT_DISTANCE_UNIT,
    OPT_PRESSURE_UNIT,
    PRESSURE_UNIT_BAR,
    PRESSURE_UNIT_DEFAULT,
    PRESSURE_UNIT_KPA,
    PRESSURE_UNIT_PSI,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_PIN): str,
    }
)

STEP_REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})

STEP_CODE_SCHEMA = vol.Schema({vol.Required("code"): str})

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(
            OPT_DISTANCE_UNIT,
            default=DISTANCE_UNIT_DEFAULT,
        ): vol.In(
            {
                DISTANCE_UNIT_DEFAULT: "Use Home Assistant default",
                DISTANCE_UNIT_MILES: "Miles",
                DISTANCE_UNIT_KM: "Kilometres",
            }
        ),
        vol.Optional(
            OPT_PRESSURE_UNIT,
            default=PRESSURE_UNIT_DEFAULT,
        ): vol.In(
            {
                PRESSURE_UNIT_DEFAULT: "Use Home Assistant default",
                PRESSURE_UNIT_KPA: "kPa",
                PRESSURE_UNIT_BAR: "bar",
                PRESSURE_UNIT_PSI: "psi",
            }
        ),
    }
)


class JlrConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the JLR InControl config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):  # type: ignore[no-untyped-def]
        """Get the options flow for this handler."""
        return JlrOptionsFlowHandler()

    def __init__(self) -> None:
        """Set up the transient state that spans the sign-in steps."""
        self._login: JlrLogin | None = None
        self._username: str | None = None
        self._pin: str | None = None

    async def _async_start_login(self, username: str, password: str) -> dict[str, str]:
        """Run the journey up to the emailed code. Returns form errors, if any."""
        await self._async_discard_login()
        # A dedicated session: the journey depends on its own cookie jar, which
        # must not leak into (or be disturbed by) Home Assistant's shared one.
        login = JlrLogin(username)
        try:
            await login.async_begin(password)
        except JlrInvalidCode:
            await login.async_close()
            return {"base": "invalid_auth"}
        except JlrLoginError as err:
            _LOGGER.error("JLR sign-in failed: %s", err)
            await login.async_close()
            return {"base": _login_error_code(err)}
        except Exception:  # noqa: BLE001 - surface as a generic connection error
            _LOGGER.exception("Unexpected error during JLR sign-in")
            await login.async_close()
            return {"base": "cannot_connect"}
        self._login = login
        self._username = username
        return {}

    async def _async_discard_login(self) -> None:
        """Close any half-finished journey."""
        if self._login is not None:
            await self._login.async_close()
            self._login = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect credentials and start the sign-in journey."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._pin = user_input.get(CONF_PIN)
            errors = await self._async_start_login(
                user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if not errors:
                return await self.async_step_code()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the emailed verification code and finish signing in."""
        errors: dict[str, str] = {}
        if user_input is not None and self._login is not None:
            try:
                tokens = await self._login.async_complete(user_input["code"].strip())
            except JlrInvalidCode:
                errors["base"] = "invalid_code"
            except JlrLoginError as err:
                _LOGGER.error("JLR sign-in failed at the code step: %s", err)
                errors["base"] = _login_error_code(err)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error submitting the JLR code")
                errors["base"] = "cannot_connect"
            else:
                return await self._async_finish(tokens)

        return self.async_show_form(
            step_id="code",
            data_schema=STEP_CODE_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._username or ""},
        )

    async def _async_finish(self, tokens: dict[str, Any]) -> ConfigFlowResult:
        """Verify the new tokens against the API, then write the entry."""
        assert self._username is not None
        reauth_entry = (
            self.hass.config_entries.async_get_entry(self.context["entry_id"])
            if self.source == SOURCE_REAUTH
            else None
        )
        device_id = (
            reauth_entry.data.get(CONF_DEVICE_ID) if reauth_entry else None
        ) or str(uuid.uuid4())

        client = JlrClient(
            async_get_clientsession(self.hass),
            self._username,
            device_id=device_id,
            refresh_token=tokens.get("refresh_token"),
        )
        client.apply_tokens(tokens)
        try:
            # Register the device, resolve the user id, and list vehicles — the
            # same path the coordinator takes, so a bad token fails here rather
            # than after the entry is written.
            await client.async_get_vehicles()
        except Exception:  # noqa: BLE001 - any failure here means unusable tokens
            _LOGGER.exception("JLR signed us in, but the API rejected the token")
            return self.async_show_form(
                step_id="user",
                data_schema=STEP_USER_SCHEMA,
                errors={"base": "cannot_connect"},
            )
        finally:
            await self._async_discard_login()

        data = {
            CONF_USERNAME: self._username,
            CONF_DEVICE_ID: device_id,
            CONF_USER_ID: client.user_id,
            CONF_REFRESH_TOKEN: client.refresh_token,
        }
        if reauth_entry is not None:
            # Keep the PIN and anything else already configured.
            return self.async_update_reload_and_abort(
                reauth_entry, data={**reauth_entry.data, **data}
            )
        if self._pin:
            data[CONF_PIN] = self._pin
        await self.async_set_unique_id(client.user_id or self._username)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=self._username, data=data)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Sign in again after the refresh token stopped working."""
        self._username = entry_data.get(CONF_USERNAME)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password, then re-run the emailed-code journey."""
        errors: dict[str, str] = {}
        if user_input is not None and self._username:
            errors = await self._async_start_login(
                self._username, user_input[CONF_PASSWORD]
            )
            if not errors:
                return await self.async_step_code()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._username or ""},
        )


def _login_error_code(err: JlrLoginError) -> str:
    """Map a sign-in failure to a translated form error."""
    text = str(err).lower()
    if "reject" in text or "password" in text or "credential" in text:
        return "invalid_auth"
    if "does not support" in text or "changed" in text:
        return "unsupported_journey"
    return "cannot_connect"


class JlrOptionsFlowHandler(OptionsFlow):
    """Handle JLR InControl options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage unit options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        schema = self.add_suggested_values_to_schema(
            OPTIONS_SCHEMA,
            {
                OPT_DISTANCE_UNIT: options.get(
                    OPT_DISTANCE_UNIT, DISTANCE_UNIT_DEFAULT
                ),
                OPT_PRESSURE_UNIT: options.get(
                    OPT_PRESSURE_UNIT, PRESSURE_UNIT_DEFAULT
                ),
            },
        )
        return self.async_show_form(step_id="init", data_schema=schema)

"""Config flow for Jaguar Land Rover InControl."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import JlrClient, JlrConnectionError
from .auth import JlrInvalidCode, JlrLogin, JlrLoginError, JlrSessionExpired
from .const import (
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_SSO_COOKIES,
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
    }
)

STEP_REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})

STEP_CODE_SCHEMA = vol.Schema({vol.Required("code"): str})


def _unit_selector(translation_key: str, values: list[str]) -> SelectSelector:
    """A dropdown whose labels come from strings.json, not from here.

    vol.In with English labels baked in was untranslatable: "Miles" and "Use
    Home Assistant default" reached every user in every language exactly as
    written. The stored values are unchanged, so existing entries keep their
    setting.
    """
    return SelectSelector(
        SelectSelectorConfig(
            options=values,
            translation_key=translation_key,
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(
            OPT_DISTANCE_UNIT,
            default=DISTANCE_UNIT_DEFAULT,
        ): _unit_selector(
            "distance_unit",
            [DISTANCE_UNIT_DEFAULT, DISTANCE_UNIT_MILES, DISTANCE_UNIT_KM],
        ),
        vol.Optional(
            OPT_PRESSURE_UNIT,
            default=PRESSURE_UNIT_DEFAULT,
        ): _unit_selector(
            "pressure_unit",
            [
                PRESSURE_UNIT_DEFAULT,
                PRESSURE_UNIT_KPA,
                PRESSURE_UNIT_BAR,
                PRESSURE_UNIT_PSI,
            ],
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

    def _async_restart_form(self, error: str) -> ConfigFlowResult:
        """Return to the start of sign-in with an explanation.

        Once the journey is dead, re-showing the code form traps the user in a
        loop asking for a code that can never be accepted — it survives a
        reload and only clears on a Home Assistant restart (#10).
        """
        if self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE):
            return self.async_show_form(
                step_id=(
                    "reauth_confirm" if self.source == SOURCE_REAUTH else "reconfigure"
                ),
                data_schema=STEP_REAUTH_SCHEMA,
                errors={"base": error},
                description_placeholders={"email": self._username or ""},
            )
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors={"base": error}
        )

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
            username = user_input[CONF_USERNAME]
            # Check for a duplicate *before* signing in. The unique-id check
            # used to happen at the very end, so adding an account that already
            # existed cost a full sign-in and a one-time code just to be told
            # no — and a disabled entry still holds its unique id, so this is
            # exactly what someone re-adding after a disable would hit (#10).
            if any(
                entry.data.get(CONF_USERNAME, "").casefold() == username.casefold()
                for entry in self._async_current_entries()
            ):
                return self.async_abort(reason="already_configured")
            errors = await self._async_start_login(username, user_input[CONF_PASSWORD])
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
        if user_input is not None:
            if self._login is None:
                # No journey in progress (e.g. the flow was resumed later).
                return self._async_restart_form("session_expired")
            try:
                tokens = await self._login.async_complete(user_input["code"].strip())
            except JlrInvalidCode:
                # The journey is still alive — let them retype the code.
                errors["base"] = "invalid_code"
            except JlrSessionExpired as err:
                _LOGGER.error("JLR sign-in session expired: %s", err)
                await self._async_discard_login()
                return self._async_restart_form("session_expired")
            except JlrLoginError as err:
                _LOGGER.error("JLR sign-in failed at the code step: %s", err)
                await self._async_discard_login()
                return self._async_restart_form(_login_error_code(err))
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error submitting the JLR code")
                await self._async_discard_login()
                return self._async_restart_form("cannot_connect")
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
        # Harvest the ForgeRock session before the journey is closed: the owner
        # portal — the only remaining source of location and the real vehicle
        # names — authenticates against that session, and nothing headless can
        # mint another one.
        sso_cookies = self._login.session_cookies() if self._login else {}
        # Reauth and reconfigure both sign an existing entry in again; only a
        # brand-new setup creates one. Updating in place matters — deleting and
        # re-adding would take every entity id, and with them every automation
        # and dashboard card pointing at this car.
        existing = (
            self.hass.config_entries.async_get_entry(self.context["entry_id"])
            if self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE)
            else None
        )
        device_id = (existing.data.get(CONF_DEVICE_ID) if existing else None) or str(
            uuid.uuid4()
        )

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
        except JlrConnectionError as err:
            # Sign-in worked; we simply never reached the vehicle API. Saying
            # "the API rejected the token" here sends people hunting a JLR-side
            # problem when the fault is between them and JLR (#10).
            _LOGGER.error("JLR signed us in, but the API is unreachable: %s", err)
            return self._async_restart_form("cannot_reach_api")
        except Exception:  # noqa: BLE001 - any failure here means unusable tokens
            _LOGGER.exception("JLR signed us in, but the API rejected the token")
            return self._async_restart_form("api_rejected")
        finally:
            await self._async_discard_login()

        data = {
            CONF_USERNAME: self._username,
            CONF_DEVICE_ID: device_id,
            CONF_USER_ID: client.user_id,
            CONF_REFRESH_TOKEN: client.refresh_token,
            CONF_SSO_COOKIES: sso_cookies,
        }
        if existing is not None:
            # Keep the device id and anything else already configured.
            return self.async_update_reload_and_abort(
                existing, data={**existing.data, **data}
            )
        await self.async_set_unique_id(client.user_id or self._username)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=self._username, data=data)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in again on demand, keeping the entry and its entities.

        Not everything the integration needs comes from a renewable token: the
        owner portal, which serves location and the real vehicle names, rides a
        ForgeRock session that only an interactive sign-in can establish. This
        is how you refresh it — or adopt it, on an entry created before the
        portal was used — without deleting the integration and losing every
        entity id along with it.
        """
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="unknown_entry")
        self._username = entry.data.get(CONF_USERNAME)

        errors: dict[str, str] = {}
        if user_input is not None and self._username:
            errors = await self._async_start_login(
                self._username, user_input[CONF_PASSWORD]
            )
            if not errors:
                return await self.async_step_code()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=STEP_REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._username or ""},
        )

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


class JlrOptionsFlowHandler(OptionsFlowWithReload):
    """Handle JLR InControl options.

    ``WithReload`` rather than a listener of our own. Home Assistant reloads
    the entry itself when an options flow finishes, and an integration that
    also registers an update listener gets both — a double reload now, and a
    deprecation that becomes an error in Home Assistant 2026.12.

    If a listener is ever added back, it must not reload on every entry write.
    JLR rotate the refresh token on each renewal and the coordinator persists
    the new one, so a listener that fires on any change turns a five-minute
    token into a five-minute teardown and re-setup of the whole integration,
    around the clock. That happened once already.
    """

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

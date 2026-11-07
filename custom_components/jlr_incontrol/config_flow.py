"""Config flow for Jaguar Land Rover InControl."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

# Core 2026.10 replaced voluptuous with probatio, and types every flow schema
# as probatio's. Same API, so the schemas below are written once; voluptuous
# is only reached on the older cores this still supports, where probatio does
# not exist. It would import on 2026.10 as well, but only because two of
# core's own dependencies still happen to pull it in.
try:
    import probatio as vol
except ImportError:
    import voluptuous as vol  # type: ignore[no-redef]

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
    CONF_DATAHUB,
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_SSO_COOKIES,
    CONF_USER_ID,
    CONF_USERNAME,
    DATAHUB_ACCOUNT_ID,
    DATAHUB_CERTIFICATE,
    DATAHUB_CLIENT_ID,
    DATAHUB_PRIVATE_KEY,
    DATAHUB_REFRESH_TOKEN,
    DATAHUB_VEHICLES,
    DISTANCE_UNIT_DEFAULT,
    DISTANCE_UNIT_KM,
    DISTANCE_UNIT_MILES,
    DOMAIN,
    OPT_DISTANCE_UNIT,
    OPT_PRESSURE_UNIT,
    OPT_UNSETTLED_MINUTES,
    PRESSURE_UNIT_BAR,
    PRESSURE_UNIT_DEFAULT,
    PRESSURE_UNIT_KPA,
    PRESSURE_UNIT_PSI,
    UNSETTLED_MINUTES_CHOICES,
    UNSETTLED_MINUTES_DEFAULT,
)
from .datahub import (
    PORTAL_URL,
    Credentials,
    DataHubAuthError,
    DataHubClient,
    DataHubDenied,
    DataHubError,
    DataHubExpired,
    DeviceLogin,
    Tokens,
    generate_credentials,
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

# Passed in rather than written into strings.json: hassfest rejects a URL in
# a translation string, and failed the 1.9.0-beta.4 build on exactly this.
GUIDE_URL = (
    "https://github.com/willbeeching/ha-jlr-incontrol/blob/master/docs/DATA_HUB.md"
)

STEP_DATAHUB_SCHEMA = vol.Schema({vol.Required(DATAHUB_CLIENT_ID): str})


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
        vol.Optional(
            OPT_UNSETTLED_MINUTES,
            # A string, because that is what a select selector stores and
            # validates. The constant stays an int — it is a duration — and
            # the coordinator coerces on the way back out.
            default=str(UNSETTLED_MINUTES_DEFAULT),
        ): SelectSelector(
            SelectSelectorConfig(
                options=[str(m) for m in UNSETTLED_MINUTES_CHOICES],
                translation_key="unsettled_minutes",
                mode=SelectSelectorMode.DROPDOWN,
            )
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
        login = JlrLogin(self.hass, username)
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

    def __init__(self) -> None:
        self._credentials: Credentials | None = None
        self._client: DataHubClient | None = None
        self._login: DeviceLogin | None = None
        self._link_task: asyncio.Task[None] | None = None
        self._tokens: Tokens | None = None
        self._link_error = "unknown"

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between the settings and the Data Hub link."""
        menu = ["settings", "datahub"]
        if self.config_entry.data.get(CONF_DATAHUB):
            menu.append("datahub_remove")
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_settings(
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
                OPT_UNSETTLED_MINUTES: str(
                    options.get(OPT_UNSETTLED_MINUTES, UNSETTLED_MINUTES_DEFAULT)
                ),
            },
        )
        return self.async_show_form(step_id="settings", data_schema=schema)

    async def async_step_datahub(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the certificate to paste at data.jlr.com, then take the client id.

        The key pair is made here, in Home Assistant, so the private key never
        exists anywhere else. Only the certificate is shown.
        """
        errors: dict[str, str] = {}
        if self._credentials is None:
            self._credentials = await self.hass.async_add_executor_job(
                generate_credentials
            )
        if user_input is not None:
            client_id = user_input[DATAHUB_CLIENT_ID].strip()
            self._client = DataHubClient(
                async_get_clientsession(self.hass),
                client_id,
                self._credentials.private_key,
            )
            try:
                self._login = await self._client.async_start_device_login()
            except DataHubAuthError:
                # The client id is wrong, or the certificate pasted at the
                # portal is not this one: the two look identical from here.
                errors["base"] = "datahub_rejected"
            except DataHubError:
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_datahub_link()
        return self.async_show_form(
            step_id="datahub",
            data_schema=STEP_DATAHUB_SCHEMA,
            errors=errors,
            description_placeholders={
                "portal": PORTAL_URL,
                "guide": GUIDE_URL,
                "certificate": self._credentials.certificate.strip(),
            },
        )

    async def async_step_datahub_link(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait while the owner approves the link on their phone."""
        assert self._login is not None
        if self._link_task is None:
            self._link_task = self.hass.async_create_task(
                self._async_wait_for_approval()
            )
        if not self._link_task.done():
            return self.async_show_progress(
                step_id="datahub_link",
                progress_action="datahub_link",
                description_placeholders={
                    "url": self._login.verification_uri_complete,
                    "code": self._login.user_code,
                },
                progress_task=self._link_task,
            )
        return self.async_show_progress_done(
            next_step_id="datahub_done" if self._tokens else "datahub_failed"
        )

    async def _async_wait_for_approval(self) -> None:
        """Poll at the interval JLR asked for until approved, refused or expired."""
        assert self._client is not None and self._login is not None
        interval = self._login.interval
        while time.time() < self._login.expires_at:
            await asyncio.sleep(interval)
            try:
                tokens = await self._client.async_poll_device_login(self._login)
            except DataHubDenied:
                self._link_error = "datahub_denied"
                return
            except DataHubExpired:
                break
            except DataHubAuthError:
                self._link_error = "datahub_rejected"
                return
            except DataHubError as err:
                # A dropped poll is not a refusal; the code is still good.
                _LOGGER.debug("Data Hub sign-in poll failed: %s", err)
                interval += 5
                continue
            if tokens is not None:
                if tokens.refresh_token:
                    self._tokens = tokens
                else:
                    self._link_error = "datahub_rejected"
                return
        self._link_error = "datahub_expired"

    async def async_step_datahub_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Keep the link on the entry and reload so it starts."""
        assert self._tokens is not None
        assert self._client is not None and self._credentials is not None
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **self.config_entry.data,
                CONF_DATAHUB: {
                    DATAHUB_CLIENT_ID: self._client.client_id,
                    DATAHUB_PRIVATE_KEY: self._credentials.private_key,
                    DATAHUB_CERTIFICATE: self._credentials.certificate,
                    DATAHUB_REFRESH_TOKEN: self._tokens.refresh_token,
                    DATAHUB_VEHICLES: self._tokens.vehicles,
                    DATAHUB_ACCOUNT_ID: self._tokens.account_id,
                },
            },
        )
        # The options are unchanged, so the automatic reload will not fire.
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        return self.async_create_entry(title="", data=dict(self.config_entry.options))

    async def async_step_datahub_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Say why the link was not made."""
        return self.async_abort(reason=self._link_error)

    async def async_step_datahub_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Forget the link. The client at data.jlr.com is the owner's to delete."""
        if user_input is None:
            return self.async_show_form(step_id="datahub_remove")
        data = dict(self.config_entry.data)
        data.pop(CONF_DATAHUB, None)
        self.hass.config_entries.async_update_entry(self.config_entry, data=data)
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        return self.async_create_entry(title="", data=dict(self.config_entry.options))

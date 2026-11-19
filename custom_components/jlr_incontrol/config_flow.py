"""Config flow for Jaguar Land Rover InControl, on the JLR Data Hub."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
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
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DATAHUB,
    DATAHUB_ACCOUNT_ID,
    DATAHUB_CERTIFICATE,
    DATAHUB_CLIENT_ID,
    DATAHUB_PRIVATE_KEY,
    DATAHUB_REFRESH_TOKEN,
    DATAHUB_RENEW_WITHIN,
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
    DataHubRateLimited,
    DeviceLogin,
    Tokens,
    certificate_expiry,
    generate_credentials,
)

_LOGGER = logging.getLogger(__name__)


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
    """Set up the integration on the JLR Data Hub."""

    VERSION = 1
    # 2: the InControl sign-in and everything it kept are gone (3.0.0).
    MINOR_VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):  # type: ignore[no-untyped-def]
        """Get the options flow for this handler."""
        return JlrOptionsFlowHandler()

    def __init__(self) -> None:
        """Set up the transient state that spans the linking steps."""
        self._linker = DataHubLinker()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Straight to the Data Hub: since 3.0.0 it is the only way in."""
        return await self.async_step_datahub(user_input)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """JLR stopped accepting the link: link a client to the same entry."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Say why before the certificate, which otherwise arrives unexplained."""
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                description_placeholders={"title": self._get_reauth_entry().title},
            )
        return await self.async_step_datahub()

    async def async_step_datahub(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the certificate to paste at data.jlr.com, then take the client id."""
        errors = await self._linker.async_submit(self.hass, user_input)
        if user_input is not None and not errors:
            return await self.async_step_datahub_link()
        return self.async_show_form(
            step_id="datahub",
            data_schema=STEP_DATAHUB_SCHEMA,
            errors=errors,
            description_placeholders=self._linker.form_placeholders(),
        )

    async def async_step_datahub_link(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait while the owner approves the link on their phone."""
        task = self._linker.waiting(self.hass)
        if not task.done():
            return self.async_show_progress(
                step_id="datahub_link",
                progress_action="datahub_link",
                description_placeholders=self._linker.progress_placeholders(),
                progress_task=task,
            )
        return self.async_show_progress_done(
            next_step_id="datahub_done" if self._linker.tokens else "datahub_failed"
        )

    async def async_step_datahub_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the entry.

        Identified by the Data Hub account, which is what a second attempt at
        the same cars would share; the client id changes every time someone
        makes a new client.
        """
        settings = self._linker.settings()
        if self.source == SOURCE_REAUTH:
            return self._async_relink(settings)
        await self.async_set_unique_id(
            settings[DATAHUB_ACCOUNT_ID] or settings[DATAHUB_CLIENT_ID]
        )
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="JLR Data Hub", data={CONF_DATAHUB: settings}
        )

    async def async_step_datahub_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Say why the link was not made."""
        return self.async_abort(reason=self._linker.error)

    def _async_relink(self, settings: dict[str, Any]) -> ConfigFlowResult:
        """Put the new link on the entry being reauthenticated, and reload.

        Matched on the Data Hub account rather than the entry's unique id: an
        entry from the InControl sign-in is identified by that account, not
        this one. Another owner's account would hang different cars on these
        devices, and the next listing would forget every car the entry has.
        """
        entry = self._get_reauth_entry()
        held = (entry.data.get(CONF_DATAHUB) or {}).get(DATAHUB_ACCOUNT_ID)
        new = settings[DATAHUB_ACCOUNT_ID]
        if held and new and held != new:
            return self.async_abort(reason="wrong_account")
        return self.async_update_reload_and_abort(
            entry, data={**entry.data, CONF_DATAHUB: settings}
        )


class DataHubLinker:
    """Linking a Data Hub client: shared by setup and by the options.

    Both flows walk the same three steps — show a certificate and take the
    client id, wait for approval on a phone, keep what came back — and only
    differ in where the result goes. Holding the state here keeps the two
    from drifting apart.
    """

    # The wait between polls, in one place a test can stand in for.
    _pause = staticmethod(asyncio.sleep)

    def __init__(self) -> None:
        self.credentials: Credentials | None = None
        self.client: DataHubClient | None = None
        self.login: DeviceLogin | None = None
        self.task: asyncio.Task[None] | None = None
        self.tokens: Tokens | None = None
        self.error = "unknown"

    async def async_submit(
        self, hass: HomeAssistant, user_input: dict[str, Any] | None
    ) -> dict[str, str]:
        """Make the key pair if there is none yet; start sign-in on submit.

        The key pair is made here, in Home Assistant, so the private key never
        exists anywhere else. Only the certificate is ever shown.
        """
        if self.credentials is None:
            self.credentials = await hass.async_add_executor_job(generate_credentials)
        if user_input is None:
            return {}
        self.client = DataHubClient(
            async_get_clientsession(hass),
            user_input[DATAHUB_CLIENT_ID].strip(),
            self.credentials.private_key,
        )
        try:
            self.login = await self.client.async_start_device_login()
        except DataHubAuthError:
            # The client id is wrong, or the certificate pasted at the
            # portal is not this one: the two look identical from here.
            return {"base": "datahub_rejected"}
        except DataHubError:
            return {"base": "cannot_connect"}
        return {}

    def form_placeholders(self) -> dict[str, str]:
        assert self.credentials is not None
        return {
            "portal": PORTAL_URL,
            "guide": GUIDE_URL,
            "certificate": self.credentials.certificate.strip(),
        }

    def progress_placeholders(self) -> dict[str, str]:
        assert self.login is not None
        return {
            "url": self.login.verification_uri_complete,
            "code": self.login.user_code,
        }

    def waiting(self, hass: HomeAssistant) -> asyncio.Task[None]:
        if self.task is None:
            self.task = hass.async_create_task(self._async_wait_for_approval())
        return self.task

    async def _async_wait_for_approval(self) -> None:
        """Poll at the interval JLR asked for until approved, refused or expired."""
        assert self.client is not None and self.login is not None
        interval = self.login.interval
        while time.time() < self.login.expires_at:
            if time.time() + interval >= self.login.expires_at:
                # The code runs out before the next poll may be sent: with 25
                # seconds left and an hour's Retry-After, the flow sat out the
                # hour and then polled with a dead code. Ended now, the owner
                # can start again straight away.
                break
            await self._pause(interval)
            if time.time() >= self.login.expires_at:
                # And after it: a wait that ran late, five seconds that took
                # eight, polled with a code that had run out at seven.
                break
            try:
                tokens = await self.client.async_poll_device_login(self.login)
            except DataHubDenied:
                self.error = "datahub_denied"
                return
            except DataHubExpired:
                break
            except DataHubAuthError:
                self.error = "datahub_rejected"
                return
            except DataHubRateLimited as err:
                # Asked to slow down while the owner approves: the next poll
                # waits as long as JLR asked, not five seconds more.
                interval = max(interval, err.retry_after)
                continue
            except DataHubError as err:
                # A dropped poll is not a refusal; the code is still good.
                _LOGGER.debug("Data Hub sign-in poll failed: %s", err)
                interval += 5
                continue
            if tokens is not None:
                if tokens.refresh_token:
                    self.tokens = tokens
                else:
                    self.error = "datahub_rejected"
                return
        self.error = "datahub_expired"

    def settings(self) -> dict[str, Any]:
        """What the entry keeps under CONF_DATAHUB."""
        assert self.tokens is not None
        assert self.client is not None and self.credentials is not None
        return {
            DATAHUB_CLIENT_ID: self.client.client_id,
            DATAHUB_PRIVATE_KEY: self.credentials.private_key,
            DATAHUB_CERTIFICATE: self.credentials.certificate,
            DATAHUB_REFRESH_TOKEN: self.tokens.refresh_token,
            DATAHUB_VEHICLES: self.tokens.vehicles,
            DATAHUB_ACCOUNT_ID: self.tokens.account_id,
        }


class JlrOptionsFlowHandler(OptionsFlowWithReload):
    """Handle the options: units and readings, and linking the Data Hub.

    ``WithReload`` rather than a listener of our own. Home Assistant reloads
    the entry itself when an options flow finishes, and an integration that
    also registers an update listener gets both — a double reload now, and a
    deprecation that becomes an error in Home Assistant 2026.12.

    If a listener is ever added back, it must not reload on every entry write.
    JLR rotate the Data Hub's refresh token on each renewal and the link
    persists the new one, so a listener that fires on any change turns a
    five-minute token into a five-minute teardown of the whole integration.
    That happened once already, with the InControl token.
    """

    def __init__(self) -> None:
        self._linker = DataHubLinker()

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between the settings and the Data Hub link."""
        menu = ["settings"]
        if not self.config_entry.data.get(CONF_DATAHUB):
            # An entry from the InControl sign-in, which reads nothing until
            # this is done.
            menu.append("datahub")
        elif not self._link_working() or self._certificate_running_out():
            # Linking an entry that is already linked and working replaces a
            # good key for nothing, so it is offered only when there is a
            # reason: the link has stopped, or its certificate is running out.
            menu.append("datahub_relink")
        return self.async_show_menu(step_id="init", menu_options=menu)

    def _link_working(self) -> bool:
        """Whether the entry is running on a link JLR accept.

        Not just the running link's word: a link refused before setup
        finished leaves no running link to ask, the entry not loaded and a
        repair saying to link again — which has to be offered here.
        """
        if not self.config_entry.data.get(CONF_DATAHUB):
            return False
        if self.config_entry.state is not ConfigEntryState.LOADED:
            return False
        link = getattr(
            getattr(self.config_entry, "runtime_data", None), "datahub", None
        )
        return link is not None and not link.signed_out

    def _certificate_running_out(self) -> bool:
        certificate = self.config_entry.data[CONF_DATAHUB].get(DATAHUB_CERTIFICATE)
        try:
            expires = certificate_expiry(certificate)
        except (TypeError, ValueError):
            return False
        return expires - dt_util.utcnow() < DATAHUB_RENEW_WITHIN

    async def async_step_datahub_relink(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The same steps as linking, under the name that says why."""
        return await self.async_step_datahub(user_input)

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
        """Show the certificate to paste at data.jlr.com, then take the client id."""
        errors = await self._linker.async_submit(self.hass, user_input)
        if user_input is not None and not errors:
            return await self.async_step_datahub_link()
        return self.async_show_form(
            step_id="datahub",
            data_schema=STEP_DATAHUB_SCHEMA,
            errors=errors,
            description_placeholders=self._linker.form_placeholders(),
        )

    async def async_step_datahub_link(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait while the owner approves the link on their phone."""
        task = self._linker.waiting(self.hass)
        if not task.done():
            return self.async_show_progress(
                step_id="datahub_link",
                progress_action="datahub_link",
                description_placeholders=self._linker.progress_placeholders(),
                progress_task=task,
            )
        return self.async_show_progress_done(
            next_step_id="datahub_done" if self._linker.tokens else "datahub_failed"
        )

    async def async_step_datahub_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Keep the link on the entry and reload so it starts."""
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_DATAHUB: self._linker.settings()},
        )
        # The options are unchanged, so the automatic reload will not fire.
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        return self.async_create_entry(title="", data=dict(self.config_entry.options))

    async def async_step_datahub_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Say why the link was not made."""
        return self.async_abort(reason=self._linker.error)

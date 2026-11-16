"""Linking and unlinking the Data Hub from the integration's options.

Driven through Home Assistant's own flow manager, progress step and all, so
what is tested is the dialog a person actually clicks through: the certificate
shown, the code to approve, and what is left on the entry afterwards.
"""

from __future__ import annotations

import asyncio
import time
from functools import lru_cache
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT, Doubles, FakeDataHub  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol import config_flow, datahub_link  # noqa: E402
from custom_components.jlr_incontrol.datahub import (  # noqa: E402
    Credentials,
    DataHubAuthError,
    DataHubDenied,
    DataHubError,
    DataHubExpired,
    DeviceLogin,
    generate_credentials,
)


@lru_cache
def credentials() -> Credentials:
    # One real pair for the whole module; generating is the slow part.
    return generate_credentials()


def device_login(expires_in: float = 600) -> DeviceLogin:
    return DeviceLogin(
        device_code="dev",
        user_code="ABCD-EFGH",
        verification_uri="https://auth.example/device",
        verification_uri_complete="https://auth.example/device?user_code=ABCD-EFGH",
        interval=0,
        expires_at=time.time() + expires_in,
        verifier="v" * 64,
    )


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch, doubles: Doubles) -> Doubles:
    # The real link, reading the fake JLR client below.
    monkeypatch.setattr(
        "custom_components.jlr_incontrol.coordinator.DataHubLink",
        datahub_link.DataHubLink,
    )
    build = doubles.builder("datahub", FakeDataHub)
    made: list[FakeDataHub] = []

    def scripted(*args: Any, **kwargs: Any) -> FakeDataHub:
        fake = build(*args, **kwargs)
        fake.login_result = device_login()
        fake.poll_results = [None, DataHubError("blip"), fake.tokens]
        made.append(fake)
        return fake

    monkeypatch.setattr(config_flow, "DataHubClient", scripted)
    monkeypatch.setattr(datahub_link, "DataHubClient", build)
    monkeypatch.setattr(config_flow, "generate_credentials", credentials)

    async def parked(**kwargs: Any) -> str:
        await asyncio.Event().wait()
        return "unreachable"

    monkeypatch.setattr(datahub_link, "run_session", parked)
    doubles.flow_clients = made
    return doubles


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """An entry from the InControl sign-in, waiting for the Data Hub.

    Since 3.0.0 the only entry that links from Configure: one set up on the
    Data Hub has its link from the start, and offers linking again only when
    it stops working.
    """
    created = MockConfigEntry(
        domain="jlr_incontrol",
        title="someone@example.com",
        data={"username": "someone@example.com"},
        unique_id="user-01H8XK4Q2N",
        version=1,
        minor_version=2,
    )
    created.add_to_hass(hass)
    return created


async def open_datahub(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "datahub"}
    )


async def submit(hass: HomeAssistant, result: dict[str, Any]) -> dict[str, Any]:
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"client_id": "  dsas-0123456789ab "}
    )


async def finish(hass: HomeAssistant, result: dict[str, Any]) -> dict[str, Any]:
    """Let the approval wait run out, then take the step the dialog would.

    A wait that ends before the dialog is even shown (a code already expired)
    has been finished by the flow manager on the way back; that is returned.
    """
    if result["type"] is not FlowResultType.SHOW_PROGRESS:
        return result
    await hass.async_block_till_done()
    return await hass.config_entries.options.async_configure(result["flow_id"])


class TestLinking:
    async def test_the_form_shows_the_certificate_and_never_the_key(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles, hub: Doubles
    ) -> None:
        result = await open_datahub(hass, entry)
        assert result["type"] is FlowResultType.FORM
        placeholders = result["description_placeholders"]
        assert placeholders["certificate"] == credentials().certificate.strip()
        assert placeholders["portal"] == "https://data.jlr.com"
        assert placeholders["guide"].endswith("/docs/DATA_HUB.md")
        assert "PRIVATE KEY" not in str(result)

    async def test_approving_it_on_the_phone_links_it(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles, hub: Doubles
    ) -> None:
        result = await submit(hass, await open_datahub(hass, entry))
        assert result["step_id"] == "datahub_link"
        assert result["description_placeholders"] == {
            "url": "https://auth.example/device?user_code=ABCD-EFGH",
            "code": "ABCD-EFGH",
        }
        result = await finish(hass, result)
        await hass.async_block_till_done()
        assert result["type"] is FlowResultType.CREATE_ENTRY

        stored = entry.data["datahub"]
        assert stored["client_id"] == "dsas-0123456789ab"  # trimmed
        assert stored["private_key"] == credentials().private_key
        assert stored["certificate"] == credentials().certificate
        assert stored["refresh_token"] == "offline-2"
        assert stored["vehicles"] == [KEPT]
        assert stored["account_id"] == "acct-1"
        # Polled through a pending answer and a dropped poll.
        assert hub.flow_clients[0].calls == ["start", "poll", "poll", "poll"]
        # Reloaded, and running with the link.
        assert entry.runtime_data.datahub is not None
        assert entry.options == {}

    @pytest.mark.parametrize(
        ("error", "reason"),
        [
            (DataHubAuthError("invalid_client"), "datahub_rejected"),
            (DataHubError("down"), "cannot_connect"),
        ],
    )
    async def test_a_client_jlr_refuse_is_said_on_the_form(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        hub: Doubles,
        monkeypatch: pytest.MonkeyPatch,
        error: Exception,
        reason: str,
    ) -> None:
        result = await open_datahub(hass, entry)
        build = config_flow.DataHubClient

        def refusing(*args: Any) -> Any:
            fake = build(*args)
            fake.login_result = error
            return fake

        monkeypatch.setattr(config_flow, "DataHubClient", refusing)
        result = await submit(hass, result)
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": reason}
        # The same key pair is offered again, so a certificate already pasted
        # at the portal still matches.
        assert result["description_placeholders"]["certificate"] == (
            credentials().certificate.strip()
        )

    @pytest.mark.parametrize(
        ("answers", "reason"),
        [
            ([DataHubDenied("no")], "datahub_denied"),
            ([None, DataHubExpired("late")], "datahub_expired"),
            ([DataHubAuthError("gone")], "datahub_rejected"),
        ],
    )
    async def test_a_link_not_approved_says_why(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        hub: Doubles,
        answers: list[Any],
        reason: str,
    ) -> None:
        result = await open_datahub(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"client_id": "dsas-x"}
        )
        hub.flow_clients[0].poll_results = answers
        result = await finish(hass, result)
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == reason
        assert "datahub" not in entry.data

    async def test_an_approval_without_a_refresh_token_is_refused(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles, hub: Doubles
    ) -> None:
        # Without one the link would die with the first access token.
        result = await open_datahub(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"client_id": "dsas-x"}
        )
        fake = hub.flow_clients[0]
        fake.tokens.refresh_token = None
        fake.poll_results = [fake.tokens]
        result = await finish(hass, result)
        assert result["reason"] == "datahub_rejected"

    async def test_a_code_that_runs_out_while_polling_expires(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        hub: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        build = config_flow.DataHubClient

        def short(*args: Any) -> Any:
            fake = build(*args)
            fake.login_result = device_login(expires_in=-1)
            return fake

        monkeypatch.setattr(config_flow, "DataHubClient", short)
        result = await submit(hass, await open_datahub(hass, entry))
        result = await finish(hass, result)
        assert result["reason"] == "datahub_expired"


async def open_setup(hass: HomeAssistant) -> dict[str, Any]:
    """Open "Add integration": straight to the Data Hub since 3.0.0."""
    return await hass.config_entries.flow.async_init(
        "jlr_incontrol", context={"source": "user"}
    )


async def finish_setup(hass: HomeAssistant, result: dict[str, Any]) -> dict[str, Any]:
    if result["type"] is not FlowResultType.SHOW_PROGRESS:
        return result
    await hass.async_block_till_done()
    return await hass.config_entries.flow.async_configure(result["flow_id"])


class TestSettingUpOnTheDataHub:
    async def test_no_incontrol_sign_in_is_asked_for(
        self, hass: HomeAssistant, hub: Doubles
    ) -> None:
        result = await open_setup(hass)
        assert result["step_id"] == "datahub"
        assert result["description_placeholders"]["certificate"] == (
            credentials().certificate.strip()
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"client_id": "dsas-0123456789ab"}
        )
        result = await finish_setup(hass, result)
        await hass.async_block_till_done()
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == "JLR Data Hub"
        entry = result["result"]
        assert entry.unique_id == "acct-1"
        # Nothing of an InControl sign-in: no email, password or tokens. (The
        # car details the coordinator caches are kept on the entry too.)
        assert not {"username", "password", "refresh_token"} & set(entry.data)
        assert entry.data["datahub"]["refresh_token"] == "offline-2"
        # And it runs.
        assert entry.runtime_data.datahub is not None
        # A working link: nothing to link again.
        menu = await hass.config_entries.options.async_init(entry.entry_id)
        assert menu["menu_options"] == ["settings"]
        await hass.config_entries.options.async_configure(
            menu["flow_id"], {"next_step_id": "settings"}
        )
        assert await hass.config_entries.async_unload(entry.entry_id)

    async def test_the_same_account_twice_is_refused(
        self, hass: HomeAssistant, hub: Doubles
    ) -> None:
        MockConfigEntry(
            domain="jlr_incontrol", data={"datahub": {}}, unique_id="acct-1"
        ).add_to_hass(hass)
        result = await open_setup(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"client_id": "dsas-0123456789ab"}
        )
        result = await finish_setup(hass, result)
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"

    async def test_a_link_declined_on_the_phone_says_so(
        self, hass: HomeAssistant, hub: Doubles
    ) -> None:
        result = await open_setup(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"client_id": "dsas-0123456789ab"}
        )
        hub.flow_clients[0].poll_results = [DataHubDenied("no")]
        result = await finish_setup(hass, result)
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "datahub_denied"

    async def test_a_client_jlr_refuse_is_said_on_the_form(
        self, hass: HomeAssistant, hub: Doubles, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = await open_setup(hass)
        build = config_flow.DataHubClient

        def refusing(*args: Any) -> Any:
            fake = build(*args)
            fake.login_result = DataHubAuthError("invalid_client")
            return fake

        monkeypatch.setattr(config_flow, "DataHubClient", refusing)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"client_id": "dsas-x"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "datahub_rejected"}


class TestLinkingAgain:
    """Offered only when the link needs it, under a name that says so."""

    async def link(self, hass: HomeAssistant, entry: MockConfigEntry) -> None:
        await finish(hass, await submit(hass, await open_datahub(hass, entry)))
        await hass.async_block_till_done()

    async def test_when_the_link_has_stopped(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles, hub: Doubles
    ) -> None:
        await self.link(hass, entry)
        entry.runtime_data.datahub.signed_out = True
        menu = await hass.config_entries.options.async_init(entry.entry_id)
        assert "datahub_relink" in menu["menu_options"]
        # It is the same steps as linking: the certificate to paste first.
        result = await hass.config_entries.options.async_configure(
            menu["flow_id"], {"next_step_id": "datahub_relink"}
        )
        assert result["step_id"] == "datahub"

    async def test_when_the_certificate_is_running_out(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        hub: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from datetime import timedelta

        from homeassistant.util import dt as dt_util

        await self.link(hass, entry)
        menu = await hass.config_entries.options.async_init(entry.entry_id)
        assert "datahub_relink" not in menu["menu_options"]
        await hass.config_entries.options.async_configure(
            menu["flow_id"], {"next_step_id": "settings"}
        )
        monkeypatch.setattr(
            config_flow,
            "certificate_expiry",
            lambda _: dt_util.utcnow() + timedelta(days=10),
        )
        menu = await hass.config_entries.options.async_init(entry.entry_id)
        assert "datahub_relink" in menu["menu_options"]

"""Signing in, and the several ways it goes wrong.

The journey is not a normal OAuth exchange: a password, then a code emailed to
the account holder, then a ForgeRock session that has to be harvested before
the journey is closed because nothing headless can mint another one. Most of
the reported bugs have been in the failure paths — a dead journey re-showing
the code form forever, a duplicate account costing a full sign-in and a
one-time code just to be refused.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT  # noqa: E402
from homeassistant import config_entries  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol.auth import (  # noqa: E402
    JlrInvalidCode,
    JlrLoginError,
    JlrSessionExpired,
)
from custom_components.jlr_incontrol.const import DOMAIN  # noqa: E402

FLOW = "custom_components.jlr_incontrol.config_flow"
EMAIL = "someone@example.com"
PASSWORD = "hunter2-and-then-some"
CODE = "482913"
TOKENS = {"access_token": "an-access-token", "refresh_token": "a-refresh-token"}


class FakeLogin:
    """The emailed-code journey, without the email."""

    begin_error: Exception | None = None
    complete_error: Exception | None = None

    def __init__(self, username: str) -> None:
        self.username = username
        self.closed = False

    async def async_begin(self, password: str) -> None:
        if self.begin_error is not None:
            raise self.begin_error

    async def async_complete(self, code: str) -> dict[str, Any]:
        if self.complete_error is not None:
            raise self.complete_error
        return dict(TOKENS)

    def session_cookies(self) -> dict[str, str]:
        return {"iPlanetDirectoryPro": "a-forgerock-session"}

    async def async_close(self) -> None:
        self.closed = True


class FakeFlowClient:
    """Just enough client for the verification the flow does before writing."""

    vehicles_error: Exception | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.user_id = "user-01H8XK4Q2N"
        self.refresh_token = kwargs.get("refresh_token")

    def apply_tokens(self, tokens: dict[str, Any]) -> None:
        self.refresh_token = tokens.get("refresh_token")

    async def async_get_vehicles(self) -> list[dict[str, Any]]:
        if self.vehicles_error is not None:
            raise self.vehicles_error
        return [{"vin": KEPT, "role": "Owner"}]


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations: Any) -> None:
    """Let Home Assistant find this repo's custom_components directory."""


@pytest.fixture(autouse=True)
def signing_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the two things the flow reaches the network with."""
    monkeypatch.setattr(FakeLogin, "begin_error", None)
    monkeypatch.setattr(FakeLogin, "complete_error", None)
    monkeypatch.setattr(FakeFlowClient, "vehicles_error", None)
    monkeypatch.setattr(f"{FLOW}.JlrLogin", FakeLogin)
    monkeypatch.setattr(f"{FLOW}.JlrClient", FakeFlowClient)


async def start(hass: HomeAssistant) -> dict[str, Any]:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def credentials(hass: HomeAssistant, flow_id: str) -> dict[str, Any]:
    return await hass.config_entries.flow.async_configure(
        flow_id, {"username": EMAIL, "password": PASSWORD}
    )


class TestSigningIn:
    async def test_the_form_comes_up(self, hass: HomeAssistant) -> None:
        result = await start(hass)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

    async def test_credentials_lead_to_the_code_step(self, hass: HomeAssistant) -> None:
        result = await credentials(hass, (await start(hass))["flow_id"])
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "code"
        # The form says which inbox to look in.
        assert result["description_placeholders"]["email"] == EMAIL

    async def test_the_code_creates_the_entry(self, hass: HomeAssistant) -> None:
        result = await credentials(hass, (await start(hass))["flow_id"])
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": CODE}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"]["username"] == EMAIL
        assert result["data"]["refresh_token"] == TOKENS["refresh_token"]
        assert result["data"]["user_id"] == "user-01H8XK4Q2N"

    async def test_the_forgerock_session_is_kept(self, hass: HomeAssistant) -> None:
        # Location and the real vehicle names ride this session, and nothing
        # headless can mint another one — so losing it here costs the user an
        # emailed code to get back.
        result = await credentials(hass, (await start(hass))["flow_id"])
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": CODE}
        )
        assert result["data"]["sso_cookies"] == {
            "iPlanetDirectoryPro": "a-forgerock-session"
        }

    async def test_a_code_with_stray_whitespace_still_works(
        self, hass: HomeAssistant
    ) -> None:
        # It is pasted out of an email.
        result = await credentials(hass, (await start(hass))["flow_id"])
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": f"  {CODE} "}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY


class TestAnAccountAlreadyAdded:
    async def test_it_is_refused_before_any_sign_in(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The unique-id check used to happen at the very end, so re-adding an
        # account cost a full sign-in and a one-time code just to be told no.
        MockConfigEntry(
            domain=DOMAIN, data={"username": EMAIL}, unique_id=EMAIL
        ).add_to_hass(hass)

        attempted = False

        async def begin(self: FakeLogin, password: str) -> None:
            nonlocal attempted
            attempted = True

        monkeypatch.setattr(FakeLogin, "async_begin", begin)

        result = await credentials(hass, (await start(hass))["flow_id"])
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"
        assert not attempted, "it signed in before checking for a duplicate"

    async def test_the_check_ignores_case(self, hass: HomeAssistant) -> None:
        MockConfigEntry(
            domain=DOMAIN, data={"username": EMAIL.upper()}, unique_id=EMAIL
        ).add_to_hass(hass)
        result = await credentials(hass, (await start(hass))["flow_id"])
        assert result["type"] is FlowResultType.ABORT


class TestWhenItGoesWrong:
    async def test_a_bad_password_says_so(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            FakeLogin, "begin_error", JlrLoginError("the password was rejected")
        )
        result = await credentials(hass, (await start(hass))["flow_id"])
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"
        assert result["errors"] == {"base": "invalid_auth"}

    async def test_credentials_refused_outright_are_an_auth_problem(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The journey can reject the password by raising the same exception it
        # uses for a bad code, before any code has been asked for.
        monkeypatch.setattr(
            FakeLogin, "begin_error", JlrInvalidCode("credentials refused")
        )
        result = await credentials(hass, (await start(hass))["flow_id"])
        assert result["step_id"] == "user"
        assert result["errors"] == {"base": "invalid_auth"}

    async def test_a_wrong_code_lets_them_retype_it(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = await credentials(hass, (await start(hass))["flow_id"])
        monkeypatch.setattr(
            FakeLogin, "complete_error", JlrInvalidCode("that code is not right")
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "000000"}
        )
        # Still on the code step: the journey is alive, only the code is wrong.
        assert result["step_id"] == "code"
        assert result["errors"] == {"base": "invalid_code"}

        monkeypatch.setattr(FakeLogin, "complete_error", None)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": CODE}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY

    async def test_a_dead_journey_sends_them_back_to_the_start(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Re-showing the code form here trapped people asking for a code that
        # could never be accepted, surviving a reload (#10).
        result = await credentials(hass, (await start(hass))["flow_id"])
        monkeypatch.setattr(
            FakeLogin, "complete_error", JlrSessionExpired("the journey timed out")
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": CODE}
        )
        assert result["step_id"] == "user"
        assert result["errors"] == {"base": "session_expired"}

    async def test_an_unreachable_api_is_not_blamed_on_jlr(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sign-in worked; we never reached the vehicle API. Saying the token
        # was rejected sends people hunting a JLR-side problem (#10).
        from custom_components.jlr_incontrol.api import JlrConnectionError

        result = await credentials(hass, (await start(hass))["flow_id"])
        monkeypatch.setattr(
            FakeFlowClient, "vehicles_error", JlrConnectionError("it timed out")
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": CODE}
        )
        assert result["errors"] == {"base": "cannot_reach_api"}

    async def test_a_rejected_token_writes_no_entry(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = await credentials(hass, (await start(hass))["flow_id"])
        monkeypatch.setattr(
            FakeFlowClient, "vehicles_error", RuntimeError("the API said no")
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": CODE}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "api_rejected"}
        assert not hass.config_entries.async_entries(DOMAIN)


class TestSigningInAgain:
    """Reauth and reconfigure both update in place. That is the whole point.

    Deleting and re-adding would take every entity id with it, and with them
    every automation and dashboard card pointing at the car.
    """

    @pytest.fixture
    def existing(self, hass: HomeAssistant) -> MockConfigEntry:
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                "username": EMAIL,
                "device_id": "an-existing-device-id",
                "refresh_token": "a-spent-token",
            },
            unique_id="user-01H8XK4Q2N",
        )
        entry.add_to_hass(hass)
        return entry

    async def reauth(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ) -> dict[str, Any]:
        return await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=dict(entry.data),
        )

    async def test_reauth_updates_the_entry_rather_than_adding_one(
        self, hass: HomeAssistant, existing: MockConfigEntry
    ) -> None:
        result = await self.reauth(hass, existing)
        assert result["step_id"] == "reauth_confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": PASSWORD}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": CODE}
        )

        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        assert len(hass.config_entries.async_entries(DOMAIN)) == 1
        assert existing.data["refresh_token"] == TOKENS["refresh_token"]

    async def test_reauth_keeps_the_device_id(
        self, hass: HomeAssistant, existing: MockConfigEntry
    ) -> None:
        # It is registered with JLR against this account. A new one on every
        # sign-in would leave a trail of dead clients behind.
        result = await self.reauth(hass, existing)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": PASSWORD}
        )
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": CODE}
        )
        assert existing.data["device_id"] == "an-existing-device-id"

    async def test_reauth_with_a_bad_password_stays_on_its_own_step(
        self,
        hass: HomeAssistant,
        existing: MockConfigEntry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            FakeLogin, "begin_error", JlrLoginError("the password was rejected")
        )
        result = await self.reauth(hass, existing)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": PASSWORD}
        )
        assert result["step_id"] == "reauth_confirm"
        assert result["errors"] == {"base": "invalid_auth"}

    async def test_reconfigure_refreshes_the_portal_session(
        self, hass: HomeAssistant, existing: MockConfigEntry
    ) -> None:
        # The reason this step exists: adopting a ForgeRock session on an entry
        # created before the owner portal was used, without losing entity ids.
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": existing.entry_id,
            },
        )
        assert result["step_id"] == "reconfigure"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": PASSWORD}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": CODE}
        )
        assert result["type"] is FlowResultType.ABORT
        assert existing.data["sso_cookies"] == {
            "iPlanetDirectoryPro": "a-forgerock-session"
        }


class TestTheJourneyFallingOverInWaysNobodyPlanned:
    """The catch-alls, and the two states the flow can be resumed into."""

    async def test_an_unexpected_failure_at_sign_in_is_a_connection_error(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Not a crash in someone's config dialog: whatever went wrong, the
        # honest thing to show is that we could not complete the sign-in.
        monkeypatch.setattr(
            FakeLogin, "begin_error", RuntimeError("something nobody predicted")
        )
        result = await credentials(hass, (await start(hass))["flow_id"])
        assert result["errors"] == {"base": "cannot_connect"}

    async def test_an_unexpected_failure_at_the_code_step_too(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = await credentials(hass, (await start(hass))["flow_id"])
        monkeypatch.setattr(
            FakeLogin, "complete_error", RuntimeError("something nobody predicted")
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": CODE}
        )
        assert result["step_id"] == "user"
        assert result["errors"] == {"base": "cannot_connect"}

    async def test_a_journey_refused_at_the_code_step_restarts(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = await credentials(hass, (await start(hass))["flow_id"])
        monkeypatch.setattr(
            FakeLogin, "complete_error", JlrLoginError("the password was rejected")
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": CODE}
        )
        assert result["step_id"] == "user"
        assert result["errors"] == {"base": "invalid_auth"}

    async def test_a_code_submitted_with_no_journey_behind_it(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Home Assistant restarted, or the flow sat open for a long time.
        # Re-showing the code form would ask for one that can never work.
        result = await credentials(hass, (await start(hass))["flow_id"])

        async def begin(self: FakeLogin, password: str) -> None:
            return None

        monkeypatch.setattr(FakeLogin, "async_begin", begin)
        flow = hass.config_entries.flow._progress[result["flow_id"]]
        flow._login = None

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": CODE}
        )
        assert result["step_id"] == "user"
        assert result["errors"] == {"base": "session_expired"}

    async def test_a_bad_code_during_reauth_stays_on_the_reauth_step(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The restart form has to send them back to the step they came from,
        # not to the add-integration form they never saw.
        existing = MockConfigEntry(
            domain=DOMAIN, data={"username": EMAIL}, unique_id="u"
        )
        existing.add_to_hass(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": existing.entry_id,
            },
            data=dict(existing.data),
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": PASSWORD}
        )
        monkeypatch.setattr(FakeLogin, "complete_error", JlrSessionExpired("timed out"))
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": CODE}
        )
        assert result["step_id"] == "reauth_confirm"

    async def test_reconfiguring_an_entry_that_has_gone_aborts(
        self, hass: HomeAssistant
    ) -> None:
        entry = MockConfigEntry(domain=DOMAIN, data={"username": EMAIL})
        entry.add_to_hass(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        await hass.config_entries.async_remove(entry.entry_id)
        flow = hass.config_entries.flow._progress[result["flow_id"]]
        result = await flow.async_step_reconfigure()
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "unknown_entry"

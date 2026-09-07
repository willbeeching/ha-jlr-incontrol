"""The prompt that tells someone their portal session has gone.

Only the user can fix it, and it costs them an emailed code, so it has to
appear when it is true and disappear the moment it stops being.

It used to be a repair raised with is_fixable=False, which is a notice and not
a prompt: it spelled out the steps and left you to go and perform them, and
its only button dismissed it. It is a reauth flow now, which is what every
other integration uses for this and what Home Assistant will actually walk
somebody through.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import Doubles, FakePortal  # noqa: E402
from homeassistant.config_entries import SOURCE_REAUTH  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import issue_registry as ir  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol.const import (  # noqa: E402
    DOMAIN,
    ISSUE_PORTAL_SIGNED_OUT,
)
from custom_components.jlr_incontrol.portal import (  # noqa: E402
    JlrPortalAuthError,
)


def prompts(hass: HomeAssistant, entry: MockConfigEntry) -> list[dict]:
    """Sign-in prompts Home Assistant is showing for this entry."""
    return list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))


def issue_for(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_PORTAL_SIGNED_OUT}_{entry.entry_id}"
    )


@pytest.fixture
def signed_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the owner portal refuse us from the very first read."""
    monkeypatch.setattr(
        FakePortal, "error", JlrPortalAuthError("the owner portal signed us out")
    )


class TestRaising:
    async def test_a_refused_portal_asks_for_a_sign_in(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        signed_out: None,
        loaded: Doubles,
    ) -> None:
        assert prompts(hass, entry)

    async def test_the_prompt_collects_the_sign_in_rather_than_describing_it(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        signed_out: None,
        loaded: Doubles,
    ) -> None:
        # The point of the change: this is a form you fill in, not a notice
        # whose only button means "I have read this".
        flow = prompts(hass, entry)[0]
        result = await hass.config_entries.flow.async_configure(flow["flow_id"])
        assert result["type"] == "form"
        assert result["step_id"] == "reauth_confirm"
        assert "password" in str(result["data_schema"].schema)

    async def test_it_appears_in_repairs_and_opens_the_sign_in(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        signed_out: None,
        loaded: Doubles,
    ) -> None:
        """The prompt has to be somewhere a person will actually meet it.

        Home Assistant raises its own repair for an active reauth flow and
        carries the flow id on it, which is what lets the Repairs list open
        the sign-in dialog rather than describe it. This asserts core's
        behaviour rather than ours on purpose: it is the reason there is no
        bespoke repair here any more, and if it ever stopped being true the
        prompt would quietly go back to being hard to find.
        """
        issue = ir.async_get(hass).async_get_issue(
            "homeassistant", f"config_entry_reauth_{DOMAIN}_{entry.entry_id}"
        )
        assert issue is not None
        assert issue.issue_domain == DOMAIN
        assert issue.severity is ir.IssueSeverity.ERROR
        assert issue.data["flow_id"] == prompts(hass, entry)[0]["flow_id"]

    async def test_the_rest_of_the_integration_still_loads(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        signed_out: None,
        loaded: Doubles,
    ) -> None:
        # Location and names degrade; live status does not depend on the
        # portal at all and must not be taken down with it.
        assert entry.runtime_data.data["vehicles"]

    async def test_a_working_portal_asks_for_nothing(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        assert not prompts(hass, entry)

    async def test_it_belongs_to_this_entry(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        signed_out: None,
        loaded: Doubles,
    ) -> None:
        # The repair this replaced was once raised under a fixed id that no
        # second account could ever clear. A flow carries its entry with it.
        assert prompts(hass, entry)[0]["context"]["entry_id"] == entry.entry_id

    async def test_it_is_asked_once_not_on_every_retry(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        signed_out: None,
        loaded: Doubles,
    ) -> None:
        coordinator = entry.runtime_data
        for _ in range(3):
            coordinator._portal_signed_out = None
            coordinator._portal_due = None
            await coordinator.async_refresh()
            await hass.async_block_till_done()
        assert len(prompts(hass, entry)) == 1


class TestClearing:
    async def test_recovery_withdraws_the_prompt(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        signed_out: None,
        loaded: Doubles,
    ) -> None:
        # A session that came back on its own leaves a prompt that would cost
        # somebody an emailed code for nothing.
        assert prompts(hass, entry)

        loaded.portal.error = None
        coordinator = entry.runtime_data
        coordinator._portal_signed_out = None
        coordinator._portal_due = None

        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert not prompts(hass, entry)
        assert (
            ir.async_get(hass).async_get_issue(
                "homeassistant", f"config_entry_reauth_{DOMAIN}_{entry.entry_id}"
            )
            is None
        ), "aborting the flow must take Home Assistant's repair with it"

    async def test_recovery_also_clears_a_repair_left_by_an_older_version(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
    ) -> None:
        # Upgrading does not remove what the old code already put in the
        # Repairs list, and nothing else will ever take it away.
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{ISSUE_PORTAL_SIGNED_OUT}_{entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_PORTAL_SIGNED_OUT,
        )
        assert issue_for(hass, entry) is not None

        coordinator = entry.runtime_data
        coordinator._portal_signed_out = None
        coordinator._portal_due = None
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert issue_for(hass, entry) is None

    async def test_removing_the_entry_takes_the_prompt_with_it(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        signed_out: None,
        loaded: Doubles,
    ) -> None:
        # Otherwise the prompt outlives the account it is about.
        assert prompts(hass, entry)
        assert await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
        assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

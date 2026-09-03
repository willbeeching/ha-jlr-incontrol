"""The repair that tells someone their portal session has gone.

Only the user can fix it, and it costs them an emailed code, so it has to
appear when it is true and disappear the moment it stops being — and it has to
belong to one config entry, because it used to be raised under a fixed id that
no second account could ever clear.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import Doubles, FakePortal  # noqa: E402
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
    async def test_a_refused_portal_raises_a_repair(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        signed_out: None,
        loaded: Doubles,
    ) -> None:
        assert issue_for(hass, entry) is not None

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

    async def test_a_working_portal_raises_nothing(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        assert issue_for(hass, entry) is None

    async def test_the_repair_is_scoped_to_this_entry(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        signed_out: None,
        loaded: Doubles,
    ) -> None:
        # Under the old fixed id a second account could not raise its own, and
        # whichever entry recovered first cleared the other's warning.
        assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_PORTAL_SIGNED_OUT) is (
            None
        )


class TestClearing:
    async def test_recovery_clears_the_repair(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        signed_out: None,
        loaded: Doubles,
    ) -> None:
        assert issue_for(hass, entry) is not None

        # The portal starts working again. Both clocks are reset by hand
        # rather than by travelling six hours: the back-off is the thing being
        # stepped over here, not the thing under test.
        loaded.portal.error = None
        coordinator = entry.runtime_data
        coordinator._portal_signed_out = None
        coordinator._portal_due = None

        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert issue_for(hass, entry) is None

    async def test_removing_the_entry_takes_the_repair_with_it(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        signed_out: None,
        loaded: Doubles,
    ) -> None:
        # Otherwise the warning outlives the account it is about, and the
        # coordinator that would have cleared it no longer exists.
        assert issue_for(hass, entry) is not None
        assert await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
        assert issue_for(hass, entry) is None

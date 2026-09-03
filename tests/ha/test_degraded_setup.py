"""Setting up when part of the account is not answering.

Two failures that used to take the whole integration down with them: a second
vehicle that never sends a snapshot, and a config entry created before the
owner portal was used and so carrying no session for it.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT, SOLD, Doubles, FakeClient, FakePortal  # noqa: E402
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import issue_registry as ir  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol import (  # noqa: E402
    coordinator as coordinator_module,
)
from custom_components.jlr_incontrol.const import (  # noqa: E402
    DOMAIN,
    ISSUE_PORTAL_SIGNED_OUT,
)


class TestOneCarSilent:
    @pytest.fixture
    def two_cars_one_quiet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A second vehicle on the account that never reports."""

        async def vehicles(self: FakeClient) -> list[dict[str, Any]]:
            return [
                {"vin": KEPT, "role": "Owner"},
                {"vin": SOLD, "role": "Owner"},
            ]

        monkeypatch.setattr(FakeClient, "async_get_vehicles", vehicles)

        from doubles import FakeTelemetry

        async def start(self: FakeTelemetry) -> None:
            self.connected = True
            self.on_connected(True)
            # Only the first car answers. The second stays silent, which is
            # what a car with a flat 12V or no signal looks like.
            self.push(KEPT)

        monkeypatch.setattr(FakeTelemetry, "async_start", start)
        # Setup genuinely waits this out. Real seconds, so make it few.
        monkeypatch.setattr(coordinator_module, "FIRST_SNAPSHOT_TIMEOUT", 0.05)

    async def test_the_account_still_loads(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        two_cars_one_quiet: None,
        doubles: Doubles,
    ) -> None:
        # Leaving the whole account down because one car is asleep would be a
        # poor trade for the owner of the other one.
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED

    async def test_the_car_that_answered_gets_its_entities(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        two_cars_one_quiet: None,
        doubles: Doubles,
    ) -> None:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.async_entity_ids("sensor")

    async def test_the_silent_one_is_still_subscribed(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        two_cars_one_quiet: None,
        doubles: Doubles,
    ) -> None:
        # So its entities appear when it finally reports, rather than needing
        # someone to notice and reload.
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert SOLD in doubles.telemetry.vins


class TestAnEntryWithNoPortalSession:
    """Created before the owner portal was used. Nothing can mint one headlessly."""

    @pytest.fixture
    def unconfigured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(FakePortal, "configured", False)

    async def test_everything_else_still_works(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        unconfigured: None,
        doubles: Doubles,
    ) -> None:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data.data["vehicles"]

    async def test_a_repair_says_how_to_fix_it(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        unconfigured: None,
        doubles: Doubles,
    ) -> None:
        # A repair rather than a back-off: there is nothing to retry, and only
        # Reconfigure can put it right — which keeps every entity id.
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert ir.async_get(hass).async_get_issue(
            DOMAIN, f"{ISSUE_PORTAL_SIGNED_OUT}_{entry.entry_id}"
        )

    async def test_the_location_is_never_asked_for(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        unconfigured: None,
        doubles: Doubles,
    ) -> None:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert doubles.portal.asked == []


class TestPersistingThePortalSession:
    async def test_a_new_session_is_written_to_the_entry(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # So the next start resumes instead of spending the identity session,
        # which by then is usually dead and only the user can replace.
        entry.runtime_data._store_portal_session(
            "https://incontrol.jaguar.com/jaguar-portal-owner-web",
            {"JSESSIONID": "a-new-session"},
            "2026-09-03T12:00:00+00:00",
        )
        await hass.async_block_till_done()
        assert entry.data["portal_cookies"] == {"JSESSIONID": "a-new-session"}

    async def test_an_unchanged_session_writes_nothing(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # Every entry write fires listeners; one that changes nothing is pure
        # churn, and this integration has been bitten by that before.
        coordinator = entry.runtime_data
        coordinator._store_portal_session(
            entry.data["portal_base"], entry.data["portal_cookies"], "whenever"
        )
        await hass.async_block_till_done()
        assert entry.data["portal_cookies"] == {"JSESSIONID": "a-session"}

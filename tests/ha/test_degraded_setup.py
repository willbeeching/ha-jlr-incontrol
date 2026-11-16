"""Setting up when part of the account is not answering.

A second car that never reports used to take the whole integration down
with it.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT, SOLD, Doubles, FakeLink  # noqa: E402
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)


class TestOneCarSilent:
    @pytest.fixture
    def two_cars_one_quiet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A second vehicle on the account that never reports."""

        built = FakeLink.__init__

        def two_cars(self: FakeLink, *args: Any, **kwargs: Any) -> None:
            built(self, *args, **kwargs)
            self.vehicles = [KEPT, SOLD]

        def start(self: FakeLink) -> None:
            self.started = True
            self.connected = True
            # Only the first car answers. The second stays silent, which is
            # what a car with a flat 12V or no signal looks like.
            self.push(KEPT)

        monkeypatch.setattr(FakeLink, "__init__", two_cars)
        monkeypatch.setattr(FakeLink, "async_start", start)
        # Setup genuinely waits this out. Real seconds, so make it few.
        monkeypatch.setattr(
            "custom_components.jlr_incontrol.coordinator." "FIRST_SNAPSHOT_TIMEOUT",
            0.05,
        )

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

    async def test_the_silent_one_gets_entities_when_it_reports(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        two_cars_one_quiet: None,
        doubles: Doubles,
    ) -> None:
        # Rather than needing someone to notice and reload.
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        before = len(hass.states.async_entity_ids("sensor"))
        doubles.link.push(SOLD)
        await hass.async_block_till_done()
        assert len(hass.states.async_entity_ids("sensor")) > before

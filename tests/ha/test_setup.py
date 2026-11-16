"""Setting up, unloading, and failing to do either.

Setup is the path every user takes. It ends one of two ways that matter —
loaded, or retry later — and an entry that loads into a house of unavailable
entities is the third way it used to end, which looked like success.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT, Doubles, FakeLink, device_for  # noqa: E402
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.const import EVENT_HOMEASSISTANT_STOP  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.helpers import issue_registry as ir  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

COORDINATOR = "custom_components.jlr_incontrol.coordinator"


class TestSetup:
    async def test_the_entry_loads(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data.data["vehicles"].keys() == {KEPT}

    async def test_the_vehicle_gets_a_device(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        device = device_for(hass, entry, KEPT)
        assert device is not None
        assert device.manufacturer == "Jaguar"
        assert device.name == "Test Car"

    async def test_the_vehicle_gets_entities(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        assert entities, "no entities were created at all"
        domains = {entity.domain for entity in entities}
        assert "sensor" in domains
        assert "device_tracker" in domains

    async def test_the_link_is_running(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        assert loaded.link.started
        assert loaded.link.details_asked == 1

    async def test_a_repair_an_earlier_version_left_is_cleared(
        self, hass: HomeAssistant, entry: MockConfigEntry, doubles: Doubles
    ) -> None:
        # A revoked link was a repair before 3.0.0-beta.2 and is a reauth now,
        # so nothing else would ever take the old one down.
        issue_id = f"datahub_signed_out_{entry.entry_id}"
        ir.async_create_issue(
            hass,
            "jlr_incontrol",
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="datahub_signed_out",
        )
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert ir.async_get(hass).async_get_issue("jlr_incontrol", issue_id) is None


class TestSetupFailures:
    async def test_no_status_at_all_asks_to_retry(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        doubles: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Not loaded into a house of unavailable entities: that is how a
        # walled endpoint once looked like a working install for a day.
        monkeypatch.setattr(f"{COORDINATOR}.FIRST_SNAPSHOT_TIMEOUT", 0.05)

        def silent(self: FakeLink) -> None:
            self.started = True

        monkeypatch.setattr(FakeLink, "async_start", silent)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert "no vehicle status" in str(entry.reason)


class TestUnload:
    async def test_the_entry_unloads(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.NOT_LOADED

    async def test_the_link_is_stopped(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert loaded.link.stopped

    async def test_reloading_leaves_one_live_link(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        first = loaded.link
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert first.stopped, "the link from before the reload is still running"
        assert loaded.link is not first
        assert loaded.link.connected


class TestHomeAssistantStopping:
    """A restart is not an unload.

    Core does not unload config entries when it shuts down, so unload never
    ran on a restart: the stream was left to time out at the broker, and
    anything held only in memory was lost.
    """

    async def test_the_link_is_stopped_when_core_stops(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
        assert loaded.link.stopped, "the broker was left holding the session"

    async def test_unloading_afterwards_is_not_a_second_teardown(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.NOT_LOADED

"""Setting up, unloading, and failing to do either.

Setup is the path every user takes and the one nothing covered. It has three
distinct ways to end — loaded, retry later, ask for a sign-in — and telling
them apart wrongly is what sent people off to fetch an emailed code that could
not have helped.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT, Doubles, FakeClient  # noqa: E402
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.const import EVENT_HOMEASSISTANT_STOP  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import device_registry as dr  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol.api import (  # noqa: E402
    JlrApiError,
    JlrAuthError,
)
from custom_components.jlr_incontrol.const import DOMAIN  # noqa: E402


class TestSetup:
    async def test_the_entry_loads(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data.data["vehicles"].keys() == {KEPT}

    async def test_the_vehicle_gets_a_device(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, KEPT)})
        assert device is not None
        assert device.manufacturer == "Jaguar"

    async def test_the_vehicle_gets_entities(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        assert entities, "no entities were created at all"
        # The status keys the fake pushes back each of these.
        domains = {entity.domain for entity in entities}
        assert "sensor" in domains
        assert "device_tracker" in domains

    async def test_the_socket_is_subscribed_to_the_vehicle(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        assert loaded.telemetry.vins == [KEPT]

    async def test_the_portal_is_asked_for_that_vehicle(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        assert loaded.portal.asked == ["id-kept"]


class TestSetupFailures:
    """The distinction that matters: retry later, or ask the user for a code."""

    async def test_a_backend_outage_asks_to_retry(
        self, hass: HomeAssistant, entry: MockConfigEntry, doubles: Doubles
    ) -> None:
        def build(*args: object, **kwargs: object) -> object:
            made = FakeClient(*args, **kwargs)
            made.connect_error = JlrApiError("Jaguar Land Rover returned 503")
            doubles.client = made
            return made

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                "custom_components.jlr_incontrol.coordinator.JlrClient", build
            )
            await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

        # Retry, not reauth: an outage says nothing about the credentials.
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert not list(entry.async_get_active_flows(hass, {"reauth"}))

    async def test_spent_credentials_ask_for_a_sign_in(
        self, hass: HomeAssistant, entry: MockConfigEntry, doubles: Doubles
    ) -> None:
        def build(*args: object, **kwargs: object) -> object:
            made = FakeClient(*args, **kwargs)
            made.connect_error = JlrAuthError("the refresh token has been spent")
            doubles.client = made
            return made

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                "custom_components.jlr_incontrol.coordinator.JlrClient", build
            )
            await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.SETUP_ERROR
        assert list(entry.async_get_active_flows(hass, {"reauth"}))


class TestUnload:
    async def test_the_entry_unloads(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.NOT_LOADED

    async def test_the_socket_and_the_portal_are_closed(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # Neither closes itself. A reload used to leave the old socket running
        # alongside the new one, both subscribed to the same vehicles.
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert loaded.telemetry.stopped
        assert loaded.portal.closed

    async def test_reloading_leaves_one_live_socket(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        first = loaded.telemetry
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert first.stopped, "the socket from before the reload is still running"
        assert loaded.telemetry is not first
        assert loaded.telemetry.connected


class TestHomeAssistantStopping:
    """A restart is not an unload, and used to leave the socket dangling.

    Core does not unload config entries when it shuts down, so
    async_unload_entry never ran on a restart. The socket was never closed —
    core simply tore the aiohttp session down underneath the read loop, which
    then logged a dropped connection and scheduled a retry on the way out of a
    process that was exiting. Four of those in one day's log on a live install.
    """

    async def test_the_socket_is_closed_when_core_stops(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
        assert loaded.telemetry.stopped, "the broker was left holding the session"
        assert loaded.portal.closed

    async def test_unloading_afterwards_is_not_a_second_teardown(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # Both paths can fire in one shutdown; the second must be a no-op
        # rather than an error on an already-cancelled task.
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.NOT_LOADED

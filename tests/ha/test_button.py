"""The one button, and the silence it used to answer with.

Pressing refresh when Jaguar Land Rover are not answering did nothing visible:
async_request_refresh swallows the failure, which is right for a background
poll and wrong for someone standing in front of the button waiting.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT, Doubles  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol import coordinator  # noqa: E402
from custom_components.jlr_incontrol.api import JlrApiError  # noqa: E402
from custom_components.jlr_incontrol.coordinator import (  # noqa: E402
    Resubscription,
)


async def press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id}, blocking=True
    )


def refresh_button(hass: HomeAssistant) -> str:
    (entity_id,) = [
        item for item in hass.states.async_entity_ids("button") if "refresh" in item
    ]
    return entity_id


class TestPressingRefresh:
    async def test_it_refreshes(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        freezer: Any,
    ) -> None:
        # Strictly more. ">=" is what this said for months, and it passes
        # whether or not the press reaches anything — which is exactly how a
        # button that did nothing at all went unnoticed.
        freezer.tick(timedelta(minutes=10))
        before = len(loaded.portal.asked)
        await press(hass, refresh_button(hass))
        assert len(loaded.portal.asked) > before

    async def test_a_backend_that_is_not_answering_says_so(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # The difference between "nothing to update" and "Jaguar Land Rover are
        # down" is the whole reason someone pressed it.
        loaded.client.connect_error = JlrApiError("returned 503")
        with pytest.raises(HomeAssistantError) as raised:
            await press(hass, refresh_button(hass))
        assert raised.value.translation_key == "refresh_failed"

    async def test_the_message_is_translatable_not_baked_in(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        loaded.client.connect_error = JlrApiError("returned 503")
        with pytest.raises(HomeAssistantError) as raised:
            await press(hass, refresh_button(hass))
        assert raised.value.translation_domain == "jlr_incontrol"

    async def test_it_recovers_once_the_backend_does(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        loaded.client.connect_error = JlrApiError("returned 503")
        with pytest.raises(HomeAssistantError):
            await press(hass, refresh_button(hass))

        loaded.client.connect_error = None
        await press(hass, refresh_button(hass))

    async def test_the_button_works_with_the_socket_down(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # Greying it out because telemetry is down disables it exactly when
        # someone would reach for it, and it does not read from that socket.
        loaded.telemetry.drop()
        await hass.async_block_till_done()
        await press(hass, refresh_button(hass))


class TestRefreshReachesThePortal:
    """The gate that made this button a no-op most of the time.

    The portal is read on a half-hourly background cadence, and the button
    went through the same gate. So for twenty-nine minutes in every thirty a
    press re-authenticated, listed the vehicles and returned without reading
    location at all — the one thing here worth pressing a button for. It
    looked identical either way, which is why it took a stopwatch on a real
    instance to catch: the press finished in a quarter of a second.
    """

    async def test_a_press_reads_the_portal_long_after_the_last_one(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        freezer: Any,
    ) -> None:
        # Ten minutes on: nowhere near the half-hourly cadence, which is the
        # window the button spends most of its life in.
        freezer.tick(timedelta(minutes=10))
        before = len(loaded.portal.asked)

        await press(hass, refresh_button(hass))
        await hass.async_block_till_done()

        assert (
            len(loaded.portal.asked) > before
        ), "the press was gated by the background cadence and did nothing"

    # The floor itself is not tested through the button: pressing goes via the
    # coordinator's refresh debouncer, which runs on the monotonic clock that
    # a frozen wall clock does not move — so a second press here measures the
    # debouncer rather than anything this integration decides. It is covered
    # directly instead, in tests/test_coordinator_vehicles.py.


class TestRefreshReachesTheCar:
    """The other half of the press, and the half people meant.

    Vehicle status does not come from the portal or the REST API — JLR wall
    the status endpoint behind app attestation — it comes over the telemetry
    socket, and the broker sends a snapshot only when a subscription is made.
    So a press that read the portal moved the map pin and left every door,
    window and fuel reading exactly as it was.
    """

    async def test_a_press_resubscribes_the_socket(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        await press(hass, refresh_button(hass))
        assert loaded.telemetry.stopped is True
        assert loaded.telemetry.connected is True

    async def test_a_second_press_does_not_hammer_the_broker(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # Somebody leaning on the button must not turn into a reconnect loop
        # against somebody else's server.
        await press(hass, refresh_button(hass))
        loaded.telemetry.stopped = False

        with pytest.raises(HomeAssistantError):
            await press(hass, refresh_button(hass))
        assert loaded.telemetry.stopped is False

    async def test_a_press_inside_the_floor_says_so_rather_than_nothing(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # Skipping the half of the press somebody pressed it for, and
        # reporting success, is the fault this button spent two releases
        # living down. It does not get to come back as a rate limit.
        await press(hass, refresh_button(hass))
        with pytest.raises(HomeAssistantError) as raised:
            await press(hass, refresh_button(hass))
        assert raised.value.translation_key == "refresh_too_soon"

    async def test_the_floor_lifts(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        freezer: Any,
    ) -> None:
        await press(hass, refresh_button(hass))
        loaded.telemetry.stopped = False
        freezer.tick(timedelta(minutes=2))

        await press(hass, refresh_button(hass))
        assert loaded.telemetry.stopped is True

    async def test_a_socket_that_does_not_come_back_says_so(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        monkeypatch: Any,
    ) -> None:
        # Silence here is the original complaint in a new place: the press
        # would report success while the readings stayed as stale as before.
        async def never(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(coordinator, "RESUBSCRIBE_TIMEOUT", 0.01)
        monkeypatch.setattr(loaded.telemetry, "async_start", never)

        with pytest.raises(HomeAssistantError) as raised:
            await press(hass, refresh_button(hass))
        assert raised.value.translation_key == "refresh_failed"


class TestAPressThatOutlivesTheEntry:
    """A press in flight when the integration goes away.

    Stopping the old socket yields while the supervisor unwinds and says
    goodbye to the broker — properly, which is the point, but it is a wide
    gap and an unload fits inside it. Resuming afterwards into a start left a
    supervisor running on an entry that no longer existed, and the reload that
    usually follows built a second one beside it: two connections to somebody
    else's broker from one integration.
    """

    async def test_a_press_after_unload_starts_nothing(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        coordinator = entry.runtime_data
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        loaded.telemetry.stopped = False

        result = await coordinator.async_resubscribe_telemetry(KEPT)

        assert result is Resubscription.FAILED
        assert loaded.telemetry.connected is False
        assert loaded.telemetry.stopped is False, "it restarted the socket"

    async def test_the_flag_outranks_a_press_already_inside(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # The race itself: the press is past its first check and waiting on
        # the old socket to unwind when the entry starts going down.
        coordinator = entry.runtime_data
        original = loaded.telemetry.async_stop

        async def unload_lands_here() -> None:
            coordinator._stopping = True
            await original()

        loaded.telemetry.async_stop = unload_lands_here

        result = await coordinator.async_resubscribe_telemetry(KEPT)

        assert result is Resubscription.FAILED
        assert loaded.telemetry.connected is False


class TestAPressThatGetsNoData:
    """Connected is not the same as answered.

    The broker sets the connection up, accepts the subscriptions, and only
    then pushes. Returning on the first of those reports success while every
    reading on the dashboard is still the one the press was meant to replace.
    """

    async def test_a_socket_that_sends_nothing_is_not_a_success(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        monkeypatch: Any,
    ) -> None:
        monkeypatch.setattr(coordinator, "RESUBSCRIBE_TIMEOUT", 0.05)

        async def connects_but_says_nothing() -> None:
            loaded.telemetry.connected = True
            loaded.telemetry.on_connected(True)

        monkeypatch.setattr(loaded.telemetry, "async_start", connects_but_says_nothing)

        result = await entry.runtime_data.async_resubscribe_telemetry(KEPT)

        assert (
            result is Resubscription.FAILED
        ), "the press reported success having changed nothing"

    async def test_a_snapshot_is_what_makes_it_a_success(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        result = await entry.runtime_data.async_resubscribe_telemetry(KEPT)
        assert result is Resubscription.DONE

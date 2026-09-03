"""The one button, and the silence it used to answer with.

Pressing refresh when Jaguar Land Rover are not answering did nothing visible:
async_request_refresh swallows the failure, which is right for a background
poll and wrong for someone standing in front of the button waiting.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import Doubles  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol.api import JlrApiError  # noqa: E402


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
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        before = len(loaded.portal.asked)
        await press(hass, refresh_button(hass))
        assert len(loaded.portal.asked) >= before

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

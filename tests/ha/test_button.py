"""The one button, and the silence it used to answer with.

Pressing refresh when Jaguar Land Rover were not answering once did nothing
visible, and a press that read nothing looked the same as one that did. So
every outcome is said: done, too soon, or failed.
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
    async def test_it_reads_this_car_now(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        await press(hass, refresh_button(hass))
        assert loaded.link.reads == [KEPT]

    async def test_a_read_that_brings_nothing_says_so(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # The difference between "nothing to update" and "Jaguar Land Rover
        # are not answering" is the whole reason someone pressed it.
        loaded.link.read_now_result = False
        with pytest.raises(HomeAssistantError) as raised:
            await press(hass, refresh_button(hass))
        assert raised.value.translation_key == "refresh_failed"
        assert raised.value.translation_domain == "jlr_incontrol"

    async def test_a_link_jlr_no_longer_accept_says_so_without_asking(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        loaded.link.signed_out = True
        with pytest.raises(HomeAssistantError) as raised:
            await press(hass, refresh_button(hass))
        assert raised.value.translation_key == "refresh_failed"
        assert loaded.link.reads == []

    async def test_it_works_while_the_link_is_unhealthy(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # Greying it out then would disable it exactly when someone would
        # reach for it.
        loaded.link.drop()
        entry.runtime_data.async_update_listeners()
        await hass.async_block_till_done()
        await press(hass, refresh_button(hass))
        assert loaded.link.reads == [KEPT]


class TestTheFloor:
    """A press is not a hammer: one read a minute, however often it is pressed."""

    async def test_a_second_press_inside_the_minute_says_so(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        await press(hass, refresh_button(hass))
        with pytest.raises(HomeAssistantError) as raised:
            await press(hass, refresh_button(hass))
        assert raised.value.translation_key == "refresh_too_soon"
        assert loaded.link.reads == [KEPT]

    async def test_the_floor_lifts(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        freezer: Any,
    ) -> None:
        await press(hass, refresh_button(hass))
        freezer.tick(timedelta(minutes=1, seconds=1))
        await press(hass, refresh_button(hass))
        assert loaded.link.reads == [KEPT, KEPT]

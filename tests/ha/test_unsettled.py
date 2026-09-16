"""Readings taken while somebody still had the key in the car.

Twice a car has been reported with its windows down for hours after being
parked, and both times the held snapshot said the same thing: the key was
still in it and the alarm had not yet armed. That is a photograph of a car
somebody is still getting out of, and the doors and windows in it are where
they were at that instant rather than where the car was left. No newer
snapshot followed, because the staleness lives in Jaguar Land Rover's copy
and nothing this integration can do reaches past it.

So the readings are withheld rather than asserted. This does not make the
data fresher; it stops a guess being displayed as a fact.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT, STATUS, Doubles  # noqa: E402
from homeassistant.const import STATE_UNKNOWN  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

CAUGHT_MID_USE = {
    **STATUS,
    "VEHICLE_STATE_TYPE": "KEY_ON_ENGINE_OFF",
    "THEFT_ALARM_STATUS": "ALARM_OFF",
    "DOOR_IS_ALL_DOORS_LOCKED": "FALSE",
}
SETTLED = {
    **STATUS,
    "VEHICLE_STATE_TYPE": "KEY_REMOVED",
    "DOOR_IS_ALL_DOORS_LOCKED": "FALSE",
}


def locking(hass: HomeAssistant) -> Any:
    """The central locking sensor: in the fixture, and volatile.

    Doors and windows behave identically — they carry the same flag — but the
    fixture's status document has no window key, so no window entity exists to
    assert against.
    """
    (entity_id,) = [
        item
        for item in hass.states.async_entity_ids("binary_sensor")
        if item.endswith("_central_locking")
    ]
    return hass.states.get(entity_id)


async def age(hass: HomeAssistant, entry: MockConfigEntry, freezer: Any, **kw) -> None:
    """Move past the threshold without anything new arriving."""
    freezer.tick(timedelta(**kw))
    entry.runtime_data._push()
    await hass.async_block_till_done()


class TestACarCaughtMidUse:
    async def test_a_fresh_snapshot_is_still_believed(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # Thirty seconds after the key came out is not stale, it is current.
        loaded.telemetry.push(KEPT, CAUGHT_MID_USE)
        await hass.async_block_till_done()
        assert locking(hass).state == "on"

    async def test_it_stops_being_asserted_once_it_goes_quiet(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        freezer: Any,
    ) -> None:
        loaded.telemetry.push(KEPT, CAUGHT_MID_USE)
        await hass.async_block_till_done()

        await age(hass, entry, freezer, minutes=45)

        assert locking(hass).state == STATE_UNKNOWN, (
            "a window reported open on a car parked three quarters of an hour "
            "ago, from a snapshot taken while somebody was still in it"
        )

    async def test_a_settled_car_keeps_its_readings_for_as_long_as_it_likes(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        freezer: Any,
    ) -> None:
        # The case that rules out blanking on age alone. A car left with a
        # window genuinely open, parked for a day, is reporting the truth.
        loaded.telemetry.push(KEPT, SETTLED)
        await hass.async_block_till_done()

        await age(hass, entry, freezer, hours=26)

        assert locking(hass).state == "on"

    async def test_an_unrecognised_state_is_left_alone(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        freezer: Any,
    ) -> None:
        # Only two values have ever been seen. A car reporting a third must
        # behave as it does today rather than have its readings hidden on the
        # strength of a guess about what the word means.
        loaded.telemetry.push(
            KEPT, {**CAUGHT_MID_USE, "VEHICLE_STATE_TYPE": "SOMETHING_NEW"}
        )
        await hass.async_block_till_done()

        await age(hass, entry, freezer, hours=3)

        assert locking(hass).state == "on"

    async def test_the_readings_that_do_not_decay_are_untouched(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        freezer: Any,
    ) -> None:
        # An odometer from an hour ago is still the best answer there is.
        loaded.telemetry.push(KEPT, CAUGHT_MID_USE)
        await hass.async_block_till_done()

        await age(hass, entry, freezer, hours=3)

        (odo,) = [
            hass.states.get(item)
            for item in hass.states.async_entity_ids("sensor")
            if item.endswith("_odometer")
        ]
        assert odo.state not in (STATE_UNKNOWN, "unavailable")

    async def test_turning_it_off_restores_the_old_behaviour(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        freezer: Any,
    ) -> None:
        # "0" from the selector is a string, and a truthy one.
        hass.config_entries.async_update_entry(
            entry, options={"unsettled_minutes": "0"}
        )
        await hass.async_block_till_done()
        loaded.telemetry.push(KEPT, CAUGHT_MID_USE)
        await hass.async_block_till_done()

        await age(hass, entry, freezer, hours=3)

        assert locking(hass).state == "on"

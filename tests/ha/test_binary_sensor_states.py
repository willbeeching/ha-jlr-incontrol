"""Binary sensors that have to answer "I do not know" rather than guess.

Every one of these is gated on a status key at creation, so the interesting
cases are the ones where the key is present but says nothing useful, or stops
being sent by a car that was sending it a moment ago.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT, STATUS, Doubles, FakeClient  # noqa: E402
from homeassistant.const import STATE_UNKNOWN  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol.const import DOMAIN  # noqa: E402

DOORS = {
    "DOOR_FRONT_LEFT_POSITION": "CLOSED",
    "DOOR_REAR_LEFT_POSITION": "CLOSED",
    "DOOR_REAR_RIGHT_POSITION": "CLOSED",
    "WINDOW_REAR_LEFT_STATUS": "CLOSED",
    "WINDOW_REAR_RIGHT_STATUS": "CLOSED",
}


def reporting(monkeypatch: pytest.MonkeyPatch, extra: dict[str, str]) -> None:
    """Make the car's first snapshot carry more than the shared default."""
    from doubles import FakeTelemetry

    async def start(self: FakeTelemetry) -> None:
        self.connected = True
        self.on_connected(True)
        for vin in self.vins:
            self.push(vin, {**STATUS, **extra})

    monkeypatch.setattr(FakeTelemetry, "async_start", start)


def state_of(hass: HomeAssistant, key: str) -> str | None:
    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{KEPT}_{key}"
    )
    return None if entity_id is None else hass.states.get(entity_id).state


class TestBodiesWithoutRearDoors:
    async def test_the_car_reporting_rear_doors_it_does_not_have_is_believed_once(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        doubles: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A 2-door body still sends the rear-door keys, filled with CLOSED.

        Trusting the keys alone put four sensors on a car with nothing behind
        the front seats, permanently reading closed.
        """

        async def two_door(self: FakeClient, vin: str) -> dict[str, Any]:
            return {
                "vehicleBrand": "Jaguar",
                "fuelType": "Petrol",
                "nickname": "Test Car",
                "numberOfDoors": "2",
            }

        monkeypatch.setattr(FakeClient, "async_get_attributes", two_door)
        reporting(monkeypatch, DOORS)

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert state_of(hass, "door_front_left") is not None
        for key in ("door_rear_left", "window_rear_right"):
            assert state_of(hass, key) is None

    async def test_a_car_that_does_not_say_gets_the_benefit_of_the_doubt(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        doubles: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # numberOfDoors is missing on plenty of accounts. Reading that as
        # "2-door" would strip the rear doors off every one of them.
        reporting(monkeypatch, DOORS)

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert state_of(hass, "door_rear_left") == "off"


class TestValuesThatMeanNothing:
    async def test_an_unknown_sentinel_reads_unknown_not_off(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        doubles: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # JLR sends UNKNOWN for anything the car has not reported this trip.
        # Mapping it through is_on would say "alarm not going off" on the
        # strength of no information at all.
        reporting(monkeypatch, {"THEFT_ALARM_STATUS": "UNKNOWN"})
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert state_of(hass, "alarm_triggered") == STATE_UNKNOWN

    async def test_a_key_that_stops_being_sent_reads_unknown_not_stale(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # Seen live: a car drops a key it was sending. The last value is not
        # the current one, and saying so is the only honest answer.
        # Central locking is inverted — on means a door is unlocked.
        assert state_of(hass, "doors_locked") == "off"
        dropped = {k: v for k, v in STATUS.items() if k != "DOOR_IS_ALL_DOORS_LOCKED"}
        loaded.telemetry.push(KEPT, dropped)
        await hass.async_block_till_done()
        assert state_of(hass, "doors_locked") == STATE_UNKNOWN

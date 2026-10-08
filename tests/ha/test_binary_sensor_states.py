"""Binary sensors that have to answer "I do not know" rather than guess.

Every one of these is gated on a status key at creation, so the interesting
cases are the ones where the key is present but says nothing useful, or stops
being sent by a car that was sending it a moment ago.
"""

from __future__ import annotations

import logging
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


LOCKS = {
    "DOOR_FRONT_LEFT_LOCK_STATUS": "LOCKED",
    "DOOR_FRONT_RIGHT_LOCK_STATUS": "UNLOCKED",
    "DOOR_REAR_LEFT_LOCK_STATUS": "LOCKED",
    "DOOR_REAR_RIGHT_LOCK_STATUS": "LOCKED",
    "DOOR_BOOT_LOCK_STATUS": "LOCKED",
}
LOCK_KEYS = (
    "door_front_left_lock",
    "door_front_right_lock",
    "door_rear_left_lock",
    "door_rear_right_lock",
    "boot_lock",
)


def switched_on(hass: HomeAssistant, entry: MockConfigEntry, *keys: str) -> None:
    """Register these as enabled before setup, as a user switching them on would.

    They are off by default, and an entity that is off has no state to read.
    """
    registry = er.async_get(hass)
    for key in keys:
        registry.async_get_or_create(
            "binary_sensor", DOMAIN, f"{KEPT}_{key}", config_entry=entry
        )


def disabled(hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
    return {
        item.unique_id
        for item in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        if item.disabled_by is not None
    }


class TestEachDoorsOwnLock:
    """#31: per-door lock status, for what central locking cannot say.

    Single-point entry unlocks the driver's door alone, which leaves the car
    in a state no all-doors flag can describe.
    """

    async def test_they_exist_but_are_off_by_default(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        doubles: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The central sensor answers the usual question, and these are as
        # stale as it is. Five more entities per car is a lot to switch on
        # for everybody.
        reporting(monkeypatch, LOCKS)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert {f"{KEPT}_{key}" for key in LOCK_KEYS} <= disabled(hass, entry)

    async def test_a_car_that_does_not_report_them_gets_none(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        registered = {
            item.unique_id
            for item in er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
        }
        assert not {f"{KEPT}_{key}" for key in LOCK_KEYS} & registered

    async def test_each_reads_its_own_door(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        doubles: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The case the central flag cannot show: one door open to the world,
        # the rest locked. LOCK device class, so on means unlocked.
        switched_on(hass, entry, *LOCK_KEYS)
        reporting(monkeypatch, LOCKS)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert state_of(hass, "door_front_right_lock") == "on"
        for key in ("door_front_left_lock", "door_rear_left_lock", "boot_lock"):
            assert state_of(hass, key) == "off"

    async def test_a_value_nobody_has_seen_reads_unknown(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        doubles: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # JLR cars double-lock, and the spelling of that state has never been
        # captured. Reading it as unlocked sends somebody out to a car that is
        # more locked than usual; reading anything strange as locked is false
        # comfort. Neither guess is safe.
        switched_on(hass, entry, "door_front_left_lock")
        reporting(monkeypatch, {"DOOR_FRONT_LEFT_LOCK_STATUS": "DOUBLE_LOCKED"})
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert state_of(hass, "door_front_left_lock") == STATE_UNKNOWN

    async def test_a_two_door_body_gets_no_rear_door_locks(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        doubles: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Same rule, and the same reason, as the rear doors themselves.
        async def two_door(self: FakeClient, vin: str) -> dict[str, Any]:
            return {
                "vehicleBrand": "Jaguar",
                "fuelType": "Petrol",
                "nickname": "Test Car",
                "numberOfDoors": "2",
            }

        monkeypatch.setattr(FakeClient, "async_get_attributes", two_door)
        reporting(monkeypatch, LOCKS)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        off = disabled(hass, entry)
        assert f"{KEPT}_door_front_left_lock" in off
        assert f"{KEPT}_door_rear_left_lock" not in off
        assert f"{KEPT}_door_rear_right_lock" not in off


class TestSayingWhyItIsUnknown:
    """Three causes, one Unknown on the dashboard (#31).

    A reporter saw short-lived Unknowns on the new lock sensors and could not
    say which it was: a value nobody has mapped, a snapshot without the key,
    or a stale reading held back. The debug log now says, once per episode.
    """

    @staticmethod
    def why(caplog: pytest.LogCaptureFixture, key: str) -> list[str]:
        return [m for m in caplog.messages if f" {key} reads unknown: " in m]

    async def test_a_value_nobody_has_mapped_is_named(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        doubles: Doubles,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        switched_on(hass, entry, "door_front_left_lock")
        reporting(monkeypatch, {"DOOR_FRONT_LEFT_LOCK_STATUS": "DOUBLE_LOCKED"})
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        (line,) = self.why(caplog, "door_front_left_lock")
        assert "'DOUBLE_LOCKED' is not a value it understands" in line
        assert KEPT not in caplog.text, "the VIN reached the log"

    async def test_a_missing_key_says_so(
        self,
        hass: HomeAssistant,
        loaded: Doubles,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        dropped = {k: v for k, v in STATUS.items() if k != "DOOR_IS_ALL_DOORS_LOCKED"}
        loaded.telemetry.push(KEPT, dropped)
        await hass.async_block_till_done()

        (line,) = self.why(caplog, "doors_locked")
        assert "its key was not in the latest snapshot" in line

    async def test_the_cars_own_unknown_is_told_apart(
        self,
        hass: HomeAssistant,
        loaded: Doubles,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        loaded.telemetry.push(KEPT, {**STATUS, "DOOR_IS_ALL_DOORS_LOCKED": "UNKNOWN"})
        await hass.async_block_till_done()

        (line,) = self.why(caplog, "doors_locked")
        assert "the car reported UNKNOWN" in line

    async def test_once_per_episode_not_once_per_write(
        self,
        hass: HomeAssistant,
        loaded: Doubles,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The broker hands the same snapshot back on every reconnect, every
        # few minutes. One line per Unknown, not one per redelivery; and a
        # fresh line when it comes back after a real reading.
        caplog.set_level(logging.DEBUG)
        dropped = {k: v for k, v in STATUS.items() if k != "DOOR_IS_ALL_DOORS_LOCKED"}
        for snapshot in (dropped, dropped, dropped):
            loaded.telemetry.push(KEPT, snapshot)
            await hass.async_block_till_done()
        assert len(self.why(caplog, "doors_locked")) == 1

        loaded.telemetry.push(KEPT, STATUS)
        await hass.async_block_till_done()
        loaded.telemetry.push(KEPT, dropped)
        await hass.async_block_till_done()
        assert len(self.why(caplog, "doors_locked")) == 2

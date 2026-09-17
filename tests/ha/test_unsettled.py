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

import doubles  # noqa: E402
from doubles import KEPT, STATUS, Doubles  # noqa: E402
from homeassistant.const import STATE_UNKNOWN  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

# No LAST_UPDATED_TIME: neither of the cars this was built for sends one,
# which is the whole reason freshness has to be observed rather than read.
NO_TIMESTAMP = {k: v for k, v in STATUS.items() if k != "LAST_UPDATED_TIME"}

CAUGHT_MID_USE = {
    **NO_TIMESTAMP,
    "VEHICLE_STATE_TYPE": "KEY_ON_ENGINE_OFF",
    "THEFT_ALARM_STATUS": "ALARM_OFF",
    "DOOR_IS_ALL_DOORS_LOCKED": "FALSE",
}
SETTLED = {
    **NO_TIMESTAMP,
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


class TestTheClockItRunsOn:
    """Two ways the threshold never arrives."""

    async def test_a_car_that_has_never_changed_still_expires(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        freezer: Any,
    ) -> None:
        # A fresh install. The first snapshot is not a change — we have
        # nothing to compare it with — so nothing records when it arrived,
        # and a car that then repeats it forever has no age at all.
        coord = entry.runtime_data
        coord._last_changed.pop(KEPT, None)
        coord._last_status_seen.pop(KEPT, None)
        loaded.telemetry.push(KEPT, CAUGHT_MID_USE)
        await hass.async_block_till_done()

        await age(hass, entry, freezer, minutes=45)

        assert locking(hass).state == STATE_UNKNOWN

    async def test_battery_drift_does_not_renew_a_door_reading(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        freezer: Any,
    ) -> None:
        # The 12V voltage moves on a parked car. If any field moving counts
        # as the car reporting, a battery slowly discharging keeps a door
        # reading from last night looking current indefinitely.
        loaded.telemetry.push(KEPT, CAUGHT_MID_USE)
        await hass.async_block_till_done()

        for volts in ("12.5", "12.4", "12.3", "12.2"):
            freezer.tick(timedelta(minutes=12))
            loaded.telemetry.push(KEPT, {**CAUGHT_MID_USE, "BATTERY_VOLTAGE": volts})
            await hass.async_block_till_done()

        assert (
            locking(hass).state == STATE_UNKNOWN
        ), "a discharging battery kept a stale lock reading trusted"


class TestTheTwoListsAgree:
    """The key set and the sensors it governs, kept in step.

    The coordinator cannot import the platform, so the keys are written out in
    const.py as well as implied by the descriptions. Two lists over one idea
    drift, and this is the cheap thing that stops it: a sensor marked volatile
    whose key nobody watches would simply never expire.
    """

    def test_every_volatile_sensor_has_its_key_watched(self) -> None:
        from custom_components.jlr_incontrol.binary_sensor import (
            VEHICLE_BINARY_SENSORS,
        )
        from custom_components.jlr_incontrol.const import VOLATILE_STATUS_KEYS

        missing = {
            d.status_key
            for d in VEHICLE_BINARY_SENSORS
            if d.volatile and d.status_key not in VOLATILE_STATUS_KEYS
        }
        assert not missing, f"marked volatile but unwatched: {sorted(missing)}"

    def test_nothing_is_watched_that_no_sensor_uses(self) -> None:
        from custom_components.jlr_incontrol.binary_sensor import (
            VEHICLE_BINARY_SENSORS,
        )
        from custom_components.jlr_incontrol.const import VOLATILE_STATUS_KEYS

        used = {d.status_key for d in VEHICLE_BINARY_SENSORS if d.volatile}
        # VEHICLE_STATE_TYPE is watched without being a binary sensor: it is
        # what decides the car is mid-use in the first place.
        assert set(VOLATILE_STATUS_KEYS) - used == {"VEHICLE_STATE_TYPE"}


class TestTheClockSurvivesARestart:
    async def test_a_reload_does_not_hand_back_the_threshold(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        freezer: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Restarts are not rare, and one that reset this would trust a
        # mid-shutdown snapshot for another half hour every time.
        loaded.telemetry.push(KEPT, CAUGHT_MID_USE)
        await hass.async_block_till_done()
        entry.runtime_data._persist()
        freezer.tick(timedelta(minutes=45))

        # The car is still sitting there, so every snapshot after the reload
        # is the same one — including the one the new socket is handed the
        # moment it subscribes. A settled snapshot would rightly clear the
        # clock; this is the case where nothing has settled.
        monkeypatch.setattr(doubles, "STATUS", CAUGHT_MID_USE)
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        # What the broker actually does on a resubscription: hands back the
        # same snapshot it was already holding. The clock must not treat that
        # as the car having just reported.
        loaded.telemetry.push(KEPT, CAUGHT_MID_USE)
        await hass.async_block_till_done()

        assert locking(hass).state == STATE_UNKNOWN


class TestARestartThatMissesAChange:
    """The cost of not restarting the clock on the first snapshot back.

    That guard exists so a broker handing back what it already held does not
    look like the car reporting in. But after a restart there is nothing to
    compare against, so a snapshot whose locks have genuinely moved is treated
    the same as one that has not — and a reading that just became true stays
    hidden.
    """

    async def test_a_changed_reading_gets_a_fresh_window(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        freezer: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        loaded.telemetry.push(KEPT, CAUGHT_MID_USE)
        await hass.async_block_till_done()
        entry.runtime_data._persist()
        await age(hass, entry, freezer, minutes=45)
        assert locking(hass).state == STATE_UNKNOWN

        # Somebody locked it. Still mid-use — the key has not come out — but
        # this is a new observation and deserves to be believed.
        locked = {**CAUGHT_MID_USE, "DOOR_IS_ALL_DOORS_LOCKED": "TRUE"}
        monkeypatch.setattr(doubles, "STATUS", locked)
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

        assert locking(hass).state == "off", (
            "a lock reading that had just changed stayed hidden across a "
            "restart, because nothing remembered what it changed from"
        )

    async def test_an_unchanged_one_stays_hidden(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        freezer: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The other half, and the reason the guard is there at all.
        loaded.telemetry.push(KEPT, CAUGHT_MID_USE)
        await hass.async_block_till_done()
        entry.runtime_data._persist()
        await age(hass, entry, freezer, minutes=45)

        monkeypatch.setattr(doubles, "STATUS", CAUGHT_MID_USE)
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

        assert locking(hass).state == STATE_UNKNOWN


class TestOnlyTheUnsecureSideIsWithheld:
    """The asymmetry, and the car that forced it.

    Withholding both directions assumed every car passes briefly through the
    mid-use state on its way to a settled one. One of the two this was built
    on does: it reports KEY_REMOVED within minutes. The other sits in
    KEY_ON_ENGINE_OFF for fifteen hours at a stretch, so on that car every
    door, window and lock went unknown overnight — all of them shut, all of
    them correct, all of them hidden.

    A car somebody is walking away from moves towards shut, locked and armed.
    So a stale mid-use snapshot claiming a door is open is the one worth
    doubting, and the one that sends somebody back out to the drive. One
    saying it is shut is where the car was heading anyway.
    """

    SHUT = {
        **NO_TIMESTAMP,
        "VEHICLE_STATE_TYPE": "KEY_ON_ENGINE_OFF",
        "DOOR_IS_ALL_DOORS_LOCKED": "TRUE",
    }

    async def test_a_shut_car_keeps_reading_shut(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        freezer: Any,
    ) -> None:
        loaded.telemetry.push(KEPT, self.SHUT)
        await hass.async_block_till_done()

        await age(hass, entry, freezer, hours=15)

        assert (
            locking(hass).state == "off"
        ), "a locked car went unknown overnight on a reading that was right"

    async def test_an_open_one_is_still_doubted(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        freezer: Any,
    ) -> None:
        loaded.telemetry.push(KEPT, CAUGHT_MID_USE)
        await hass.async_block_till_done()

        await age(hass, entry, freezer, hours=15)

        assert locking(hass).state == STATE_UNKNOWN

    def test_the_alarm_reads_the_other_way_round(self) -> None:
        # On means armed, which is the secure side, so it is the off reading
        # that gets withheld. Asserted on the descriptions rather than through
        # an entity: the fixture's status document carries no alarm key, so
        # no alarm entity exists to poke, and this is the invariant anyway.
        from custom_components.jlr_incontrol.binary_sensor import (
            VEHICLE_BINARY_SENSORS,
        )

        backwards = {
            d.key
            for d in VEHICLE_BINARY_SENSORS
            if d.volatile and not d.insecure_when_on
        }
        assert backwards == {"alarm"}

    def test_every_other_volatile_reading_hides_the_open_side(self) -> None:
        from custom_components.jlr_incontrol.binary_sensor import (
            VEHICLE_BINARY_SENSORS,
        )

        for description in VEHICLE_BINARY_SENSORS:
            if description.volatile and description.key != "alarm":
                assert description.insecure_when_on, description.key

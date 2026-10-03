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
import doubles as doubles_module  # noqa: E402
from doubles import KEPT, STATUS, Doubles  # noqa: E402
from homeassistant.const import STATE_UNKNOWN  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol.const import (  # noqa: E402
    CONF_UNSETTLED_SINCE,
    CONF_VOLATILE_SEEN,
    VOLATILE_STATUS_KEYS,
    VOLATILE_STATUS_KEYS_1_7,
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


class TestAnUpgradeThatWatchesMoreKeys:
    """1.8.0 watches five more keys than 1.7.x, and the clock must survive it.

    The readings used to be stored as a bare list compared by position, so one
    more key made every stored list differ from every fresh one. The first
    snapshot after upgrading restarted the clock on every car, and central
    locking that had been unknown for fifteen hours read unlocked again for
    another window — on cars that report none of the new keys as well.
    """

    @staticmethod
    def installed_by_1_7(
        hass: HomeAssistant,
        entry: MockConfigEntry,
        seen: Any,
        hours_ago: int = 15,
    ) -> None:
        """Leave the entry as a 1.7.x install would, mid-use and long quiet."""
        from homeassistant.util import dt as dt_util

        since = dt_util.utcnow() - timedelta(hours=hours_ago)
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_UNSETTLED_SINCE: {KEPT: since.isoformat()},
                CONF_VOLATILE_SEEN: {KEPT: seen},
            },
        )

    @staticmethod
    def as_1_7_stored(status: dict[str, Any]) -> list[Any]:
        return [status.get(key) for key in VOLATILE_STATUS_KEYS_1_7]

    async def test_the_old_clock_is_kept(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        doubles: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.installed_by_1_7(hass, entry, self.as_1_7_stored(CAUGHT_MID_USE))
        monkeypatch.setattr(doubles_module, "STATUS", CAUGHT_MID_USE)

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert locking(hass).state == STATE_UNKNOWN, (
            "a lock reading fifteen hours stale was believed again because "
            "the upgrade watched more keys"
        )
        # And it is written back in the shape that can grow.
        entry.runtime_data._persist()
        stored = entry.data[CONF_VOLATILE_SEEN][KEPT]
        assert isinstance(stored, dict)
        assert set(stored) == set(VOLATILE_STATUS_KEYS)

    async def test_a_genuine_change_across_the_upgrade_still_counts(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        doubles: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The other half: the old list is read by key, not thrown away. It was
        # locked when 1.7 last looked and is unlocked now, which is news.
        was_locked = {**CAUGHT_MID_USE, "DOOR_IS_ALL_DOORS_LOCKED": "TRUE"}
        self.installed_by_1_7(hass, entry, self.as_1_7_stored(was_locked))
        monkeypatch.setattr(doubles_module, "STATUS", CAUGHT_MID_USE)

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert locking(hass).state == "on"

    async def test_a_key_watched_for_the_first_time_is_not_a_change(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        doubles: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A car that does report the new keys has a value for them where the
        # stored copy has none. No earlier value means nothing to have moved.
        stored = {key: CAUGHT_MID_USE.get(key) for key in VOLATILE_STATUS_KEYS_1_7}
        self.installed_by_1_7(hass, entry, stored)
        with_locks = {**CAUGHT_MID_USE, "DOOR_FRONT_LEFT_LOCK_STATUS": "UNLOCKED"}
        monkeypatch.setattr(doubles_module, "STATUS", with_locks)

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert locking(hass).state == STATE_UNKNOWN

    async def test_a_list_from_the_first_beta_is_read_too(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        doubles: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 1.8.0-beta.1 shipped watching the new keys but still storing a bare
        # list, in the current order. Read by key, a change across that
        # upgrade is still seen.
        was_locked = {**CAUGHT_MID_USE, "DOOR_IS_ALL_DOORS_LOCKED": "TRUE"}
        beta = [was_locked.get(key) for key in VOLATILE_STATUS_KEYS]
        self.installed_by_1_7(hass, entry, beta)
        monkeypatch.setattr(doubles_module, "STATUS", CAUGHT_MID_USE)

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert locking(hass).state == "on"

    @pytest.mark.parametrize("seen", [["TRUE", "FALSE"], "not a list", 7])
    async def test_a_shape_nothing_wrote_is_dropped_not_trusted(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        doubles: Doubles,
        monkeypatch: pytest.MonkeyPatch,
        seen: Any,
    ) -> None:
        # Nothing to compare against means nothing proved to have moved: the
        # clock keeps running, as it does for a car with no stored copy.
        self.installed_by_1_7(hass, entry, seen)
        monkeypatch.setattr(doubles_module, "STATUS", CAUGHT_MID_USE)

        assert await hass.config_entries.async_setup(entry.entry_id)
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

    async def test_a_door_left_unlocked_is_doubted_and_a_locked_one_is_not(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        doubles: Doubles,
        freezer: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Per-door locks (#31) travel with central locking, so they get the
        # same one-sided treatment: the unlocked reading is the one a car
        # walking away from is about to overtake.
        from doubles import FakeTelemetry
        from homeassistant.helpers import entity_registry as er

        from custom_components.jlr_incontrol.const import DOMAIN

        snapshot = {
            **CAUGHT_MID_USE,
            "DOOR_FRONT_LEFT_LOCK_STATUS": "LOCKED",
            "DOOR_FRONT_RIGHT_LOCK_STATUS": "UNLOCKED",
        }

        # In the first snapshot, not a later one: entities are built from the
        # keys a car reports, and anything not built is pruned from the
        # registry, switched on or not.
        async def start(self: FakeTelemetry) -> None:
            self.connected = True
            self.on_connected(True)
            for vin in self.vins:
                self.push(vin, snapshot)

        monkeypatch.setattr(FakeTelemetry, "async_start", start)

        registry = er.async_get(hass)
        for key in ("door_front_left_lock", "door_front_right_lock"):
            registry.async_get_or_create(
                "binary_sensor", DOMAIN, f"{KEPT}_{key}", config_entry=entry
            )
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await age(hass, entry, freezer, hours=15)

        def lock(key: str) -> str:
            entity_id = registry.async_get_entity_id(
                "binary_sensor", DOMAIN, f"{KEPT}_{key}"
            )
            return hass.states.get(entity_id).state

        assert lock("door_front_left_lock") == "off"
        assert lock("door_front_right_lock") == STATE_UNKNOWN

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


class TestCoolantAfterADrive:
    """The reading that disappeared for good.

    It was gated on the car reporting a running engine. No car does: across
    four days and several drives on two vehicles, VEHICLE_STATE_TYPE was only
    ever KEY_REMOVED or KEY_ON_ENGINE_OFF, because the telematics unit does
    not push while the engine is turning. So the sensor read unknown always,
    including twenty-five minutes after a drive with the figure sitting right
    there in the snapshot.

    Age is the honest test instead. The figure is true when it is taken and
    less true every minute after, which is also how an engine behaves.
    """

    WARM = {
        **NO_TIMESTAMP,
        "VEHICLE_STATE_TYPE": "KEY_ON_ENGINE_OFF",
        "ENGINE_COOLANT_TEMP": "89",
    }

    def coolant(self, hass: HomeAssistant) -> Any:
        (entity_id,) = [
            item
            for item in hass.states.async_entity_ids("sensor")
            if item.endswith("_engine_coolant_temperature")
        ]
        return hass.states.get(entity_id)

    async def test_it_shows_just_after_the_car_was_used(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        loaded.telemetry.push(KEPT, self.WARM)
        await hass.async_block_till_done()

        assert self.coolant(hass).state == "89.0"

    async def test_it_fades_once_the_figure_has_stood_still(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        freezer: Any,
    ) -> None:
        loaded.telemetry.push(KEPT, self.WARM)
        await hass.async_block_till_done()

        await age(hass, entry, freezer, minutes=45)

        assert self.coolant(hass).state == STATE_UNKNOWN

    async def test_a_drifting_battery_does_not_keep_it_alive(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        freezer: Any,
    ) -> None:
        # The trap the other clock fell into. A parked car's voltage moves on
        # its own, and if that counted as the car reporting, a coolant figure
        # from before dark would look current all night.
        loaded.telemetry.push(KEPT, self.WARM)
        await hass.async_block_till_done()

        for volts in ("12.5", "12.4", "12.3", "12.2"):
            freezer.tick(timedelta(minutes=12))
            loaded.telemetry.push(KEPT, {**self.WARM, "BATTERY_VOLTAGE": volts})
            await hass.async_block_till_done()

        assert self.coolant(hass).state == STATE_UNKNOWN

    async def test_a_car_driven_again_gets_its_reading_back(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        freezer: Any,
    ) -> None:
        loaded.telemetry.push(KEPT, self.WARM)
        await hass.async_block_till_done()
        await age(hass, entry, freezer, minutes=45)
        assert self.coolant(hass).state == STATE_UNKNOWN

        loaded.telemetry.push(KEPT, {**self.WARM, "ENGINE_COOLANT_TEMP": "91"})
        await hass.async_block_till_done()

        assert self.coolant(hass).state == "91.0"

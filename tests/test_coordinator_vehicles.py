"""Selling a car has to actually remove it, not just hide it.

The cars on the Data Hub link are authoritative, and the coordinator already
stopped showing a car that has left the account. What it went on doing, in an
earlier version, was everything else: keeping its nickname and registration
in the config entry, where diagnostics would report them indefinitely.

These need a real Home Assistant to import the coordinator at all, so they are
skipped where one is not installed and run in the CI lane that has one.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

pytest.importorskip("homeassistant", reason="coordinator behaviour needs a core")

from homeassistant.util import dt as dt_util  # noqa: E402
from jlr.coordinator import JlrCoordinator  # noqa: E402

KEPT = "SAJAA1234567890AB"
SOLD = "SALBB9876543210CD"


def coordinator(**state: Any) -> JlrCoordinator:
    """A coordinator with only the fields under test.

    Built without __init__ deliberately: constructing one for real needs a
    running Home Assistant, a config entry and a live API client, none of
    which say anything about whether a sold car is forgotten.
    """
    instance = JlrCoordinator.__new__(JlrCoordinator)
    defaults: dict[str, Any] = {
        "_vehicles": {},
        "_attributes": {},
        "_attributes_attempted": {},
        "_status": {},
        "_status_times": {},
        "_position": {},
        "_last_status_seen": {},
        "_last_changed": {},
        "_unsettled_since": {},
        "_volatile_seen": {},
        "_decayed_since": {},
        "_decayed_seen": {},
        "_position_read_at": {},
        "_awaiting": set(),
        "_snapshots_ready": asyncio.Event(),
    }
    for name, value in {**defaults, **state}.items():
        setattr(instance, name, value)
    return instance


def with_both_cars(**state: Any) -> JlrCoordinator:
    """One car kept, one sold, with every cache populated for both.

    Anything passed in overrides the pair rather than colliding with it, so a
    test can narrow the vehicle list to what survives a removal.
    """
    both: dict[str, Any] = {
        "_vehicles": {KEPT: {"vin": KEPT}, SOLD: {"vin": SOLD}},
        "_attributes": {KEPT: {"nickname": "Keeper"}, SOLD: {"nickname": "Sold"}},
        "_attributes_attempted": {KEPT: 1.0, SOLD: 1.0},
        "_status": {KEPT: {"ODOMETER": "1"}, SOLD: {"ODOMETER": "2"}},
        "_status_times": {KEPT: {}, SOLD: {}},
        "_position": {KEPT: {"latitude": 1}, SOLD: {"latitude": 2}},
        "_last_status_seen": {KEPT: {}, SOLD: {}},
        "_last_changed": {KEPT: "t1", SOLD: "t2"},
        "_unsettled_since": {KEPT: "t1", SOLD: "t2"},
        "_volatile_seen": {KEPT: {}, SOLD: {}},
        "_decayed_since": {KEPT: "t1", SOLD: "t2"},
        "_decayed_seen": {KEPT: {}, SOLD: {}},
        "_position_read_at": {},
    }
    return coordinator(**{**both, **state})


CACHES = (
    "_attributes",
    "_attributes_attempted",
    "_status",
    "_status_times",
    "_position",
    "_last_status_seen",
    "_last_changed",
    "_unsettled_since",
    "_volatile_seen",
    "_decayed_since",
    "_decayed_seen",
)


class TestForgetting:
    @pytest.mark.parametrize("cache", CACHES)
    def test_one_of_many_leaves_no_trace(self, cache: str) -> None:
        coord = with_both_cars()
        coord._forget({SOLD})
        assert SOLD not in getattr(coord, cache)
        assert KEPT in getattr(coord, cache), "the remaining car kept its data"

    @pytest.mark.parametrize("cache", CACHES)
    def test_the_last_vehicle_leaves_no_trace(self, cache: str) -> None:
        # Selling the only car. Nothing is left to anchor the caches, so this
        # is where a missed one keeps a whole vehicle alive forever.
        coord = with_both_cars()
        coord._forget({KEPT, SOLD})
        assert getattr(coord, cache) == {}

    def test_a_vehicle_we_were_waiting_on_stops_being_waited_on(self) -> None:
        coord = with_both_cars(_awaiting={KEPT, SOLD})
        coord._forget({SOLD})
        assert coord._awaiting == {KEPT}
        assert not coord._snapshots_ready.is_set(), "still waiting on the other"

        coord._forget({KEPT})
        assert coord._snapshots_ready.is_set(), "nothing left to wait for"


class FakeEntry:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


class FakeConfigEntries:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    def async_update_entry(self, entry: FakeEntry, data: dict[str, Any]) -> None:
        self.writes.append(data)
        entry.data = data


class FakeHass:
    def __init__(self) -> None:
        self.config_entries = FakeConfigEntries()


def persisting(stored: dict[str, Any], attributes: dict[str, Any]) -> JlrCoordinator:
    coord = coordinator(_attributes=attributes)
    coord.hass = FakeHass()
    coord.entry = FakeEntry({"attributes": stored})
    return coord


class TestPersistingAttributes:
    """The config entry is where a sold car's details would otherwise outlive it."""

    def test_emptying_the_last_vehicle_reaches_the_entry(self) -> None:
        coord = persisting({SOLD: {"nickname": "Sold"}}, {})
        coord._persist()
        assert coord.entry.data["attributes"] == {}

    def test_removing_one_of_two_reaches_the_entry(self) -> None:
        coord = persisting(
            {KEPT: {"nickname": "Keeper"}, SOLD: {"nickname": "Sold"}},
            {KEPT: {"nickname": "Keeper"}},
        )
        coord._persist()
        assert coord.entry.data["attributes"] == {KEPT: {"nickname": "Keeper"}}

    def test_an_unchanged_cache_writes_nothing(self) -> None:
        # Every housekeeping calls this. A write it does not need fires the
        # entry update listeners, and that once meant a reload every few
        # minutes.
        coord = persisting(
            {KEPT: {"nickname": "Keeper"}}, {KEPT: {"nickname": "Keeper"}}
        )
        coord._persist()
        assert coord.hass.config_entries.writes == []

    def test_a_new_entry_with_nothing_to_say_writes_nothing(self) -> None:
        coord = coordinator(_attributes={})
        coord.hass = FakeHass()
        coord.entry = FakeEntry({})
        coord._persist()
        assert coord.hass.config_entries.writes == []


class TestFreshness:
    """Which timestamp wins, and when a reading stops being worth trusting."""

    def test_the_newest_of_several_wins(self) -> None:
        assert (
            JlrCoordinator._newest(
                "2026-08-26T07:00:00Z",
                "2026-08-26T09:00:00Z",
                "2026-08-26T08:00:00Z",
            )
            == "2026-08-26T09:00:00Z"
        )

    def test_empty_values_are_skipped(self) -> None:
        assert JlrCoordinator._newest(None, "", "2026-08-26T07:00:00Z") == (
            "2026-08-26T07:00:00Z"
        )

    def test_nothing_at_all_is_nothing(self) -> None:
        assert JlrCoordinator._newest(None, "") is None

    def test_an_unparseable_stamp_is_better_than_none(self) -> None:
        # It still tells a reader something; discarding it would show a
        # vehicle as never having reported.
        assert JlrCoordinator._newest("not a date") == "not a date"

    def test_a_parseable_stamp_beats_an_unparseable_one(self) -> None:
        assert JlrCoordinator._newest("not a date", "2026-08-26T07:00:00Z") == (
            "2026-08-26T07:00:00Z"
        )

    def test_a_naive_stamp_is_assumed_utc(self) -> None:
        # JLR are not consistent about the suffix, and comparing naive to
        # aware raises.
        assert JlrCoordinator._newest("2026-08-26T07:00:00") is not None

    def test_a_recent_reading_is_not_stale(self) -> None:
        recent = (dt_util.utcnow() - timedelta(hours=1)).isoformat()
        assert not JlrCoordinator._is_stale(recent)

    def test_a_day_old_reading_is(self) -> None:
        old = (dt_util.utcnow() - timedelta(days=2)).isoformat()
        assert JlrCoordinator._is_stale(old)

    @pytest.mark.parametrize("value", [None, "", "not a date"])
    def test_an_unreadable_stamp_is_not_called_stale(self, value) -> None:
        # Reporting "stale" for something we cannot date is a claim we cannot
        # support; unknown age is not the same as old.
        assert not JlrCoordinator._is_stale(value)


class TestChargeNowSetting:
    def coordinator_with(self, status: dict[str, Any]) -> JlrCoordinator:
        made = coordinator()
        made.data = {"vehicles": {KEPT: {"status": status}}}
        return made

    def test_it_is_upper_cased_for_the_caller(self) -> None:
        assert (
            self.coordinator_with(
                {"EV_CHARGE_NOW_SETTING": "force_on"}
            ).charge_now_setting(KEPT)
            == "FORCE_ON"
        )

    def test_a_car_that_has_not_said_reports_nothing(self) -> None:
        assert self.coordinator_with({}).charge_now_setting(KEPT) is None

    def test_a_vehicle_we_do_not_know_reports_nothing(self) -> None:
        assert self.coordinator_with({}).charge_now_setting("nobody") is None


class TestPushedData:
    def quiet(self) -> JlrCoordinator:
        coord = with_both_cars(_last_status_seen={}, _last_changed={})
        # Both stubbed for the same reason: this class is about what counts
        # as a change, and neither publishing to entities nor writing the
        # config entry can happen without a running Home Assistant. That the
        # write is made at all is covered against a real one, in
        # tests/ha/test_persistence.py.
        coord._push = lambda: None
        coord._persist = lambda: None
        return coord

    def test_a_status_without_a_report_time_still_lands(self) -> None:
        coord = self.quiet()
        coord._handle_status(KEPT, {"ODOMETER": "999"})
        assert coord._status[KEPT] == {"ODOMETER": "999"}

    def test_a_position_push_is_adopted(self) -> None:
        coord = self.quiet()
        coord._handle_position(KEPT, {"latitude": 51.5, "longitude": -0.1})
        assert coord._position[KEPT]["latitude"] == 51.5

    def test_an_unchanged_snapshot_is_not_called_a_change(self) -> None:
        # The socket redelivers a full snapshot on every reconnect, which is
        # every few minutes. Treating each as a change would make "last
        # changed" mean "last reconnected".
        coord = self.quiet()
        coord._handle_status(KEPT, {"ODOMETER": "1"})
        first = coord._last_changed.get(KEPT)
        coord._handle_status(KEPT, {"ODOMETER": "1"})
        assert coord._last_changed.get(KEPT) == first

    def test_a_real_change_is(self) -> None:
        coord = self.quiet()
        coord._handle_status(KEPT, {"ODOMETER": "1"})
        coord._handle_status(KEPT, {"ODOMETER": "2"})
        assert coord._last_changed.get(KEPT)

    def test_a_position_push_is_not_a_status_change(self) -> None:
        # The one that mattered on a real car. A parked Range Rover reports
        # nothing from its body controller for hours, but keeps producing GPS
        # fixes; counting those as the status changing renewed the freshness
        # of a door reading from the night before, so an open window looked
        # current all day.
        coord = self.quiet()
        coord._handle_status(KEPT, {"DOOR_IS_ALL_DOORS_LOCKED": "FALSE"})
        coord._handle_status(KEPT, {"DOOR_IS_ALL_DOORS_LOCKED": "FALSE"})
        before = coord._last_changed.get(KEPT)

        coord._handle_position(KEPT, {"latitude": 52.0, "longitude": -1.0})
        coord._handle_position(KEPT, {"latitude": 52.1, "longitude": -1.1})

        assert (
            coord._last_changed.get(KEPT) == before
        ), "a new fix is not the car reporting its state"

    def test_a_status_change_still_registers_while_the_car_is_moving(self) -> None:
        # The other half: separating the two must not make a real change
        # invisible just because a fix arrived alongside it.
        coord = self.quiet()
        coord._handle_status(KEPT, {"ODOMETER": "1"})
        coord._handle_position(KEPT, {"latitude": 52.0})
        coord._handle_status(KEPT, {"ODOMETER": "2"})
        assert coord._last_changed.get(KEPT)


class TestPositionTrustIsPerVehicle:
    """One car being found does not vouch for another.

    The freshness stamp was account-wide, so a car with no portal id — or one
    the portal had nothing to say about — had its old coordinates re-trusted
    on the strength of a different car being located. A location nobody can
    vouch for still resolves to a zone, which is how a tracker ends up
    confidently reporting "home" for a car that is miles away.
    """

    def test_a_car_that_was_not_located_is_not_trusted(self) -> None:
        coord = coordinator(
            _vehicles={KEPT: {}, SOLD: {}},
            _position_read_at={KEPT: dt_util.utcnow()},
        )
        assert coord._position_trusted(KEPT) is True
        assert coord._position_trusted(SOLD) is False

    def test_an_old_fix_stops_being_trusted(self) -> None:
        coord = coordinator(
            _vehicles={KEPT: {}},
            _position_read_at={KEPT: dt_util.utcnow() - timedelta(days=1)},
        )
        assert coord._position_trusted(KEPT) is False

    def test_a_car_never_located_is_not_trusted(self) -> None:
        coord = coordinator(_vehicles={KEPT: {}}, _position_read_at={})
        assert coord._position_trusted(KEPT) is False


class TestVehiclesSoldWhileTheLightsWereOff:
    """A car removed while Home Assistant was stopped.

    Removal was detected by comparing the account's listing against the
    vehicles seen this run, and that starts empty — so the first listing after
    a restart had nothing to find a sold car missing from. Its nickname,
    registration and freshness caches were restored from the config entry and
    then stayed there for good.
    """

    def test_restored_caches_count_as_known(self) -> None:
        coord = coordinator(
            _vehicles={},
            _attributes={SOLD: {"nickname": "Gone"}},
            _last_changed={SOLD: "t"},
        )
        assert SOLD in coord._known_vins()


class TestTheBrandFromTheVin:
    """The one thing a VIN says for certain, as a floor under the details."""

    def test_a_jaguar(self) -> None:
        from jlr.coordinator import brand_from_vin

        assert brand_from_vin(KEPT) == {"vehicleBrand": "Jaguar"}

    def test_a_land_rover(self) -> None:
        from jlr.coordinator import brand_from_vin

        assert brand_from_vin(SOLD) == {"vehicleBrand": "Land Rover"}

    def test_anything_else_says_nothing(self) -> None:
        from jlr.coordinator import brand_from_vin

        assert brand_from_vin("WVWZZZ1JZXW000001") == {}

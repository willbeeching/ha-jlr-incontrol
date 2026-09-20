"""Selling a car has to actually remove it, not just hide it.

The vehicle list from JLR is authoritative, and the coordinator already
stopped showing a car that has left the account. What it went on doing was
everything else: asking the owner portal for that car's location on every
cycle, and keeping its nickname and registration in the config entry, where
diagnostics would report them indefinitely.

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


class FakePortal:
    """Records which vehicles it was asked about."""

    configured = True
    session_age = "0:01:00"

    def __init__(self, listing: dict[str, dict[str, Any]] | None = None) -> None:
        self._listing = listing or {}
        self.asked: list[str] = []

    async def async_get_vehicles(self) -> dict[str, dict[str, Any]]:
        # Copies: the real caller pops "portal_id" out of each record.
        return {vin: dict(record) for vin, record in self._listing.items()}

    async def async_get_position(self, portal_id: str) -> dict[str, Any]:
        self.asked.append(portal_id)
        return {"latitude": 51.5, "longitude": -0.1}


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
        "_pushed_at": {},
        "_position": {},
        "_portal_ids": {},
        "_last_status_seen": {},
        "_last_changed": {},
        "_unsettled_since": {},
        "_volatile_seen": {},
        "_snapshot_seen": {},
        "_decayed_since": {},
        "_decayed_seen": {},
        "_awaiting": set(),
        "_snapshots_ready": asyncio.Event(),
        "_portal_signed_out": None,
        "_portal_due": None,
        "_portal_vehicles_due": None,
        "_portal_read_at": None,
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
        "_pushed_at": {KEPT: "t1", SOLD: "t2"},
        "_position": {KEPT: {"latitude": 1}, SOLD: {"latitude": 2}},
        "_portal_ids": {KEPT: "id-kept", SOLD: "id-sold"},
        "_last_status_seen": {KEPT: {}, SOLD: {}},
        "_last_changed": {KEPT: "t1", SOLD: "t2"},
        "_unsettled_since": {KEPT: "t1", SOLD: "t2"},
        "_volatile_seen": {KEPT: (), SOLD: ()},
        "_snapshot_seen": {KEPT: asyncio.Event(), SOLD: asyncio.Event()},
        "_decayed_since": {KEPT: "t1", SOLD: "t2"},
        "_decayed_seen": {KEPT: (), SOLD: ()},
    }
    return coordinator(**{**both, **state})


CACHES = (
    "_attributes",
    "_attributes_attempted",
    "_status",
    "_pushed_at",
    "_position",
    "_portal_ids",
    "_last_status_seen",
    "_last_changed",
    "_unsettled_since",
    "_volatile_seen",
    "_snapshot_seen",
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


class TestPositionRequests:
    """A removed car must stop being asked after by name."""

    async def test_only_current_vehicles_are_located(self) -> None:
        portal = FakePortal()
        coord = with_both_cars(
            _vehicles={KEPT: {"vin": KEPT}},
            _portal_vehicles_due=dt_util.utcnow() + timedelta(hours=1),
        )
        coord.portal = portal
        coord._async_clear_signed_out_issue = lambda: None

        await coord._async_read_portal()

        assert coord._portal_read_at is not None, "the read reached its end"
        assert portal.asked == ["id-kept"]
        assert "id-sold" not in portal.asked

    async def test_a_vehicle_with_no_portal_id_is_skipped(self) -> None:
        portal = FakePortal()
        coord = with_both_cars(
            _vehicles={KEPT: {"vin": KEPT}, SOLD: {"vin": SOLD}},
            _portal_ids={SOLD: "id-sold"},
            _portal_vehicles_due=dt_util.utcnow() + timedelta(hours=1),
        )
        coord.portal = portal
        coord._async_clear_signed_out_issue = lambda: None

        await coord._async_read_portal()

        assert portal.asked == ["id-sold"]


class TestPortalListing:
    """The listing replaces the id cache; it does not accumulate into it."""

    async def test_a_vehicle_absent_from_the_listing_loses_its_id(self) -> None:
        coord = with_both_cars()
        coord.portal = FakePortal({KEPT: {"portal_id": "id-kept-new"}})

        await coord._async_read_portal_vehicles(dt_util.utcnow())

        assert coord._portal_ids == {KEPT: "id-kept-new"}

    async def test_an_empty_garage_clears_every_id(self) -> None:
        # An unreadable reply raises rather than arriving here empty, so this
        # really does mean the account has no vehicles.
        coord = with_both_cars()
        coord.portal = FakePortal({})

        await coord._async_read_portal_vehicles(dt_util.utcnow())

        assert coord._portal_ids == {}

    async def test_an_empty_listing_still_sets_the_next_due_time(self) -> None:
        coord = with_both_cars()
        coord.portal = FakePortal({})

        await coord._async_read_portal_vehicles(dt_util.utcnow())

        assert coord._portal_vehicles_due is not None

    async def test_a_listed_vehicle_keeps_its_id_when_the_record_omits_one(
        self,
    ) -> None:
        # The setup link is absent while a car is mid-enrolment. Losing the id
        # there would cost it its location for no reason at all.
        coord = with_both_cars()
        coord.portal = FakePortal({KEPT: {"nickname": "Keeper"}})

        await coord._async_read_portal_vehicles(dt_util.utcnow())

        assert coord._portal_ids == {KEPT: "id-kept"}


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


class FakeClient:
    user_id = None
    refresh_token = None


def persisting(stored: dict[str, Any], attributes: dict[str, Any]) -> JlrCoordinator:
    coord = coordinator(_attributes=attributes)
    coord.hass = FakeHass()
    coord.entry = FakeEntry({"attributes": stored})
    coord.client = FakeClient()
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
        # Every token rotation calls this. A write it does not need fires the
        # entry update listener, and that used to mean a reload every few
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
        coord.client = FakeClient()
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


class TestNamingACarWithoutAskingJlr:
    """The attributes endpoint is behind the attestation wall much of the time."""

    def test_the_vin_prefix_names_the_brand(self) -> None:
        coord = coordinator()
        coord._seed_identity(KEPT, {"vin": KEPT})
        assert coord._attributes[KEPT]["vehicleBrand"]

    def test_a_name_already_known_is_not_overwritten(self) -> None:
        # A real attributes document beats a guess, and the guess must not
        # undo it on the next poll.
        coord = coordinator(_attributes={KEPT: {"vehicleBrand": "Jaguar"}})
        coord._seed_identity(KEPT, {"vin": KEPT})
        assert coord._attributes[KEPT]["vehicleBrand"] == "Jaguar"

    def test_a_vehicle_record_with_nothing_in_it_adds_nothing(self) -> None:
        coord = coordinator()
        coord._seed_identity("", {})
        assert coord._attributes == {}


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

    def test_a_status_push_is_adopted(self) -> None:
        coord = self.quiet()
        coord._handle_status(KEPT, {"ODOMETER": "999"}, "2026-08-26T08:00:00Z")
        assert coord._status[KEPT] == {"ODOMETER": "999"}
        assert coord._pushed_at[KEPT] == "2026-08-26T08:00:00Z"

    def test_a_push_without_a_send_time_still_lands(self) -> None:
        coord = self.quiet()
        coord._handle_status(KEPT, {"ODOMETER": "999"}, None)
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
        coord._handle_status(KEPT, {"ODOMETER": "1"}, None)
        first = coord._last_changed.get(KEPT)
        coord._handle_status(KEPT, {"ODOMETER": "1"}, None)
        assert coord._last_changed.get(KEPT) == first

    def test_a_real_change_is(self) -> None:
        coord = self.quiet()
        coord._handle_status(KEPT, {"ODOMETER": "1"}, None)
        coord._handle_status(KEPT, {"ODOMETER": "2"}, None)
        assert coord._last_changed.get(KEPT)

    def test_a_position_push_is_not_a_status_change(self) -> None:
        # The one that mattered on a real car. A parked Range Rover reports
        # nothing from its body controller for hours, but keeps producing GPS
        # fixes; counting those as the status changing renewed the freshness
        # of a door reading from the night before, so an open window looked
        # current all day.
        coord = self.quiet()
        coord._handle_status(KEPT, {"DOOR_IS_ALL_DOORS_LOCKED": "FALSE"}, None)
        coord._handle_status(KEPT, {"DOOR_IS_ALL_DOORS_LOCKED": "FALSE"}, None)
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
        coord._handle_status(KEPT, {"ODOMETER": "1"}, None)
        coord._handle_position(KEPT, {"latitude": 52.0})
        coord._handle_status(KEPT, {"ODOMETER": "2"}, None)
        assert coord._last_changed.get(KEPT)


class TestASnapshotThatArrivesLate:
    """The only ordering key these cars give us.

    They send no timestamp of their own, and the envelope's is when the broker
    sent the message rather than when the car recorded it — measured to the
    millisecond against a payload three hours old. An odometer is different:
    it never counts down, so a lower one is proof the snapshot predates what
    is already held. JLR deliver late and out of order, and a week-old
    snapshot has overwritten a current one before now.
    """

    def test_a_lower_odometer_is_not_adopted(self) -> None:
        coord = coordinator(_status={KEPT: {"ODOMETER_MILES": "18546"}})
        coord._push = lambda: None
        coord._persist = lambda: None

        coord._handle_status(KEPT, {"ODOMETER_MILES": "18540"}, None)

        assert coord._status[KEPT]["ODOMETER_MILES"] == "18546"

    def test_a_higher_one_is(self) -> None:
        coord = coordinator(_status={KEPT: {"ODOMETER_MILES": "18546"}})
        coord._push = lambda: None
        coord._persist = lambda: None

        coord._handle_status(KEPT, {"ODOMETER_MILES": "18549"}, None)

        assert coord._status[KEPT]["ODOMETER_MILES"] == "18549"

    def test_an_equal_one_is_too(self) -> None:
        # The common case by a long way: a parked car redelivering the same
        # snapshot every four minutes. Rejecting those would freeze every
        # other field in them.
        coord = coordinator(_status={KEPT: {"ODOMETER_MILES": "18546", "A": "1"}})
        coord._push = lambda: None
        coord._persist = lambda: None

        coord._handle_status(KEPT, {"ODOMETER_MILES": "18546", "A": "2"}, None)

        assert coord._status[KEPT]["A"] == "2"

    @pytest.mark.parametrize("held,arriving", [(None, "1"), ("1", None), ("x", "1")])
    def test_an_unreadable_odometer_never_rejects(
        self, held: Any, arriving: Any
    ) -> None:
        # A car that does not report one, or reports something that is not a
        # number, must not have every snapshot thrown away.
        coord = coordinator(_status={KEPT: {"ODOMETER_MILES": held} if held else {}})
        coord._push = lambda: None
        coord._persist = lambda: None

        coord._handle_status(KEPT, {"ODOMETER_MILES": arriving, "SEEN": "yes"}, None)

        assert coord._status[KEPT]["SEEN"] == "yes"


class TestForcingAPortalRead:
    """What the Refresh button clears, and the floor under it.

    The portal is read on a half-hourly cadence, and the button used to share
    that gate — so for twenty-nine minutes in every thirty a press did nothing
    at all, silently. Clearing the gate is what makes the button mean
    something; the floor is what stops it meaning too much.
    """

    def test_it_clears_the_gate_when_the_last_read_is_old(self) -> None:
        coord = coordinator(
            _portal_due=dt_util.utcnow() + timedelta(minutes=20),
            _portal_read_at=dt_util.utcnow() - timedelta(minutes=10),
        )
        assert coord.async_force_portal_read() is True
        assert coord._portal_due is None, "the next read is still gated"

    def test_it_refuses_inside_the_floor(self) -> None:
        # Somebody leaning on the button is not a reason to hammer somebody
        # else's server.
        due = dt_util.utcnow() + timedelta(minutes=20)
        coord = coordinator(
            _portal_due=due,
            _portal_read_at=dt_util.utcnow() - timedelta(seconds=5),
        )
        assert coord.async_force_portal_read() is False
        assert coord._portal_due == due, "the gate was cleared anyway"

    def test_a_first_press_is_never_refused(self) -> None:
        # Nothing has been read yet, so there is nothing to be too soon after.
        coord = coordinator(_portal_due=None, _portal_read_at=None)
        assert coord.async_force_portal_read() is True


class TestAttributesBehindTheWall:
    async def test_a_refusal_leaves_what_we_already_had(self) -> None:
        # Losing the name because JLR blocked one request would rename every
        # car on the dashboard until the wall lifted.
        from jlr.api import JlrApiError

        coord = coordinator(_attributes={KEPT: {"nickname": "Keeper"}})

        async def refuses(vin: str) -> dict[str, Any]:
            raise JlrApiError("returned 498")

        coord.client = type("C", (), {"async_get_attributes": staticmethod(refuses)})()
        await coord._async_refresh_attributes(KEPT)
        assert coord._attributes[KEPT]["nickname"] == "Keeper"

    async def test_a_recent_attempt_is_not_repeated(self) -> None:
        # The clock here is wall time, not monotonic: the retry window is
        # hours, and it is compared against dt_util.utcnow().
        asked = 0

        async def counts(vin: str) -> dict[str, Any]:
            nonlocal asked
            asked += 1
            return {}

        coord = coordinator(
            _attributes={KEPT: {"nickname": "Keeper"}},
            _attributes_attempted={KEPT: dt_util.utcnow()},
        )
        coord.client = type("C", (), {"async_get_attributes": staticmethod(counts)})()
        await coord._async_refresh_attributes(KEPT)
        assert asked == 0

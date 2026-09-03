"""The owner portal's session, which only a person can renew.

Location and the real vehicle names come from a legacy servlet app riding a
ForgeRock session. Nothing headless can mint another one, so every failure
here is either recoverable by retrying or costs the user an emailed code —
and telling those two apart correctly is the whole job.
"""

from __future__ import annotations

import json
from typing import Any

import aiohttp
import pytest
from jlr.portal import (
    JlrPortal,
    JlrPortalAuthError,
    JlrPortalError,
    _iso,
    _parked,
)

LOGIN_PAGE = '<html><form><input name="j_username"></form></html>'
DASHBOARD = '{"vehicles": []}'


class Reply:
    """One scripted portal response, or an exception to raise instead."""

    def __init__(self, status: int = 200, body: str = "", url: str = "") -> None:
        self.status, self._body, self.url = status, body, url or "https://portal/x"

    async def __aenter__(self) -> Reply:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def text(self) -> str:
        return self._body


class Session:
    def __init__(self, *replies: Reply | Exception) -> None:
        self._replies = list(replies)
        self.requested: list[str] = []
        self.closed = False

    def get(self, url: str, **kwargs: object):
        self.requested.append(url)
        reply = self._replies.pop(0) if self._replies else Reply()
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def close(self) -> None:
        self.closed = True


def portal(*replies: Reply | Exception) -> JlrPortal:
    """A portal already holding a session, so no sign-in is attempted."""
    made = JlrPortal.__new__(JlrPortal)
    made._session = Session(*replies)
    made._base = "https://portal"
    made.ensured = 0

    async def ensure() -> str:
        made.ensured += 1
        if made._base is None:
            made._base = "https://portal"
        return made._base

    made._async_ensure_session = ensure
    return made


class TestASessionThatStillWorks:
    async def test_a_page_is_returned(self) -> None:
        client = portal(Reply(200, DASHBOARD))
        landed, body = await client._async_get("/keepalive")
        assert body == DASHBOARD
        assert client._session.requested == ["https://portal/keepalive"]

    async def test_json_is_never_mistaken_for_a_login_page(self) -> None:
        # A body starting with { is a real answer whatever else it contains.
        client = portal(Reply(200, '{"sign in to your account": false}'))
        _, body = await client._async_get("/keepalive")
        assert body.startswith("{")


class TestASessionThatHasLapsed:
    async def test_a_bounce_to_the_login_page_is_retried_once(self) -> None:
        # A single bounce is often just a stale affinity cookie, and rebuilding
        # the session fixes it without troubling the user.
        client = portal(Reply(200, LOGIN_PAGE), Reply(200, DASHBOARD))
        _, body = await client._async_get("/keepalive")
        assert body == DASHBOARD
        assert client.ensured == 2

    async def test_a_second_bounce_is_reported_as_expired(self) -> None:
        client = portal(Reply(200, LOGIN_PAGE), Reply(200, LOGIN_PAGE))
        with pytest.raises(JlrPortalAuthError):
            await client._async_get("/keepalive")

    async def test_a_401_is_treated_the_same_way(self) -> None:
        client = portal(Reply(401, ""), Reply(401, ""))
        with pytest.raises(JlrPortalAuthError):
            await client._async_get("/keepalive")

    @pytest.mark.parametrize(
        "landed",
        [
            "https://portal/select-locale",
            "https://identity.jaguarlandrover.com/am/XUI",
            "https://portal/login",
        ],
    )
    async def test_where_it_landed_gives_it_away(self, landed: str) -> None:
        client = portal(Reply(200, DASHBOARD, landed), Reply(200, DASHBOARD, landed))
        with pytest.raises(JlrPortalAuthError):
            await client._async_get("/keepalive")


class TestAPortalThatIsJustSlow:
    """A timeout is not an expired session, and must not be reported as one."""

    async def test_one_timeout_is_retried(self) -> None:
        client = portal(TimeoutError(), Reply(200, DASHBOARD))
        _, body = await client._async_get("/keepalive")
        assert body == DASHBOARD

    async def test_two_timeouts_are_a_portal_problem_not_a_sign_in_one(self) -> None:
        client = portal(TimeoutError(), TimeoutError())
        with pytest.raises(JlrPortalError) as raised:
            await client._async_get("/keepalive")
        # The distinction that matters: this must not send anyone to their
        # inbox for a code that would not have helped.
        assert not isinstance(raised.value, JlrPortalAuthError)

    async def test_a_network_failure_is_not_retried(self) -> None:
        # Nothing about a reset connection improves on a second go.
        client = portal(aiohttp.ClientError("connection reset"))
        with pytest.raises(JlrPortalError) as raised:
            await client._async_get("/keepalive")
        assert not isinstance(raised.value, JlrPortalAuthError)


class TestReadingVehicles:
    async def test_an_unreadable_reply_raises_rather_than_looking_empty(self) -> None:
        # The coordinator treats an empty listing as authoritative and forgets
        # cars, so these two must never look the same.
        client = portal(Reply(200, "<html>not json</html>"))
        with pytest.raises(JlrPortalError, match="vehicle JSON"):
            await client.async_get_vehicles()


class TestReadingPosition:
    async def test_a_car_with_no_journeys_reports_nothing(self) -> None:
        # A real state, not a failure. Reporting a coordinate would be worse.
        client = portal(Reply(200, "<html>no trail here</html>"))
        assert await client.async_get_position("id-1") == {}

    async def test_the_parked_fix_is_read_off_the_dashboard(self) -> None:
        trail = [{"position": {"latitude": 51.5074, "longitude": -0.1278}}]
        body = f"<script>let waypoints = {json.dumps(trail)};</script>"
        client = portal(Reply(200, body))
        assert (await client.async_get_position("id-1"))["latitude"] == 51.5074

    async def test_an_unreadable_trail_is_an_error(self) -> None:
        body = "let waypoints = [not json];"
        client = portal(Reply(200, body))
        with pytest.raises(JlrPortalError, match="journey trail"):
            await client.async_get_position("id-1")


class TestFindingWhereItParked:
    def test_the_last_point_with_coordinates_wins(self) -> None:
        assert (
            _parked(
                [
                    {"position": {"latitude": 1.0, "longitude": 2.0}},
                    {"position": {"latitude": 51.5074, "longitude": -0.1278}},
                ]
            )["latitude"]
            == 51.5074
        )

    def test_coordinates_and_timestamp_are_taken_independently(self) -> None:
        # The trail's final entries can carry a position with a null time, and
        # the reverse. Insisting one point supplies both loses the fix.
        parked = _parked(
            [
                {
                    "position": {"latitude": 51.5074, "longitude": -0.1278},
                    "timestamp": None,
                },
                {"position": {}, "timestamp": 1756900000000},
            ]
        )
        assert parked["latitude"] == 51.5074
        assert parked["timestamp"] is not None

    def test_a_trail_with_no_coordinates_reports_nothing(self) -> None:
        assert _parked([{"timestamp": 1756900000000}]) == {}

    def test_junk_in_the_trail_is_stepped_over(self) -> None:
        assert (
            _parked(
                ["not a point", None, {"position": {"latitude": 1.0, "longitude": 2.0}}]
            )["longitude"]
            == 2.0
        )

    def test_an_empty_trail_reports_nothing(self) -> None:
        assert _parked([]) == {}


class TestTimestamps:
    def test_epoch_milliseconds_become_iso(self) -> None:
        assert _iso(1756900000000).endswith("Z")

    @pytest.mark.parametrize("value", [None, "", "not a number", [], {}])
    def test_anything_else_is_not_guessed_at(self, value: object) -> None:
        assert _iso(value) is None

    def test_a_value_beyond_the_calendar_is_not_a_crash(self) -> None:
        assert _iso(10**18) is None


class TestJsonIsNotHtml:
    def test_a_json_list_is_a_real_answer(self) -> None:
        client = portal(Reply(200, json.dumps([1, 2, 3])))
        assert client


LAND_ROVER, JAGUAR = (
    "https://incontrol.landrover.com/jlr-portal-owner-web",
    "https://incontrol.jaguar.com/jaguar-portal-owner-web",
)
GARAGE = json.dumps({"vehicles": [{"fullVin": "SAJAA1234567890AB"}]})
EMPTY_GARAGE = json.dumps({"vehicles": []})
DASHBOARD_LANDING = "https://incontrol.jaguar.com/dashboard"


class Cookie:
    """Enough of an aiohttp cookie for the harvester to read."""

    def __init__(self, key: str, value: str, domain: str) -> None:
        self.key, self.value, self._domain = key, value, domain

    def __getitem__(self, name: str) -> str:
        return self._domain if name == "domain" else ""


class Routed:
    """A session that answers by URL fragment, in order."""

    def __init__(self, *routes: tuple[str, Any], cookies: list[Cookie] | None = None):
        self._routes = [(fragment, list(replies)) for fragment, replies in routes]
        self.cookie_jar = cookies or []
        self.requested: list[str] = []
        self.closed = False

    def get(self, url: str, **kwargs: object) -> Any:
        self.requested.append(url)
        for fragment, replies in self._routes:
            if fragment in url and replies:
                reply = replies.pop(0)
                if isinstance(reply, Exception):
                    raise reply
                return reply
        return Reply(200, "")

    def detach(self) -> None:
        # What release looks like now: the connector is Home Assistant's, so
        # the session lets go of it rather than closing it.
        self.closed = True

    async def close(self) -> None:
        raise AssertionError("close() would take Home Assistant's connector down")


def bare(**state: Any) -> JlrPortal:
    made = JlrPortal.__new__(JlrPortal)
    made._hass = None
    made._cookies = {"SSOSession": "an-am-session"}
    made._portal_cookies = {}
    made._portal_base = None
    made._minted = None
    made._on_portal_session = None
    made._session = None
    made._base = None
    for name, value in state.items():
        setattr(made, name, value)
    return made


class TestWhetherThereIsAnythingToTry:
    def test_a_stored_identity_session_counts(self) -> None:
        assert bare().configured

    def test_an_entry_from_before_the_portal_does_not(self) -> None:
        # Nothing can be conjured from a refresh token, so this is a repair
        # and a single log line rather than a retry loop.
        assert not bare(_cookies={}).configured


class TestResumingTheRememberedSession:
    async def test_a_live_session_with_cars_is_reused(self) -> None:
        # Every avoided sign-in is an emailed code the user does not have to
        # go and find.
        portal = bare(_session=Routed(("pollvehiclestatus", [Reply(200, GARAGE)])))
        assert await portal._async_can_resume(JAGUAR)

    async def test_a_bounce_to_the_login_page_means_it_has_lapsed(self) -> None:
        portal = bare(_session=Routed(("pollvehiclestatus", [Reply(200, LOGIN_PAGE)])))
        assert not await portal._async_can_resume(JAGUAR)

    async def test_a_live_session_on_the_wrong_brand_is_rejected(self) -> None:
        # A session saved before the brand check existed can be a perfectly
        # live Land Rover session belonging to someone who owns a Jaguar.
        # Resuming it lands back in the empty garage the check was added for.
        portal = bare(
            _session=Routed(("pollvehiclestatus", [Reply(200, EMPTY_GARAGE)]))
        )
        assert not await portal._async_can_resume(LAND_ROVER)

    async def test_a_body_we_cannot_read_gets_the_benefit_of_the_doubt(self) -> None:
        # Minting a new session spends the identity session, which only the
        # user can replace. Not worth spending on a parse failure.
        portal = bare(_session=Routed(("pollvehiclestatus", [Reply(200, "<html>")])))
        assert await portal._async_can_resume(JAGUAR)

    async def test_a_failed_probe_is_not_a_resume(self) -> None:
        portal = bare(_session=Routed(("pollvehiclestatus", [TimeoutError()])))
        assert not await portal._async_can_resume(JAGUAR)


class TestSigningIn:
    def routes(self, landed: str, body: str = "") -> Routed:
        return Routed(
            ("select-locale", [Reply(200, ""), Reply(200, "")]),
            ("forgerock/redirect", [Reply(200, body, landed)]),
        )

    async def test_landing_on_the_dashboard_is_success(self) -> None:
        portal = bare(_session=self.routes(DASHBOARD_LANDING))
        assert await portal._async_login(JAGUAR)

    async def test_a_page_offering_logout_is_success_too(self) -> None:
        portal = bare(_session=self.routes("https://portal/home", "<a>Logout</a>"))
        assert await portal._async_login(JAGUAR)

    async def test_landing_anywhere_else_is_failure(self) -> None:
        portal = bare(_session=self.routes("https://portal/select-locale"))
        assert not await portal._async_login(JAGUAR)

    async def test_the_locale_gate_is_visited_first(self) -> None:
        # Every other path bounces back to it until it is set.
        session = self.routes(DASHBOARD_LANDING)
        await bare(_session=session)._async_login(JAGUAR)
        assert "select-locale" in session.requested[0]

    async def test_a_timeout_is_reported_as_a_portal_problem(self) -> None:
        portal = bare(_session=Routed(("select-locale", [TimeoutError()])))
        with pytest.raises(JlrPortalError, match="timed out"):
            await portal._async_login(JAGUAR)

    async def test_an_unreachable_portal_says_so(self) -> None:
        portal = bare(
            _session=Routed(("select-locale", [aiohttp.ClientError("no route")]))
        )
        with pytest.raises(JlrPortalError, match="could not reach"):
            await portal._async_login(JAGUAR)


class TestWhetherThisBrandHasTheCars:
    async def test_a_garage_with_a_car_counts(self) -> None:
        portal = bare(_session=Routed(("pollvehiclestatus", [Reply(200, GARAGE)])))
        assert await portal._async_has_vehicles(JAGUAR)

    async def test_an_empty_garage_does_not(self) -> None:
        portal = bare(
            _session=Routed(("pollvehiclestatus", [Reply(200, EMPTY_GARAGE)]))
        )
        assert not await portal._async_has_vehicles(JAGUAR)

    @pytest.mark.parametrize(
        "reply", [Reply(200, "<html>"), TimeoutError(), aiohttp.ClientError("x")]
    )
    async def test_anything_unreadable_does_not(self, reply: Any) -> None:
        portal = bare(_session=Routed(("pollvehiclestatus", [reply])))
        assert not await portal._async_has_vehicles(JAGUAR)


class TestChoosingAPortal:
    """The Jaguar bug: both brands share one identity, so signing in succeeds
    whichever you try. Only the garage says which one has the cars."""

    def both_brands(self, land_rover: str, jaguar: str) -> Routed:
        return Routed(
            ("select-locale", [Reply(200, "")] * 8),
            ("forgerock/redirect", [Reply(200, "", DASHBOARD_LANDING)] * 4),
            (
                "landrover.com/jlr-portal-owner-web/ajax",
                [Reply(200, land_rover)] * 2,
            ),
            ("jaguar.com/jaguar-portal-owner-web/ajax", [Reply(200, jaguar)] * 2),
            cookies=[Cookie("JSESSIONID", "s", "incontrol.jaguar.com")],
        )

    async def test_the_brand_with_the_cars_wins(self) -> None:
        portal = bare(_session=self.both_brands(EMPTY_GARAGE, GARAGE))
        portal._new_session = lambda: portal._session
        assert await portal._async_ensure_session() == JAGUAR

    async def test_signing_in_everywhere_and_finding_nothing_is_not_a_sign_in_failure(
        self,
    ) -> None:
        # Reporting it as one sends the user off for an emailed code that
        # cannot help. Take the portal that let us in and report what is there.
        portal = bare(_session=self.both_brands(EMPTY_GARAGE, EMPTY_GARAGE))
        portal._new_session = lambda: portal._session
        assert await portal._async_ensure_session() == LAND_ROVER

    async def test_a_session_that_opens_nothing_needs_the_user(self) -> None:
        session = Routed(
            ("select-locale", [Reply(200, "")] * 8),
            (
                "forgerock/redirect",
                [Reply(200, "", "https://portal/select-locale")] * 4,
            ),
        )
        portal = bare(_session=session)
        portal._new_session = lambda: session
        with pytest.raises(JlrPortalAuthError, match="fresh sign-in"):
            await portal._async_ensure_session()

    async def test_a_base_already_chosen_is_not_rechosen(self) -> None:
        session = Routed()
        portal = bare(_session=session, _base=JAGUAR)
        assert await portal._async_ensure_session() == JAGUAR
        assert session.requested == []


class TestRememberingTheSession:
    def test_the_portal_cookies_are_kept_for_next_time(self) -> None:
        # So a restart resumes rather than spending the identity session,
        # which by then is usually dead and only the user can replace.
        saved: list[tuple] = []
        portal = bare(
            _session=Routed(
                cookies=[Cookie("JSESSIONID", "s", "incontrol.jaguar.com")]
            ),
            _on_portal_session=lambda *args: saved.append(args),
        )
        portal._remember_portal_session(JAGUAR)
        assert portal._portal_cookies == {"JSESSIONID": "s"}
        assert portal._portal_base == JAGUAR
        assert saved and saved[0][0] == JAGUAR

    def test_cookies_from_another_host_are_not_kept(self) -> None:
        portal = bare(_session=Routed(cookies=[Cookie("X", "y", "example.com")]))
        portal._remember_portal_session(JAGUAR)
        assert portal._portal_cookies == {}


class TestClosing:
    async def test_it_releases_the_session_without_closing_it(self) -> None:
        # close() would tear down the connector every other integration is
        # sharing, which is why Home Assistant wraps it in a warning. The
        # fake fails the test outright if it is called.
        session = Routed()
        session.closed = False
        portal = bare(_session=session, _base=JAGUAR)
        await portal.async_close()
        assert session.closed
        assert portal._session is None
        assert portal._base is None

    async def test_closing_twice_is_harmless(self) -> None:
        portal = bare()
        await portal.async_close()
        await portal.async_close()


class TestBuildingTheSession:
    async def test_it_carries_both_cookie_sets(self, hass) -> None:
        # The identity session for minting, and the portal session so a
        # restart resumes instead of spending the identity one. A real
        # Home Assistant here because this is the one place a session is
        # genuinely created, and it is created through Home Assistant.
        portal = bare(
            _hass=hass, _portal_cookies={"JSESSIONID": "s"}, _portal_base=JAGUAR
        )
        session = portal._new_session()
        try:
            keys = {cookie.key for cookie in session.cookie_jar}
            assert "SSOSession" in keys
            assert "JSESSIONID" in keys
        finally:
            session.detach()

    async def test_with_no_portal_session_only_the_identity_one(self, hass) -> None:
        portal = bare(_hass=hass)
        session = portal._new_session()
        try:
            assert {cookie.key for cookie in session.cookie_jar} == {"SSOSession"}
        finally:
            session.detach()


class TestKeepingItAlive:
    async def test_the_touch_asks_for_the_cheapest_page(self) -> None:
        # Letting the servlet session idle out costs a re-login, and a
        # re-login is the one thing that cannot be done without the user.
        client = portal(Reply(200, DASHBOARD))
        await client.async_touch()
        assert client._session.requested == ["https://portal/ajax/pollvehiclestatus"]


class TestRecoveringAVehicleId:
    async def test_one_car_and_one_spare_link_is_unambiguous(self) -> None:
        body = '<a href="/dashboard/vehicle/id-recovered">car</a>'
        client = portal(Reply(200, body))
        vehicles = {"SAJAA1234567890AB": {"nickname": "Test Car"}}
        await client._async_fill_missing_ids(vehicles)
        assert vehicles["SAJAA1234567890AB"]["portal_id"] == "id-recovered"

    async def test_nothing_missing_means_no_request(self) -> None:
        client = portal()
        await client._async_fill_missing_ids({"A": {"portal_id": "id-1"}})
        assert client._session.requested == []

    async def test_two_missing_is_a_guess_and_is_refused(self) -> None:
        # Attaching the wrong car's location to a vehicle is worse than
        # having none.
        body = (
            '<a href="/dashboard/vehicle/id-1">a</a>'
            '<a href="/dashboard/vehicle/id-2">b</a>'
        )
        client = portal(Reply(200, body))
        vehicles = {"A": {}, "B": {}}
        await client._async_fill_missing_ids(vehicles)
        assert "portal_id" not in vehicles["A"]

    async def test_a_link_already_claimed_is_not_reused(self) -> None:
        body = '<a href="/dashboard/vehicle/id-taken">a</a>'
        client = portal(Reply(200, body))
        vehicles = {"A": {"portal_id": "id-taken"}, "B": {}}
        await client._async_fill_missing_ids(vehicles)
        assert "portal_id" not in vehicles["B"]

    async def test_an_unreadable_dashboard_is_not_fatal(self) -> None:
        client = portal(Reply(200, LOGIN_PAGE), Reply(200, LOGIN_PAGE))
        vehicles = {"A": {}}
        await client._async_fill_missing_ids(vehicles)
        assert "portal_id" not in vehicles["A"]

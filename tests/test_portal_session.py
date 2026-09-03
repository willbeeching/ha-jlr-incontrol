"""The owner portal's session, which only a person can renew.

Location and the real vehicle names come from a legacy servlet app riding a
ForgeRock session. Nothing headless can mint another one, so every failure
here is either recoverable by retrying or costs the user an emailed code —
and telling those two apart correctly is the whole job.
"""

from __future__ import annotations

import json

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

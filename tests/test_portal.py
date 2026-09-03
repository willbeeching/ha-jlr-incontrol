"""The owner portal: choosing a brand, reusing a session, and failing honestly.

Location and vehicle names come from the portal owners log into in a browser,
behind a session created by typing an emailed code. Nothing can mint another
without the user, so almost every rule here exists to avoid spending one
needlessly — or to avoid asserting something false when it has gone.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import aiohttp
import pytest
from jlr import portal as portal_module
from jlr.portal import JlrPortal, JlrPortalAuthError, JlrPortalError, _is_login_page

LR, JAG = portal_module.PORTAL_BASES
LOGIN_HTML = "<html><form><input name='j_username'></form></html>"


def garage(count: int) -> str:
    return json.dumps({"vehicles": [{"fullVin": f"V{i}"} for i in range(count)]})


class Resp:
    def __init__(self, url: str, body: str, status: int = 200) -> None:
        self.url, self._body, self.status = url, body, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self) -> str:
        return self._body


class Raises:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def __aenter__(self):
        raise self._error

    async def __aexit__(self, *args):
        return False


class FakeSession:
    """Replays a scripted sequence; anything past the end is empty JSON."""

    def __init__(self, script) -> None:
        self.script, self.urls, self.closed = list(script), [], False
        self.cookie_jar = aiohttp.CookieJar()

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self.script.pop(0) if self.script else Resp(url, "{}")

    async def close(self) -> None:
        self.closed = True


def build(hass, script, logins=None, **kwargs) -> tuple[JlrPortal, FakeSession]:
    client = JlrPortal(hass, {"SSOSession": "am"}, **kwargs)
    session = FakeSession(script)
    client._new_session = lambda: session

    async def login(base: str) -> bool:
        return (logins or {}).get(base, False)

    client._async_login = login
    return client, session


class TestBrandSelection:
    """Signing in succeeds on both brands whatever the account owns (#14)."""

    async def test_passes_over_a_brand_with_none_of_the_cars(self, hass) -> None:
        client, _ = build(
            hass, [Resp(LR, garage(0)), Resp(JAG, garage(1))], {LR: True, JAG: True}
        )
        assert await client._async_ensure_session() == JAG

    async def test_stops_at_the_first_brand_that_has_them(self, hass) -> None:
        client, session = build(hass, [Resp(LR, garage(2))], {LR: True, JAG: True})
        assert await client._async_ensure_session() == LR
        assert len(session.urls) == 1, "the second brand should not be touched"

    async def test_signed_in_everywhere_but_no_cars_is_not_a_sign_in_failure(
        self, hass
    ) -> None:
        # Reporting one would send the user for an emailed code that cannot help.
        client, _ = build(
            hass, [Resp(LR, garage(0)), Resp(JAG, garage(0))], {LR: True, JAG: True}
        )
        assert await client._async_ensure_session() == LR

    async def test_a_garage_that_bounces_to_login_does_not_count(self, hass) -> None:
        client, _ = build(
            hass,
            [
                Resp("https://identity.jaguarlandrover.com/auth", "<html>"),
                Resp(JAG, garage(1)),
            ],
            {LR: True, JAG: True},
        )
        assert await client._async_ensure_session() == JAG

    async def test_signed_out_everywhere_still_raises(self, hass) -> None:
        client, _ = build(hass, [], {})
        with pytest.raises(JlrPortalAuthError):
            await client._async_ensure_session()


class TestResumingASavedSession:
    """A saved session is worth hours; spending one needlessly costs a code."""

    kept = {"portal_cookies": {"JSESSIONID": "kept"}, "portal_base": LR}

    async def test_a_live_session_with_cars_is_reused_without_signing_in(
        self, hass
    ) -> None:
        client, session = build(hass, [Resp(LR, garage(2))], **self.kept)
        assert await client._async_ensure_session() == LR
        assert len(session.urls) == 1

    async def test_a_live_session_on_the_wrong_brand_is_not_reused(self, hass) -> None:
        # The saved base predates the brand check, so a Jaguar owner can be
        # holding a perfectly live Land Rover session (#14).
        client, _ = build(
            hass,
            [Resp(LR, garage(0)), Resp(LR, garage(0)), Resp(JAG, garage(1))],
            {LR: True, JAG: True},
            **self.kept,
        )
        assert await client._async_ensure_session() == JAG

    async def test_a_lapsed_session_falls_back_to_signing_in(self, hass) -> None:
        client, _ = build(
            hass, [Resp(LR, LOGIN_HTML), Resp(LR, garage(1))], {LR: True}, **self.kept
        )
        assert await client._async_ensure_session() == LR

    async def test_an_unreadable_body_gets_the_benefit_of_the_doubt(self, hass) -> None:
        # Minting spends the identity session, which only the user can replace.
        client, session = build(
            hass, [Resp(LR, "<html>a dashboard, not JSON</html>")], **self.kept
        )
        assert await client._async_ensure_session() == LR
        assert len(session.urls) == 1

    async def test_a_probe_that_cannot_complete_re_mints(self, hass) -> None:
        client, _ = build(
            hass, [Raises(TimeoutError()), Resp(LR, garage(1))], {LR: True}, **self.kept
        )
        assert await client._async_ensure_session() == LR


class TestSessionAge:
    """Age separates 'we touched it too slowly' from 'it expired anyway'."""

    @staticmethod
    def aged(hass, delta: timedelta) -> JlrPortal:
        when = datetime.now(UTC) - delta
        return JlrPortal(hass, {"SSOSession": "am"}, portal_minted=when.isoformat())

    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (timedelta(hours=14, minutes=46), "14h46m old"),
            (timedelta(hours=21, minutes=7), "21h07m old"),
            (timedelta(minutes=4), "0h04m old"),
            (timedelta(hours=30, minutes=1), "30h01m old"),
        ],
    )
    def test_reports_hours_and_minutes(self, hass, delta, expected) -> None:
        assert self.aged(hass, delta).session_age == expected

    @pytest.mark.parametrize("stored", [None, "", "not a date"])
    def test_says_unknown_rather_than_inventing_zero(self, hass, stored) -> None:
        # "0h00m old" would read as a session just minted, which is worse
        # than admitting we do not know.
        assert (
            JlrPortal(hass, {"a": "b"}, portal_minted=stored).session_age
            == "age unknown"
        )

    def test_a_clock_that_went_backwards_does_not_produce_a_negative_age(
        self, hass
    ) -> None:
        ahead = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        assert (
            JlrPortal(hass, {"a": "b"}, portal_minted=ahead).session_age
            == "age unknown"
        )

    def test_a_naive_timestamp_is_read_as_utc(self, hass) -> None:
        naive = (
            (datetime.now(UTC) - timedelta(hours=3)).replace(tzinfo=None).isoformat()
        )
        assert (
            JlrPortal(hass, {"a": "b"}, portal_minted=naive).session_age == "3h00m old"
        )


class TestLoginPageDetection:
    """Telling a bounce from the page asked for decides whether to re-auth."""

    def test_json_is_never_a_login_page(self) -> None:
        assert not _is_login_page(".../ajax/pollvehiclestatus", garage(1))

    def test_the_sign_in_form_is(self) -> None:
        assert _is_login_page(".../dashboard", LOGIN_HTML)

    def test_the_locale_gate_is(self) -> None:
        assert _is_login_page(".../select-locale", "")

    def test_the_identity_host_is(self) -> None:
        assert _is_login_page("https://identity.jaguarlandrover.com/auth", "")


class TestFailuresAreReportedHonestly:
    kept = {"portal_cookies": {"JSESSIONID": "x"}, "portal_base": LR}

    async def test_a_timeout_surfaces_as_a_portal_error(self, hass) -> None:
        # Escaping as TimeoutError produced "unexpected failure reading the
        # owner portal" and froze the tracker.
        client, _ = build(
            hass,
            [
                Resp(LR, garage(1)),  # resume probe: session is good
                Raises(TimeoutError()),  # the read itself
                Resp(LR, garage(1)),  # garage check while re-minting
                Raises(TimeoutError()),  # and again on the retry
            ],
            {LR: True},
            **self.kept,
        )
        with pytest.raises(JlrPortalError) as raised:
            await client.async_get_vehicles()
        assert "timed out" in str(raised.value)

    async def test_one_timeout_then_success_is_retried_not_reported(self, hass) -> None:
        client, _ = build(
            hass,
            [
                Resp(LR, garage(1)),  # resume probe
                Raises(TimeoutError()),  # the read times out once
                Resp(LR, garage(1)),  # garage check while re-minting
                Resp(LR, garage(1)),  # the retry succeeds
            ],
            {LR: True},
            **self.kept,
        )
        assert await client.async_get_vehicles()

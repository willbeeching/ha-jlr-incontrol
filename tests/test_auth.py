"""What gets kept from a sign-in, and what must not be.

The cookies harvested here are the only way back into the owner portal without
asking the user for another emailed code, so getting the set wrong is expensive
in a way most bugs are not. Each rule below cost a release to learn.
"""

from __future__ import annotations

import pytest
from jlr.auth import JlrLogin
from jlr.const import IDENTITY_HOST, ONE_SHOT_COOKIES, SESSION_COOKIE
from yarl import URL


@pytest.fixture
async def login(hass):
    # A real session now, created through Home Assistant so its lifetime is
    # Home Assistant's — which is the whole point of the change.
    client = JlrLogin(hass, "someone@example.com")
    try:
        yield client
    finally:
        await client.async_close()


def hold(client: JlrLogin, cookies: dict[str, str], url: str = IDENTITY_HOST) -> None:
    client._session.cookie_jar.update_cookies(cookies, response_url=URL(url))


class TestSessionCookies:
    async def test_keeps_the_whole_identity_set_not_just_the_session(
        self, login
    ) -> None:
        # Sending SSOSession alone lands on a node that has never heard of the
        # session: lbcookie routes to the one holding it, and INGRESSCOOKIE and
        # IG_SESSIONID route within the gateway. Missing any of them looks
        # exactly like an expired session.
        hold(
            login,
            {
                SESSION_COOKIE: "s",
                "lbcookie": "01",
                "INGRESSCOOKIE": "ig",
                "IG_SESSIONID": "gw",
            },
        )
        held = login.session_cookies()
        assert set(held) >= {
            SESSION_COOKIE,
            "lbcookie",
            "INGRESSCOOKIE",
            "IG_SESSIONID",
        }

    async def test_drops_the_one_shot_oauth_cookie(self, login) -> None:
        # Replaying a spent authorize request poisons the next sign-in.
        hold(login, {SESSION_COOKIE: "s", **dict.fromkeys(ONE_SHOT_COOKIES, "x")})
        held = login.session_cookies()
        assert not ONE_SHOT_COOKIES & set(held)
        assert held[SESSION_COOKIE] == "s"

    async def test_derives_the_session_cookie_from_the_token_id(self, login) -> None:
        # SSOSession's value is the tokenId the journey just returned — the
        # same session delivered twice. Deriving it means a jar that missed the
        # Set-Cookie cannot silently cost us the one cookie that matters.
        login._token_id = "a-token-id"
        hold(login, {"lbcookie": "01"})
        assert login.session_cookies()[SESSION_COOKIE] == "a-token-id"

    async def test_a_real_session_cookie_wins_over_the_derived_one(self, login) -> None:
        login._token_id = "a-token-id"
        hold(login, {SESSION_COOKIE: "from-the-jar"})
        assert login.session_cookies()[SESSION_COOKIE] == "from-the-jar"

    async def test_ignores_cookies_from_other_hosts(self, login) -> None:
        hold(login, {"elsewhere": "nope"}, url="https://example.invalid/")
        assert "elsewhere" not in login.session_cookies()

    async def test_survives_a_journey_that_set_nothing(self, login) -> None:
        assert login.session_cookies() == {}

"""A failed token refresh must not send the user to fetch an emailed code.

Every non-200 from the token endpoint used to raise JlrAuthError, which Home
Assistant turns into ConfigEntryAuthFailed and a "sign in again" prompt. So a
rate limit or an outage at JLR asked the user for an emailed code that could
not possibly have helped — while their credentials were perfectly good.
"""

from __future__ import annotations

import pytest
from fakes import FakeResponse, FakeSession
from jlr.api import (
    JlrApiError,
    JlrAuthError,
    JlrClient,
    JlrRateLimitError,
    _retry_after,
)


def client_for(status: int, payload, headers: dict | None = None) -> JlrClient:
    client = JlrClient.__new__(JlrClient)
    client._session = FakeSession(FakeResponse(status, payload, headers))
    client._refresh_token = "a-refresh-token"
    return client


class TestExpiredCredentials:
    """Only these mean the user genuinely has to sign in again."""

    async def test_invalid_grant_is_an_auth_failure(self) -> None:
        # RFC 6749: the refresh token is expired, revoked, or already rotated.
        with pytest.raises(JlrAuthError):
            await client_for(400, {"error": "invalid_grant"})._refresh()

    async def test_invalid_grant_is_believed_whatever_the_status(self) -> None:
        with pytest.raises(JlrAuthError):
            await client_for(401, {"error": "invalid_grant"})._refresh()

    async def test_a_missing_refresh_token_is_an_auth_failure(self) -> None:
        client = client_for(200, {})
        client._refresh_token = None
        with pytest.raises(JlrAuthError):
            await client._refresh()


class TestTemporaryFailures:
    """None of these say anything about whether the credentials still work."""

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    async def test_outages_are_temporary(self, status: int) -> None:
        with pytest.raises(JlrApiError) as raised:
            await client_for(status, None)._refresh()
        assert not isinstance(raised.value, JlrAuthError)

    async def test_rate_limits_are_temporary(self) -> None:
        with pytest.raises(JlrRateLimitError) as raised:
            await client_for(429, None, {"Retry-After": "120"})._refresh()
        assert not isinstance(raised.value, JlrAuthError)
        assert raised.value.retry_after == 120

    @pytest.mark.parametrize("status", [401, 403])
    async def test_a_bare_rejection_is_not_proof_of_spent_credentials(
        self, status: int
    ) -> None:
        # 401 without a body is invalid_client — our client id, not the user's
        # credentials — and JLR return 403 from an edge rule as readily as from
        # anything about authorisation. Neither is fixed by an emailed code.
        with pytest.raises(JlrApiError) as raised:
            await client_for(status, {})._refresh()
        assert not isinstance(raised.value, JlrAuthError)

    async def test_invalid_client_is_not_the_users_problem(self) -> None:
        with pytest.raises(JlrApiError) as raised:
            await client_for(401, {"error": "invalid_client"})._refresh()
        assert not isinstance(raised.value, JlrAuthError)

    async def test_a_400_without_invalid_grant_is_temporary(self) -> None:
        # A malformed request is our bug, not spent credentials — signing in
        # again would not fix it either.
        with pytest.raises(JlrApiError) as raised:
            await client_for(400, {"error": "invalid_request"})._refresh()
        assert not isinstance(raised.value, JlrAuthError)

    async def test_an_unparseable_body_is_temporary(self) -> None:
        with pytest.raises(JlrApiError) as raised:
            await client_for(200, "not json")._refresh()
        assert not isinstance(raised.value, JlrAuthError)


class TestRetryAfter:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [("120", 120.0), ("0", 0.0), ("1.5", 1.5), (None, None), ("", None)],
    )
    def test_delay_seconds(self, header, expected) -> None:
        assert _retry_after(header) == expected

    def test_http_date_form_is_ignored_rather_than_guessed(self) -> None:
        assert _retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None

    def test_a_negative_delay_is_ignored(self) -> None:
        assert _retry_after("-5") is None

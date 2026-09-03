"""Error bodies get logged, and logs get pasted into public issues.

The token endpoint is the one most likely to answer with a body worth hiding,
and it is also the one whose failures are most worth logging. Both were true at
once for a while: the payload was serialised to text and then only checked for
VIN-shaped strings, so a credential in a JSON error body went out in full.
"""

from __future__ import annotations

import logging

import pytest
from fakes import FakeResponse
from jlr.api import JlrClient

# 17 characters and no I, O or Q — the standard excludes them so they
# cannot be misread as 1 and 0, which is what makes the shape detectable.
VIN = "SAJAA1234567890AB"


@pytest.fixture(autouse=True)
def debug_logging(caplog):
    caplog.set_level(logging.DEBUG, logger="jlr.api")
    return caplog


# What this client knows about itself. A block page quotes any of it back.
PASSWORD = "hunter2-and-then-some"
USERNAME = "someone@example.com"
USER_ID = "user-01H8XK4Q2N"
DEVICE_ID = "3f2c9a71-5d84-4c1e-9a30-6b7d8e5f4a21"
ACCESS_TOKEN = "eyJhbGciOiJSUzI1NiJ9.access"
REFRESH_TOKEN = "eyJhbGciOiJSUzI1NiJ9.refresh"


def client() -> JlrClient:
    instance = JlrClient.__new__(JlrClient)
    instance._password = PASSWORD
    instance._username = USERNAME
    instance._user_id = USER_ID
    instance._device_id = DEVICE_ID
    instance._access_token = ACCESS_TOKEN
    instance._refresh_token = REFRESH_TOKEN
    return instance


class TestErrorBodies:
    async def test_a_credential_in_a_json_body_is_not_logged(self, caplog) -> None:
        await client()._log_error_response(
            FakeResponse(400, {"refresh_token": "a-live-token"}),
            "token refresh",
            {"refresh_token": "a-live-token"},
        )
        assert "a-live-token" not in caplog.text
        assert "REDACTED" in caplog.text

    async def test_nested_credentials_are_not_logged(self, caplog) -> None:
        payload = {"error": "invalid_grant", "debug": {"access_token": "secret"}}
        await client()._log_error_response(
            FakeResponse(400, payload), "token refresh", payload
        )
        assert "secret" not in caplog.text
        # The part worth reading survives.
        assert "invalid_grant" in caplog.text

    async def test_a_vin_in_a_json_body_is_not_logged(self, caplog) -> None:
        payload = {"message": f"no such vehicle {VIN}"}
        await client()._log_error_response(
            FakeResponse(404, payload), "vehicle list", payload
        )
        assert VIN not in caplog.text

    async def test_the_password_is_not_echoed_back(self, caplog) -> None:
        payload = {"detail": f"bad credentials for {PASSWORD}"}
        await client()._log_error_response(
            FakeResponse(401, payload), "sign in", payload
        )
        assert PASSWORD not in caplog.text

    async def test_a_useful_error_still_reaches_the_log(self, caplog) -> None:
        payload = {"error": "invalid_client", "error_description": "unknown client"}
        await client()._log_error_response(
            FakeResponse(401, payload), "token refresh", payload
        )
        assert "invalid_client" in caplog.text
        assert "unknown client" in caplog.text
        assert "401" in caplog.text


class TestBodiesNobodyCanParse:
    """The WAF case: HTML quoting our own request back at us.

    There is no JSON here for scrub() to walk and nothing VIN-shaped for
    scrub_text() to spot, so the only handle on a block page is that what it
    echoes came from us in the first place.
    """

    async def test_an_html_block_page_still_reaches_the_log(self, caplog) -> None:
        await client()._log_error_response(
            FakeResponse(403, None, text="<html><body>Access Denied</body></html>"),
            "vehicle list",
            None,
        )
        assert "Access Denied" in caplog.text
        assert "403" in caplog.text

    @pytest.mark.parametrize(
        "secret",
        [USERNAME, USER_ID, DEVICE_ID, ACCESS_TOKEN, REFRESH_TOKEN, PASSWORD],
    )
    async def test_our_own_identifiers_are_not_echoed_back(
        self, caplog, secret: str
    ) -> None:
        page = f"<html><body>Blocked request: {secret}</body></html>"
        await client()._log_error_response(
            FakeResponse(403, None, text=page), "vehicle list", None
        )
        assert secret not in caplog.text
        assert "REDACTED" in caplog.text

    async def test_a_bearer_header_quoted_back_is_not_logged(self, caplog) -> None:
        page = f"Denied. Authorization: Bearer {ACCESS_TOKEN} X-Device-Id: {DEVICE_ID}"
        await client()._log_error_response(
            FakeResponse(403, None, text=page), "vehicle list", None
        )
        assert ACCESS_TOKEN not in caplog.text
        assert DEVICE_ID not in caplog.text

    async def test_an_unreadable_body_is_not_fatal(self, caplog) -> None:
        await client()._log_error_response(
            FakeResponse(502, None, text=UnicodeDecodeError("utf-8", b"", 0, 1, "bad")),
            "vehicle list",
            None,
        )
        assert "502" in caplog.text

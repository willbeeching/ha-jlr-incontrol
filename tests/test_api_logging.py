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


def client() -> JlrClient:
    instance = JlrClient.__new__(JlrClient)
    instance._password = "hunter2"
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
        payload = {"detail": "bad credentials for hunter2"}
        await client()._log_error_response(
            FakeResponse(401, payload), "sign in", payload
        )
        assert "hunter2" not in caplog.text

    async def test_a_useful_error_still_reaches_the_log(self, caplog) -> None:
        payload = {"error": "invalid_client", "error_description": "unknown client"}
        await client()._log_error_response(
            FakeResponse(401, payload), "token refresh", payload
        )
        assert "invalid_client" in caplog.text
        assert "unknown client" in caplog.text
        assert "401" in caplog.text

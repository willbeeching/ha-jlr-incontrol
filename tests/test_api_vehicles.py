"""The vehicle list is acted on by forgetting cars, so its shape matters.

An empty list is authoritative: the coordinator takes it to mean the account
has no vehicles and drops the ones it knew. That is right when the account
really is empty and catastrophic when the reply was simply unreadable, so the
two must never be allowed to look the same.
"""

from __future__ import annotations

import pytest
from fakes import FakeResponse, FakeSession
from jlr.api import JlrApiError, JlrClient


def client_for(status: int, payload) -> JlrClient:
    client = JlrClient.__new__(JlrClient)
    client._session = FakeSession(FakeResponse(status, payload))
    client._user_id = "a-user"
    client._access_token = "a-token"
    client._expires_at = float("inf")
    client._device_id = "a-device"

    async def connected() -> None:
        return None

    client.async_connect = connected
    return client


class TestUsableReplies:
    async def test_returns_the_vehicles(self) -> None:
        client = client_for(200, {"vehicles": [{"vin": "A"}, {"vin": "B"}]})
        assert len(await client.async_get_vehicles()) == 2

    async def test_an_empty_garage_is_reported_as_empty(self) -> None:
        # Not an error. Someone who has sold their only car has no vehicles,
        # and the coordinator has to be told so it can forget the old one.
        assert await client_for(200, {"vehicles": []}).async_get_vehicles() == []


class TestUnreadableReplies:
    """None of these may arrive at the coordinator looking like an empty garage."""

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            {},
            {"vehicles": None},
            {"vehicles": {"vin": "A"}},
            "not a mapping",
            [],
        ],
    )
    async def test_a_reply_we_cannot_read_raises(self, payload) -> None:
        with pytest.raises(JlrApiError):
            await client_for(200, payload).async_get_vehicles()

    async def test_a_failed_request_raises(self) -> None:
        with pytest.raises(JlrApiError):
            await client_for(500, None).async_get_vehicles()

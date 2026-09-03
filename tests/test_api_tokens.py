"""The access token, which lives about five minutes and rotates when renewed.

Two things make this delicate. The refresh token is single-use, so two callers
renewing at once means the second spends one JLR has already retired — which
looks exactly like expired credentials and costs the user an emailed code for
nothing. And the telemetry socket binds its session to the bearer it presented,
so it has to know when to reconnect rather than waiting to be disconnected.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fakes import FakeResponse, FakeSession
from jlr.api import JlrApiError, JlrAuthError, JlrClient
from jlr.const import (
    TOKEN_RENEW_MARGIN_MAX,
    TOKEN_RENEW_MARGIN_MIN,
    TOKEN_RENEW_RATIO,
)


def client(**state: object) -> JlrClient:
    made = JlrClient.__new__(JlrClient)
    made._username = "someone@example.com"
    made._password = "hunter2-and-then-some"
    made._device_id = "a-device"
    made._user_id = "a-user"
    made._access_token = None
    made._refresh_token = "a-refresh-token"
    made._expires_at = 0.0
    made._device_registered = False
    made._on_tokens = None
    made._token_lock = asyncio.Lock()
    for name, value in state.items():
        setattr(made, name, value)
    return made


class TestAdoptingTokens:
    def test_the_pair_is_kept(self) -> None:
        made = client()
        made.apply_tokens({"access_token": "new-access", "refresh_token": "new-r"})
        assert made.access_token == "new-access"
        assert made.refresh_token == "new-r"

    def test_a_reply_without_a_new_refresh_token_keeps_the_old_one(self) -> None:
        # Losing it would mean a sign-in on the next restart.
        made = client()
        made.apply_tokens({"access_token": "new-access"})
        assert made.refresh_token == "a-refresh-token"

    def test_the_renewal_margin_is_proportional(self) -> None:
        # A fixed margin larger than the lifetime would treat every token as
        # already expired and refresh on every single request.
        made = client()
        made.apply_tokens({"access_token": "a", "expires_in": 300})
        expected = min(
            TOKEN_RENEW_MARGIN_MAX,
            max(TOKEN_RENEW_MARGIN_MIN, 300 * TOKEN_RENEW_RATIO),
        )
        assert made.seconds_until_renewal() == pytest.approx(300 - expected, abs=1)

    def test_a_very_short_token_still_leaves_time_to_use_it(self) -> None:
        made = client()
        made.apply_tokens({"access_token": "a", "expires_in": 30})
        assert made.seconds_until_renewal() >= 0

    def test_a_new_token_means_the_device_needs_registering_again(self) -> None:
        made = client(_device_registered=True)
        made.apply_tokens({"access_token": "a"})
        assert made._device_registered is False

    def test_the_entry_is_told_so_the_rotation_survives_a_restart(self) -> None:
        # JLR retire the old refresh token immediately, so one held in memory
        # and not written is a restart away from an emailed code.
        written = []
        made = client(_on_tokens=lambda: written.append(True))
        made.apply_tokens({"access_token": "a", "refresh_token": "b"})
        assert written == [True]

    def test_with_no_token_there_is_no_time_left(self) -> None:
        assert client().seconds_until_renewal() == 0.0


class TestRenewingOnlyWhenNeeded:
    async def test_a_live_token_is_left_alone(self) -> None:
        # Every request calls this. Renewing needlessly spends a single-use
        # refresh token and puts avoidable load on JLR.
        made = client(_access_token="live", _expires_at=time.monotonic() + 600)
        refreshed = 0

        async def refresh() -> None:
            nonlocal refreshed
            refreshed += 1

        made._refresh = refresh
        await made.async_ensure_token()
        assert refreshed == 0

    async def test_an_expired_token_is_renewed(self) -> None:
        made = client(_access_token="stale", _expires_at=time.monotonic() - 1)
        refreshed = 0

        async def refresh() -> None:
            nonlocal refreshed
            refreshed += 1
            made._access_token = "fresh"
            made._expires_at = time.monotonic() + 600

        made._refresh = refresh
        await made.async_ensure_token()
        assert refreshed == 1

    async def test_a_missing_token_is_minted(self) -> None:
        made = client(_access_token=None)
        refreshed = 0

        async def refresh() -> None:
            nonlocal refreshed
            refreshed += 1
            made._access_token = "fresh"
            made._expires_at = time.monotonic() + 600

        made._refresh = refresh
        await made.async_ensure_token()
        assert refreshed == 1

    async def test_a_crowd_of_callers_spends_exactly_one_refresh_token(self) -> None:
        # The failure this prevents: the second caller spends a token JLR has
        # already retired, gets a 400, and the user is sent for a code.
        made = client(_access_token=None)
        refreshed = 0

        async def refresh() -> None:
            nonlocal refreshed
            refreshed += 1
            await asyncio.sleep(0)
            made._access_token = "fresh"
            made._expires_at = time.monotonic() + 600

        made._refresh = refresh
        await asyncio.gather(*(made.async_ensure_token() for _ in range(10)))
        assert refreshed == 1


class TestReadingAVehicle:
    def with_reply(self, status: int, payload) -> JlrClient:
        made = client(_access_token="live", _expires_at=time.monotonic() + 600)
        made._session = FakeSession(FakeResponse(status, payload))
        return made

    async def test_status_is_flattened(self) -> None:
        payload = {
            "vehicleStatus": {
                "coreStatus": [{"key": "ODOMETER", "value": "123456"}],
                "evStatus": [{"key": "EV_STATE_OF_CHARGE", "value": "80"}],
            }
        }
        status = await self.with_reply(200, payload).async_get_status("SAJ")
        assert status["ODOMETER"] == "123456"
        assert status["EV_STATE_OF_CHARGE"] == "80"

    async def test_attestation_is_named_rather_than_guessed_at(self) -> None:
        # 498 is Approov's. Reporting it as a generic failure sent people
        # hunting their own credentials for a wall they cannot get past.
        with pytest.raises(JlrApiError, match="attestation"):
            await self.with_reply(498, None).async_get_status("SAJ")

    async def test_a_401_is_the_token_not_the_account(self) -> None:
        with pytest.raises(JlrAuthError):
            await self.with_reply(401, None).async_get_status("SAJ")

    async def test_a_403_is_not_treated_as_bad_credentials(self) -> None:
        # JLR return it from an edge rule as readily as from authorisation.
        with pytest.raises(JlrApiError) as raised:
            await self.with_reply(403, None).async_get_status("SAJ")
        assert not isinstance(raised.value, JlrAuthError)

    async def test_a_position_is_unwrapped(self) -> None:
        payload = {"position": {"latitude": 51.5074, "longitude": -0.1278}}
        assert (await self.with_reply(200, payload).async_get_position("SAJ"))[
            "latitude"
        ] == 51.5074

    async def test_a_vehicle_with_no_position_reports_nothing(self) -> None:
        assert await self.with_reply(200, {}).async_get_position("SAJ") == {}

    async def test_a_failed_position_read_raises(self) -> None:
        with pytest.raises(JlrApiError):
            await self.with_reply(500, None).async_get_position("SAJ")


class TestAttributesBehindTheWall:
    """Every identity endpoint is tried; only total failure is reported."""

    def falling_back(self, *replies) -> JlrClient:
        made = client(_access_token="live", _expires_at=time.monotonic() + 600)
        queue = list(replies)

        async def connected() -> None:
            return None

        async def request(method, url, **kwargs):
            return queue.pop(0)

        made.async_connect = connected
        made._request = request
        return made

    async def test_the_first_endpoint_that_names_the_car_wins(self) -> None:
        made = self.falling_back((200, {"nickname": "Test Car"}))
        assert (await made.async_get_attributes("SAJ"))["nickname"] == "Test Car"

    async def test_a_walled_primary_does_not_hide_a_working_alternative(self) -> None:
        # The Approov rule sits on /vehicles/{vin}/*, so a /users/-rooted path
        # can still answer. Reporting the 498 immediately would lose that.
        made = self.falling_back((498, None), (200, {"nickname": "Test Car"}))
        assert (await made.async_get_attributes("SAJ"))["nickname"] == "Test Car"

    async def test_a_200_that_names_nothing_is_not_an_answer(self) -> None:
        made = self.falling_back(
            (200, {"unrelated": 1}), (200, {"registrationNumber": "AB12 CDE"})
        )
        assert "registrationNumber" in await made.async_get_attributes("SAJ")

    async def test_when_nothing_answers_the_last_failure_is_reported(self) -> None:
        made = self.falling_back((498, None), (498, None), (403, None))
        with pytest.raises(JlrApiError):
            await made.async_get_attributes("SAJ")


class TestRegisteringTheDevice:
    def registering(self, status: int) -> JlrClient:
        made = client(_access_token="live", _expires_at=time.monotonic() + 600)
        made._session = FakeSession(FakeResponse(status, None))
        return made

    @pytest.mark.parametrize("status", [200, 204])
    async def test_either_success_code_counts(self, status: int) -> None:
        made = self.registering(status)
        await made.async_register_device()
        assert made._device_registered

    async def test_it_is_not_repeated(self) -> None:
        # Idempotent at JLR's end, but a request nobody needs is still a
        # request to somebody else's servers.
        made = self.registering(204)
        await made.async_register_device()
        made._session = FakeSession(FakeResponse(500, None))
        await made.async_register_device()

    async def test_a_refusal_is_reported(self) -> None:
        with pytest.raises(JlrApiError, match="device registration"):
            await self.registering(403).async_register_device()

    async def test_a_new_token_means_registering_again(self) -> None:
        made = self.registering(204)
        await made.async_register_device()
        made.apply_tokens({"access_token": "a-newer-token"})
        assert not made._device_registered


class TestResolvingTheUserId:
    def looking_up(self, status: int, payload: object) -> JlrClient:
        made = client(_access_token="live", _expires_at=time.monotonic() + 600)
        made._session = FakeSession(FakeResponse(status, payload))
        return made

    async def test_the_id_is_returned_and_kept(self) -> None:
        made = self.looking_up(200, {"userId": "user-01H8XK4Q2N"})
        assert await made.async_get_user_id() == "user-01H8XK4Q2N"
        assert made.user_id == "user-01H8XK4Q2N"

    async def test_a_reply_without_one_is_an_error(self) -> None:
        # Every vehicle URL is built from it, so carrying on with None would
        # produce a stream of 404s that look like a JLR outage.
        with pytest.raises(JlrApiError, match="did not return a userId"):
            await self.looking_up(200, {}).async_get_user_id()

    async def test_a_failed_lookup_is_an_error(self) -> None:
        with pytest.raises(JlrApiError):
            await self.looking_up(500, None).async_get_user_id()


class TestConnecting:
    async def test_it_does_the_three_things_in_order(self) -> None:
        made = client()
        done: list[str] = []

        async def token() -> None:
            done.append("token")

        async def register() -> None:
            done.append("register")

        async def user() -> str:
            done.append("user")
            return "a-user"

        made.async_ensure_token = token
        made.async_register_device = register
        made.async_get_user_id = user
        made._user_id = None
        await made.async_connect()
        assert done == ["token", "register", "user"]

    async def test_a_known_user_id_is_not_looked_up_again(self) -> None:
        made = client(_user_id="already-known")
        looked_up = False

        async def token() -> None:
            return None

        async def register() -> None:
            return None

        async def user() -> str:
            nonlocal looked_up
            looked_up = True
            return "x"

        made.async_ensure_token = token
        made.async_register_device = register
        made.async_get_user_id = user
        await made.async_connect()
        assert not looked_up

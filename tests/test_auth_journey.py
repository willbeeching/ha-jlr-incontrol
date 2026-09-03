"""The emailed-code sign-in, step by step.

This is the most fragile code in the integration and the source of most of the
bug reports: it drives someone else's ForgeRock journey, whose shape can change
without notice, and every failure costs the user a one-time code to retry. It
was also the least covered module in the package.
"""

from __future__ import annotations

import base64
import hashlib

import aiohttp
import pytest
from fakes import FakeResponse, FakeSession
from jlr.auth import (
    STAGE_OTP_COLLECT,
    JlrInvalidCode,
    JlrLogin,
    JlrLoginError,
    JlrSessionExpired,
    _callback_name,
    _pkce,
    _set,
)

PASSWORD = "hunter2-and-then-some"


@pytest.fixture
async def login():
    client = JlrLogin("someone@example.com")
    try:
        yield client
    finally:
        await client.async_close()


def callback(kind: str, name: str | None = None, value: object = "") -> dict:
    made: dict = {"type": kind, "input": [{"name": "IDToken1", "value": value}]}
    if name is not None:
        made["output"] = [{"name": "id", "value": name}]
    return made


def answered(step: dict) -> list:
    return [item["input"][0]["value"] for item in step["callbacks"]]


class TestPkce:
    def test_the_challenge_is_the_verifier_hashed(self) -> None:
        # If these ever stop matching, the token exchange is refused and the
        # user has spent a code to find out.
        verifier, challenge = _pkce()
        digest = hashlib.sha256(verifier.encode()).digest()
        assert challenge == base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def test_each_login_gets_its_own(self) -> None:
        assert _pkce()[0] != _pkce()[0]


class TestAnsweringCallbacks:
    def test_the_password_step_is_filled(self, login) -> None:
        step = {"stage": "Password", "callbacks": [callback("PasswordCallback")]}
        login._fill(step, PASSWORD)
        assert answered(step) == [PASSWORD]

    def test_the_username_step_is_filled(self, login) -> None:
        step = {
            "stage": "Username",
            "callbacks": [callback("ValidatedCreateUsernameCallback")],
        }
        login._fill(step, PASSWORD)
        assert answered(step) == ["someone@example.com"]

    def test_local_sign_in_is_chosen_over_a_social_provider(self, login) -> None:
        step = {"stage": "IdP", "callbacks": [callback("SelectIdPCallback")]}
        login._fill(step, PASSWORD)
        assert answered(step) == ["localAuthentication"]

    def test_the_passkey_prompt_is_declined(self, login) -> None:
        # "unsupported" is what the app sends when the user dismisses it, and
        # it is what drops the journey to the password step.
        step = {
            "stage": "WebAuthn",
            "callbacks": [callback("HiddenValueCallback", "webauthn_data")],
        }
        login._fill(step, PASSWORD)
        assert answered(step) == ["unsupported"]

    @pytest.mark.parametrize("kind", ["ChoiceCallback", "ConfirmationCallback"])
    def test_a_choice_takes_the_first_option(self, login, kind: str) -> None:
        step = {"stage": "Choice", "callbacks": [callback(kind)]}
        login._fill(step, PASSWORD)
        assert answered(step) == [0]

    def test_informational_text_is_posted_back_untouched(self, login) -> None:
        step = {
            "stage": "Info",
            "callbacks": [
                callback("TextOutputCallback", value="check your email"),
                callback("PasswordCallback"),
            ],
        }
        login._fill(step, PASSWORD)
        assert answered(step) == ["check your email", PASSWORD]

    def test_a_callback_we_do_not_know_is_left_alone(self, login) -> None:
        step = {
            "stage": "Odd",
            "callbacks": [
                callback("SomethingNew", value="leave me"),
                callback("PasswordCallback"),
            ],
        }
        login._fill(step, PASSWORD)
        assert answered(step) == ["leave me", PASSWORD]

    def test_a_step_asking_nothing_at_all_is_a_dead_end(self, login) -> None:
        with pytest.raises(JlrLoginError, match="stalled"):
            login._fill({"stage": "Empty", "callbacks": []}, PASSWORD)

    def test_a_code_step_with_nowhere_to_put_the_code_is_an_error(self, login) -> None:
        with pytest.raises(JlrLoginError, match="verification code"):
            login._set_password_callback(
                {"callbacks": [callback("ChoiceCallback")]},
                "482913",
                what=("verification code"),
            )

    def test_a_callback_with_no_name_is_still_readable(self) -> None:
        assert _callback_name({"type": "HiddenValueCallback"}) == ""

    def test_setting_a_value_on_a_callback_with_no_input_is_harmless(self) -> None:
        _set({"type": "PasswordCallback"}, "x")


class TestReachingTheCode:
    """async_begin, with the network replaced by a scripted journey."""

    def script(self, login: JlrLogin, steps: list[dict]) -> list[dict]:
        posted: list[dict] = []

        async def seed() -> None:
            return None

        async def authenticate(payload: dict) -> dict:
            posted.append(payload)
            return steps.pop(0)

        login._async_seed_authorize = seed
        login._async_authenticate = authenticate
        return posted

    async def test_it_stops_at_the_code_collector(self, login) -> None:
        self.script(
            login,
            [
                {"stage": "Password", "callbacks": [callback("PasswordCallback")]},
                {
                    "stage": STAGE_OTP_COLLECT,
                    "callbacks": [callback("PasswordCallback")],
                },
            ],
        )
        await login.async_begin(PASSWORD)
        assert login._callbacks["stage"] == STAGE_OTP_COLLECT

    async def test_an_account_with_no_code_finishes_early(self, login) -> None:
        # Not every account is enrolled in the emailed-code journey.
        self.script(login, [{"tokenId": "a-session", "callbacks": []}])
        await login.async_begin(PASSWORD)
        assert login._callbacks["tokenId"] == "a-session"

    async def test_a_journey_that_never_arrives_is_reported(self, login) -> None:
        # Rather than looping forever against JLR's servers.
        self.script(
            login,
            [{"stage": "Password", "callbacks": [callback("PasswordCallback")]}] * 50,
        )
        with pytest.raises(JlrLoginError, match="did not reach the verification"):
            await login.async_begin(PASSWORD)

    async def test_the_password_is_actually_sent(self, login) -> None:
        posted = self.script(
            login,
            [
                {"stage": "Password", "callbacks": [callback("PasswordCallback")]},
                {"stage": STAGE_OTP_COLLECT, "callbacks": []},
            ],
        )
        await login.async_begin(PASSWORD)
        assert answered(posted[-1]) == [PASSWORD]


class TestSubmittingTheCode:
    async def test_submitting_before_starting_is_refused(self, login) -> None:
        with pytest.raises(JlrLoginError, match="not started"):
            await login.async_complete("482913")

    async def test_a_wrong_code_can_be_retyped(self, login) -> None:
        # The journey re-presents the same collector rather than erroring, so
        # "still asking" is how a rejected code looks.
        login._callbacks = {
            "stage": STAGE_OTP_COLLECT,
            "callbacks": [callback("PasswordCallback")],
        }

        async def authenticate(payload: dict) -> dict:
            return {"stage": STAGE_OTP_COLLECT, "callbacks": payload["callbacks"]}

        login._async_authenticate = authenticate
        with pytest.raises(JlrInvalidCode):
            await login.async_complete("000000")
        # Still alive: the user gets another go without a fresh code.
        assert login._callbacks["stage"] == STAGE_OTP_COLLECT

    async def test_a_journey_that_moved_elsewhere_is_a_dead_session(
        self, login
    ) -> None:
        login._callbacks = {
            "stage": STAGE_OTP_COLLECT,
            "callbacks": [callback("PasswordCallback")],
        }

        async def authenticate(payload: dict) -> dict:
            return {"stage": "Username", "callbacks": []}

        login._async_authenticate = authenticate
        with pytest.raises(JlrSessionExpired):
            await login.async_complete("482913")

    async def test_a_good_code_is_traded_for_tokens(self, login) -> None:
        login._callbacks = {
            "stage": STAGE_OTP_COLLECT,
            "callbacks": [callback("PasswordCallback")],
        }

        async def authenticate(payload: dict) -> dict:
            return {"tokenId": "an-am-session"}

        async def authorize(token_id: str) -> str:
            assert token_id == "an-am-session"
            return "an-auth-code"

        async def exchange(code: str) -> dict:
            assert code == "an-auth-code"
            return {"access_token": "a", "refresh_token": "b"}

        login._async_authenticate = authenticate
        login._async_authorize = authorize
        login._async_exchange = exchange

        assert await login.async_complete("482913") == {
            "access_token": "a",
            "refresh_token": "b",
        }
        # Kept because it is also the SSOSession cookie value.
        assert login._token_id == "an-am-session"


def unstarted(status: int, payload, text: str = "") -> JlrLogin:
    """A login with a scripted reply and no real session.

    Built without __init__ on purpose: that opens an aiohttp session, and
    swapping it out afterwards leaks the original — which this suite treats
    as an error, correctly.
    """
    made = JlrLogin.__new__(JlrLogin)
    made._stage = "start"
    made._session = FakeSession(FakeResponse(status, payload, text=text))
    return made


class TestWhatJlrSendsBack:
    """_async_authenticate's mapping of a reply to an exception."""

    async def test_a_step_is_returned_and_remembered(self) -> None:
        login = unstarted(200, {"stage": "Password", "callbacks": []})
        assert (await login._async_authenticate({}))["stage"] == "Password"
        assert login._stage == "Password"

    async def test_bad_credentials_are_not_a_dead_session(self) -> None:
        login = unstarted(401, {"message": "Authentication Failed"})
        with pytest.raises(JlrLoginError) as raised:
            await login._async_authenticate({})
        assert not isinstance(raised.value, JlrSessionExpired)

    @pytest.mark.parametrize(
        "message", ["Session has timed out", "Login session expired"]
    )
    async def test_a_lapsed_session_says_so(self, message: str) -> None:
        # It has to be told apart from a bad password: one means retype the
        # code, the other means start again from the beginning.
        login = unstarted(401, {"message": message})
        with pytest.raises(JlrSessionExpired):
            await login._async_authenticate({})

    async def test_a_body_that_is_not_json_is_reported_with_its_text(self) -> None:
        login = unstarted(503, ValueError("not json"), text="<html>down</html>")
        with pytest.raises(JlrLoginError, match="non-JSON"):
            await login._async_authenticate({})

    async def test_any_other_status_carries_the_step_it_failed_at(self) -> None:
        login = unstarted(500, {})
        login._stage = "Password"
        with pytest.raises(JlrLoginError, match="Password"):
            await login._async_authenticate({})

    async def test_a_network_failure_is_reported_as_unreachable(self) -> None:
        class Broken:
            def post(self, *args: object, **kwargs: object):
                raise aiohttp.ClientError("connection reset")

        login = JlrLogin.__new__(JlrLogin)
        login._stage = "start"
        login._session = Broken()
        with pytest.raises(JlrLoginError, match="could not reach"):
            await login._async_authenticate({})

"""ForgeRock OIDC login for Jaguar Land Rover InControl.

JLR edge-blocked the legacy IFAS password grant, so the bearer token now comes
from the app's ForgeRock client. Getting one is interactive: the journey ends
with an 8-digit code emailed to the account holder, which means login cannot be
headless. Once through, the rotating refresh token keeps things running without
further interaction (see ``JlrClient.async_ensure_token``) — the code is only
needed at setup, or if the refresh token dies.

The journey is a callback chain: POST an empty body, get back a list of
callbacks to fill in, POST it back, repeat. Rather than hard-coding the exact
sequence, ``_fill`` answers whatever callbacks it recognises and the loop runs
until the journey asks for the emailed code. A journey that differs (a
registered passkey, say) fails with a clear error instead of silently filling
the wrong field.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

from .const import (
    ACCESS_TOKEN_URL,
    AUTH_API_VERSION,
    AUTH_MAX_STEPS,
    AUTHENTICATE_URL,
    AUTHORIZE_URL,
    IAM_CLIENT_ID,
    IAM_REDIRECT_URI,
    IAM_SCOPES,
    IDENTITY_HOST,
    SESSION_COOKIE,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

# Journey stage that collects the emailed code — where phase one stops.
STAGE_OTP_COLLECT = "customer-otp-collector"


class JlrLoginError(Exception):
    """Raised when the ForgeRock journey cannot be completed."""


class JlrInvalidCode(JlrLoginError):
    """Raised when the emailed verification code is rejected.

    Recoverable: the journey is still alive, so the user can retype the code.
    """


class JlrSessionExpired(JlrLoginError):
    """Raised when the sign-in session died before the journey finished.

    Not recoverable: JLR's sign-in session is short-lived and often expires
    while the user is fetching the code from their inbox. Nothing entered from
    this point can succeed — the whole journey has to start again.
    """


def _pkce() -> tuple[str, str]:
    """Return a PKCE (verifier, S256 challenge) pair."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


class JlrLogin:
    """Drives one interactive ForgeRock login.

    Owns its own session and cookie jar: the journey pins server affinity
    across several cookies, and Home Assistant's shared session must neither be
    polluted with them nor closed by us. Call ``async_close`` when finished or
    abandoned.
    """

    def __init__(self, username: str) -> None:
        # A private session, not one of Home Assistant's: the journey needs its
        # own cookie jar, and HA warns if an integration closes a session it
        # manages. We create it, we close it.
        self._session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
        self._username = username
        self._verifier, self._challenge = _pkce()
        self._state = secrets.token_urlsafe(16)
        self._nonce = secrets.token_urlsafe(16)
        self._callbacks: dict[str, Any] | None = None
        self._stage = "start"
        # The AM session id the journey ends with. Captured because it is also
        # the value of the SSOSession cookie — see session_cookies.
        self._token_id: str | None = None

    def session_cookies(self) -> dict[str, str]:
        """The ForgeRock session cookies this journey established.

        The owner web portal — the only surviving source of location and the
        real vehicle names — authenticates against the AM session rather than a
        bearer token, and there is no way to mint one of these from a refresh
        token. So the jar is harvested here, before it is thrown away, and the
        cookies are persisted with the entry.

        Every cookie on the identity host is taken, whatever its path, and not
        one picked by name. The session itself is ``SSOSession``, but sending it
        alone lands on a node that has never heard of the session: AM is behind
        a load balancer and ``lbcookie`` is what routes the request to the node
        holding it, while ``INGRESSCOOKIE`` and ``IG_SESSIONID`` route within
        the gateway and are scoped to ``/gateway``. Missing any of them produces
        a redirect to the login page — identical to an expired session, and
        exactly as misleading.
        """
        host = urlparse(IDENTITY_HOST).hostname or ""
        cookies = {
            cookie.key: cookie.value
            for cookie in self._session.cookie_jar
            if host.endswith(cookie["domain"].lstrip(".") or host)
        }
        # SSOSession's value is the tokenId the journey just returned — the same
        # session, delivered twice. Deriving it means a jar that missed the
        # Set-Cookie cannot silently cost us the one cookie that matters.
        if self._token_id:
            cookies.setdefault(SESSION_COOKIE, self._token_id)
        return cookies

    async def async_close(self) -> None:
        """Drop the cookie jar and its session."""
        if not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------ phase 1
    async def async_begin(self, password: str) -> None:
        """Run the journey up to the emailed code, which it triggers.

        Raises JlrLoginError if the credentials are refused or the journey
        takes a shape we don't recognise.
        """
        # Hit /authorize first: it seeds OAUTH_REQUEST_ATTRIBUTES and pins
        # server affinity. Without it /authorize later bounces back to the
        # login UI intermittently.
        await self._async_seed_authorize()

        payload: dict[str, Any] = {}
        for _ in range(AUTH_MAX_STEPS):
            data = await self._async_authenticate(payload)
            _LOGGER.debug(
                "sign-in stage %s: %s",
                data.get("stage") or "-",
                [c.get("type") for c in data.get("callbacks", [])],
            )
            if data.get("tokenId"):
                # No code was demanded — nothing to collect.
                self._callbacks = data
                return
            stage = data.get("stage", "")
            if stage == STAGE_OTP_COLLECT:
                self._callbacks = data
                return
            self._fill(data, password)
            payload = data
        raise JlrLoginError(
            f"the JLR login journey did not reach the verification step (stopped "
            f"at '{self._stage}'); it may have changed, or this account may use a "
            "sign-in method this integration does not support"
        )

    # ------------------------------------------------------------------ phase 2
    async def async_complete(self, code: str) -> dict[str, Any]:
        """Submit the emailed code and exchange the session for tokens."""
        if self._callbacks is None:
            raise JlrLoginError("login was not started")

        data = self._callbacks
        if not data.get("tokenId"):
            self._set_password_callback(data, code, what="verification code")
            data = await self._async_authenticate(data)

        token_id = data.get("tokenId")
        if not token_id:
            # A wrong code re-presents the same collector stage rather than
            # erroring, so treat "still asking" as a rejected code.
            if data.get("stage") == STAGE_OTP_COLLECT:
                self._callbacks = data
                raise JlrInvalidCode("that verification code was not accepted")
            # Anything else means the journey moved somewhere we can't continue
            # from — in practice the session having lapsed and JLR rewinding us.
            raise JlrSessionExpired(
                f"JLR did not return a session after the code (at sign-in step "
                f"'{self._stage}')"
            )

        self._token_id = token_id
        auth_code = await self._async_authorize(token_id)
        return await self._async_exchange(auth_code)

    # ------------------------------------------------------------------ helpers
    def _authorize_params(self) -> dict[str, str]:
        return {
            "client_id": IAM_CLIENT_ID,
            "redirect_uri": IAM_REDIRECT_URI,
            "response_type": "code",
            "scope": IAM_SCOPES,
            "state": self._state,
            "nonce": self._nonce,
            "code_challenge": self._challenge,
            "code_challenge_method": "S256",
        }

    async def _async_seed_authorize(self) -> None:
        """Prime the cookie jar. The redirect to the login UI is expected."""
        try:
            async with self._session.get(
                AUTHORIZE_URL,
                params=self._authorize_params(),
                headers={"User-Agent": USER_AGENT},
                allow_redirects=False,
            ):
                pass
        except aiohttp.ClientError as err:
            raise JlrLoginError(f"could not reach JLR sign-in: {err}") from err

    async def _async_authenticate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST one step of the callback chain."""
        try:
            async with self._session.post(
                AUTHENTICATE_URL,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept-API-Version": AUTH_API_VERSION,
                    "User-Agent": USER_AGENT,
                },
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    text = (await resp.text())[:400]
                    raise JlrLoginError(
                        f"JLR sign-in returned {resp.status} with a non-JSON "
                        f"body: {' '.join(text.split())}"
                    ) from None
                if resp.status == 401:
                    message = str(body.get("message") or "rejected")
                    detail = f"{message} (at sign-in step '{self._stage}')"
                    if "time" in message.lower() or "session" in message.lower():
                        raise JlrSessionExpired(detail)
                    raise JlrLoginError(detail)
                if resp.status != 200 or not isinstance(body, dict):
                    raise JlrLoginError(
                        f"JLR sign-in returned {resp.status} "
                        f"(at sign-in step '{self._stage}')"
                    )
                self._stage = body.get("stage") or self._stage
                return body
        except aiohttp.ClientError as err:
            raise JlrLoginError(f"could not reach JLR sign-in: {err}") from err

    def _fill(self, data: dict[str, Any], password: str) -> None:
        """Answer every callback in this step that we recognise, in place."""
        stage = data.get("stage", "")
        answered = False
        for callback in data.get("callbacks", []):
            kind = callback.get("type")
            name = _callback_name(callback)
            if kind == "SelectIdPCallback":
                _set(callback, "localAuthentication")
            elif kind == "ValidatedCreateUsernameCallback":
                _set(callback, self._username)
            elif kind == "HiddenValueCallback" and (
                "webauthn" in name.lower() or "webauthn" in stage.lower()
            ):
                # "unsupported" is what the app sends when the user dismisses
                # the passkey prompt; it is what drops us to the password step.
                _set(callback, "unsupported")
            elif kind == "PasswordCallback":
                _set(callback, password)
            elif kind in ("ChoiceCallback", "ConfirmationCallback"):
                # Every choice we meet (login vs register, continue vs resend)
                # wants the first option.
                _set(callback, 0)
            elif kind == "TextOutputCallback":
                # Informational only — posted back untouched.
                continue
            else:
                continue
            answered = True
        if not answered and not data.get("callbacks"):
            raise JlrLoginError(f"JLR sign-in stalled at an empty step ({stage})")

    @staticmethod
    def _set_password_callback(data: dict[str, Any], value: str, *, what: str) -> None:
        for callback in data.get("callbacks", []):
            if callback.get("type") == "PasswordCallback":
                _set(callback, value)
                return
        raise JlrLoginError(f"JLR sign-in did not ask for a {what}")

    async def _async_authorize(self, token_id: str) -> str:
        """Trade the AM session for an authorization code."""
        params = {**self._authorize_params(), "decision": "allow", "csrf": token_id}
        try:
            async with self._session.get(
                AUTHORIZE_URL,
                params=params,
                headers={"User-Agent": USER_AGENT},
                allow_redirects=False,
            ) as resp:
                location = resp.headers.get("Location", "")
        except aiohttp.ClientError as err:
            raise JlrLoginError(f"could not reach JLR sign-in: {err}") from err

        # The redirect targets the app's custom scheme, which we parse rather
        # than follow.
        code = parse_qs(urlparse(location).query).get("code", [None])[0]
        if not code:
            raise JlrLoginError("JLR did not issue an authorization code after sign-in")
        return code

    async def _async_exchange(self, code: str) -> dict[str, Any]:
        """Swap the authorization code for tokens."""
        body = urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": IAM_REDIRECT_URI,
                "client_id": IAM_CLIENT_ID,
                "code_verifier": self._verifier,
            }
        )
        try:
            async with self._session.post(
                ACCESS_TOKEN_URL,
                data=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": USER_AGENT,
                },
            ) as resp:
                tokens = await resp.json(content_type=None)
                if resp.status != 200 or not isinstance(tokens, dict):
                    detail = ""
                    if isinstance(tokens, dict):
                        detail = f": {tokens.get('error', '')} {tokens.get('error_description', '')}"
                    raise JlrLoginError(
                        f"JLR token exchange returned {resp.status}{detail}".strip()
                    )
                return tokens
        except aiohttp.ClientError as err:
            raise JlrLoginError(f"could not reach JLR sign-in: {err}") from err


# ForgeRock labels a callback under different output keys depending on its
# type: "id" for HiddenValueCallback, "prompt" for choices and inputs, "name"
# for some others. Reading only one of them silently finds nothing.
_NAME_KEYS = ("id", "name", "prompt")


def _callback_name(callback: dict[str, Any]) -> str:
    """The callback's ForgeRock identifier, whichever key it arrived under."""
    for key in _NAME_KEYS:
        for output in callback.get("output", []):
            if output.get("name") == key:
                return str(output.get("value", ""))
    return ""


def _set(callback: dict[str, Any], value: Any) -> None:
    """Write a value into a callback's first input slot."""
    inputs = callback.get("input")
    if inputs:
        inputs[0]["value"] = value

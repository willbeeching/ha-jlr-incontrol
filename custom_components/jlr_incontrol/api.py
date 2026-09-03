"""Async client for the Jaguar Land Rover "webview" backend.

The flow:

    ForgeRock OIDC token (see auth.py) -> device registration (IFOP) -> userId +
    vehicles (IF9 webview).

Only the token source is new: JLR edge-blocked the legacy IFAS password grant in
August 2026. Everything from device registration onward is unchanged and still
validated live.

The ``/if9/webview/*`` API is fronted by a browser-style edge that accepts a
plain bearer token as long as the request carries the webview ``Origin`` /
``Referer`` headers and a registered ``X-Device-Id`` / ``clientId``. That used to
be enough for everything. Since late August 2026 it is only enough for the
identity and vehicle-list endpoints: the per-vehicle reads (status, attributes,
position) and the command endpoints now demand Approov attestation as well and
answer 498 without it.

Vehicle data comes from telemetry.py over the websocket, and location and
vehicle names from portal.py, neither of which is attested. What is left here is
auth, device registration, the vehicle list and the attributes attempt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp

from .const import (
    ACCESS_TOKEN_URL,
    APPROOV_HINT,
    BROWSER_HEADERS,
    DIAGNOSTIC_HEADERS,
    FORBIDDEN_HINT,
    IAM_CLIENT_ID,
    IDENTITY_ALIASES,
    IF9_BASE,
    IFOP_BASE,
    MEDIA_HEALTHSTATUS,
    MEDIA_JSON,
    MEDIA_USER,
    TELEMATICS_PROGRAM,
    TOKEN_RENEW_MARGIN_MAX,
    TOKEN_RENEW_MARGIN_MIN,
    TOKEN_RENEW_RATIO,
    USER_AGENT,
    VIN_BRANDS,
)
from .redact import scrub_text, vehicle_label

_LOGGER = logging.getLogger(__name__)

# Hard per-request ceiling. HA's shared session has no useful default (aiohttp's
# 300s total), and the IF9 edge can stall indefinitely on endpoints its backends
# no longer serve (seen live with the legacy /trips endpoint, which negotiates
# media types but then 504s), which hangs the first refresh until HA cancels the
# whole config entry setup.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class JlrAuthError(Exception):
    """Raised when authentication fails (bad credentials)."""


class JlrApiError(Exception):
    """Raised when a backend request fails."""


def _retry_after(header: str | None) -> float | None:
    """Seconds to wait, from a Retry-After header given as a delay."""
    if not header:
        return None
    try:
        seconds = float(header.strip())
    except ValueError:
        # The HTTP-date form is legal but JLR do not use it, and guessing at a
        # date parse here would be inventing behaviour we cannot test.
        return None
    return seconds if seconds >= 0 else None


class JlrRateLimitError(JlrApiError):
    """Raised when JLR asks us to slow down.

    Deliberately not an auth error. Every non-200 from the token endpoint used
    to mean "sign in again", so a rate limit or an outage at JLR sent the user
    off to find an emailed code that could not have helped — the one thing this
    integration works hardest to avoid asking for.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class JlrConnectionError(JlrApiError):
    """Raised when a request never reached JLR at all.

    DNS failures, refused connections and timeouts — as opposed to JLR
    answering and refusing. Worth separating, because "we couldn't reach the
    servers" and "the servers said no" send someone looking in completely
    different places (seen live: a DNS timeout on jlrmotor.com reported as the
    API rejecting a token, #10).
    """


def identity_fields(source: dict[str, Any]) -> dict[str, Any]:
    """Pull naming fields out of any payload, under whichever spelling.

    Several endpoints could name a vehicle and they do not agree on key names,
    so normalise onto the ones the entity layer reads rather than teaching the
    entity layer every variant.
    """
    found: dict[str, Any] = {}
    for canonical, aliases in IDENTITY_ALIASES.items():
        for alias in aliases:
            value = source.get(alias)
            if value not in (None, ""):
                found[canonical] = value
                break
    return found


def brand_from_vin(vin: str) -> dict[str, str]:
    """The one thing a VIN tells us for certain: which marque built it.

    Enough to stop a Jaguar being labelled "Land Rover" when the endpoint that
    knows better is blocked. The model is deliberately not guessed from the
    remaining characters — a confidently wrong model is worse than none.
    """
    brand = VIN_BRANDS.get(vin[:3].upper())
    return {"vehicleBrand": brand} if brand else {}


def flatten_status(payload: dict[str, Any]) -> dict[str, str]:
    """Flatten the coreStatus/evStatus key/value lists into a single dict.

    Shared by the REST status read and the telemetry socket: the VHS payload
    pushed over the websocket has exactly the same shape as the one the REST
    /status endpoint used to return, which is why the sensor layer above did
    not have to change at all when the reads moved.

    Some vehicles never report a LAST_UPDATED_TIME status key; when the raw
    items carry per-item lastUpdatedTime fields, synthesise it from the newest
    one so freshness isn't pinned to the (static while parked) position
    timestamp.
    """
    status: dict[str, str] = {}
    newest_item_ts = ""
    vehicle_status = payload.get("vehicleStatus", payload)
    for group in ("coreStatus", "evStatus"):
        for item in vehicle_status.get(group, []) or []:
            key = item.get("key")
            if key is not None:
                status[key] = item.get("value")
            item_ts = item.get("lastUpdatedTime")
            # ISO timestamps in a consistent format sort lexicographically.
            if isinstance(item_ts, str) and item_ts > newest_item_ts:
                newest_item_ts = item_ts
    # Per-item timestamps are authoritative: on cars that do report a
    # LAST_UPDATED_TIME key it can lag the individual values (the "frozen
    # last_updated" from the first field reports).
    existing = status.get("LAST_UPDATED_TIME") or ""
    if newest_item_ts and newest_item_ts > existing:
        status["LAST_UPDATED_TIME"] = newest_item_ts
    return status


class JlrClient:
    """Talks to the JLR webview backend on behalf of one account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str | None = None,
        device_id: str | None = None,
        user_id: str | None = None,
        refresh_token: str | None = None,
        on_tokens: Callable[[], None] | None = None,
    ) -> None:
        self._session = session
        # Called the instant a token set is adopted. JLR rotate the refresh
        # token on every use and the old one dies immediately, so whoever owns
        # persistence has to hear about it now, not on the next poll.
        self._on_tokens = on_tokens
        # Two things renew tokens now — the telemetry socket before each
        # reconnect and the housekeeping poll — and a rotating single-use
        # refresh token cannot survive two callers spending it at once.
        self._token_lock = asyncio.Lock()
        self._username = username
        self._password = password
        # A stable per-install device UUID, generated once and persisted in the entry.
        self._device_id = device_id or str(uuid.uuid4())
        self._user_id = user_id
        self._access_token: str | None = None
        # Seeded from the entry so a restart refreshes rather than re-logging in.
        self._refresh_token: str | None = refresh_token
        self._expires_at: float = 0.0
        self._device_registered = False

    @property
    def device_id(self) -> str:
        """The stable device id used for this client (persist it in the entry)."""
        return self._device_id

    @property
    def username(self) -> str:
        """The account this client is signed in as."""
        return self._username

    @property
    def access_token(self) -> str | None:
        """The current bearer, for callers that authenticate outside _request."""
        return self._access_token

    def seconds_until_renewal(self) -> float:
        """How long the current access token is still good for, in seconds.

        The telemetry socket binds its STOMP session to the bearer presented at
        CONNECT, so it needs to know when to reconnect rather than waiting to be
        disconnected.
        """
        if not self._access_token:
            return 0.0
        return max(0.0, self._expires_at - time.monotonic())

    @property
    def user_id(self) -> str | None:
        """The resolved IF9 user id (persist it in the entry)."""
        return self._user_id

    @property
    def refresh_token(self) -> str | None:
        """The current refresh token (persist it; JLR rotates these)."""
        return self._refresh_token

    # ------------------------------------------------------------------ auth
    def apply_tokens(self, tokens: dict[str, Any]) -> None:
        """Adopt a freshly minted ForgeRock token set."""
        self._access_token = tokens["access_token"]
        self._refresh_token = tokens.get("refresh_token", self._refresh_token)
        expires_in = int(tokens.get("expires_in", 300))
        # Renew proportionally early. The ForgeRock token only lives ~5 minutes,
        # so a fixed margin bigger than the lifetime would treat every token as
        # already expired and refresh on every request.
        margin = min(
            TOKEN_RENEW_MARGIN_MAX,
            max(TOKEN_RENEW_MARGIN_MIN, expires_in * TOKEN_RENEW_RATIO),
        )
        self._expires_at = time.monotonic() + expires_in - margin
        # A new token means the device may need re-registering.
        self._device_registered = False
        if self._on_tokens is not None:
            self._on_tokens()

    async def _refresh(self) -> None:
        """Renew the access token using the (rotating) refresh token."""
        if not self._refresh_token:
            raise JlrAuthError("no refresh token available; sign in again")
        body = urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": IAM_CLIENT_ID,
            }
        )
        what = "token refresh"
        status, tokens = await self._request(
            "POST",
            ACCESS_TOKEN_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": MEDIA_JSON,
                "User-Agent": USER_AGENT,
            },
            data=body,
            what=what,
        )
        if status != 200 or not isinstance(tokens, dict):
            error = ""
            if isinstance(tokens, dict):
                error = str(tokens.get("error") or "")
            if error == "invalid_grant" or status in (401, 403):
                # Genuinely spent: the refresh token is dead or already
                # rotated, and only a fresh sign-in can replace it.
                raise JlrAuthError(
                    f"{what} was refused ({error or status}); sign in again"
                )
            # Everything else — a 5xx, a gateway hiccup, a malformed reply —
            # says nothing about whether the credentials are still good, and
            # an emailed code would not fix it. Fail temporarily so the caller
            # backs off and tries again.
            raise JlrApiError(
                f"{what} returned {status}"
                f"{f' ({error})' if error else ''}; will retry"
            )
        self.apply_tokens(tokens)

    async def async_ensure_token(self) -> None:
        """Refresh the access token if it is missing or near expiry.

        Serialised. The refresh token is single-use and rotates, so two callers
        renewing at once means the second spends a token JLR has already
        retired: it answers 400, which surfaces as "sign in again" and costs the
        user a fresh emailed code for no reason.
        """
        if self._access_token and time.monotonic() < self._expires_at:
            return
        async with self._token_lock:
            # Re-check inside the lock: whoever held it may have just renewed.
            if self._access_token and time.monotonic() < self._expires_at:
                return
            await self._refresh()

    # -------------------------------------------------------- device / identity
    async def async_register_device(self) -> None:
        """Register this device with IFOP (idempotent; -> 204)."""
        if self._device_registered:
            return
        headers = {
            **BROWSER_HEADERS,
            "Authorization": f"Bearer {self._access_token}",
            "X-Device-Id": self._device_id,
            "Accept": "*/*",
            "Content-Type": MEDIA_JSON,
            "x-telematicsprogramtype": TELEMATICS_PROGRAM,
        }
        body = {
            "access_token": self._access_token,
            # ForgeRock issues no separate authorization token.
            "authorization_token": None,
            "expires_in": 300,
            "deviceID": self._device_id,
        }
        status, _ = await self._request(
            "POST",
            f"{IFOP_BASE}/users/{quote(self._username)}/clients",
            headers=headers,
            data=json.dumps(body),
            what="device registration",
        )
        if status not in (200, 204):
            raise JlrApiError(f"device registration returned {status}")
        self._device_registered = True

    async def async_get_user_id(self) -> str:
        """Resolve and cache the numeric IF9 user id."""
        status, payload = await self._request(
            "GET",
            f"{IF9_BASE}/users?loginName={quote(self._username)}",
            headers=self._webview_headers(MEDIA_USER),
            what="user lookup",
        )
        if status != 200:
            raise self._error("user lookup", status)
        self._user_id = (payload or {}).get("userId")
        if not self._user_id:
            raise JlrApiError("user lookup did not return a userId")
        return self._user_id

    async def async_connect(self) -> None:
        """Ensure a valid token, a registered device, and a known user id."""
        await self.async_ensure_token()
        await self.async_register_device()
        if not self._user_id:
            await self.async_get_user_id()

    # --------------------------------------------------------------- vehicles
    async def async_get_vehicles(self) -> list[dict[str, Any]]:
        """Return the account's vehicles (uses application/json; vnd.* 406s here)."""
        await self.async_connect()
        status, payload = await self._request(
            "GET",
            f"{IF9_BASE}/users/{self._user_id}/vehicles",
            headers=self._webview_headers(MEDIA_JSON),
            what="vehicle list",
        )
        if status != 200:
            raise self._error("vehicle list", status)
        return (payload or {}).get("vehicles", [])

    def _identity_urls(self, vin: str) -> tuple[tuple[str, str], ...]:
        """Endpoints that might name the vehicle, best first.

        The Approov rule looks like it sits on ``/vehicles/{vin}/*`` — the
        vehicle list and the identity lookups, which are rooted at ``/users/``,
        still answer 200 while everything under a VIN answers 498. The real
        attributes endpoint is tried first because it is the richest source and
        costs nothing the day JLR lift the wall; the ``/users/``-rooted paths
        after it are candidates on that theory rather than endpoints anyone has
        seen work, which is why every attempt is logged with what it returned.
        """
        return (
            ("attributes", f"{IF9_BASE}/vehicles/{vin}/attributes"),
            (
                "attributes (via user)",
                f"{IF9_BASE}/users/{self._user_id}/vehicles/{vin}/attributes",
            ),
            (
                "vehicle record (via user)",
                f"{IF9_BASE}/users/{self._user_id}/vehicles/{vin}",
            ),
        )

    async def async_get_attributes(self, vin: str) -> dict[str, Any]:
        """Return whatever identity/capability attributes can still be had.

        Raises the last failure only if nothing anywhere answered, so a walled
        primary endpoint does not hide a working alternative.
        """
        await self.async_connect()
        last_error: JlrApiError | None = None
        for what, url in self._identity_urls(vin):
            try:
                status, payload = await self._request(
                    "GET", url, headers=self._webview_headers(MEDIA_JSON), what=what
                )
            except JlrApiError as err:
                last_error = err
                continue
            if status != 200 or not isinstance(payload, dict):
                last_error = self._error(what, status)
                continue
            identity = identity_fields(payload)
            if identity:
                _LOGGER.debug("identity for %s came from %s", vehicle_label(vin), what)
                # Keep the whole payload when it is the real attributes
                # document: capability flags live alongside the names.
                return {**payload, **identity}
            _LOGGER.debug(
                "%s answered 200 for %s but named nothing; keys=%s",
                what,
                vin,
                sorted(payload)[:20],
            )
        if last_error:
            raise last_error
        return {}

    async def async_get_status(self, vin: str) -> dict[str, Any]:
        """Return the flattened vehicle status ({key: value} from coreStatus/evStatus)."""
        status, payload = await self._request(
            "GET",
            f"{IF9_BASE}/vehicles/{vin}/status",
            headers=self._webview_headers(MEDIA_HEALTHSTATUS),
            what="status",
        )
        if status != 200:
            raise self._error("status", status)
        return flatten_status(payload or {})

    async def async_get_position(self, vin: str) -> dict[str, Any]:
        """Return the vehicle position ({latitude, longitude, timestamp, ...})."""
        status, payload = await self._request(
            "GET",
            f"{IF9_BASE}/vehicles/{vin}/position",
            headers=self._webview_headers(MEDIA_JSON),
            what="position",
        )
        if status != 200:
            raise self._error("position", status)
        return (payload or {}).get("position", {})

    # NOTE: there is no charge-profile READ endpoint — verified against a live
    # charging BEV (GET /chargeProfile: 406 for every media type, 204 empty for
    # */*) and confirmed in the app's code, which only has charge WRITES. The
    # charge override state rides in /status as EV_CHARGE_NOW_SETTING.

    # NOTE: there is deliberately no trips/journeys support. The /trips endpoint
    # is routed on the webview edge (wrong Accept -> JBoss 406) but the legacy
    # backend behind it never answers with the correct triplist-v2 media type —
    # it 504s after ~70s. The modern app dropped trips entirely (no trip
    # endpoints in its JS bundle) and the old direct /if9/jlr/ path is behind
    # the Approov wall, so there is nothing reliable to build on.

    # Remote commands used to live here: a PIN-gated authenticate followed by
    # a start-service POST. JLR put app attestation (Approov) in front of both
    # in August 2026 and every alternative was checked — the telemetry broker
    # accepts nothing but acknowledgements, and the owner web portal offers no
    # remote control at all. There is no read-style way round attestation, so
    # the code is gone rather than kept as something that can only fail.

    # ----------------------------------------------------------------- helpers
    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        data: str | None = None,
        what: str,
    ) -> tuple[int, Any]:
        """Run a request with a hard timeout; return (status, parsed JSON or None).

        Transport failures and stalls surface as JlrApiError so callers (and the
        coordinator's per-endpoint best-effort fetches) never see raw aiohttp
        errors or hang past REQUEST_TIMEOUT.
        """
        try:
            async with self._session.request(
                method, url, headers=headers, data=data, timeout=REQUEST_TIMEOUT
            ) as resp:
                payload: Any = None
                if resp.status != 204:
                    try:
                        payload = await resp.json()
                    except (aiohttp.ContentTypeError, ValueError):
                        payload = None
                if resp.status >= 400:
                    await self._log_error_response(resp, what, payload)
                if resp.status == 429:
                    raise JlrRateLimitError(
                        f"{what} was rate limited by Jaguar Land Rover",
                        _retry_after(resp.headers.get("Retry-After")),
                    )
                return resp.status, payload
        except TimeoutError as err:
            raise JlrConnectionError(
                f"{what} timed out after {REQUEST_TIMEOUT.total:.0f}s"
            ) from err
        except aiohttp.ClientError as err:
            raise JlrConnectionError(f"{what} failed: {err}") from err

    async def _log_error_response(
        self, resp: aiohttp.ClientResponse, what: str, payload: Any
    ) -> None:
        """Debug-log what a failing response actually said.

        A refusal from JLR's own API and one from an edge/WAF appliance in front
        of it can carry the same status code, and the parser above drops any
        non-JSON body — so a 403 arrived as nothing but "returned 403" with no
        way to tell the two apart. Capture the body and the headers that
        identify the responder.
        """
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return
        if payload is not None:
            body = json.dumps(payload)
        else:
            try:
                body = await resp.text()
            except (aiohttp.ClientError, UnicodeDecodeError):
                body = "<unreadable>"
        # Debug logs get pasted into public issues; never echo the password back
        # out if an error body happens to quote the request.
        if self._password and self._password in body:
            body = body.replace(self._password, "**REDACTED**")
        # Collapse whitespace: block pages are multi-line HTML, and a raw body
        # would put everything after the first line beyond the log entry.
        body = " ".join(body.split())
        seen = {
            name: resp.headers[name]
            for name in DIAGNOSTIC_HEADERS
            if name in resp.headers
        }
        _LOGGER.debug(
            "%s returned %s; headers=%s; body=%.500s",
            what,
            resp.status,
            seen,
            scrub_text(body.strip()) or "<empty>",
        )

    def _webview_headers(self, accept: str) -> dict[str, str]:
        return {
            **BROWSER_HEADERS,
            "Authorization": f"Bearer {self._access_token}",
            "X-Device-Id": self._device_id,
            # The edge literally expects a header named "clientId" equal to the device id.
            "clientId": self._device_id,
            "Accept": accept,
        }

    @staticmethod
    def _error(what: str, status: int) -> JlrApiError:
        if status == 498:
            return JlrApiError(APPROOV_HINT.format(what=what))
        if status == 401:
            return JlrAuthError(f"{what} returned 401 (token expired or invalid)")
        if status == 403:
            return JlrApiError(FORBIDDEN_HINT.format(what=what))
        return JlrApiError(f"{what} returned {status}")

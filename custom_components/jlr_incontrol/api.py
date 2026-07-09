"""Async client for the Jaguar Land Rover "webview" backend.

The whole flow below was validated live:

    password grant (IFAS) -> device registration (IFOP) -> userId + vehicles (IF9
    webview) -> per-vehicle status / position / attributes -> PIN-gated commands.

The trick that makes this work where the native-app IF9 host does not: the
``/if9/webview/*`` API is fronted by a browser-style edge that accepts a plain
bearer token as long as the request carries the webview ``Origin`` / ``Referer``
headers and a registered ``X-Device-Id`` / ``clientId``. That combination bypasses
the Approov attestation wall (HTTP 498) that blocks the app's IF9 host.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any
from urllib.parse import quote

import aiohttp

from .const import (
    BROWSER_HEADERS,
    IF9_BASE,
    IFAS_TOKENS_URL,
    IFOP_BASE,
    MEDIA_AUTHENTICATE,
    MEDIA_HEALTHSTATUS,
    MEDIA_JSON,
    MEDIA_SERVICE_STATUS,
    MEDIA_START_SERVICE,
    MEDIA_USER,
    SERVICE_ENDPOINTS,
    TELEMATICS_PROGRAM,
    TOKENS_BASIC_AUTH,
)

_LOGGER = logging.getLogger(__name__)


class JlrAuthError(Exception):
    """Raised when authentication fails (bad credentials)."""


class JlrApiError(Exception):
    """Raised when a backend request fails."""


class JlrClient:
    """Talks to the JLR webview backend on behalf of one account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        device_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        # A stable per-install device UUID, generated once and persisted in the entry.
        self._device_id = device_id or str(uuid.uuid4())
        self._user_id = user_id
        self._access_token: str | None = None
        self._authorization_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0
        self._device_registered = False

    @property
    def device_id(self) -> str:
        """The stable device id used for this client (persist it in the entry)."""
        return self._device_id

    @property
    def user_id(self) -> str | None:
        """The resolved IF9 user id (persist it in the entry)."""
        return self._user_id

    # ------------------------------------------------------------------ auth
    async def async_login(self) -> None:
        """Obtain tokens via the IFAS password grant. Validated live."""
        await self._token_request(
            {
                "grant_type": "password",
                "username": self._username,
                "password": self._password,
            }
        )

    async def _refresh(self) -> None:
        """Renew the access token using the refresh token."""
        if not self._refresh_token:
            raise JlrApiError("no refresh token available")
        await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": self._refresh_token}
        )

    async def _token_request(self, body: dict[str, str]) -> None:
        headers = {
            **BROWSER_HEADERS,
            "Authorization": TOKENS_BASIC_AUTH,
            "Content-Type": MEDIA_JSON,
            "Accept": MEDIA_JSON,
        }
        async with self._session.post(
            IFAS_TOKENS_URL, headers=headers, data=json.dumps(body)
        ) as resp:
            if resp.status != 200:
                raise JlrAuthError(
                    f"token request ({body['grant_type']}) returned {resp.status}"
                )
            tokens = await resp.json()
        self._access_token = tokens["access_token"]
        self._authorization_token = tokens.get("authorization_token")
        self._refresh_token = tokens.get("refresh_token", self._refresh_token)
        # Token lives 24h; renew a little early.
        self._expires_at = time.monotonic() + int(tokens.get("expires_in", 86400)) - 300
        # A new token means the device may need re-registering.
        self._device_registered = False

    async def async_ensure_token(self) -> None:
        """Refresh (or re-login) if the access token is missing or near expiry."""
        if self._access_token and time.monotonic() < self._expires_at:
            return
        if self._refresh_token:
            try:
                await self._refresh()
                return
            except (JlrAuthError, JlrApiError):
                _LOGGER.debug("token refresh failed, re-running password login")
        await self.async_login()

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
            "authorization_token": self._authorization_token,
            "expires_in": "86400",
            "deviceID": self._device_id,
        }
        async with self._session.post(
            f"{IFOP_BASE}/users/{quote(self._username)}/clients",
            headers=headers,
            data=json.dumps(body),
        ) as resp:
            if resp.status not in (200, 204):
                raise JlrApiError(f"device registration returned {resp.status}")
        self._device_registered = True

    async def async_get_user_id(self) -> str:
        """Resolve and cache the numeric IF9 user id."""
        async with self._session.get(
            f"{IF9_BASE}/users?loginName={quote(self._username)}",
            headers=self._webview_headers(MEDIA_USER),
        ) as resp:
            if resp.status != 200:
                raise self._error("user lookup", resp.status)
            self._user_id = (await resp.json()).get("userId")
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
        async with self._session.get(
            f"{IF9_BASE}/users/{self._user_id}/vehicles",
            headers=self._webview_headers(MEDIA_JSON),
        ) as resp:
            if resp.status != 200:
                raise self._error("vehicle list", resp.status)
            return (await resp.json()).get("vehicles", [])

    async def async_get_attributes(self, vin: str) -> dict[str, Any]:
        """Return the raw vehicle attributes (make / model / capabilities)."""
        async with self._session.get(
            f"{IF9_BASE}/vehicles/{vin}/attributes",
            headers=self._webview_headers(MEDIA_JSON),
        ) as resp:
            if resp.status != 200:
                raise self._error("attributes", resp.status)
            return await resp.json()

    async def async_get_status(self, vin: str) -> dict[str, Any]:
        """Return the flattened vehicle status ({key: value} from coreStatus/evStatus)."""
        async with self._session.get(
            f"{IF9_BASE}/vehicles/{vin}/status",
            headers=self._webview_headers(MEDIA_HEALTHSTATUS),
        ) as resp:
            if resp.status != 200:
                raise self._error("status", resp.status)
            payload = await resp.json()
        return self._flatten_status(payload)

    async def async_get_position(self, vin: str) -> dict[str, Any]:
        """Return the vehicle position ({latitude, longitude, timestamp, ...})."""
        async with self._session.get(
            f"{IF9_BASE}/vehicles/{vin}/position",
            headers=self._webview_headers(MEDIA_JSON),
        ) as resp:
            if resp.status != 200:
                raise self._error("position", resp.status)
            return (await resp.json()).get("position", {})

    @staticmethod
    def _flatten_status(payload: dict[str, Any]) -> dict[str, str]:
        """Flatten the coreStatus/evStatus key/value lists into a single dict."""
        status: dict[str, str] = {}
        vehicle_status = payload.get("vehicleStatus", {})
        for group in ("coreStatus", "evStatus"):
            for item in vehicle_status.get(group, []):
                key = item.get("key")
                if key is not None:
                    status[key] = item.get("value")
        return status

    # ---------------------------------------------------------------- commands
    #
    # The PIN-gated authenticate + start-service round trip was validated live: a
    # honk-and-flash (HBLF) returned HTTP 202 {"status":"Started"}. Lock/unlock/climate
    # use the identical flow with a different serviceName + endpoint, so they are
    # expected to work but have not each been individually actuated.
    async def async_send_command(
        self,
        vin: str,
        service_name: str,
        pin: str,
    ) -> dict[str, Any]:
        """Run a PIN-gated remote command via the classic two-step webview flow.

        1. authenticate {pin, serviceName} -> service token
        2. POST the service endpoint with {token} -> customerServiceId / status
        """
        await self.async_connect()
        endpoint = SERVICE_ENDPOINTS.get(service_name)
        if endpoint is None:
            raise JlrApiError(f"unknown service {service_name}")

        # a) get a one-time service token.
        auth_headers = self._webview_headers(MEDIA_JSON)
        auth_headers["Content-Type"] = MEDIA_AUTHENTICATE
        async with self._session.post(
            f"{IF9_BASE}/vehicles/{vin}/users/{self._user_id}/authenticate",
            headers=auth_headers,
            data=json.dumps({"pin": pin, "serviceName": service_name}),
        ) as resp:
            if resp.status not in (200, 201):
                raise self._error(f"authenticate ({service_name})", resp.status)
            token = (await resp.json()).get("token")
        if not token:
            raise JlrApiError(f"authenticate ({service_name}) returned no token")

        # b) start the service. The response Accept MUST be ServiceStatus-v4 (v5/json 406).
        svc_headers = self._webview_headers(MEDIA_SERVICE_STATUS)
        svc_headers["Content-Type"] = MEDIA_START_SERVICE
        async with self._session.post(
            f"{IF9_BASE}/vehicles/{vin}/{endpoint}",
            headers=svc_headers,
            data=json.dumps({"token": token}),
        ) as resp:
            if resp.status not in (200, 202):
                raise self._error(f"service {service_name}", resp.status)
            started = await resp.json()

        # The 202 only means "accepted/started" — the vehicle may still fail it
        # (e.g. Failed/timeout when the car is asleep). Poll to a terminal state so
        # failures surface to the user instead of looking like nothing happened.
        service_id = started.get("customerServiceId")
        if not service_id:
            return started
        return await self._await_service(vin, service_id, started)

    async def _await_service(
        self, vin: str, service_id: str, started: dict[str, Any]
    ) -> dict[str, Any]:
        """Poll a started service to a terminal state; raise on failure."""
        status = started
        for _ in range(10):  # ~30s of polling
            state = str(status.get("status", "")).lower()
            if state in ("successful", "success"):
                return status
            if state in ("failed", "aborted", "cancelled"):
                why = (
                    " / ".join(
                        p
                        for p in (
                            status.get("failureReason"),
                            status.get("failureDescription"),
                        )
                        if p
                    )
                    or "unknown"
                )
                # NegativeAcknowledge means the vehicle actively refused it (common
                # for remote unlock even when the service is "enabled"). A plain
                # timeout usually means the car was asleep / out of signal.
                raise JlrApiError(
                    f"{status.get('serviceType', 'command')} was not carried out by "
                    f"the vehicle (reason: {why}). If it was a timeout, retry once the "
                    f"car is awake; a NegativeAcknowledge means the car declined it."
                )
            await asyncio.sleep(3)
            status = await self.async_get_service_status(vin, service_id)
        return status  # still pending after the poll window; treat as best-effort

    async def async_get_service_status(
        self, vin: str, customer_service_id: str
    ) -> dict[str, Any]:
        """Poll the status of a previously started remote command."""
        await self.async_connect()
        async with self._session.get(
            f"{IF9_BASE}/vehicles/{vin}/services/{customer_service_id}",
            headers=self._webview_headers(MEDIA_SERVICE_STATUS),
        ) as resp:
            if resp.status != 200:
                raise self._error("service status", resp.status)
            return await resp.json()

    # ----------------------------------------------------------------- helpers
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
            return JlrApiError(
                f"{what} returned 498 (Approov edge wall) — the Origin/Referer/clientId "
                "headers are required to pass this gate"
            )
        if status == 401:
            return JlrAuthError(f"{what} returned 401 (token expired or invalid)")
        return JlrApiError(f"{what} returned {status}")

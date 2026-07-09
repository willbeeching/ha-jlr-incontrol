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
    ICE_RCC_MAX,
    ICE_RCC_MIN,
    IF9_BASE,
    IFAS_TOKENS_URL,
    IFOP_BASE,
    MEDIA_AUTHENTICATE,
    MEDIA_HEALTHSTATUS,
    MEDIA_JSON,
    MEDIA_SERVICE_STATUS,
    MEDIA_START_SERVICE,
    MEDIA_USER,
    SERVICE_CHARGE,
    SERVICE_ENDPOINTS,
    SERVICE_ENGINE_ON,
    SERVICE_PRECONDITIONING,
    SERVICE_PROV,
    SERVICE_START_CONTENT_TYPES,
    SERVICE_VHS,
    SERVICES_EMPTY_PIN,
    TELEMATICS_PROGRAM,
    TOKENS_BASIC_AUTH,
)

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
        what = f"token request ({body['grant_type']})"
        status, tokens = await self._request(
            "POST", IFAS_TOKENS_URL, headers=headers, data=json.dumps(body), what=what
        )
        if status != 200 or not isinstance(tokens, dict):
            raise JlrAuthError(f"{what} returned {status}")
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

    async def async_get_attributes(self, vin: str) -> dict[str, Any]:
        """Return the raw vehicle attributes (make / model / capabilities)."""
        status, payload = await self._request(
            "GET",
            f"{IF9_BASE}/vehicles/{vin}/attributes",
            headers=self._webview_headers(MEDIA_JSON),
            what="attributes",
        )
        if status != 200:
            raise self._error("attributes", status)
        return payload or {}

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
        return self._flatten_status(payload or {})

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

    # NOTE: there is deliberately no trips/journeys support. The /trips endpoint
    # is routed on the webview edge (wrong Accept -> JBoss 406) but the legacy
    # backend behind it never answers with the correct triplist-v2 media type —
    # it 504s after ~70s. The modern app dropped trips entirely (no trip
    # endpoints in its JS bundle) and the old direct /if9/jlr/ path is behind
    # the Approov wall, so there is nothing reliable to build on.

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
    async def _async_authenticate(
        self, vin: str, service_name: str, pin: str = ""
    ) -> dict[str, Any]:
        """Authenticate for a service and return the full auth payload."""
        await self.async_connect()
        auth_pin = "" if service_name in SERVICES_EMPTY_PIN else pin
        auth_headers = self._webview_headers(MEDIA_JSON)
        auth_headers["Content-Type"] = MEDIA_AUTHENTICATE
        status, payload = await self._request(
            "POST",
            f"{IF9_BASE}/vehicles/{vin}/users/{self._user_id}/authenticate",
            headers=auth_headers,
            data=json.dumps({"pin": auth_pin, "serviceName": service_name}),
            what=f"authenticate ({service_name})",
        )
        if status not in (200, 201):
            raise self._error(f"authenticate ({service_name})", status)
        if not payload or not payload.get("token"):
            raise JlrApiError(f"authenticate ({service_name}) returned no token")
        return payload

    async def async_send_command(
        self,
        vin: str,
        service_name: str,
        pin: str = "",
        service_parameters: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Run a PIN-gated remote command via the classic two-step webview flow.

        1. authenticate {pin, serviceName} -> service token
        2. POST the service endpoint with {token} -> customerServiceId / status

        Some services (ECC, VHS) authenticate with an empty PIN per the native-app
        behaviour documented in jlrpy.
        """
        await self.async_connect()
        endpoint = SERVICE_ENDPOINTS.get(service_name)
        if endpoint is None:
            raise JlrApiError(f"unknown service {service_name}")

        auth_pin = "" if service_name in SERVICES_EMPTY_PIN else pin

        # a) get a one-time service token.
        auth_payload = await self._async_authenticate(vin, service_name, auth_pin)
        token = auth_payload["token"]

        # b) start the service. The response Accept MUST be ServiceStatus-v4 (v5/json 406).
        content_type = SERVICE_START_CONTENT_TYPES.get(
            service_name, MEDIA_START_SERVICE
        )
        svc_headers = self._webview_headers(MEDIA_SERVICE_STATUS)
        svc_headers["Content-Type"] = content_type
        body: dict[str, Any] = {"token": token}
        if service_parameters:
            body["serviceParameters"] = service_parameters
        status, started = await self._request(
            "POST",
            f"{IF9_BASE}/vehicles/{vin}/{endpoint}",
            headers=svc_headers,
            data=json.dumps(body),
            what=f"service {service_name}",
        )
        if status not in (200, 202):
            raise self._error(f"service {service_name}", status)
        started = started or {}

        # The 202 only means "accepted/started" — the vehicle may still fail it
        # (e.g. Failed/timeout when the car is asleep). Poll to a terminal state so
        # failures surface to the user instead of looking like nothing happened.
        service_id = started.get("customerServiceId")
        if not service_id:
            return started
        return await self._await_service(vin, service_id, started)

    async def async_send_preconditioning(
        self, vin: str, *, start: bool, target_temp_c: float = 21.0
    ) -> dict[str, Any]:
        """Start or stop electric climate control (ECC)."""
        if start:
            parameters = [
                {"key": "PRECONDITIONING", "value": "START"},
                {
                    "key": "TARGET_TEMPERATURE_CELSIUS",
                    "value": str(int(round(target_temp_c * 10))),
                },
            ]
        else:
            parameters = [{"key": "PRECONDITIONING", "value": "STOP"}]
        return await self.async_send_command(
            vin,
            SERVICE_PRECONDITIONING,
            service_parameters=parameters,
        )

    async def async_send_charge_now(
        self, vin: str, *, enable: bool, pin: str
    ) -> dict[str, Any]:
        """Force charge on or off (CP)."""
        value = "FORCE_ON" if enable else "FORCE_OFF"
        return await self.async_send_command(
            vin,
            SERVICE_CHARGE,
            pin,
            service_parameters=[{"key": "CHARGE_NOW_SETTING", "value": value}],
        )

    async def async_send_vhs(self, vin: str) -> dict[str, Any]:
        """Request a vehicle health status refresh (VHS)."""
        return await self.async_send_command(vin, SERVICE_VHS)

    async def _async_post_vehicle(
        self,
        vin: str,
        endpoint: str,
        *,
        body: dict[str, Any],
        content_type: str = MEDIA_START_SERVICE,
        accept: str = MEDIA_SERVICE_STATUS,
        error_label: str,
    ) -> dict[str, Any]:
        """POST to a vehicle endpoint and return JSON (best-effort for empty bodies)."""
        await self.async_connect()
        headers = self._webview_headers(accept)
        headers["Content-Type"] = content_type
        status, payload = await self._request(
            "POST",
            f"{IF9_BASE}/vehicles/{vin}/{endpoint}",
            headers=headers,
            data=json.dumps(body),
            what=error_label,
        )
        if status not in (200, 202, 204):
            raise self._error(error_label, status)
        return payload or {}

    async def _async_enable_provisioning(self, vin: str, pin: str) -> None:
        """Enable provisioning mode (required before ICE RCC settings)."""
        auth = await self._async_authenticate(vin, SERVICE_PROV, pin)
        body = {
            **auth,
            "serviceCommand": "provisioning",
            "startTime": None,
            "endTime": None,
        }
        await self._async_post_vehicle(
            vin,
            SERVICE_ENDPOINTS[SERVICE_PROV],
            body=body,
            error_label="provisioning",
        )

    async def _async_set_rcc_target(self, vin: str, pin: str, rcc_value: int) -> None:
        """Set the ICE remote climate target on the RCC 31 (cool) – 57 (heat) scale."""
        await self._async_enable_provisioning(vin, pin)
        await self._async_post_vehicle(
            vin,
            "settings",
            body={
                "key": "ClimateControlRccTargetTemp",
                "value": str(rcc_value),
                "applied": 1,
            },
            content_type=MEDIA_JSON,
            accept=MEDIA_JSON,
            error_label="climate settings",
        )

    @staticmethod
    def celsius_to_rcc(target_temp_c: float) -> int:
        """Map a Celsius setpoint to the ICE RCC scale (31 = LO, 57 = HI)."""
        return min(ICE_RCC_MAX, max(ICE_RCC_MIN, int(target_temp_c * 2)))

    async def async_remote_engine_start(
        self, vin: str, pin: str, target_temp_c: float
    ) -> dict[str, Any]:
        """Start ICE remote climate via RCC target + REON."""
        rcc_value = self.celsius_to_rcc(target_temp_c)
        await self._async_set_rcc_target(vin, pin, rcc_value)
        return await self.async_send_command(vin, SERVICE_ENGINE_ON, pin)

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
        status, payload = await self._request(
            "GET",
            f"{IF9_BASE}/vehicles/{vin}/services/{customer_service_id}",
            headers=self._webview_headers(MEDIA_SERVICE_STATUS),
            what="service status",
        )
        if status != 200:
            raise self._error("service status", status)
        return payload or {}

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
                return resp.status, payload
        except TimeoutError as err:
            raise JlrApiError(
                f"{what} timed out after {REQUEST_TIMEOUT.total:.0f}s"
            ) from err
        except aiohttp.ClientError as err:
            raise JlrApiError(f"{what} failed: {err}") from err

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

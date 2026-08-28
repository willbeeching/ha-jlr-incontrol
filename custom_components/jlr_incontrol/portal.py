"""Vehicle location and real names, read from JLR's owner web portal.

Approov closed the direct routes: ``/vehicles/{vin}/position`` and
``/attributes`` both answer 498, and the telemetry socket carries neither. What
it did not close is the portal owners log into in a browser. That portal's own
backend calls the same if9 API with its own whitelisted credentials, so
everything it renders has *already* been past attestation on JLR's side — and
it authenticates with an ordinary ForgeRock session rather than a bearer token.

So this module logs in the way a browser would and reads two things:

* ``/ajax/pollvehiclestatus`` — the vehicles, with nickname, brand, model and
  the opaque per-account id the dashboard needs.
* ``/dashboard/vehicle/{id}`` — an HTML page with the last journey's GPS trail
  embedded in a script variable. Its final point is where the car is parked.

Two honest limits. The location is the end of the last completed journey, not a
live position — right for "is it home", wrong for "where is it now". And the
session is one a person established by typing an emailed code, so when AM
finally expires it there is no way to mint another without asking them again;
location and names simply stop updating until they next sign in.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import aiohttp
from yarl import URL

from .const import (
    IDENTITY_HOST,
    PORTAL_BASES,
    PORTAL_KEEPALIVE_PATH,
    PORTAL_LOCALE,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

# The portal is a slow legacy app and the dashboard page is large, and this
# total has to cover the whole redirect chain plus reading the body. A tight
# ceiling here is what froze the tracker: the read timed out every cycle, so the
# last position was served forever. sock_read still catches a genuinely dead
# socket without waiting out the total.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=90, connect=15, sock_read=30)

# The dashboard page carries the trail as a JavaScript literal. Non-greedy to
# the first closing bracket that ends the array, DOTALL because it is formatted
# across many lines.
_WAYPOINTS = re.compile(r"let\s+waypoints\s*=\s*(\[.*?\])\s*;", re.DOTALL)
# The per-account vehicle id, as it appears in the setup link the JSON returns.
_ENC_ID = re.compile(r"vehicle/add/\d+/([^/]+)/continue")

# Fields worth lifting off the portal's vehicle record, in its spelling.
_VEHICLE_FIELDS = ("nickname", "vehicleBrand", "vehicleType", "registrationNumber")


class JlrPortalError(Exception):
    """Raised when the owner portal cannot be read."""


class JlrPortalAuthError(JlrPortalError):
    """Raised when the stored ForgeRock session no longer opens the portal."""


class JlrPortal:
    """Reads the owner portal on behalf of one account.

    Owns its session and cookie jar: the login hand-off depends on carrying the
    ForgeRock session cookie to one host and the portal's own session cookie to
    another, which is not something to do in a jar shared with the rest of Home
    Assistant.
    """

    def __init__(self, cookies: dict[str, str]) -> None:
        # The set captured at sign-in, replayed unchanged for the life of the
        # session. Refreshing it from later responses was tried and is what
        # broke this repeatedly — see _async_login.
        self._cookies = dict(cookies or {})
        self._session: aiohttp.ClientSession | None = None
        self._base: str | None = None

    @property
    def configured(self) -> bool:
        """Whether there is a session to try at all."""
        return bool(self._cookies)

    async def async_close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        self._base = None

    # ------------------------------------------------------------------ login
    def _new_session(self) -> aiohttp.ClientSession:
        jar = aiohttp.CookieJar()
        # Seed the AM session the interactive sign-in established. Without it
        # the portal's OAuth hand-off lands on the login page instead of the
        # dashboard, and there is nothing headless that can answer that.
        jar.update_cookies(self._cookies, response_url=URL(IDENTITY_HOST))
        return aiohttp.ClientSession(cookie_jar=jar, timeout=REQUEST_TIMEOUT)

    async def _async_login(self, base: str) -> bool:
        """Run the browser's login sequence against one brand's portal."""
        assert self._session is not None
        try:
            # The locale gate: every other path bounces back here until set.
            async with self._session.get(
                f"{base}/select-locale", headers={"User-Agent": USER_AGENT}
            ):
                pass
            async with self._session.get(
                f"{base}/select-locale/{PORTAL_LOCALE}",
                headers={"User-Agent": USER_AGENT},
            ):
                pass
            async with self._session.get(
                f"{base}/forgerock/redirect",
                headers={"User-Agent": USER_AGENT},
                allow_redirects=True,
            ) as resp:
                landed = str(resp.url)
                body = await resp.text()
        except TimeoutError as err:
            raise JlrPortalError("the owner portal timed out during sign-in") from err
        except aiohttp.ClientError as err:
            raise JlrPortalError(f"could not reach the owner portal: {err}") from err

        signed_in = "/dashboard" in landed or "logout" in body.lower()
        _LOGGER.debug(
            "portal login at %s %s (landed on %s)",
            base,
            "succeeded" if signed_in else "failed",
            landed.split("?")[0],
        )
        if not signed_in:
            # Ambiguous by nature: an expired session and a request routed to
            # an identity node that never held the session both land here.
            #
            # Refreshing the stored cookies from this exchange was tried twice
            # and made it worse both times. The authorize round-trip can hand
            # back routing cookies pointing at a different node from the one
            # holding the session, and persisting that mismatch poisons every
            # later sign-in — a fault only the user can clear. The set captured
            # at sign-in is internally consistent; it is replayed unchanged.
            _LOGGER.debug("portal cookies presented: %s", sorted(self._cookies))
        return signed_in

    async def _async_ensure_session(self) -> str:
        """Log in if needed and return the portal base that answered."""
        _LOGGER.debug(
            "portal sign-in cookies held: %s", sorted(self._cookies) or "none"
        )
        if self._session is None or self._session.closed:
            self._session = self._new_session()
            self._base = None
        if self._base is not None:
            return self._base
        for base in PORTAL_BASES:
            if await self._async_login(base):
                self._base = base
                return base
        raise JlrPortalAuthError(
            "the stored Jaguar Land Rover sign-in session no longer opens the "
            "owner portal; location and vehicle names need a fresh sign-in"
        )

    async def _async_get(self, path: str) -> tuple[str, str]:
        """GET a portal path, retrying once on a timeout or a lapsed session.

        Both failures look the same from here — a slow portal and an expired
        session can each end in a timeout part-way through the login redirect
        chain — so both get one fresh attempt against a rebuilt session before
        being reported.
        """
        timed_out: Exception | None = None
        for attempt in (1, 2):
            base = await self._async_ensure_session()
            assert self._session is not None
            try:
                async with self._session.get(
                    f"{base}{path}",
                    headers={"User-Agent": USER_AGENT},
                    allow_redirects=True,
                ) as resp:
                    landed, body = str(resp.url), await resp.text()
                    status = resp.status
            except TimeoutError as err:
                timed_out = err
                self._base = None
                if attempt == 1:
                    continue
                raise JlrPortalError(
                    f"the owner portal timed out reading {path}"
                ) from err
            except aiohttp.ClientError as err:
                raise JlrPortalError(f"portal request failed: {err}") from err

            if status == 401 or _is_login_page(landed, body):
                # Bounced to the locale gate, the login page, or back to
                # ForgeRock: the session lapsed rather than the page being gone.
                self._base = None
                if attempt == 1:
                    continue
                raise JlrPortalAuthError("the owner portal session has expired")
            return landed, body
        if timed_out is not None:
            raise JlrPortalError(f"the owner portal timed out reading {path}")
        raise JlrPortalAuthError("the owner portal session has expired")

    # --------------------------------------------------------------- readings
    async def async_touch(self) -> None:
        """Keep the portal session alive without fetching anything heavy.

        The cheapest authenticated page there is, requested purely so the
        servlet session does not idle out. Letting it lapse costs a re-login,
        and a re-login is the one thing that cannot be done without the user.
        """
        await self._async_get(PORTAL_KEEPALIVE_PATH)

    async def async_get_vehicles(self) -> dict[str, dict[str, Any]]:
        """Return {vin: {names…, "portal_id": …}} for the account's vehicles."""
        _, body = await self._async_get(PORTAL_KEEPALIVE_PATH)
        try:
            payload = json.loads(body)
        except ValueError as err:
            raise JlrPortalError(
                "the owner portal did not return vehicle JSON"
            ) from err

        vehicles: dict[str, dict[str, Any]] = {}
        for record in (payload or {}).get("vehicles") or []:
            vin = record.get("fullVin") or record.get("vin")
            if not vin:
                continue
            found = {
                field: record[field]
                for field in _VEHICLE_FIELDS
                if record.get(field) not in (None, "")
            }
            match = _ENC_ID.search(str(record.get("continueSetupLink") or ""))
            if match:
                found["portal_id"] = match.group(1)
            vehicles[vin] = found
        return vehicles

    async def async_get_position(self, portal_id: str) -> dict[str, Any]:
        """Return the parked position from the last journey's trail."""
        _, body = await self._async_get(f"/dashboard/vehicle/{portal_id}")
        match = _WAYPOINTS.search(body)
        if not match:
            # A car with no journeys logged has no trail. That is a real state,
            # not a failure — report nothing rather than a coordinate.
            _LOGGER.debug("portal dashboard carried no waypoints")
            return {}
        try:
            waypoints = json.loads(match.group(1))
        except ValueError as err:
            raise JlrPortalError("could not read the portal's journey trail") from err
        return _parked(waypoints)


# Where the portal sends someone whose session has gone.
_LOGIN_MARKERS = ("select-locale", "identity.jaguarlandrover.com", "/login")


def _is_login_page(landed: str, body: str) -> bool:
    """Whether this response is a sign-in bounce rather than the page asked for."""
    if any(marker in landed for marker in _LOGIN_MARKERS):
        return True
    # A body that is neither JSON nor a dashboard, but does mention signing in.
    stripped = body.lstrip()
    if stripped.startswith(("{", "[")):
        return False
    lowered = body.lower()
    return "j_username" in lowered or "sign in to your account" in lowered


def _parked(waypoints: list[Any]) -> dict[str, Any]:
    """The last point with real coordinates, plus the last real timestamp.

    The trail's final entries can carry coordinates with a null timestamp (and
    the reverse), so the two are taken independently rather than insisting one
    point supplies both.
    """
    latitude = longitude = None
    stamp: str | None = None
    for point in waypoints:
        if not isinstance(point, dict):
            continue
        position = point.get("position") or {}
        if (
            position.get("latitude") is not None
            and position.get("longitude") is not None
        ):
            latitude, longitude = position["latitude"], position["longitude"]
        moment = _iso(point.get("timestamp"))
        if moment:
            stamp = moment
    if latitude is None or longitude is None:
        return {}
    return {"latitude": latitude, "longitude": longitude, "timestamp": stamp}


def _iso(value: Any) -> str | None:
    """Epoch milliseconds to an ISO string, or None if it isn't one."""
    try:
        moment = datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return moment.isoformat().replace("+00:00", "Z")

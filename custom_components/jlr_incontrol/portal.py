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
from collections.abc import Callable
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
# That link is named for cars still awaiting setup and is missing on some fully
# registered ones, so the dashboard's own links are a second place to find it.
_ENC_ID = re.compile(r"vehicle/add/\d+/([^/]+)/continue")
_DASHBOARD_ID = re.compile(r'href="[^"]*/dashboard/vehicle/([^"/?]+)"')

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

    def __init__(
        self,
        cookies: dict[str, str],
        portal_cookies: dict[str, str] | None = None,
        portal_base: str | None = None,
        on_portal_session: Callable[[str, dict[str, str]], None] | None = None,
    ) -> None:
        # The identity cookies captured at sign-in, replayed unchanged.
        # Measured lifetime: 60 minutes idle, 2 hours absolute, and nothing
        # this integration does extends either. They are the way in, once.
        self._cookies = dict(cookies or {})
        # The portal session those bought. This is the durable half — one in
        # active use has been observed working for over thirty hours — so it is
        # what gets reused, and the identity chain is only a fallback for
        # minting a new one while the two-hour window is still open.
        self._portal_cookies = dict(portal_cookies or {})
        self._portal_base = portal_base
        self._on_portal_session = on_portal_session
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
        # The identity session, for minting a portal session if one is needed.
        jar.update_cookies(self._cookies, response_url=URL(IDENTITY_HOST))
        # And the portal session itself, so a restart resumes where it left off
        # instead of spending the identity session — which by then is usually
        # dead, and only the user can replace it.
        if self._portal_cookies and self._portal_base:
            jar.update_cookies(
                self._portal_cookies, response_url=URL(self._portal_base)
            )
        return aiohttp.ClientSession(cookie_jar=jar, timeout=REQUEST_TIMEOUT)

    async def _async_can_resume(self, base: str) -> bool:
        """Whether the remembered portal session is still usable for this account.

        Two questions answered by one request. Alive, obviously — but also the
        right brand. The base is remembered from whenever the session was
        minted, so a session saved before the brand check existed can perfectly
        well be a live Land Rover session belonging to someone who owns only a
        Jaguar (#14). Resuming that lands straight back in the empty garage the
        brand check was added to avoid, and skips the check that would catch it.

        Only an unambiguous empty garage rejects the session. Minting a new one
        spends the identity session, which nothing but the user can replace, so
        a body we cannot read gets the benefit of the doubt.
        """
        assert self._session is not None
        try:
            async with self._session.get(
                f"{base}{PORTAL_KEEPALIVE_PATH}",
                headers={"User-Agent": USER_AGENT},
                allow_redirects=True,
            ) as resp:
                landed, body, status = str(resp.url), await resp.text(), resp.status
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug("portal session probe failed: %s", err)
            return False
        if status != 200 or _is_login_page(landed, body):
            _LOGGER.debug("remembered portal session has lapsed")
            return False
        try:
            payload = json.loads(body)
        except ValueError:
            _LOGGER.debug("remembered portal session still valid")
            return True
        count = len((payload or {}).get("vehicles") or [])
        if not count:
            _LOGGER.debug(
                "remembered portal session is live but %s lists no vehicles; "
                "signing in again to find the brand that has them",
                base,
            )
            return False
        _LOGGER.debug("remembered portal session still valid, %s vehicle(s)", count)
        return True

    def _remember_portal_session(self, base: str) -> None:
        """Persist the portal session so the next start need not mint one."""
        assert self._session is not None
        host = URL(base).host or ""
        cookies = {
            cookie.key: cookie.value
            for cookie in self._session.cookie_jar
            if host.endswith((cookie["domain"] or host).lstrip("."))
        }
        if not cookies:
            return
        self._portal_cookies, self._portal_base = cookies, base
        if self._on_portal_session is not None:
            self._on_portal_session(base, dict(cookies))

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

    async def _async_has_vehicles(self, base: str) -> bool:
        """Whether this brand's portal has any of the account's cars on it.

        A successful login says nothing about which portal to use: the two
        brands share one ForgeRock identity, so signing in to Land Rover works
        perfectly well for someone who only owns a Jaguar — and then shows them
        an empty garage. Land Rover is tried first, so that is exactly what
        every Jaguar-only owner has been getting, silently (#14).

        Requested directly rather than through ``_async_get``, which would call
        back into session setup and recurse. An account with cars on both
        brands would need a session per portal; the first brand holding a car
        is taken, and the vehicle count logged in ``async_get_vehicles`` is
        what would show that up.
        """
        assert self._session is not None
        try:
            async with self._session.get(
                f"{base}{PORTAL_KEEPALIVE_PATH}",
                headers={"User-Agent": USER_AGENT},
                allow_redirects=True,
            ) as resp:
                landed, body, status = str(resp.url), await resp.text(), resp.status
            if status != 200 or _is_login_page(landed, body):
                return False
            payload = json.loads(body)
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            _LOGGER.debug("could not read the garage at %s: %s", base, err)
            return False
        count = len((payload or {}).get("vehicles") or [])
        _LOGGER.debug("owner portal at %s lists %s vehicle(s)", base, count)
        return count > 0

    async def _async_ensure_session(self) -> str:
        """Return a portal base we are signed in to, signing in only if needed.

        Reusing the remembered portal session is the whole point: minting a new
        one needs the identity session, which expires within two hours of the
        user's last sign-in and cannot be renewed headlessly. Every avoided
        sign-in is an emailed code the user does not have to go and find.
        """
        fresh = self._session is None or self._session.closed
        if fresh:
            self._session = self._new_session()
            self._base = None
        if self._base is not None:
            return self._base
        if fresh and self._portal_base and self._portal_cookies:
            if await self._async_can_resume(self._portal_base):
                self._base = self._portal_base
                return self._base
        _LOGGER.debug(
            "minting a portal session; identity cookies held: %s",
            sorted(self._cookies) or "none",
        )
        signed_in: str | None = None
        for base in PORTAL_BASES:
            if not await self._async_login(base):
                continue
            signed_in = signed_in or base
            if await self._async_has_vehicles(base):
                self._base = base
                self._remember_portal_session(base)
                return base
        if signed_in is not None:
            # Signed in everywhere and found a car nowhere. That is not a
            # sign-in failure, and reporting it as one would send the user off
            # for an emailed code that cannot help. Take the portal that let us
            # in and let the reads report what they actually find.
            _LOGGER.warning(
                "signed in to the owner portal but it lists no vehicles, so "
                "location and vehicle names are unavailable. Please report "
                "this with debug logging enabled"
            )
            self._base = signed_in
            self._remember_portal_session(signed_in)
            return signed_in
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
            else:
                # Log which fields this record does have. Guessing the name of
                # the one carrying the id is how the last several days went;
                # the payload can say it instead. Keys only — the values hold
                # the VIN and the registration.
                _LOGGER.debug(
                    "no setup link on this vehicle; fields present: %s",
                    sorted(record),
                )
            vehicles[vin] = found

        await self._async_fill_missing_ids(vehicles)
        # Zero vehicles and a payload we read wrongly look identical from the
        # outside, and telling them apart cost a day of someone else's time.
        _LOGGER.debug(
            "owner portal listed %s vehicle(s), %s of them with an id",
            len(vehicles),
            sum(1 for found in vehicles.values() if "portal_id" in found),
        )
        return vehicles

    async def _async_fill_missing_ids(
        self, vehicles: dict[str, dict[str, Any]]
    ) -> None:
        """Recover a vehicle id from the dashboard's own links.

        Only where it is unambiguous. With one vehicle short of an id and one
        link spare, the pairing is certain; with more of either it is a guess,
        and quietly attaching the wrong car's location to a vehicle would be
        worse than having none.
        """
        missing = [vin for vin, found in vehicles.items() if "portal_id" not in found]
        if not missing:
            return
        try:
            _, body = await self._async_get("/dashboard")
        except JlrPortalError as err:
            _LOGGER.debug("could not read the dashboard for vehicle ids: %s", err)
            return
        taken = {found.get("portal_id") for found in vehicles.values()}
        spare = [
            i for i in dict.fromkeys(_DASHBOARD_ID.findall(body)) if i not in taken
        ]
        if len(missing) == 1 and len(spare) == 1:
            vehicles[missing[0]]["portal_id"] = spare[0]
            _LOGGER.debug("recovered the vehicle id from the dashboard links")
            return
        _LOGGER.warning(
            "could not work out the owner portal's id for %s vehicle(s), so their "
            "location cannot be read (%s unclaimed link(s) on the dashboard). "
            "Please report this with debug logging enabled",
            len(missing),
            len(spare),
        )

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

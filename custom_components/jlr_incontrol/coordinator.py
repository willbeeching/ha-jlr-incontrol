"""Data coordinator for Jaguar Land Rover InControl.

Vehicle data is pushed, not polled: telemetry.py holds a STOMP subscription open
and hands snapshots here as they arrive. What is left on a timer is housekeeping
— renew the token, keep the device registration alive, notice a vehicle being
added or removed, and retry the attributes endpoint that Approov currently
blocks. None of that produces the numbers on the dashboard.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    JlrApiError,
    JlrAuthError,
    JlrClient,
    brand_from_vin,
    identity_fields,
)
from .const import (
    ATTRIBUTES_RETRY,
    ATTRIBUTES_TTL,
    CONF_ATTRIBUTES,
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_PORTAL_BASE,
    CONF_PORTAL_COOKIES,
    CONF_PORTAL_MINTED,
    CONF_REFRESH_TOKEN,
    CONF_SSO_COOKIES,
    CONF_USER_ID,
    CONF_USERNAME,
    DOMAIN,
    ISSUE_PORTAL_SIGNED_OUT,
    PORTAL_INTERVAL,
    PORTAL_KEEPALIVE_INTERVAL,
    PORTAL_RETRY_AFTER,
    PORTAL_VEHICLES_TTL,
    POSITION_TRUST_WINDOW,
    SCAN_INTERVAL_HOUSEKEEPING,
    STALE_AFTER,
    TELEMETRY_GRACE,
)
from .portal import JlrPortal, JlrPortalAuthError, JlrPortalError
from .redact import vehicle_label
from .telemetry import JlrTelemetry

_LOGGER = logging.getLogger(__name__)

# How long to wait at setup for the first snapshot to arrive over the socket.
# Generous: it covers the token refresh, the device registration, the websocket
# handshake and the broker's first push.
FIRST_SNAPSHOT_TIMEOUT = 60


class JlrCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Owns the JLR client, the telemetry socket, and the merged vehicle state."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL_HOUSEKEEPING,
        )
        self.entry = entry
        # What the options looked like at setup, so the update listener can tell
        # an options change (reload) from a token rotation (do not reload).
        # Change detection for the last-updated signal: some cars (I-Pace)
        # report no LAST_UPDATED_TIME at all and the position timestamp goes
        # static while parked, so observing when the data actually changes is
        # the only freshness signal that always works.
        self._last_snapshot: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self._last_changed: dict[str, str] = {}
        # Attributes come from a walled endpoint, so the last known set is
        # persisted in the config entry: losing them would cost every vehicle
        # its name, model and fuel type until JLR lift the wall.
        stored = entry.data.get(CONF_ATTRIBUTES) or {}
        self._attributes: dict[str, dict[str, Any]] = {
            vin: dict(attrs) for vin, attrs in stored.items()
        }
        self._attributes_attempted: dict[str, Any] = {}
        # Pushed state, keyed by VIN.
        self._status: dict[str, dict[str, Any]] = {}
        # When the broker last pushed for this VIN. Message time, not car time —
        # surfaced in diagnostics only, never used as a freshness signal.
        self._pushed_at: dict[str, str] = {}
        self._position: dict[str, dict[str, Any]] = {}
        self._vehicles: dict[str, dict[str, Any]] = {}
        self._disconnected_since: datetime | None = dt_util.utcnow()
        # Vehicles subscribed but not yet heard from. Platform setup reads the
        # status to decide which entities a car gets, so it must not run while
        # one of them is still silent.
        self._awaiting: set[str] = set()
        self._snapshots_ready = asyncio.Event()
        self.client = JlrClient(
            async_get_clientsession(hass),
            entry.data[CONF_USERNAME],
            entry.data.get(CONF_PASSWORD),
            device_id=entry.data.get(CONF_DEVICE_ID),
            user_id=entry.data.get(CONF_USER_ID),
            refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
            on_tokens=self._persist,
        )
        # The owner portal: the only surviving source of location and of the
        # real vehicle names. Optional — an entry created before this existed
        # has no session stored, and everything else still works without it.
        self.portal = JlrPortal(
            entry.data.get(CONF_SSO_COOKIES) or {},
            portal_cookies=entry.data.get(CONF_PORTAL_COOKIES) or {},
            portal_base=entry.data.get(CONF_PORTAL_BASE),
            portal_minted=entry.data.get(CONF_PORTAL_MINTED),
            on_portal_session=self._store_portal_session,
        )
        self._portal_ids: dict[str, str] = {}
        self._portal_due: Any = None
        self._portal_vehicles_due: Any = None
        # When to try the portal again after it refused us; None means now.
        self._portal_signed_out: Any = None
        self._portal_unconfigured_logged = False
        self._signed_out_issue_raised = False
        # When a portal read last succeeded. Position is only as trustworthy as
        # this is recent — see position_trusted.
        self._portal_read_at: datetime | None = None
        self.telemetry = JlrTelemetry(
            async_get_clientsession(hass),
            self.client,
            on_status=self._handle_status,
            on_position=self._handle_position,
            on_connected=self._handle_connected,
        )

    # ------------------------------------------------------------- lifecycle
    async def async_start_telemetry(self) -> None:
        """Open the socket and wait for a snapshot from *every* vehicle.

        Every vehicle, not just the first: the sensor platform only creates an
        entity when the status key backing it is present, so a car whose
        snapshot lands after setup gets no fuel, odometer or tyre entities at
        all — permanently unavailable while its stablemate works perfectly.

        Setup fails outright if nothing arrives, rather than succeeding into a
        house of unavailable entities: that is precisely how the REST block
        presented itself, and it cost a day to notice because nothing looked
        wrong at the config-entry level.
        """
        self._awaiting = set(self._vehicles)
        self._snapshots_ready.clear()
        self.telemetry.async_set_vehicles(self._vehicles)
        await self.telemetry.async_start()
        try:
            async with asyncio.timeout(FIRST_SNAPSHOT_TIMEOUT):
                await self._snapshots_ready.wait()
        except TimeoutError as err:
            if not self._status:
                await self.telemetry.async_stop()
                raise ConfigEntryNotReady(
                    "connected to Jaguar Land Rover, but no vehicle data arrived "
                    f"over the telemetry socket within {FIRST_SNAPSHOT_TIMEOUT}s"
                ) from err
            # Some vehicles answered. Carry on with those rather than leaving
            # the whole account down, and reload when the stragglers appear.
            _LOGGER.warning(
                "no telemetry snapshot from %s within %ss; reloading once their "
                "data arrives so their entities can be created",
                ", ".join(sorted(vehicle_label(v) for v in self._awaiting)),
                FIRST_SNAPSHOT_TIMEOUT,
            )

    def async_start_keepalive(self) -> None:
        """Put the portal keep-alive on its own short clock."""
        self.entry.async_on_unload(
            async_track_time_interval(
                self.hass, self._async_keepalive, PORTAL_KEEPALIVE_INTERVAL
            )
        )

    async def _async_keepalive(self, _now: Any = None) -> None:
        """Touch the owner portal so its session does not idle out.

        Deliberately not part of housekeeping. Measured on a live account: a
        portal session was already gone fifteen minutes and four seconds after
        an interactive sign-in, and the identity session behind it refused to
        mint a replacement — so the touch was doing nothing but discovering the
        loss. Only the user can recover from that, at the cost of an emailed
        code, which makes never reaching it worth a small request every few
        minutes. See PORTAL_KEEPALIVE_PATH for what that request is.
        """
        if not self.portal.configured or self._portal_signed_out is not None:
            return
        if self._portal_due is None:
            # No portal read yet, so there is no session to keep warm and a
            # touch here would only spend the identity session minting one.
            return
        try:
            await self.portal.async_touch()
            # Say so. A touch that succeeds silently is indistinguishable in
            # the log from a timer that never fired at all, and not being able
            # to tell those apart is what left both of this weekend's failures
            # undiagnosed.
            _LOGGER.debug("portal keep-alive ok, session %s", self.portal.session_age)
        except JlrPortalAuthError as err:
            # Gone, and unrecoverable without the user. Say so now rather than
            # waiting for the next half-hourly read to notice, and stop
            # touching — repeating a login chain that cannot succeed would be
            # both useless and rude.
            self._async_portal_signed_out(dt_util.utcnow(), err)
        except JlrPortalError as err:
            _LOGGER.debug("portal keep-alive failed: %s", err)
        except Exception:  # noqa: BLE001 - a timer callback must never raise
            _LOGGER.exception("unexpected failure keeping the portal awake")

    async def async_shutdown(self) -> None:
        """Stop the telemetry socket and portal session, then the coordinator."""
        await self.telemetry.async_stop()
        await self.portal.async_close()
        await super().async_shutdown()

    # ----------------------------------------------------------- housekeeping
    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self._async_housekeeping()
        except JlrAuthError as err:
            raise ConfigEntryAuthFailed(f"authentication failed: {err}") from err
        except JlrApiError as err:
            raise UpdateFailed(f"could not reach the JLR backend: {err}") from err

    async def _async_housekeeping(self) -> dict[str, Any]:
        await self.client.async_connect()
        vehicles = await self.client.async_get_vehicles()

        found: dict[str, dict[str, Any]] = {}
        for vehicle in vehicles:
            vin = vehicle.get("vin") or vehicle.get("vehicleId")
            if vin:
                found[vin] = vehicle
        # Authoritative, including when it is empty. Keeping the old list on
        # an empty result meant selling your only car left it on the dashboard
        # showing cached state, and undeletable — the removal hook still saw it
        # as current. A reply we cannot parse raises in async_get_vehicles
        # rather than arriving here as an empty list.
        removed = set(self._vehicles) - set(found)
        self._vehicles = found
        if removed:
            self._forget(removed)
        self.telemetry.async_set_vehicles(found)

        for vin, vehicle in self._vehicles.items():
            # Free sources first: the vehicle-list entry is already in hand, and
            # the VIN's manufacturer prefix costs nothing. Both are floors — a
            # real attributes document overwrites them.
            self._seed_identity(vin, vehicle)
            await self._async_refresh_attributes(vin)

        await self._async_read_portal()
        self._persist()
        return self._build()

    def _forget(self, removed: set[str]) -> None:
        """Drop everything held about vehicles the account no longer has.

        Dropping them from _vehicles alone only hid them. Their location was
        still fetched from the owner portal on every cycle — a car sold months
        ago, still being asked after by name — and their nickname and
        registration stayed in the config entry indefinitely, where the
        diagnostics dump would go on reporting them.

        Every per-VIN cache is listed here rather than a subset, because the
        ones that were missed are exactly the ones nobody thinks about: the
        freshness snapshots and the attributes-retry clock hold no secrets but
        do keep a sold car alive in the coordinator's idea of the account.
        """
        for vin in removed:
            self._attributes.pop(vin, None)
            self._attributes_attempted.pop(vin, None)
            self._status.pop(vin, None)
            self._pushed_at.pop(vin, None)
            self._position.pop(vin, None)
            self._portal_ids.pop(vin, None)
            self._last_snapshot.pop(vin, None)
            self._last_changed.pop(vin, None)
            self._awaiting.discard(vin)
        # A vehicle we were still waiting on has now gone; nothing will ever
        # arrive for it, and setup would otherwise sit out the full timeout.
        if not self._awaiting:
            self._snapshots_ready.set()

    def _seed_identity(self, vin: str, vehicle: dict[str, Any]) -> None:
        """Name the vehicle from what we already have, without asking JLR."""
        seed = {**brand_from_vin(vin), **identity_fields(vehicle)}
        if not seed:
            return
        current = self._attributes.get(vin, {})
        missing = {key: value for key, value in seed.items() if not current.get(key)}
        if missing:
            self._attributes[vin] = {**current, **missing}

    async def _async_refresh_attributes(self, vin: str) -> None:
        """Retry the attributes endpoint, keeping whatever we already had.

        Approov walls this one, so a refusal is the expected case for now and
        must not disturb the cached copy — hence no exception escaping here.
        Back off hard between attempts: a vehicle we already have attributes for
        does not need asking again for a day, and one we don't is behind a door
        that will not open until JLR decide otherwise.
        """
        now = dt_util.utcnow()
        attempted = self._attributes_attempted.get(vin)
        wait = ATTRIBUTES_TTL if vin in self._attributes else ATTRIBUTES_RETRY
        if attempted and now - attempted < wait:
            return
        self._attributes_attempted[vin] = now
        try:
            attributes = await self.client.async_get_attributes(vin)
        except JlrApiError as err:
            _LOGGER.debug("attributes for %s unavailable: %s", vehicle_label(vin), err)
            return
        if attributes:
            self._attributes[vin] = {**self._attributes.get(vin, {}), **attributes}

    async def _async_read_portal(self) -> None:
        """Top up names and location from the owner portal.

        Never fatal. The portal is a legacy servlet app behind a session that
        will eventually expire, and none of the live vehicle data depends on
        it — so a failure here degrades those two things and leaves everything
        else alone.
        """
        now = dt_util.utcnow()
        if self._portal_signed_out is not None:
            # Refused before. Try again eventually rather than never: the fix
            # needs the user, but a one-off failure should not cost location
            # until the next restart.
            if now < self._portal_signed_out:
                return
            self._portal_signed_out = None
        if not self.portal.configured:
            # Entries created before the portal was used have no session
            # stored, and one cannot be conjured from a refresh token. Nothing
            # to retry here, so this is a repair and a single log line rather
            # than a back-off.
            if not self._portal_unconfigured_logged:
                self._portal_unconfigured_logged = True
                self._async_raise_signed_out_issue()
                _LOGGER.warning(
                    "no stored sign-in session for the owner portal, so vehicle "
                    "location and names are unavailable. Settings > Devices & "
                    "Services > Jaguar Land Rover InControl > the three dots on "
                    "the entry > Reconfigure will sign in again and restore "
                    "them, keeping your existing entities"
                )
            return
        if self._portal_due is not None and now < self._portal_due:
            # Not time for a location read. Keeping the session alive between
            # them is _async_keepalive's job, on a clock short enough to
            # actually manage it.
            return
        self._portal_due = now + PORTAL_INTERVAL
        try:
            if self._portal_vehicles_due is None or now >= self._portal_vehicles_due:
                await self._async_read_portal_vehicles(now)
            # Driven by the vehicle list, not by the id cache: the two can
            # disagree for a cycle, and it is the account's list that says
            # which cars we are entitled to be asking about.
            for vin in self._vehicles:
                portal_id = self._portal_ids.get(vin)
                if not portal_id:
                    continue
                position = await self.portal.async_get_position(portal_id)
                if position:
                    self._position[vin] = position
            self._portal_read_at = dt_util.utcnow()
            _LOGGER.debug("owner portal read ok, session %s", self.portal.session_age)
            # Whatever was wrong is no longer wrong. Clearing this only when
            # the session cookies happened to change left the repair standing
            # over a portal that had been working for an hour.
            self._async_clear_signed_out_issue()
        except JlrPortalAuthError as err:
            self._async_portal_signed_out(now, err)
        except JlrPortalError as err:
            _LOGGER.debug("owner portal unavailable: %s", err)
        except Exception:  # noqa: BLE001 - the portal must never break setup
            _LOGGER.exception("unexpected failure reading the owner portal")

    def _async_portal_signed_out(self, now: Any, err: Exception) -> None:
        """Back off and tell the user: nothing headless can renew this."""
        self._portal_signed_out = now + PORTAL_RETRY_AFTER
        self._async_raise_signed_out_issue()
        _LOGGER.warning(
            "%s — live status is unaffected, but location and vehicle names "
            "will not update until you sign in again",
            err,
        )

    @property
    def issue_id(self) -> str:
        """The repair id for this entry's expired portal session.

        Scoped to the entry, because the id is what makes a repair unique
        within a domain. Two accounts shared one id, so whichever entry ran
        last decided whether the repair existed at all — a healthy account
        silently clearing the warning belonging to a broken one.
        """
        return f"{ISSUE_PORTAL_SIGNED_OUT}_{self.entry.entry_id}"

    def _async_raise_signed_out_issue(self) -> None:
        """Tell the user once, in the place Home Assistant puts such things.

        Once per load, not once per retry. Dismissing a repair is the user
        saying they have read it; re-raising it every six hours over a problem
        they cannot currently fix is nagging, not informing.
        """
        if self._signed_out_issue_raised:
            return
        self._signed_out_issue_raised = True
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self.issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_PORTAL_SIGNED_OUT,
        )

    def _async_clear_signed_out_issue(self) -> None:
        """Withdraw the sign-in repair once the portal answers again."""
        self._signed_out_issue_raised = False
        ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)

    def _store_portal_session(
        self, base: str, cookies: dict[str, str], minted: str
    ) -> None:
        """Keep the portal session across restarts.

        Worth persisting where the identity session is not: that one dies
        within two hours of the user signing in and only the user can replace
        it, while a portal session in active use outlives it by a long way.
        """
        if (
            self.entry.data.get(CONF_PORTAL_BASE) == base
            and self.entry.data.get(CONF_PORTAL_COOKIES) == cookies
        ):
            return
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={
                **self.entry.data,
                CONF_PORTAL_BASE: base,
                CONF_PORTAL_COOKIES: cookies,
                CONF_PORTAL_MINTED: minted,
            },
        )

    async def _async_read_portal_vehicles(self, now: Any) -> None:
        """Fetch names and the per-account ids the dashboard pages need.

        The listing is authoritative, including when it is empty: a reply we
        cannot read raises in async_get_vehicles rather than arriving here as
        no vehicles. So the id cache is rebuilt from it wholesale — adding to
        it instead meant a car removed from the account kept its id forever,
        and with it a location request every cycle.

        A vehicle still listed but whose record happens to carry no setup link
        keeps the id we already had. That case is common enough to matter: the
        link is absent while a car is mid-enrolment, and losing the id would
        cost it its location for no reason.
        """
        vehicles = await self.portal.async_get_vehicles()
        self._portal_vehicles_due = now + PORTAL_VEHICLES_TTL
        ids: dict[str, str] = {}
        for vin, record in vehicles.items():
            portal_id = record.pop("portal_id", None) or self._portal_ids.get(vin)
            if portal_id:
                ids[vin] = portal_id
            if record:
                self._attributes[vin] = {**self._attributes.get(vin, {}), **record}
        self._portal_ids = ids

    def _persist(self) -> None:
        """Write anything worth surviving a restart back to the config entry.

        Called on every token rotation, not just on the housekeeping poll. JLR
        rotate the refresh token each time it is spent and retire the old one
        at once, so a token held in memory but not yet written to the entry is
        a restart away from a needless "sign in again" — with an emailed code.

        Only writes when something actually changed: an entry update fires the
        update listener, and a listener that reloads unconditionally turns the
        rotating token into a reload every five minutes.
        """
        updates: dict[str, Any] = {}
        for key, value in (
            (CONF_USER_ID, self.client.user_id),
            (CONF_REFRESH_TOKEN, self.client.refresh_token),
        ):
            if value and self.entry.data.get(key) != value:
                updates[key] = value
        # No truthiness guard on the left: clearing the last vehicle's details
        # has to reach the entry too, or a sold car's nickname and registration
        # outlive it there — and _attributes only empties when the account's
        # own vehicle list says the car has gone.
        if (self.entry.data.get(CONF_ATTRIBUTES) or {}) != self._attributes:
            updates[CONF_ATTRIBUTES] = self._attributes
        if updates:
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, **updates}
            )

    # ------------------------------------------------------- telemetry inflow
    def _handle_status(
        self, vin: str, status: dict[str, Any], sent: str | None
    ) -> None:
        """Adopt a pushed VHS snapshot."""
        self._status[vin] = status
        if sent:
            self._pushed_at[vin] = sent
        self._note_change(vin)
        if vin in self._awaiting:
            # A car whose snapshot arrives after setup no longer needs the
            # integration reloaded to get entities: the platforms watch for
            # vehicles they have not built yet and pick it up on this update.
            self._awaiting.discard(vin)
            if not self._awaiting:
                self._snapshots_ready.set()
        self._push()

    def _handle_position(self, vin: str, position: dict[str, Any]) -> None:
        """Adopt a pushed position."""
        self._position[vin] = position
        self._note_change(vin)
        self._push()

    def _handle_connected(self, connected: bool) -> None:
        """Track socket state so entities can go unavailable on a real outage."""
        self._disconnected_since = None if connected else dt_util.utcnow()
        _LOGGER.debug(
            "telemetry socket %s", "connected" if connected else "disconnected"
        )
        if self.data is not None:
            self._push()

    def _note_change(self, vin: str) -> None:
        snapshot = (self._status.get(vin, {}), self._position.get(vin, {}))
        if self._last_snapshot.get(vin) not in (None, snapshot):
            self._last_changed[vin] = dt_util.utcnow().isoformat()
        self._last_snapshot[vin] = snapshot

    def _push(self) -> None:
        """Publish new state to the entities.

        Deliberately not async_set_updated_data: that also reschedules the next
        refresh, and with the socket pushing more often than the housekeeping
        interval the timer would be reset forever and the housekeeping would
        never run again.
        """
        self.data = self._build()
        self.last_update_success = True
        self.async_update_listeners()

    # ------------------------------------------------------------------ state
    def _build(self) -> dict[str, Any]:
        """Merge the vehicle list, cached attributes and pushed telemetry."""
        data: dict[str, Any] = {
            "vehicles": {},
            "connected": self.telemetry.connected,
        }
        for vin, vehicle in self._vehicles.items():
            status = self._status.get(vin, {})
            position = self._position.get(vin, {})
            # Deliberately not the STOMP envelope's time. That is when the
            # broker pushed the message and it advances on every reconnect even
            # when the payload is byte-identical, so using it — even as a
            # fallback — reports a permanently fresh vehicle. These cars send no
            # per-item timestamp at all, which leaves change detection as the
            # only honest signal, and unknown until something moves.
            status_ts = self._newest(
                position.get("timestamp"),
                status.get("LAST_UPDATED_TIME"),
                self._last_changed.get(vin),
            )
            # Whether the GPS fix is old is a question about the fix, and only
            # about the fix. Deriving it from the status freshness above let a
            # car that phones in hourly report a days-old position as current —
            # and two cars with equally old fixes disagree about it.
            position_ts = position.get("timestamp")
            data["vehicles"][vin] = {
                "role": vehicle.get("role"),
                "attributes": self._attributes.get(vin, {}),
                "status": status,
                "position": position,
                "status_ts": status_ts,
                "position_ts": position_ts,
                # Two different questions. The status is stale when the
                # vehicle stopped reporting; the position is stale when the fix
                # is old. A car that phones in hourly from a spot it parked in
                # on Friday is fresh by one measure and not the other.
                "status_stale": self._is_stale(status_ts),
                "position_stale": self._is_stale(position_ts),
                "position_trusted": self.position_trusted,
            }
        return data

    @property
    def position_trusted(self) -> bool:
        """Whether the last known position can still be asserted as current.

        A stale fix and an untrustworthy one are different problems. A car
        parked for three days has an old fix and that is fine. But when portal
        reads have been failing, the car may have moved and we would not know —
        and a location we cannot vouch for still resolves to a zone, which is
        how a tracker ends up confidently reporting "home" for a car that is
        seven kilometres away. Better to admit we do not know.
        """
        if self._portal_read_at is None:
            return False
        return dt_util.utcnow() - self._portal_read_at < POSITION_TRUST_WINDOW

    @property
    def telemetry_ok(self) -> bool:
        """Whether pushed data can still be trusted.

        A reconnect every few minutes is normal (the STOMP session is bound to a
        short-lived token), so a momentary drop must not flap every entity in
        the house. A drop that outlasts the grace period is a real outage.
        """
        if self.telemetry.connected:
            return True
        if self._disconnected_since is None:
            return False
        return dt_util.utcnow() - self._disconnected_since < TELEMETRY_GRACE

    def charge_now_setting(self, vin: str) -> str | None:
        """Return the reported EV charge override, upper-cased."""
        raw = (
            self.data.get("vehicles", {})
            .get(vin, {})
            .get("status", {})
            .get("EV_CHARGE_NOW_SETTING")
        )
        return str(raw).upper() if raw is not None else None

    @staticmethod
    def _newest(*timestamps: str | None) -> str | None:
        """Return the newest parseable timestamp string, or the first non-empty."""
        best: str | None = None
        best_dt = None
        for ts in timestamps:
            if not ts:
                continue
            parsed = dt_util.parse_datetime(ts)
            if parsed is None:
                if best is None:
                    best = ts
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt_util.UTC)
            if best_dt is None or parsed > best_dt:
                best, best_dt = ts, parsed
        return best

    @staticmethod
    def _is_stale(timestamp: str | None) -> bool:
        """Return True if the given timestamp is older than STALE_AFTER."""
        if not timestamp:
            return False
        parsed = dt_util.parse_datetime(timestamp)
        if parsed is None:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_util.UTC)
        return dt_util.utcnow() - parsed > STALE_AFTER

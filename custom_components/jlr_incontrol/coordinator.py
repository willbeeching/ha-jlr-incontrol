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
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
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
    CONF_REFRESH_TOKEN,
    CONF_SSO_COOKIES,
    CONF_USER_ID,
    CONF_USERNAME,
    DOMAIN,
    PORTAL_INTERVAL,
    PORTAL_VEHICLES_TTL,
    SCAN_INTERVAL_HOUSEKEEPING,
    STALE_AFTER,
    TELEMETRY_GRACE,
)
from .portal import JlrPortal, JlrPortalAuthError, JlrPortalError
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
        self.options_snapshot = dict(entry.options)
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
        self._disconnected_since: Any = dt_util.utcnow()
        # Vehicles subscribed but not yet heard from. Platform setup reads the
        # status to decide which entities a car gets, so it must not run while
        # one of them is still silent.
        self._awaiting: set[str] = set()
        self._snapshots_ready = asyncio.Event()
        self._setup_done = False
        self._reloaded_for_straggler = False
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
        self.portal = JlrPortal(entry.data.get(CONF_SSO_COOKIES) or {})
        self._portal_ids: dict[str, str] = {}
        self._portal_due: Any = None
        self._portal_vehicles_due: Any = None
        self._portal_signed_out = False
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
                ", ".join(sorted(self._awaiting)),
                FIRST_SNAPSHOT_TIMEOUT,
            )
        self._setup_done = True

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
        if found:
            self._vehicles = found
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
            _LOGGER.debug("attributes for %s unavailable: %s", vin, err)
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
        if self._portal_signed_out:
            return
        if not self.portal.configured:
            # Entries created before the portal was used have no session
            # stored, and one cannot be conjured from a refresh token.
            self._portal_signed_out = True
            _LOGGER.warning(
                "no stored sign-in session for the owner portal, so vehicle "
                "location and names are unavailable; signing in again (remove "
                "and re-add the integration) restores them"
            )
            return
        now = dt_util.utcnow()
        if self._portal_due is not None and now < self._portal_due:
            return
        self._portal_due = now + PORTAL_INTERVAL
        try:
            if self._portal_vehicles_due is None or now >= self._portal_vehicles_due:
                await self._async_read_portal_vehicles(now)
            for vin, portal_id in self._portal_ids.items():
                position = await self.portal.async_get_position(portal_id)
                if position:
                    self._position[vin] = position
        except JlrPortalAuthError as err:
            # Nothing headless can renew this. Say so once and stop knocking;
            # the next sign-in restores it.
            self._portal_signed_out = True
            _LOGGER.warning(
                "%s — live status is unaffected, but location and vehicle names "
                "will not update until you sign in again",
                err,
            )
        except JlrPortalError as err:
            _LOGGER.debug("owner portal unavailable: %s", err)
        except Exception:  # noqa: BLE001 - the portal must never break setup
            _LOGGER.exception("unexpected failure reading the owner portal")

    async def _async_read_portal_vehicles(self, now: Any) -> None:
        """Fetch names and the per-account ids the dashboard pages need."""
        vehicles = await self.portal.async_get_vehicles()
        if not vehicles:
            return
        self._portal_vehicles_due = now + PORTAL_VEHICLES_TTL
        for vin, record in vehicles.items():
            portal_id = record.pop("portal_id", None)
            if portal_id:
                self._portal_ids[vin] = portal_id
            if record:
                self._attributes[vin] = {**self._attributes.get(vin, {}), **record}

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
        if (
            self._attributes
            and self.entry.data.get(CONF_ATTRIBUTES) != self._attributes
        ):
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
            self._awaiting.discard(vin)
            if self._setup_done:
                # A straggler that missed platform setup: its entities were
                # never created, so a reload is the only way to give it any.
                # Once per load — a car that is reliably slower than the setup
                # window would otherwise reload the integration forever.
                if not self._reloaded_for_straggler:
                    self._reloaded_for_straggler = True
                    _LOGGER.info(
                        "late telemetry for %s; reloading so its entities exist",
                        vin,
                    )
                    self.hass.config_entries.async_schedule_reload(self.entry.entry_id)
                    return
            elif not self._awaiting:
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
            data["vehicles"][vin] = {
                "role": vehicle.get("role"),
                "attributes": self._attributes.get(vin, {}),
                "status": status,
                "position": position,
                "status_ts": status_ts,
                "position_stale": self._is_stale(status_ts),
            }
        return data

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

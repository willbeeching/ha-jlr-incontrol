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
    CHARGE_NOW_ASSUMED_WINDOW,
    CONF_ATTRIBUTES,
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    CONF_USERNAME,
    DOMAIN,
    SCAN_INTERVAL_HOUSEKEEPING,
    STALE_AFTER,
    TELEMETRY_GRACE,
)
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
        self._first_snapshot = asyncio.Event()
        # Optimistic charge-override readback after a Force charge button, held
        # until JLR's (minutes-stale) EV_CHARGE_NOW_SETTING catches up so the
        # charge-now-setting sensor reflects the press immediately.
        self._charge_now_assumed: dict[str, tuple[str, Any]] = {}
        self.client = JlrClient(
            async_get_clientsession(hass),
            entry.data[CONF_USERNAME],
            entry.data.get(CONF_PASSWORD),
            device_id=entry.data.get(CONF_DEVICE_ID),
            user_id=entry.data.get(CONF_USER_ID),
            refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        )
        self.telemetry = JlrTelemetry(
            async_get_clientsession(hass),
            self.client,
            on_status=self._handle_status,
            on_position=self._handle_position,
            on_connected=self._handle_connected,
        )

    # ------------------------------------------------------------- lifecycle
    async def async_start_telemetry(self) -> None:
        """Open the telemetry socket and wait for the first vehicle snapshot.

        Setup fails rather than succeeding into a house of unavailable entities:
        that is precisely how the REST block presented itself, and it cost a day
        to notice because nothing was wrong at the config-entry level.
        """
        self.telemetry.async_set_vehicles(self._vehicles)
        await self.telemetry.async_start()
        try:
            async with asyncio.timeout(FIRST_SNAPSHOT_TIMEOUT):
                await self._first_snapshot.wait()
        except TimeoutError as err:
            await self.telemetry.async_stop()
            raise ConfigEntryNotReady(
                "connected to Jaguar Land Rover, but no vehicle data arrived over "
                f"the telemetry socket within {FIRST_SNAPSHOT_TIMEOUT}s"
            ) from err

    async def async_shutdown(self) -> None:
        """Stop the telemetry socket, then the coordinator."""
        await self.telemetry.async_stop()
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

    def _persist(self) -> None:
        """Write anything worth surviving a restart back to the config entry.

        Only when something actually changed: an entry update fires the update
        listener, and a listener that reloads unconditionally turns the rotating
        refresh token into a reload every five minutes.
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
        self._first_snapshot.set()
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

    def note_charge_now(self, vin: str, value: str) -> None:
        """Record the CP override just written, so the sensor updates at once."""
        self._charge_now_assumed[vin] = (
            value,
            dt_util.utcnow() + CHARGE_NOW_ASSUMED_WINDOW,
        )

    def charge_now_setting(self, vin: str) -> str | None:
        """Return EV_CHARGE_NOW_SETTING, preferring a recent optimistic write."""
        assumed = self._charge_now_assumed.get(vin)
        if assumed and dt_util.utcnow() < assumed[1]:
            return assumed[0]
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

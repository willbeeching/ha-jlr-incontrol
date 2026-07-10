"""Data update coordinator for Jaguar Land Rover InControl."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import JlrApiError, JlrAuthError, JlrClient
from .const import (
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_USER_ID,
    CONF_USERNAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    STALE_AFTER,
)

_LOGGER = logging.getLogger(__name__)


class JlrCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches vehicle data from the JLR webview backend on a schedule."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.entry = entry
        # Change detection for the last-updated signal: some cars (I-Pace)
        # report no LAST_UPDATED_TIME at all and the position timestamp goes
        # static while parked, so observing when polled data actually changes
        # is the only freshness signal that always works.
        self._last_snapshot: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self._last_changed: dict[str, str] = {}
        self.client = JlrClient(
            async_get_clientsession(hass),
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
            device_id=entry.data.get(CONF_DEVICE_ID),
            user_id=entry.data.get(CONF_USER_ID),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self._async_fetch()
        except JlrAuthError as err:
            raise ConfigEntryAuthFailed(f"authentication failed: {err}") from err
        except JlrApiError as err:
            raise UpdateFailed(f"could not reach the JLR backend: {err}") from err

    async def _async_fetch(self) -> dict[str, Any]:
        await self.client.async_connect()
        vehicles = await self.client.async_get_vehicles()

        # Persist the resolved user id so future setups skip the lookup.
        if (
            self.client.user_id
            and self.entry.data.get(CONF_USER_ID) != self.client.user_id
        ):
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, CONF_USER_ID: self.client.user_id},
            )

        data: dict[str, Any] = {"vehicles": {}}
        for vehicle in vehicles:
            vin = vehicle.get("vin") or vehicle.get("vehicleId")
            if not vin:
                continue
            entry: dict[str, Any] = {
                "role": vehicle.get("role"),
                "attributes": {},
                "status": {},
                "position": {},
                "status_ts": None,
            }
            try:
                entry["attributes"] = await self.client.async_get_attributes(vin)
            except JlrApiError as err:
                _LOGGER.debug("attributes for %s unavailable: %s", vin, err)
            try:
                entry["status"] = await self.client.async_get_status(vin)
            except JlrApiError as err:
                _LOGGER.debug("status for %s unavailable: %s", vin, err)
            try:
                entry["position"] = await self.client.async_get_position(vin)
            except JlrApiError as err:
                _LOGGER.debug("position for %s unavailable: %s", vin, err)
            snapshot = (entry["status"], entry["position"])
            if self._last_snapshot.get(vin) not in (None, snapshot):
                self._last_changed[vin] = dt_util.utcnow().isoformat()
            self._last_snapshot[vin] = snapshot

            # Whichever signal is fresher: the position timestamp goes static
            # while the car is parked, the LAST_UPDATED_TIME key is missing on
            # some cars, and the change stamp resets on HA restart — together
            # they cover each other.
            entry["status_ts"] = self._newest(
                entry["position"].get("timestamp"),
                entry["status"].get("LAST_UPDATED_TIME"),
                self._last_changed.get(vin),
            )
            entry["position_stale"] = self._is_stale(entry["status_ts"])
            data["vehicles"][vin] = entry
        return data

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

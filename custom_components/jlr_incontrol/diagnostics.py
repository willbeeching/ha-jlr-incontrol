"""Diagnostics support for JLR InControl."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_PASSWORD, CONF_PIN, DOMAIN

REDACT_KEYS = {
    CONF_PASSWORD,
    CONF_PIN,
    "latitude",
    "longitude",
    "vin",
    "serial_number",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    data = coordinator.data
    redacted: dict[str, Any] = {"vehicles": {}}
    for vin, vehicle in data.get("vehicles", {}).items():
        redacted_vehicle = {
            "role": vehicle.get("role"),
            "attributes": vehicle.get("attributes", {}),
            "status": vehicle.get("status", {}),
            "status_ts": vehicle.get("status_ts"),
            "position_stale": vehicle.get("position_stale"),
            "trip_count": len(vehicle.get("trips") or []),
            "trips_error": vehicle.get("trips_error"),
        }
        if vehicle.get("trips"):
            redacted_vehicle["last_trip"] = vehicle["trips"][0]
        redacted["vehicles"][vin[-4:]] = async_redact_data(
            redacted_vehicle, REDACT_KEYS
        )
    return redacted

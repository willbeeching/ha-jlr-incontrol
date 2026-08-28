"""Diagnostics support for JLR InControl."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_PASSWORD,
    CONF_PORTAL_COOKIES,
    CONF_REFRESH_TOKEN,
    CONF_SSO_COOKIES,
    DOMAIN,
)

REDACT_KEYS = {
    CONF_PASSWORD,
    # A live credential now that it's persisted in the entry — must never reach
    # a diagnostics attachment on a public issue.
    CONF_REFRESH_TOKEN,
    # A live session for the owner portal, same as the token above.
    CONF_SSO_COOKIES,
    # A live portal session, same as the identity one above.
    CONF_PORTAL_COOKIES,
    "portal_id",
    "latitude",
    "longitude",
    "vin",
    "serial_number",
    # The attributes payload uses camelCase and includes the number plate and
    # telematics identifiers (leaked unredacted in a public attachment, #1).
    "registrationNumber",
    "serialNumber",
    "imei",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    data = coordinator.data
    redacted: dict[str, Any] = {
        # Where the data is actually coming from. Without this a dump of empty
        # vehicles looks the same whether the socket is down or the car is.
        "telemetry": {
            "connected": coordinator.telemetry.connected,
            "trusted": coordinator.telemetry_ok,
            "vehicles_subscribed": len(data.get("vehicles", {})),
            # Message time, not vehicle time. Here to show the socket is alive;
            # it is deliberately kept out of the freshness signal.
            "last_push": dict(getattr(coordinator, "_pushed_at", {})),
        },
        "vehicles": {},
    }
    for vin, vehicle in data.get("vehicles", {}).items():
        redacted_vehicle = {
            "role": vehicle.get("role"),
            "attributes": vehicle.get("attributes", {}),
            "status": vehicle.get("status", {}),
            "status_ts": vehicle.get("status_ts"),
            "position_stale": vehicle.get("position_stale"),
        }
        redacted["vehicles"][vin[-4:]] = async_redact_data(
            redacted_vehicle, REDACT_KEYS
        )
    return redacted

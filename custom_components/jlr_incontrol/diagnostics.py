"""Diagnostics support for JLR InControl."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import JlrApiError
from .const import CONF_PASSWORD, CONF_PIN, DOMAIN
from .entity import is_electrified

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
        }
        # On cars with a charge port, probe the chargeProfile read endpoint so
        # a diagnostics attachment can reveal the media type it negotiates
        # (unknowable from an ICE account; see async_probe_charge_profile).
        if is_electrified(vehicle.get("attributes", {}), vehicle.get("status", {})):
            try:
                redacted_vehicle["charge_profile_probe"] = (
                    await coordinator.client.async_probe_charge_profile(vin)
                )
            except JlrApiError as err:
                redacted_vehicle["charge_profile_probe"] = str(err)
        redacted["vehicles"][vin[-4:]] = async_redact_data(
            redacted_vehicle, REDACT_KEYS
        )
    return redacted

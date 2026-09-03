"""Diagnostics support for JLR InControl.

Everything leaving here is scrubbed as a whole rather than by a list of field
names. The list approach was tried and quietly failed: it named ``imei`` and
``serialNumber``, while the payload calls them ``TU_STATUS_IMEI`` and
``TU_STATUS_SERIAL_NUMBER``, so a permanent hardware identifier was shipped in
clear in every download. Naming fields only redacts the ones already thought of.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import JlrConfigEntry
from .redact import scrub, vehicle_label


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: JlrConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data
    pushed_at = getattr(coordinator, "_pushed_at", {})
    diagnostics: dict[str, Any] = {
        # Where the data is actually coming from. Without this a dump of empty
        # vehicles looks the same whether the socket is down or the car is.
        "telemetry": {
            "connected": coordinator.telemetry.connected,
            "trusted": coordinator.telemetry_ok,
            "vehicles_subscribed": len(data.get("vehicles", {})),
            # Message time, not vehicle time. Here to show the socket is alive;
            # it is deliberately kept out of the freshness signal. Keyed by
            # label — these keys were VINs, and being keys they went round the
            # redaction applied to the vehicles below.
            "last_push": {vehicle_label(vin): sent for vin, sent in pushed_at.items()},
        },
        "vehicles": {},
    }
    for vin, vehicle in data.get("vehicles", {}).items():
        diagnostics["vehicles"][vehicle_label(vin)] = {
            "role": vehicle.get("role"),
            "attributes": vehicle.get("attributes", {}),
            "status": vehicle.get("status", {}),
            "status_ts": vehicle.get("status_ts"),
            "position_stale": vehicle.get("position_stale"),
        }
    return scrub(diagnostics)

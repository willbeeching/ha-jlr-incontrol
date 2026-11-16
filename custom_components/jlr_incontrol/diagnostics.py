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
    diagnostics: dict[str, Any] = {
        "source": {
            # An entry from the InControl sign-in that has not linked the Data
            # Hub yet: it reads nothing, and an empty dump means that, not a
            # fault.
            "waiting_for_datahub": coordinator.dormant,
            "trusted": coordinator.telemetry_ok,
            "vehicles": len(data.get("vehicles", {})),
        },
        "vehicles": {},
    }
    # The link: whether it is up, and which signals it has seen with the
    # times the car sent them. Never the values.
    datahub = coordinator.datahub
    diagnostics["datahub"] = datahub.diagnostics() if datahub else None
    for vin, vehicle in data.get("vehicles", {}).items():
        diagnostics["vehicles"][vehicle_label(vin)] = {
            "role": vehicle.get("role"),
            "attributes": vehicle.get("attributes", {}),
            "status": vehicle.get("status", {}),
            "status_ts": vehicle.get("status_ts"),
            "position_stale": vehicle.get("position_stale"),
        }
    # scrub() takes and returns Any by design — it walks whatever JLR sent,
    # whose shape is not ours to predict. The annotation is where that Any
    # stops travelling.
    scrubbed: dict[str, Any] = scrub(diagnostics)
    return scrubbed

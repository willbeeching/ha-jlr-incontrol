"""Buttons for JLR InControl.

Only one survives. Every other button here sent a remote command, and remote
commands need the JLR app's device attestation — checked across the REST
endpoints (498), the telemetry socket (the broker accepts nothing but
acknowledgements) and the owner web portal (which offers no remote control at
all). A button that can only ever raise an error is worse than no button, so
they are removed rather than left to fail.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import JlrConfigEntry
from .const import DOMAIN
from .coordinator import JlrCoordinator
from .entity import JlrVehicleEntity, async_add_vehicle_entities

# Buttons earlier versions created that can no longer do anything.
REMOVED_BUTTONS = (
    "honk_flash",
    "update_from_vehicle",
    "force_charge_on",
    "force_charge_off",
)


# Coordinator-backed and read-only: there is nothing to serialise, and
# leaving it unset means Home Assistant assumes otherwise.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: JlrConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the refresh button and clear out the commands that cannot run."""
    coordinator = entry.runtime_data
    async_add_vehicle_entities(
        entry,
        coordinator,
        async_add_entities,
        lambda vin: [JlrRefreshButton(coordinator, vin)],
    )

    ent_reg = er.async_get(hass)
    for vin in coordinator.data.get("vehicles", {}):
        # The tri-state charge override moved from a binary switch to a sensor
        # (#6), and the command buttons are gone entirely.
        stale = ent_reg.async_get_entity_id("switch", DOMAIN, f"{vin}_charge_now")
        if stale:
            ent_reg.async_remove(stale)
        for key in REMOVED_BUTTONS:
            stale = ent_reg.async_get_entity_id("button", DOMAIN, f"{vin}_{key}")
            if stale:
                ent_reg.async_remove(stale)


class JlrRefreshButton(JlrVehicleEntity, ButtonEntity):
    """Re-read the housekeeping data, including location, straight away.

    Not a request to the car: vehicle status arrives on its own over the
    telemetry connection. This refreshes what is polled — the vehicle list and
    the parked location from the owner portal.
    """

    # An action, not a reading. Greying this out because the socket is
    # down disables it exactly when someone would reach for it.
    _requires_telemetry = False

    _attr_translation_key = "refresh"
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator: JlrCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin)
        self._attr_unique_id = f"{vin}_refresh"

    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()

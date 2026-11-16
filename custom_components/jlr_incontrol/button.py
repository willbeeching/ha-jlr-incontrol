"""Buttons for JLR InControl.

Only one survives. Every other button here sent a remote command, and remote
commands need the JLR app's device attestation — checked across the REST
endpoints of the old InControl connection, and the Data Hub is read-only by
design. A button that can only ever raise an error is worse than no button, so
they are removed rather than left to fail.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import JlrConfigEntry
from .const import DOMAIN
from .coordinator import JlrCoordinator, Refresh
from .entity import JlrVehicleEntity, async_add_vehicle_entities

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
        Platform.BUTTON,
        coordinator,
        async_add_entities,
        lambda vin: [JlrRefreshButton(coordinator, vin)],
    )

    # The tri-state charge override moved from a binary switch to a sensor
    # (#6). Every other leftover is swept up generically when the platform
    # builds its entities, but that only reaches domains this integration
    # still has a platform for, and there is no switch platform any more.
    ent_reg = er.async_get(hass)
    for vin in coordinator.data.get("vehicles", {}):
        stale = ent_reg.async_get_entity_id("switch", DOMAIN, f"{vin}_charge_now")
        if stale:
            ent_reg.async_remove(stale)


class JlrRefreshButton(JlrVehicleEntity, ButtonEntity):
    """Read this car's status and location from the Data Hub straight away.

    Not a request to the car: JLR answer with what the car last reported.
    """

    # An action, not a reading. Greying this out while the link is unhealthy
    # disables it exactly when someone would reach for it.
    _requires_telemetry = False

    _attr_translation_key = "refresh"

    def __init__(self, coordinator: JlrCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin)
        self._attr_unique_id = f"{vin}_refresh"

    async def async_press(self) -> None:
        """Refresh now, and say so if it did not work.

        Both failures are raised rather than logged. Being told the press was
        too soon is information, and a press that silently did nothing is the
        complaint this button spent several releases living down.
        """
        outcome = await self.coordinator.async_refresh_vehicle(self._vin)
        if outcome is Refresh.TOO_SOON:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="refresh_too_soon",
            )
        if outcome is Refresh.FAILED:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="refresh_failed",
            )

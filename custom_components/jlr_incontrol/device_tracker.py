"""Device tracker for JLR InControl (vehicle GPS position)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import JlrConfigEntry
from .coordinator import JlrCoordinator
from .entity import JlrVehicleEntity, async_add_vehicle_entities

# Coordinator-backed and read-only: there is nothing to serialise, and
# leaving it unset means Home Assistant assumes otherwise.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: JlrConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up JLR device trackers."""
    coordinator = entry.runtime_data
    async_add_vehicle_entities(
        entry,
        coordinator,
        async_add_entities,
        lambda vin: [JlrDeviceTracker(coordinator, vin)],
    )


class JlrDeviceTracker(JlrVehicleEntity, TrackerEntity):
    """Reports the vehicle's last known GPS position.

    Read from the owner web portal, not the telemetry socket — the two fail
    independently, and a socket outage must not hide a position the portal
    fetched successfully minutes ago.
    """

    _requires_telemetry = False

    _attr_translation_key = "location"

    def __init__(self, coordinator: JlrCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin)
        self._attr_unique_id = f"{vin}_location"

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @staticmethod
    def _coord(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @property
    def _trusted(self) -> bool:
        """Whether the last known fix can still be reported as the car's place.

        When it cannot, no coordinates are given at all. Home Assistant derives
        the zone from the coordinates, so returning an old fix anyway does not
        produce a cautious answer — it produces a confident wrong one, which is
        exactly how a car seven kilometres away reads as "home".
        """
        return bool(self._vehicle.get("position_trusted"))

    @property
    def latitude(self) -> float | None:
        if not self._trusted:
            return None
        return self._coord(self._position.get("latitude"))

    @property
    def longitude(self) -> float | None:
        if not self._trusted:
            return None
        return self._coord(self._position.get("longitude"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        position = self._position
        return {
            "heading": position.get("heading"),
            "speed": position.get("speed"),
            # When the vehicle recorded this fix, so the age of it is visible
            # rather than implied by the state.
            "timestamp": position.get("timestamp"),
            "stale": self._vehicle.get("position_stale", False),
            # False means we could not refresh it, not that the car has not
            # moved; the state is withheld rather than guessed while it is.
            "trusted": self._trusted,
        }

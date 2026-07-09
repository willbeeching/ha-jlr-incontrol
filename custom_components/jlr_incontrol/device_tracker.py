"""Device tracker for JLR InControl (vehicle GPS position)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import JlrCoordinator
from .entity import JlrVehicleEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up JLR device trackers."""
    coordinator: JlrCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        JlrDeviceTracker(coordinator, vin)
        for vin in coordinator.data.get("vehicles", {})
    )


class JlrDeviceTracker(JlrVehicleEntity, TrackerEntity):
    """Reports the vehicle's last known GPS position."""

    _attr_translation_key = "location"
    _attr_icon = "mdi:car"

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
    def latitude(self) -> float | None:
        return self._coord(self._position.get("latitude"))

    @property
    def longitude(self) -> float | None:
        return self._coord(self._position.get("longitude"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        position = self._position
        return {
            "heading": position.get("heading"),
            "speed": position.get("speed"),
            "timestamp": position.get("timestamp"),
            "stale": self._vehicle.get("position_stale", False),
        }

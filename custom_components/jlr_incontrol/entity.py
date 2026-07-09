"""Shared entities for JLR InControl."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import JlrCoordinator


class JlrVehicleEntity(CoordinatorEntity[JlrCoordinator]):
    """Base entity bound to a single vehicle (by VIN)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: JlrCoordinator, vin: str) -> None:
        super().__init__(coordinator)
        self._vin = vin

    @property
    def _vehicle(self) -> dict[str, Any]:
        return self.coordinator.data.get("vehicles", {}).get(self._vin, {})

    @property
    def _attributes(self) -> dict[str, Any]:
        return self._vehicle.get("attributes", {})

    @property
    def _position(self) -> dict[str, Any]:
        return self._vehicle.get("position", {})

    def _status_value(self, key: str) -> Any:
        """Read a value from the flattened vehicle status dict."""
        return self._vehicle.get("status", {}).get(key)

    @property
    def available(self) -> bool:
        return super().available and self._vin in self.coordinator.data.get(
            "vehicles", {}
        )

    @property
    def device_info(self) -> DeviceInfo:
        attrs = self._attributes
        name = attrs.get("nickname") or attrs.get("vehicleBrand") or "Land Rover"
        model = attrs.get("vehicleType") or attrs.get("model")
        return DeviceInfo(
            identifiers={(DOMAIN, self._vin)},
            manufacturer=attrs.get("vehicleBrand", "Land Rover"),
            model=model,
            name=name,
            serial_number=self._vin,
        )

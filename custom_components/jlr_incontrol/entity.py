"""Shared entities for JLR InControl."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import JlrCoordinator


def is_electric(attributes: dict[str, Any]) -> bool:
    """Return True when the vehicle is a pure BEV."""
    return str(attributes.get("fuelType", "")).lower() == "electric"


def is_electrified(attributes: dict[str, Any], status: dict[str, Any]) -> bool:
    """True for anything with a charge port (BEV or plug-in hybrid).

    ICE cars still report several EV_* status keys with UNKNOWN sentinels
    (seen live on a diesel L460, including EV_CHARGING_STATUS), so key
    presence alone would create phantom EV entities. EV_STATE_OF_CHARGE is
    the one key verified absent on ICE and present on real EVs — use its
    presence as the fallback discriminator for unexpected fuelType strings.
    """
    fuel = str(attributes.get("fuelType", "")).lower()
    if "electric" in fuel or "hybrid" in fuel:
        return True
    return "EV_STATE_OF_CHARGE" in status


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
    def is_electric(self) -> bool:
        """Whether this vehicle is a pure BEV."""
        return is_electric(self._attributes)

    @property
    def _position(self) -> dict[str, Any]:
        return self._vehicle.get("position", {})

    def _status_value(self, key: str) -> Any:
        """Read a value from the flattened vehicle status dict."""
        return self._vehicle.get("status", {}).get(key)

    @property
    def available(self) -> bool:
        """Available while the vehicle is known and telemetry is trusted.

        The coordinator's own success flag only covers the housekeeping poll,
        which keeps succeeding long after the data socket has died — so it is
        not on its own a statement about whether these values mean anything.
        """
        return (
            super().available
            and self._vin in self.coordinator.data.get("vehicles", {})
            and self.coordinator.telemetry_ok
        )

    @property
    def device_info(self) -> DeviceInfo:
        attrs = self._attributes
        # The brand comes from the VIN when the endpoint that knows better is
        # blocked, so it is safe to lean on; the model often is not there.
        brand = attrs.get("vehicleBrand") or "Vehicle"
        model = attrs.get("vehicleType") or attrs.get("model")
        # Two cars on one account would otherwise both be called "Land Rover".
        # The VIN's last four is how the V5C and JLR's own app shorten it.
        name = attrs.get("nickname") or model or f"{brand} {self._vin[-4:]}"
        return DeviceInfo(
            identifiers={(DOMAIN, self._vin)},
            manufacturer=brand,
            model=model,
            name=name,
            serial_number=self._vin,
        )

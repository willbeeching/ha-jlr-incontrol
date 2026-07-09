"""Binary sensors for JLR InControl (doors, windows, alarm, fluid warnings)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import JlrCoordinator
from .entity import JlrVehicleEntity


@dataclass(frozen=True, kw_only=True)
class JlrBinaryDescription(BinarySensorEntityDescription):
    """A JLR binary sensor with a predicate over the (upper-cased) status value."""

    status_key: str
    is_on: Callable[[str], bool]
    # When True, an UNKNOWN/empty raw value maps to HA "unknown" instead of
    # being fed to the predicate (unfitted hardware often reports UNKNOWN,
    # which would otherwise read as e.g. "window open" or "warning active").
    unknown_is_none: bool = True


def _door(key: str, status_key: str) -> JlrBinaryDescription:
    return JlrBinaryDescription(
        key=key,
        translation_key=key,
        status_key=status_key,
        device_class=BinarySensorDeviceClass.DOOR,
        is_on=lambda v: v == "OPEN",
    )


def _window(key: str, status_key: str) -> JlrBinaryDescription:
    return JlrBinaryDescription(
        key=key,
        translation_key=key,
        status_key=status_key,
        device_class=BinarySensorDeviceClass.WINDOW,
        is_on=lambda v: v != "CLOSED",
    )


def _warning(key: str, status_key: str) -> JlrBinaryDescription:
    return JlrBinaryDescription(
        key=key,
        translation_key=key,
        status_key=status_key,
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on=lambda v: v != "NORMAL",
    )


VEHICLE_BINARY_SENSORS: tuple[JlrBinaryDescription, ...] = (
    # Doors.
    _door("door_front_left", "DOOR_FRONT_LEFT_POSITION"),
    _door("door_front_right", "DOOR_FRONT_RIGHT_POSITION"),
    _door("door_rear_left", "DOOR_REAR_LEFT_POSITION"),
    _door("door_rear_right", "DOOR_REAR_RIGHT_POSITION"),
    _door("boot", "DOOR_BOOT_POSITION"),
    _door("bonnet", "DOOR_ENGINE_HOOD_POSITION"),
    # Windows.
    _window("window_front_left", "WINDOW_FRONT_LEFT_STATUS"),
    _window("window_front_right", "WINDOW_FRONT_RIGHT_STATUS"),
    _window("window_rear_left", "WINDOW_REAR_LEFT_STATUS"),
    _window("window_rear_right", "WINDOW_REAR_RIGHT_STATUS"),
    JlrBinaryDescription(
        key="sunroof",
        translation_key="sunroof",
        status_key="IS_SUNROOF_OPEN",
        device_class=BinarySensorDeviceClass.WINDOW,
        is_on=lambda v: v == "TRUE",
    ),
    # Security. LOCK device_class: on == unlocked, so invert the "all doors locked" flag.
    JlrBinaryDescription(
        key="doors_locked",
        translation_key="doors_locked",
        status_key="DOOR_IS_ALL_DOORS_LOCKED",
        device_class=BinarySensorDeviceClass.LOCK,
        is_on=lambda v: v != "TRUE",
    ),
    JlrBinaryDescription(
        key="alarm",
        translation_key="alarm",
        status_key="THEFT_ALARM_STATUS",
        device_class=BinarySensorDeviceClass.SAFETY,
        is_on=lambda v: v == "ALARM_ARMED",
    ),
    # Service / fluid warnings.
    _warning("brake_fluid_warning", "BRAKE_FLUID_WARN"),
    _warning("coolant_level_warning", "ENG_COOLANT_LEVEL_WARN"),
    _warning("oil_level_warning", "EXT_OIL_LEVEL_WARN"),
    _warning("washer_fluid_warning", "WASHER_FLUID_WARN"),
    _warning("adblue_warning", "EXT_EXHAUST_FLUID_WARN"),
    # EV / PHEV — only created when the vehicle reports charging status.
    JlrBinaryDescription(
        key="ev_charging",
        translation_key="ev_charging",
        status_key="EV_CHARGING_STATUS",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        is_on=lambda v: v in ("CHARGING", "BULKCHARGED"),
        unknown_is_none=False,
    ),
    JlrBinaryDescription(
        key="ev_plugged_in",
        translation_key="ev_plugged_in",
        status_key="EV_CHARGING_STATUS",
        device_class=BinarySensorDeviceClass.PLUG,
        is_on=lambda v: v not in ("NOTCONNECTED", "UNKNOWN", ""),
        unknown_is_none=False,
    ),
)

# Entities that only make sense on cars with rear doors.
REAR_DOOR_KEYS = {
    "door_rear_left",
    "door_rear_right",
    "window_rear_left",
    "window_rear_right",
}


def _has_rear_doors(attributes: dict) -> bool:
    """False only when the attributes clearly report a 2/3-door body."""
    try:
        return int(str(attributes.get("numberOfDoors"))) >= 4
    except (TypeError, ValueError):
        return True


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up JLR binary sensors.

    Only add a sensor if the vehicle actually reports its status key, so e.g. a
    Defender without AdBlue doesn't get an AdBlue warning stuck at unknown.
    """
    coordinator: JlrCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        JlrBinarySensor(coordinator, vin, description)
        for vin, vehicle in coordinator.data.get("vehicles", {}).items()
        for description in VEHICLE_BINARY_SENSORS
        if description.status_key in vehicle.get("status", {})
        and (
            description.key not in REAR_DOOR_KEYS
            or _has_rear_doors(vehicle.get("attributes", {}))
        )
    ]
    async_add_entities(entities)


class JlrBinarySensor(JlrVehicleEntity, BinarySensorEntity):
    """A vehicle binary sensor."""

    entity_description: JlrBinaryDescription

    def __init__(
        self, coordinator: JlrCoordinator, vin: str, description: JlrBinaryDescription
    ) -> None:
        super().__init__(coordinator, vin)
        self.entity_description = description
        self._attr_unique_id = f"{vin}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        raw = self._status_value(self.entity_description.status_key)
        if raw is None:
            return None
        value = str(raw).upper()
        if self.entity_description.unknown_is_none and value in ("UNKNOWN", ""):
            return None
        return self.entity_description.is_on(value)

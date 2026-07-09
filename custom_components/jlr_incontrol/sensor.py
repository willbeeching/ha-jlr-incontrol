"""Sensors for JLR InControl.

Values come from the flattened vehicle status dict (see coordinator data shape).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricPotential,
    UnitOfLength,
    UnitOfPressure,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    DISTANCE_UNIT_DEFAULT,
    DISTANCE_UNIT_KM,
    DISTANCE_UNIT_MILES,
    DOMAIN,
    OPT_DISTANCE_UNIT,
    OPT_PRESSURE_UNIT,
    PRESSURE_UNIT_BAR,
    PRESSURE_UNIT_DEFAULT,
    PRESSURE_UNIT_KPA,
    PRESSURE_UNIT_PSI,
)
from .coordinator import JlrCoordinator
from .entity import JlrVehicleEntity, is_electric


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _no_attrs(_status: dict[str, Any]) -> dict[str, Any]:
    return {}


def _coolant_temp(value: str) -> float | None:
    """Return coolant temp, treating sentinel values as unknown."""
    temp = _to_float(value)
    if temp is None or temp <= -40:
        return None
    return temp


def _combined_range(value: str) -> float | None:
    """Return combined range, treating negative sentinel as unknown."""
    val = _to_float(value)
    if val is None or val < 0:
        return None
    return val


def _odometer_attrs(status: dict[str, Any]) -> dict[str, Any]:
    """Expose the metric odometer (ODOMETER is in metres) as a km attribute."""
    metres = _to_float(status.get("ODOMETER"))
    if metres is None:
        return {}
    return {"kilometers": round(metres / 1000, 1)}


@dataclass(frozen=True, kw_only=True)
class JlrSensorDescription(SensorEntityDescription):
    """Describes a JLR vehicle status sensor."""

    status_key: str
    value_fn: Callable[[str], Any] = _to_float
    attr_fn: Callable[[dict[str, Any]], dict[str, Any]] = field(default=_no_attrs)
    suppress_for_ev: bool = False


VEHICLE_SENSORS: tuple[JlrSensorDescription, ...] = (
    JlrSensorDescription(
        key="fuel_level",
        translation_key="fuel_level",
        status_key="FUEL_LEVEL_PERC",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:fuel",
        suppress_for_ev=True,
    ),
    JlrSensorDescription(
        key="distance_to_empty_fuel",
        translation_key="distance_to_empty_fuel",
        status_key="DISTANCE_TO_EMPTY_FUEL",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_unit_of_measurement=UnitOfLength.MILES,
        suggested_display_precision=0,
        suppress_for_ev=True,
    ),
    JlrSensorDescription(
        key="odometer",
        translation_key="odometer",
        status_key="ODOMETER_MILES",
        native_unit_of_measurement=UnitOfLength.MILES,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_unit_of_measurement=UnitOfLength.MILES,
        suggested_display_precision=0,
        icon="mdi:counter",
        attr_fn=_odometer_attrs,
    ),
    JlrSensorDescription(
        key="adblue_range",
        translation_key="adblue_range",
        status_key="EXT_EXHAUST_FLUID_DISTANCE_TO_SERVICE_KM",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_unit_of_measurement=UnitOfLength.MILES,
        suggested_display_precision=0,
        icon="mdi:water-opacity",
    ),
    JlrSensorDescription(
        key="distance_to_service",
        translation_key="distance_to_service",
        status_key="EXT_KILOMETERS_TO_SERVICE",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_unit_of_measurement=UnitOfLength.MILES,
        suggested_display_precision=0,
        icon="mdi:wrench-clock",
    ),
    JlrSensorDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        status_key="BATTERY_VOLTAGE",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    JlrSensorDescription(
        key="battery_soc_12v",
        translation_key="battery_soc_12v",
        status_key="BATTERY_STATUS_12V_SOC",
        native_unit_of_measurement=PERCENTAGE,
        # Deliberately NOT device_class=battery: the 12V SoC is a flaky signal
        # (reports 0 when the car is asleep) and giving it the battery class made
        # HA promote it to the vehicle's device battery badge, showing a false 0%.
        # 12V health is better read from battery_voltage. Fuel % is a separate sensor.
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:car-battery",
    ),
    JlrSensorDescription(
        key="engine_coolant_temp",
        translation_key="engine_coolant_temp",
        status_key="ENGINE_COOLANT_TEMP",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=_coolant_temp,
        suppress_for_ev=True,
    ),
)


def _tyre_kpa(value: str) -> float | None:
    """Raw tyre value is kPa*10; return kPa."""
    raw = _to_float(value)
    return round(raw / 10, 1) if raw is not None else None


def _tyre_description(key: str, status_key: str) -> JlrSensorDescription:
    """Build a tyre-pressure sensor: kPa native, bar exposed as an attribute."""

    def _bar_attr(status: dict[str, Any]) -> dict[str, Any]:
        raw = _to_float(status.get(status_key))
        if raw is None:
            return {}
        # raw = kPa*10, and 1 bar = 100 kPa -> bar = (raw / 10) / 100.
        return {"bar": round((raw / 10) / 100, 2)}

    return JlrSensorDescription(
        key=key,
        translation_key=key,
        status_key=status_key,
        native_unit_of_measurement=UnitOfPressure.KPA,
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=_tyre_kpa,
        attr_fn=_bar_attr,
    )


TYRE_SENSORS: tuple[JlrSensorDescription, ...] = (
    _tyre_description("tyre_pressure_fl", "TYRE_PRESSURE_FRONT_LEFT"),
    _tyre_description("tyre_pressure_fr", "TYRE_PRESSURE_FRONT_RIGHT"),
    _tyre_description("tyre_pressure_rl", "TYRE_PRESSURE_REAR_LEFT"),
    _tyre_description("tyre_pressure_rr", "TYRE_PRESSURE_REAR_RIGHT"),
)


# EV / PHEV sensors. Keys live in the vehicle's evStatus list (merged into status),
# so these only materialise on plug-in vehicles that actually report them.
EV_SENSORS: tuple[JlrSensorDescription, ...] = (
    JlrSensorDescription(
        key="ev_battery",
        translation_key="ev_battery",
        status_key="EV_STATE_OF_CHARGE",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    JlrSensorDescription(
        key="ev_range",
        translation_key="ev_range",
        status_key="EV_RANGE_ON_BATTERY_MILES",
        native_unit_of_measurement=UnitOfLength.MILES,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_unit_of_measurement=UnitOfLength.MILES,
        suggested_display_precision=0,
        icon="mdi:map-marker-distance",
    ),
    JlrSensorDescription(
        key="ev_range_combined",
        translation_key="ev_range_combined",
        status_key="EV_PHEV_RANGE_COMBINED_MILES",
        native_unit_of_measurement=UnitOfLength.MILES,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_unit_of_measurement=UnitOfLength.MILES,
        suggested_display_precision=0,
        icon="mdi:map-marker-distance",
        value_fn=_combined_range,
        suppress_for_ev=True,
    ),
    JlrSensorDescription(
        key="ev_time_to_full",
        translation_key="ev_time_to_full",
        status_key="EV_MINUTES_TO_FULLY_CHARGED",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:timer-sand",
    ),
    JlrSensorDescription(
        key="ev_charging_status",
        translation_key="ev_charging_status",
        status_key="EV_CHARGING_STATUS",
        value_fn=lambda v: str(v).replace("_", " ").title(),
        icon="mdi:ev-station",
    ),
)


def _should_create_sensor(
    description: JlrSensorDescription,
    status: dict[str, Any],
    attributes: dict[str, Any],
) -> bool:
    """Return True if a sensor description should be created for this vehicle."""
    if description.status_key not in status:
        return False
    if description.suppress_for_ev and is_electric(attributes):
        return False
    return True


def _distance_unit_override(entry: ConfigEntry) -> str | None:
    """Return a distance unit override from options, or None for HA default."""
    unit = entry.options.get(OPT_DISTANCE_UNIT, DISTANCE_UNIT_DEFAULT)
    if unit == DISTANCE_UNIT_MILES:
        return UnitOfLength.MILES
    if unit == DISTANCE_UNIT_KM:
        return UnitOfLength.KILOMETERS
    return None


def _pressure_unit_override(entry: ConfigEntry) -> str | None:
    """Return a pressure unit override from options, or None for HA default."""
    unit = entry.options.get(OPT_PRESSURE_UNIT, PRESSURE_UNIT_DEFAULT)
    if unit == PRESSURE_UNIT_KPA:
        return UnitOfPressure.KPA
    if unit == PRESSURE_UNIT_BAR:
        return UnitOfPressure.BAR
    if unit == PRESSURE_UNIT_PSI:
        return UnitOfPressure.PSI
    return None


def _trip_distance_km(trip: dict[str, Any]) -> float | None:
    """Extract trip distance in km from a trip record."""
    for key in ("distance", "distanceKm", "distanceKM", "tripDistance"):
        value = _to_float(trip.get(key))
        if value is not None:
            return value
    metres = _to_float(trip.get("distanceMetres") or trip.get("distanceMeters"))
    if metres is not None:
        return round(metres / 1000, 1)
    return None


def _trip_attrs(trip: dict[str, Any]) -> dict[str, Any]:
    """Build attributes for the last-trip sensor."""
    attrs: dict[str, Any] = {}
    for src, dst in (
        ("startTime", "start_time"),
        ("endTime", "end_time"),
        ("startDateTime", "start_time"),
        ("endDateTime", "end_time"),
        ("averageFuelConsumption", "average_fuel_consumption"),
        ("averageEnergyConsumption", "average_energy_consumption"),
        ("energyConsumption", "energy_consumption"),
        ("tripId", "trip_id"),
    ):
        if trip.get(src) is not None:
            attrs[dst] = trip[src]
    return attrs


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up JLR sensors.

    Only add a sensor if the vehicle actually reports its status key, so signals
    a given model doesn't have (e.g. AdBlue on a non-diesel) aren't created and
    left showing unknown.
    """
    coordinator: JlrCoordinator = hass.data[DOMAIN][entry.entry_id]
    distance_unit = _distance_unit_override(entry)
    pressure_unit = _pressure_unit_override(entry)
    entities: list[Any] = []
    for vin, vehicle in coordinator.data.get("vehicles", {}).items():
        status = vehicle.get("status", {})
        attributes = vehicle.get("attributes", {})
        entities.extend(
            JlrVehicleSensor(
                coordinator, vin, description, distance_unit, pressure_unit
            )
            for description in (*VEHICLE_SENSORS, *TYRE_SENSORS, *EV_SENSORS)
            if _should_create_sensor(description, status, attributes)
        )
        entities.append(JlrLastUpdatedSensor(coordinator, vin))
        # Create the trip sensor whenever journey logging is on, not only when
        # a trip already exists — the first fetch being empty (or timing out)
        # must not permanently hide the entity.
        if vehicle.get("trips") or JlrCoordinator.journey_log_enabled(attributes):
            entities.append(JlrLastTripSensor(coordinator, vin, distance_unit))
        entities.append(JlrAllInfoSensor(coordinator, vin))
    async_add_entities(entities)


class JlrVehicleSensor(JlrVehicleEntity, SensorEntity):
    """A vehicle status sensor."""

    entity_description: JlrSensorDescription

    def __init__(
        self,
        coordinator: JlrCoordinator,
        vin: str,
        description: JlrSensorDescription,
        distance_unit: str | None,
        pressure_unit: str | None,
    ) -> None:
        super().__init__(coordinator, vin)
        self.entity_description = description
        self._attr_unique_id = f"{vin}_{description.key}"
        if distance_unit and description.device_class == SensorDeviceClass.DISTANCE:
            self._attr_suggested_unit_of_measurement = distance_unit
        if pressure_unit and description.device_class == SensorDeviceClass.PRESSURE:
            self._attr_suggested_unit_of_measurement = pressure_unit

    @property
    def native_value(self) -> Any:
        raw = self._status_value(self.entity_description.status_key)
        if raw is None or raw == "":
            return None
        return self.entity_description.value_fn(raw)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.entity_description.attr_fn(self._vehicle.get("status", {}))


class JlrLastUpdatedSensor(JlrVehicleEntity, SensorEntity):
    """Timestamp of the vehicle's last reported position / status."""

    _attr_translation_key = "last_updated"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:update"

    def __init__(self, coordinator: JlrCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin)
        self._attr_unique_id = f"{vin}_last_updated"

    @property
    def native_value(self) -> datetime | None:
        ts = self._vehicle.get("status_ts")
        if not ts:
            return None
        parsed = dt_util.parse_datetime(ts)
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_util.UTC)
        return parsed

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"stale": self._vehicle.get("position_stale", False)}


class JlrLastTripSensor(JlrVehicleEntity, SensorEntity):
    """Distance of the most recent trip."""

    _attr_translation_key = "last_trip"
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_icon = "mdi:map-marker-path"

    def __init__(
        self, coordinator: JlrCoordinator, vin: str, distance_unit: str | None
    ) -> None:
        super().__init__(coordinator, vin)
        self._attr_unique_id = f"{vin}_last_trip"
        if distance_unit:
            self._attr_suggested_unit_of_measurement = distance_unit

    @property
    def native_value(self) -> float | None:
        trips = self._vehicle.get("trips") or []
        if not trips:
            return None
        return _trip_distance_km(trips[0])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        trips = self._vehicle.get("trips") or []
        if not trips:
            return {}
        return _trip_attrs(trips[0])


class JlrAllInfoSensor(JlrVehicleEntity, SensorEntity):
    """Flattened vehicle status as attributes (disabled by default)."""

    _attr_translation_key = "all_info"
    _attr_icon = "mdi:database"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: JlrCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin)
        self._attr_unique_id = f"{vin}_all_info"

    @property
    def native_value(self) -> str:
        return "ok"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return dict(self._vehicle.get("status", {}))

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
from homeassistant.helpers import entity_registry as er
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
from .entity import JlrVehicleEntity, is_electric, is_electrified


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
    # Only create on vehicles with a charge port; ICE cars report EV_* keys
    # with UNKNOWN sentinels, so key presence alone is not enough.
    requires_ev: bool = False


def _alarm_state(value: str) -> str:
    """Humanise the THEFT_ALARM_STATUS enum (ALARM_NOT_SET__DOOR_OPEN etc.)."""
    return (
        str(value).removeprefix("ALARM_").replace("__", "_").replace("_", " ").title()
    )


VEHICLE_SENSORS: tuple[JlrSensorDescription, ...] = (
    JlrSensorDescription(
        key="alarm_state",
        translation_key="alarm_state",
        status_key="THEFT_ALARM_STATUS",
        value_fn=_alarm_state,
        icon="mdi:shield-car",
    ),
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
    """Normalise the raw tyre value to kPa.

    The scale differs by vehicle generation: an L405/L663 reports kPa*10
    (e.g. 2470) while an I-Pace reports plain kPa (e.g. 279). Real tyre
    pressures sit around 180-350 kPa, so anything above 1000 must be the
    *10 scale.
    """
    raw = _to_float(value)
    if raw is None:
        return None
    if raw > 1000:
        return round(raw / 10, 1)
    return round(raw, 1)


def _tyre_description(key: str, status_key: str) -> JlrSensorDescription:
    """Build a tyre-pressure sensor: kPa native, bar exposed as an attribute."""

    def _bar_attr(status: dict[str, Any]) -> dict[str, Any]:
        kpa = _tyre_kpa(status.get(status_key))
        if kpa is None:
            return {}
        return {"bar": round(kpa / 100, 2)}

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
        requires_ev=True,
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
        requires_ev=True,
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
        requires_ev=True,
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
        requires_ev=True,
    ),
    JlrSensorDescription(
        key="ev_charging_status",
        translation_key="ev_charging_status",
        status_key="EV_CHARGING_STATUS",
        value_fn=lambda v: str(v).replace("_", " ").title(),
        icon="mdi:ev-station",
        requires_ev=True,
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
    if description.requires_ev and not is_electrified(attributes, status):
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
        entities.append(JlrAllInfoSensor(coordinator, vin))
        if is_electrified(attributes, status) and "EV_CHARGING_METHOD" in status:
            entities.append(JlrEvccStatusSensor(coordinator, vin))
        if is_electrified(attributes, status) and "EV_CHARGE_NOW_SETTING" in status:
            entities.append(JlrChargeNowSettingSensor(coordinator, vin))
    async_add_entities(entities)

    # Drop entities left behind by earlier versions: the last-trip sensor
    # (trips support was removed — the webview edge 504s on the legacy /trips
    # backend) and EV sensors created on ICE cars before electrified gating.
    ent_reg = er.async_get(hass)
    for vin, vehicle in coordinator.data.get("vehicles", {}).items():
        stale_keys = ["last_trip"]
        if not is_electrified(vehicle.get("attributes", {}), vehicle.get("status", {})):
            stale_keys.extend(d.key for d in EV_SENSORS)
        for key in stale_keys:
            stale = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{vin}_{key}")
            if stale:
                ent_reg.async_remove(stale)

    _apply_unit_overrides(ent_reg, coordinator, distance_unit, pressure_unit)


def _apply_unit_overrides(
    ent_reg: er.EntityRegistry,
    coordinator: JlrCoordinator,
    distance_unit: str | None,
    pressure_unit: str | None,
) -> None:
    """Push the configured display units into the entity registry.

    suggested_unit_of_measurement only applies when an entity is FIRST
    registered, so changing the unit options after setup silently did
    nothing for existing entities (#4). Options changes reload the entry,
    which lands here with the registry already populated.
    """
    for vin in coordinator.data.get("vehicles", {}):
        for description in (*VEHICLE_SENSORS, *TYRE_SENSORS, *EV_SENSORS):
            if description.device_class == SensorDeviceClass.DISTANCE:
                unit = distance_unit
            elif description.device_class == SensorDeviceClass.PRESSURE:
                unit = pressure_unit
            else:
                continue
            entity_id = ent_reg.async_get_entity_id(
                "sensor", DOMAIN, f"{vin}_{description.key}"
            )
            if not entity_id:
                continue
            reg_entry = ent_reg.async_get(entity_id)
            if reg_entry is None:
                continue
            sensor_options = dict(reg_entry.options.get("sensor", {}))
            current = sensor_options.get("unit_of_measurement")
            if unit is None:
                # Back to "Use Home Assistant default": drop any override so
                # the suggested/native unit applies again.
                if current is not None:
                    sensor_options.pop("unit_of_measurement", None)
                    ent_reg.async_update_entity_options(
                        entity_id, "sensor", sensor_options or None
                    )
            elif current != unit:
                sensor_options["unit_of_measurement"] = unit
                ent_reg.async_update_entity_options(entity_id, "sensor", sensor_options)


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


class JlrEvccStatusSensor(JlrVehicleEntity, SensorEntity):
    """IEC 61851-style connector state (A/B/C) for EVCC-style automations.

    Mapping verified live across all four wallbox states on an I-Pace (#1):
    A = disconnected, B = connected but not charging (incl. INITIALIZATION
    while the wallbox withholds power), C = charging.
    """

    _attr_translation_key = "evcc_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["A", "B", "C"]
    _attr_icon = "mdi:ev-plug-type2"

    def __init__(self, coordinator: JlrCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin)
        self._attr_unique_id = f"{vin}_evcc_status"

    @property
    def native_value(self) -> str | None:
        method = str(self._status_value("EV_CHARGING_METHOD") or "").upper()
        if method in ("", "UNKNOWN"):
            return None
        if method == "NOTCONNECTED":
            return "A"
        charging = str(self._status_value("EV_CHARGING_STATUS") or "").upper()
        if charging in ("CHARGING", "BULKCHARGED"):
            return "C"
        return "B"


class JlrChargeNowSettingSensor(JlrVehicleEntity, SensorEntity):
    """The raw CP charge-now override (DEFAULT / FORCE_ON / FORCE_OFF).

    A read-only view of the tri-state override, replacing the old binary
    switch that couldn't tell DEFAULT (no override) from FORCE_OFF (charge
    actively suppressed) — both looked like "off" (#6). Drive it with the
    Force charge on / Force charge off buttons.
    """

    _attr_translation_key = "charge_now_setting"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["DEFAULT", "FORCE_ON", "FORCE_OFF"]
    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator: JlrCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin)
        self._attr_unique_id = f"{vin}_charge_now_setting"

    @property
    def native_value(self) -> str | None:
        value = self.coordinator.charge_now_setting(self._vin)
        return value if value in self._attr_options else None

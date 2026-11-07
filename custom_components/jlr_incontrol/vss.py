"""The Data Hub's signal names, translated to the status keys the entities read.

The Data Hub and the telemetry socket are the same data under two naming
schemes. Laid side by side for two cars in November 2026, every value matched
exactly — the same strings, the same raw numbers, the same units — so this is a
renaming, not a conversion. Every sensor, binary sensor and freshness rule
written against the socket's keys keeps working unchanged, and so do the
entity ids and their history.

Three things are derived rather than renamed: the odometer in miles (the Data
Hub sends metres only), the boot's lock word (the Data Hub sends TRUE/FALSE
where the socket also sent LOCKED/UNLOCKED), and LAST_UPDATED_TIME, which the
socket never reliably carried and the Data Hub stamps on every snapshot.

"DriverSide" in the Data Hub is the socket's LEFT, even on a right-hand-drive
car: a UK Range Rover reported its driver-side window open on the Data Hub and
WINDOW_FRONT_LEFT_STATUS open on the socket, at the same moment.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

METRES_PER_MILE = 1609.344
KM_PER_MILE = 1.609344

_DOORS = (
    ("Row1.DriverSide", "FRONT_LEFT"),
    ("Row1.PassengerSide", "FRONT_RIGHT"),
    ("Row2.DriverSide", "REAR_LEFT"),
    ("Row2.PassengerSide", "REAR_RIGHT"),
)
# Seats with a powered fold, as the Data Hub names them -> where they sit.
# DriverSide is LEFT here as it is for the doors.
_SEATS = (
    ("Row1.PassengerSide.Backrest", "FRONT_RIGHT"),
    ("Row2.DriverSide.Backrest", "REAR_LEFT"),
    ("Row2.PassengerSide.Backrest", "REAR_RIGHT"),
    ("Row2.Middle.Armrest", "REAR_ARMREST"),
    ("Row2.Middle.SkiHatch", "SKI_HATCH"),
    ("Row3.DriverSide.Backrest", "THIRD_ROW_LEFT"),
    ("Row3.PassengerSide.Backrest", "THIRD_ROW_RIGHT"),
)
_WHEELS = (
    ("Row1.Wheel.Left", "FRONT_LEFT"),
    ("Row1.Wheel.Right", "FRONT_RIGHT"),
    ("Row2.Wheel.Left", "REAR_LEFT"),
    ("Row2.Wheel.Right", "REAR_RIGHT"),
)

# Data Hub signal (without the leading "Vehicle.") -> socket status key.
RENAMES: dict[str, str] = {
    # Openings and locks
    "Body.Door.Lock.Status": "DOOR_IS_ALL_DOORS_LOCKED",
    "Body.Trunk.Rear.IsLocked": "DOOR_IS_BOOT_LOCKED",
    "Body.Trunk.Rear.IsOpen": "DOOR_BOOT_POSITION",
    "Body.Hood.IsLocked": "DOOR_ENGINE_HOOD_LOCK_STATUS",
    "Body.Hood.IsOpen": "DOOR_ENGINE_HOOD_POSITION",
    "Body.Roof.IsOpen": "IS_SUNROOF_OPEN",
    "Body.Convertible.IsOpen": "IS_CAB_OPEN",
    "Body.Lights.Beam.Low.IsOn": "IS_HEAD_LIGHTS_ON",
    **{
        f"Cabin.Door.{door}.IsLocked": f"DOOR_{key}_LOCK_STATUS" for door, key in _DOORS
    },
    **{f"Cabin.Door.{door}.IsOpen": f"DOOR_{key}_POSITION" for door, key in _DOORS},
    **{
        f"Cabin.Door.{door}.Window.Position": f"WINDOW_{key}_STATUS"
        for door, key in _DOORS
    },
    # Security
    "Body.Security.Alarm.Mode": "THEFT_ALARM_STATUS",
    "Body.Security.IsEngineBlocked": "ENGINE_BLOCK",
    "Body.Security.IsPanicAlarmTriggered": "IS_PANIC_ALARM_TRIGGERED",
    "Body.Crash.Airbag.IsFired": "SRS_STATUS",
    "Body.Crash.Front.IsImpactDetected": "FRONT_IMPACT_DETECTED",
    "Connectivity.PrivacyMode": "PRIVACY_SWITCH",
    # Tyres
    **{
        f"Chassis.Axle.{wheel}.Tire.Pressure": f"TYRE_PRESSURE_{key}"
        for wheel, key in _WHEELS
    },
    **{
        f"Chassis.Axle.{wheel}.Tire.IsPressureLow": f"TYRE_STATUS_{key}"
        for wheel, key in _WHEELS
    },
    # Fluids and warnings
    "Chassis.Brake.FluidLevel.IsLevelLow": "BRAKE_FLUID_WARN",
    "Body.Windshield.Front.WasherFluid.IsLevelLow": "WASHER_FLUID_WARN",
    "Powertrain.CombustionEngine.EngineCoolant.IsLevelLow": "ENG_COOLANT_LEVEL_WARN",
    "Powertrain.CombustionEngine.EngineCoolant.Temperature": "ENGINE_COOLANT_TEMP",
    "Powertrain.CombustionEngine.EngineOil.IsLevelLow": "EXT_OIL_LEVEL_WARN",
    "Powertrain.CombustionEngine.DieselExhaustFluid.IsLevelLow": (
        "EXT_EXHAUST_FLUID_WARN"
    ),
    "Powertrain.CombustionEngine.DieselExhaustFluid.Level": (
        "EXT_EXHAUST_FLUID_VOLUME_REFILL_LITRESX10"
    ),
    "Powertrain.CombustionEngine.Diesel.ExhaustFluid.Service.DistanceToService": (
        "EXT_EXHAUST_FLUID_DISTANCE_TO_SERVICE_KM"
    ),
    "Powertrain.CombustionEngine.Diesel.ParticulateFilter.IsLevelLow": (
        "EXT_PARTICULATE_FILTER_WARN"
    ),
    # Fuel, distance, service
    "Powertrain.FuelSystem.RelativeLevel": "FUEL_LEVEL_PERC",
    "Powertrain.FuelSystem.AbsoluteLevel": "FUEL_LEVEL_LITRES",
    "Powertrain.FuelSystem.Range": "DISTANCE_TO_EMPTY_FUEL",
    "Powertrain.Range": "DISTANCE_TO_EMPTY_TOTAL",
    "Service.DistanceToService": "EXT_KILOMETERS_TO_SERVICE",
    "System.DriveCycle.Status": "VEHICLE_STATE_TYPE",
    "System.Lifecycle.Mode": "VEHICLE_LIFECYCLE_MODE",
    # 12 V battery
    "LowVoltageBattery.CurrentVoltage": "BATTERY_VOLTAGE",
    "LowVoltageBattery.StateOfCharge": "BATTERY_STATUS_12V_SOC",
    "LowVoltageBattery.IsBatteryLow": "BATTERY_STATUS",
    "LowVoltageBattery.StateOfChargeEstimate": "BATTERY_STATUS_12V_ESTIMATE",
    "LowVoltageBattery.StateOfHealth": "BATTERY_STATUS_12V_HEALTH",
    # Climate
    "Cabin.HVAC.AirConditioning.Status": "CLIMATE_STATUS_OPERATING_STATUS",
    "Cabin.HVAC.AirConditioning.TimeRemaining": "CLIMATE_STATUS_REMAINING_RUNTIME",
    "Cabin.HVAC.AirConditioning.VentingTime": "CLIMATE_STATUS_VENTING_TIME",
    "Cabin.HVAC.AirConditioning.FuelFiredHeater.TimeRemaining": (
        "CLIMATE_STATUS_FFH_REMAINING_RUNTIME"
    ),
    "Cabin.HVAC.Schedule.IsActive": "CLIMATE_STATUS_TIMER_ACTIVATION_STATUS",
    **{
        f"Cabin.HVAC.Schedule.{slot}.{part}": f"CLIMATE_STATUS_TIMER{n}_{part.upper()}"
        for slot, n in (("Primary", 1), ("Secondary", 2))
        for part in ("Day", "Hour", "Minute", "Month")
    },
    "Cabin.HVAC.Cleaning.IsCycleActive": "CAC_OPERATING_STATE",
    "Cabin.HVAC.Cleaning.CycleTimeRemaining": "CAC_CYCLE_TIME",
    "Cabin.HVAC.Cleaning.LastCycleStatus": "CAC_LAST_CYCLE_STATUS",
    # The rest are the Data Hub's alone: the socket had no key for them, so
    # these names are new, in its style.
    "Cabin.HVAC.IsAirConditioningActive": "AIR_CONDITIONING_ACTIVE",
    "Cabin.HVAC.AirConditioning.Mode": "CLIMATE_MODE",
    "Cabin.HVAC.AirConditioning.PrioritySetting": "CLIMATE_PRIORITY",
    "Cabin.HVAC.AirConditioning.FuelFiredHeater.Setting": "CLIMATE_FFH_SETTING",
    "Cabin.HVAC.Cleaning.CycleStartTime": "CAC_CYCLE_START_TIME",
    "Cabin.HVAC.Cleaning.LastCompletedCycle": "CAC_LAST_COMPLETED_CYCLE",
    "Cabin.HVAC.Cleaning.LastCycle.Error": "CAC_LAST_CYCLE_ERROR",
    # Departure timer
    **{
        f"Schedule.Departure.{part}": f"DEPARTURE_TIMER_{word}"
        for part, word in (
            ("IsSet", "IS_SET"),
            ("Year", "YEAR"),
            ("Month", "MONTH"),
            ("Day", "DAY"),
            ("Hour", "HOUR"),
            ("Minute", "MINUTE"),
        )
    },
    # Seats
    **{f"Cabin.Seat.{seat}.IsFolded": f"SEAT_{key}_FOLDED" for seat, key in _SEATS},
    **{
        f"Cabin.Seat.{seat}.IsMovementInhibited": f"SEAT_{key}_INHIBITED"
        for seat, key in _SEATS
    },
    # Electric. From JLR's catalog rather than a car: neither car this was
    # checked against is electric, so these are the one part not yet matched
    # value for value against the socket.
    "Powertrain.TractionBattery.StateOfCharge.Displayed": "EV_STATE_OF_CHARGE",
    "Powertrain.TractionBattery.Charging.Status": "EV_CHARGING_STATUS",
    "Powertrain.TractionBattery.Charging.Method": "EV_CHARGING_METHOD",
    "Powertrain.TractionBattery.Charging.IsCharging": "EV_IS_CHARGING",
    "Powertrain.TractionBattery.Charging.ChargingPort.AnyPosition."
    "IsChargingCableConnected": "EV_IS_PLUGGED_IN",
    "Powertrain.TractionBattery.Charging.TimeToComplete": (
        "EV_MINUTES_TO_FULLY_CHARGED"
    ),
    "Powertrain.TractionBattery.Charging.TimeToBulkCharged": (
        "EV_MINUTES_TO_BULK_CHARGED"
    ),
    "Powertrain.TractionBattery.Charging.ChargeNowSetting": "EV_CHARGE_NOW_SETTING",
    "Powertrain.TractionBattery.Charging.Session.Energy": (
        "EV_ENERGY_CONSUMED_LAST_CHARGE_KWH"
    ),
    "Powertrain.TractionBattery.BatteryConditioning.IsActive": (
        "EV_IS_PRECONDITIONING"
    ),
    "Powertrain.TractionBattery.StateOfCharge.Current": "EV_STATE_OF_CHARGE_ACTUAL",
    "Powertrain.TractionBattery.Charging.ChargeLimit": "EV_CHARGE_LIMIT",
    "Powertrain.TractionBattery.Charging.ChargeLimitSetting.OneOffValue": (
        "EV_CHARGE_LIMIT_ONE_OFF"
    ),
    "Powertrain.TractionBattery.Charging.ChargeLimitSetting.OneOffMode": (
        "EV_CHARGE_LIMIT_ONE_OFF_MODE"
    ),
    "Powertrain.TractionBattery.Charging.ChargeLimitSetting.PermanentMode": (
        "EV_CHARGE_LIMIT_PERMANENT_MODE"
    ),
    "Powertrain.TractionBattery.Charging.ChargeRate": "EV_CHARGING_RATE_KM_PER_HOUR",
    "Powertrain.TractionBattery.Charging.StateOfChargeRate": (
        "EV_CHARGING_RATE_SOC_PER_HOUR"
    ),
    "Powertrain.TractionBattery.Charging.ChargingModeChoice": (
        "EV_CHARGING_MODE_CHOICE"
    ),
    **{
        f"Powertrain.TractionBattery.RangeEstimation.Consumption.SpeedBracket.SB{n}": (
            f"EV_CONSUMPTION_SPEED_BAND_{n}"
        )
        for n in range(1, 8)
    },
    **{
        f"Powertrain.TractionBattery.RangeEstimation.{part}": key
        for part, key in (
            ("Consumption.Ascent", "EV_CONSUMPTION_ASCENT"),
            ("Consumption.Descent", "EV_CONSUMPTION_DESCENT"),
            ("Consumption.Stationary", "EV_CONSUMPTION_STATIONARY"),
            ("UsableEnergy.Initial", "EV_USABLE_ENERGY_INITIAL"),
            ("UsableEnergy.Regenerative", "EV_USABLE_ENERGY_REGENERATIVE"),
            ("UsableEnergy.Revised", "EV_USABLE_ENERGY_REVISED"),
        )
    },
}


def _text(value: Any) -> str:
    """A value as the socket would have sent it: always a string."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_status(signals: Mapping[str, Any], reported: str | None) -> dict[str, str]:
    """Socket-shaped status from Data Hub signals.

    ``signals`` maps full signal names ("Vehicle.…") to values. Signals with
    no socket equivalent are left out rather than invented a key, and a value
    of None is left out too: the socket omitted what a car did not report, and
    the entities treat a missing key as "this car does not have it".
    """
    status: dict[str, str] = {}
    for name, value in signals.items():
        if value is None:
            continue
        key = RENAMES.get(name.removeprefix("Vehicle."))
        if key is not None:
            status[key] = _text(value)

    metres = _number(signals.get("Vehicle.TraveledDistance"))
    if metres is not None:
        # The socket carried metres under both names, and whole miles
        # truncated: 12,345,000 m is 7,670.86 miles, and arrived as 7670.
        status["ODOMETER"] = status["ODOMETER_METER"] = str(int(metres))
        status["ODOMETER_MILES"] = str(int(metres // METRES_PER_MILE))

    boot = status.get("DOOR_IS_BOOT_LOCKED")
    if boot in ("TRUE", "FALSE"):
        status["DOOR_BOOT_LOCK_STATUS"] = "LOCKED" if boot == "TRUE" else "UNLOCKED"

    range_km = _number(signals.get("Vehicle.Powertrain.TractionBattery.Range"))
    if range_km is not None:
        status["EV_RANGE_ON_BATTERY_KM"] = str(int(range_km))
        status["EV_RANGE_ON_BATTERY_MILES"] = str(round(range_km / KM_PER_MILE))

    if reported:
        status["LAST_UPDATED_TIME"] = reported
    return status


def to_position(body: Mapping[str, Any]) -> dict[str, Any] | None:
    """The location endpoint's answer, shaped like the portal's position.

    None when there is no fix in it. The fix's own time is preferred over the
    response's, which is when the location was last updated rather than when
    the car was last located.
    """
    vehicle = body.get("Vehicle")
    if not isinstance(vehicle, Mapping):
        return None
    current = vehicle.get("CurrentLocation")
    if not isinstance(current, Mapping):
        return None
    latitude = _number(current.get("Latitude"))
    longitude = _number(current.get("Longitude"))
    if latitude is None or longitude is None:
        return None
    return {
        "latitude": latitude,
        "longitude": longitude,
        "heading": _number(current.get("Heading")),
        "speed": _number(vehicle.get("Speed")),
        "timestamp": current.get("Timestamp") or body.get("timestamp"),
    }


LATITUDE = "Vehicle.CurrentLocation.Latitude"
LONGITUDE = "Vehicle.CurrentLocation.Longitude"
POSITION_SIGNALS = frozenset(
    (LATITUDE, LONGITUDE, "Vehicle.CurrentLocation.Heading", "Vehicle.Speed")
)


def signals_to_position(
    signals: Mapping[str, Any], when: str | None
) -> dict[str, Any] | None:
    """A fix from the stream's separate signals, shaped as ``to_position``'s.

    The stream sends one signal per value where REST nests them, so this is the
    same fix read from dotted names. ``when`` is the fix's own time.
    """
    latitude = _number(signals.get(LATITUDE))
    longitude = _number(signals.get(LONGITUDE))
    if latitude is None or longitude is None:
        return None
    return {
        "latitude": latitude,
        "longitude": longitude,
        "heading": _number(signals.get("Vehicle.CurrentLocation.Heading")),
        "speed": _number(signals.get("Vehicle.Speed")),
        "timestamp": when,
    }


def to_attributes(body: Mapping[str, Any]) -> dict[str, Any]:
    """Vehicle details, under the attribute names the entities read.

    The same names the old attributes endpoint and the portal used, so the
    device name, the manufacturer and the electric check read them unchanged.
    No nickname: the Data Hub does not carry one, and the portal's (when it is
    signed in) is kept.
    """
    vehicle = body.get("Vehicle")
    if not isinstance(vehicle, Mapping):
        return {}
    identity = vehicle.get("VehicleIdentification")
    identity = identity if isinstance(identity, Mapping) else {}
    powertrain = vehicle.get("Powertrain")
    powertrain = powertrain if isinstance(powertrain, Mapping) else {}
    model = identity.get("Model")
    found: dict[str, Any] = {
        "vehicleBrand": identity.get("Brand"),
        "vehicleType": model,
        "model": model,
        "modelYear": identity.get("Year"),
        "registrationNumber": identity.get("LicensePlate"),
        "fuelType": powertrain.get("FuelType"),
    }
    exterior = vehicle.get("Exterior")
    if isinstance(exterior, Mapping):
        # Spelled both ways across JLR's own docs and answers.
        found["colour"] = exterior.get("Colour") or exterior.get("Color")
    # "5 Door SWB" says how many doors; "90 Standard" does not, and the rear
    # door sensors are then kept, as they are for any car that does not say.
    body_type = identity.get("BodyType")
    if isinstance(body_type, str):
        match = re.search(r"(\d+)\s*Door", body_type, re.IGNORECASE)
        if match:
            found["numberOfDoors"] = match.group(1)
    return {key: value for key, value in found.items() if value not in (None, "")}

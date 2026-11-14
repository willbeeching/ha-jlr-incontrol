"""Sensors for JLR InControl.

Values come from the flattened vehicle status dict (see coordinator data shape).
"""

from __future__ import annotations

import logging
import re
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
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    Platform,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfEnergyDistance,
    UnitOfLength,
    UnitOfPower,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import JlrConfigEntry
from .const import (
    CONF_APPLIED_UNITS,
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
from .entity import (
    JlrDataHubEntity,
    JlrVehicleEntity,
    async_add_vehicle_entities,
    has_no_engine,
    is_electrified,
    is_not_diesel,
)


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


def _not_negative(value: Any) -> float | None:
    """A reading that cannot be below zero, with JLR's negative sentinel unknown.

    A Range Rover Sport plug-in hybrid sitting full and not charging reported
    a charging rate of -1 %/h: not a rate, the car's way of having none.
    Shown as it came, it read as the battery draining by the hour.
    """
    number = _to_float(value)
    return None if number is None or number < 0 else number


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
    # Whether this reading is true at the moment it is taken and less true
    # every minute after. Coolant temperature is the whole of it so far: the
    # car latches the gauge at shutdown and keeps pushing that figure.
    decays: bool = False
    suppress_for_ev: bool = False
    # Only create on vehicles with a charge port; ICE cars report EV_* keys
    # with UNKNOWN sentinels, so key presence alone is not enough.
    requires_ev: bool = False
    # For a reading made of several keys, such as a time sent as separate
    # day, hour and minute. Given the whole status; status_key still decides
    # whether the car has the reading at all.
    status_fn: Callable[[dict[str, Any]], Any] | None = None
    # AdBlue: hidden on a car known to burn something other than diesel.
    requires_diesel: bool = False


_LOGGER = logging.getLogger(__name__)


def _alarm_state(value: str) -> str:
    """Humanise the THEFT_ALARM_STATUS enum (ALARM_NOT_SET__DOOR_OPEN etc.).

    Deliberately not a translated ENUM sensor, unlike ev_charging_status
    below. An ENUM has to declare every option it will ever report, and a
    value outside that list becomes an error and an unavailable entity for
    whoever's car produced it. JLR's alarm enum is demonstrably open-ended —
    ALARM_NOT_SET__DOOR_OPEN is one of a family whose other members nobody
    here has seen — so declaring it closed would break exactly the owners
    whose cars report something new.

    The two states worth automating on are already proper binary sensors
    (armed, and triggered), which is where the closed-world assumption is
    safe. This one is the readable detail next to them.
    """
    return (
        str(value).removeprefix("ALARM_").replace("__", "_").replace("_", " ").title()
    )


# EV_CHARGING_STATUS, as documented in docs/DATA_MODEL.md and confirmed live.
# Unlike the alarm enum this one is closed, so it can be a translated ENUM.
# "No Message" is what the key falls to after unplugging.
_CHARGING_STATES = {
    "CHARGING": "charging",
    "BULKCHARGED": "bulk_charged",
    "FULLYCHARGED": "fully_charged",
    "WAITINGTOCHARGE": "waiting_to_charge",
    "INITIALIZATION": "initialization",
    "PAUSED": "paused",
    "NOTCONNECTED": "not_connected",
    "FAULT": "fault",
    "NOMESSAGE": "no_message",
    # Not the socket's: what the Data Hub's EV endpoint can say for a car
    # plugged in and not charging, which does not say why. See vss.
    "NOTCHARGING": "not_charging",
}


# Charging values seen that this integration has no wording for. Remembered so
# each one is reported once rather than on every evaluation: an unrecognised
# value is not a transient — a car that starts reporting one keeps reporting
# it, so the naive version fills the log for as long as it charges.
_UNKNOWN_CHARGING_STATES: set[str] = set()


def _charging_status(value: str) -> str | None:
    """Map the raw charging enum to a translated option.

    Returning None rather than the raw string for anything unrecognised: an
    ENUM sensor whose value is not in its options logs an error on every
    update. The unknown value is reported once, so it can be added here.
    """
    raw = re.sub(r"[^A-Z]", "", str(value).upper())
    slug = _CHARGING_STATES.get(raw)
    if slug is None and raw and raw not in _UNKNOWN_CHARGING_STATES:
        _UNKNOWN_CHARGING_STATES.add(raw)
        _LOGGER.warning(
            "unrecognised EV_CHARGING_STATUS %r — please report it so the "
            "charging status sensor can show it",
            value,
        )
    return slug


def _words(value: Any) -> str | None:
    """An open-ended status word, readable: NORMAL_UNBLOCKED -> Normal unblocked.

    For the values JLR never documented a full list of. Not an ENUM for the
    reason the alarm state is not one: a value outside a declared list takes
    the entity unavailable for whoever's car sends it.
    """
    text = str(value).strip()
    if text.upper() in ("", "UNKNOWN"):
        return None
    # A state is capped at 255 characters, and the free-text cleaning fields
    # have no documented length.
    # The Data Hub sends some in camel case: TimedChargingNotActive.
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    text = _spaced(text)
    return text.replace("__", "_").replace("_", " ").capitalize()[:255]


# Words JLR run together with no case to split on: an I-Pace on 2.0.4 still
# read "Timedchargingnotactive" after the camel-case split, so its charging
# mode arrives as one word. Longest first, so "charging" wins over "charge".
_RUN_TOGETHER_WORDS = sorted(
    (
        "timed",
        "charging",
        "charge",
        "not",
        "active",
        "inactive",
        "immediate",
        "smart",
        "scheduled",
        "departure",
        "eco",
        "now",
        "off",
        "on",
        "mode",
        "plugged",
        "in",
        "unplugged",
        "paused",
        "waiting",
        "complete",
        "full",
    ),
    key=len,
    reverse=True,
)


def _spaced(text: str) -> str:
    """One run-together word split into known words, or left as it came.

    Only a word made wholly of known words is split, so a value JLR add later
    shows as sent rather than cut at a guess.
    """
    if " " in text or "_" in text or len(text) < 8:
        return text
    word, parts = text.lower(), []
    while word:
        part = next((w for w in _RUN_TOGETHER_WORDS if word.startswith(w)), None)
        if part is None:
            return text
        parts.append(part)
        word = word[len(part) :]
    return " ".join(parts)


def _enum(words: dict[str, str]) -> Callable[[Any], str | None]:
    """A closed list from JLR's catalog, as translated options.

    Anything outside it reads unknown rather than an error: the catalog is
    JLR's, and a car is free to send something it does not list.
    """

    def read(value: Any) -> str | None:
        return words.get(str(value).strip().upper())

    return read


def _tenths(value: Any) -> float | None:
    """A figure the car sends in tenths, such as AdBlue litres times ten."""
    number = _to_float(value)
    return None if number is None else round(number / 10, 1)


def _per_100(value: Any) -> float | None:
    """Wh/km, as JLR send consumption, in the kWh/100 km Home Assistant uses."""
    number = _to_float(value)
    return None if number is None else round(number / 10, 2)


def _centi_kwh(value: Any) -> float | None:
    """Usable energy, which JLR send in hundredths of a kWh.

    Passed on as kWh it read 2,580 kWh on an I-Pace at 34%: about 79 per
    percent of charge, so 79 kWh full, which only hundredths make sense of.
    """
    number = _to_float(value)
    return None if number is None else round(number / 100, 2)


def _wh_as_kwh(value: Any) -> float | None:
    """Regenerated energy, which JLR send in Wh, unlike usable energy.

    Measured against the JLR app: it rose 1,690 over a drive the app said
    regenerated 1.6 kWh.
    """
    number = _to_float(value)
    return None if number is None else round(number / 1000, 2)


def _watts(value: Any) -> float | None:
    """Wh per second, as JLR send standing consumption, as a power in watts."""
    number = _to_float(value)
    return None if number is None else round(number * 3600, 1)


def _whole(value: Any) -> int | None:
    number = _to_float(value)
    return None if number is None else int(number)


def _local(year: int, month: int, day: int, hour: int, minute: int) -> datetime | None:
    try:
        return datetime(
            year, month, day, hour, minute, tzinfo=dt_util.get_default_time_zone()
        )
    except (TypeError, ValueError):
        return None


# InControl cars date from 2015. JLR's placeholder for "no timer" is a date in
# 2000, which is not a timer anybody set.
DEPARTURE_EARLIEST_YEAR = 2015


def _departure(status: dict[str, Any]) -> datetime | None:
    """The departure timer as one time.

    In Home Assistant's time zone: the car keeps the clock its owner set, and
    the timer is read off that clock. Zeros are an unset timer, not midnight on
    the first of January, and so is a date before any of these cars was built:
    an I-Pace with no timer showed one "26 years ago".
    """
    parts = [
        _whole(status.get(f"DEPARTURE_TIMER_{part}"))
        for part in ("YEAR", "MONTH", "DAY", "HOUR", "MINUTE")
    ]
    if any(part is None for part in parts) or not all(parts[:3]):
        return None
    year, month, day, hour, minute = parts
    if year < DEPARTURE_EARLIEST_YEAR:  # type: ignore[operator]
        return None
    return _local(year, month, day, hour, minute)  # type: ignore[arg-type]


def _climate_timer(slot: int) -> Callable[[dict[str, Any]], datetime | None]:
    """A climate timer as one time.

    It carries a month and a day but no year, so the year is the one that puts
    it nearest to now: a timer set for 2 January read on 30 December is next
    week, not last January.
    """

    def read(status: dict[str, Any]) -> datetime | None:
        month, day, hour, minute = (
            _whole(status.get(f"CLIMATE_STATUS_TIMER{slot}_{part}"))
            for part in ("MONTH", "DAY", "HOUR", "MINUTE")
        )
        if not month or not day or hour is None or minute is None:
            return None
        now = dt_util.now()
        candidates = [
            when
            for year in (now.year - 1, now.year, now.year + 1)
            if (when := _local(year, month, day, hour, minute)) is not None
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda when: abs(when - now))

    return read


# Seat fold positions, from JLR's catalog.
_SEAT_FOLD = {
    "UNFOLDED": "unfolded",
    "FOLDED": "folded",
    "MOVING": "moving",
    "MIDTRAVELSTATIONARY": "part_folded",
}
_CLIMATE_MODES = {"IMMEDIATE": "immediate", "TIMED": "timed", "INACTIVE": "inactive"}
_CLIMATE_PRIORITIES = {
    "PRIORITIZE_COMFORT": "comfort",
    "PRIORITIZE_RANGE": "range",
}


def _minutes(key: str, status_key: str, **kwargs: Any) -> JlrSensorDescription:
    return JlrSensorDescription(
        key=key,
        translation_key=key,
        status_key=status_key,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        **kwargs,
    )


def _wording(key: str, status_key: str, **kwargs: Any) -> JlrSensorDescription:
    return JlrSensorDescription(
        key=key,
        translation_key=key,
        status_key=status_key,
        value_fn=_words,
        **kwargs,
    )


def _seat(key: str, status_key: str) -> JlrSensorDescription:
    # Off by default: a folded seat is rarely news, and a car with all seven
    # would otherwise add seven entities nobody asked for.
    return JlrSensorDescription(
        key=key,
        translation_key=key,
        status_key=status_key,
        value_fn=_enum(_SEAT_FOLD),
        device_class=SensorDeviceClass.ENUM,
        options=sorted(set(_SEAT_FOLD.values())),
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    )


VEHICLE_SENSORS: tuple[JlrSensorDescription, ...] = (
    JlrSensorDescription(
        key="alarm_state",
        translation_key="alarm_state",
        status_key="THEFT_ALARM_STATUS",
        value_fn=_alarm_state,
        # Detail, not a primary control surface: armed and triggered are
        # binary sensors, and this is the wording underneath them.
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    JlrSensorDescription(
        key="fuel_level",
        translation_key="fuel_level",
        status_key="FUEL_LEVEL_PERC",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        suppress_for_ev=True,
    ),
    JlrSensorDescription(
        key="distance_to_empty_fuel",
        translation_key="distance_to_empty_fuel",
        status_key="DISTANCE_TO_EMPTY_FUEL",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
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
        suggested_display_precision=0,
        attr_fn=_odometer_attrs,
    ),
    JlrSensorDescription(
        key="adblue_range",
        translation_key="adblue_range",
        status_key="EXT_EXHAUST_FLUID_DISTANCE_TO_SERVICE_KM",
        requires_diesel=True,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    JlrSensorDescription(
        key="distance_to_service",
        translation_key="distance_to_service",
        status_key="EXT_KILOMETERS_TO_SERVICE",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    JlrSensorDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        status_key="BATTERY_VOLTAGE",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
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
        entity_category=EntityCategory.DIAGNOSTIC,
        # Off by default for the same reason it has no device class: it reads
        # 0 whenever the car is asleep, so left on it writes a sawtooth into
        # the recorder that means nothing. battery_voltage is the real signal.
        entity_registry_enabled_default=False,
    ),
    JlrSensorDescription(
        # The raw VEHICLE_STATE_TYPE enum (KEY_REMOVED, KEY_ON_ENGINE_OFF,
        # ...). Not an ENUM device class deliberately: the same open-ended
        # argument as the alarm sensor above -- a value outside a declared
        # list would take the entity unavailable on whoever's car reports it.
        # Here so automations can tell a car left open on the drive from one
        # being driven with a window down.
        key="vehicle_state",
        translation_key="vehicle_state",
        status_key="VEHICLE_STATE_TYPE",
        value_fn=str,
        entity_category=EntityCategory.DIAGNOSTIC,
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
        decays=True,
        suppress_for_ev=True,
    ),
    JlrSensorDescription(
        key="fuel_volume",
        translation_key="fuel_volume",
        status_key="FUEL_LEVEL_LITRES",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        device_class=SensorDeviceClass.VOLUME_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        suppress_for_ev=True,
    ),
    JlrSensorDescription(
        # Everything the car can go on, fuel and battery together: a plug-in
        # hybrid's combined range. On a car with one source of energy it is
        # that source's range again under another name, so only hybrids.
        key="total_range",
        translation_key="total_range",
        status_key="DISTANCE_TO_EMPTY_TOTAL",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        suppress_for_ev=True,
        requires_ev=True,
    ),
    JlrSensorDescription(
        # In tenths of a litre, as the socket's key name says: 80 is 8 litres.
        # The socket called it the volume to refill and the Data Hub calls the
        # same figure the level; the socket's word is the one kept.
        key="adblue_to_refill",
        translation_key="adblue_to_refill",
        status_key="EXT_EXHAUST_FLUID_VOLUME_REFILL_LITRESX10",
        requires_diesel=True,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        # Stored volume, with a measurement state class: the room left in the
        # tank. Plain volume allows no measurement, which 1.9.0-beta.7 logged
        # as a bug; dropping the state class instead, in beta.8, orphaned the
        # long-term statistics beta.7 had already kept, and Home Assistant
        # asked the owner to delete them.
        device_class=SensorDeviceClass.VOLUME_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_tenths,
    ),
    _wording(
        "engine_immobiliser",
        "ENGINE_BLOCK",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _wording(
        "lifecycle_mode",
        "VEHICLE_LIFECYCLE_MODE",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # The 12V battery's own self-assessment. Off by default: the words are
    # the telematics unit's (BATTERY_1_1, NONE, ACCURATE) and nobody has
    # mapped them to anything an owner would act on; voltage is the signal.
    _wording(
        "battery_12v_status",
        "BATTERY_STATUS",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    _wording(
        "battery_12v_health",
        "BATTERY_STATUS_12V_HEALTH",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    _wording(
        "battery_12v_estimate",
        "BATTERY_STATUS_12V_ESTIMATE",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


# Climate, cabin air cleaning and the timers. Read-only: what the car has been
# told to do, not a way to tell it.
CABIN_SENSORS: tuple[JlrSensorDescription, ...] = (
    JlrSensorDescription(
        key="climate_mode",
        translation_key="climate_mode",
        status_key="CLIMATE_MODE",
        value_fn=_enum(_CLIMATE_MODES),
        device_class=SensorDeviceClass.ENUM,
        options=sorted(set(_CLIMATE_MODES.values())),
    ),
    JlrSensorDescription(
        key="climate_priority",
        translation_key="climate_priority",
        status_key="CLIMATE_PRIORITY",
        value_fn=_enum(_CLIMATE_PRIORITIES),
        device_class=SensorDeviceClass.ENUM,
        options=sorted(set(_CLIMATE_PRIORITIES.values())),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # It reads 30 with the climate off: it is how long a run lasts, counting
    # down only while one is going.
    _minutes(
        "climate_run_time",
        "CLIMATE_STATUS_REMAINING_RUNTIME",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _minutes(
        "climate_venting_time",
        "CLIMATE_STATUS_VENTING_TIME",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    _minutes(
        "heater_time_remaining",
        "CLIMATE_STATUS_FFH_REMAINING_RUNTIME",
        entity_category=EntityCategory.DIAGNOSTIC,
        suppress_for_ev=True,
    ),
    JlrSensorDescription(
        key="climate_timer_1",
        translation_key="climate_timer_1",
        status_key="CLIMATE_STATUS_TIMER1_MONTH",
        device_class=SensorDeviceClass.TIMESTAMP,
        status_fn=_climate_timer(1),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    JlrSensorDescription(
        key="climate_timer_2",
        translation_key="climate_timer_2",
        status_key="CLIMATE_STATUS_TIMER2_MONTH",
        device_class=SensorDeviceClass.TIMESTAMP,
        status_fn=_climate_timer(2),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    JlrSensorDescription(
        key="departure_time",
        translation_key="departure_time",
        status_key="DEPARTURE_TIMER_YEAR",
        device_class=SensorDeviceClass.TIMESTAMP,
        status_fn=_departure,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Cabin air cleaning. Off by default: a feature few cars have and fewer
    # owners run, and the free-text fields have no documented format.
    _minutes(
        "air_cleaning_cycle_time",
        "CAC_CYCLE_TIME",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _wording(
        "air_cleaning_last_cycle",
        "CAC_LAST_CYCLE_STATUS",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _wording(
        "air_cleaning_started",
        "CAC_CYCLE_START_TIME",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _wording(
        "air_cleaning_last_completed",
        "CAC_LAST_COMPLETED_CYCLE",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _wording(
        "air_cleaning_last_error",
        "CAC_LAST_CYCLE_ERROR",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    _seat("seat_front_right", "SEAT_FRONT_RIGHT_FOLDED"),
    _seat("seat_rear_left", "SEAT_REAR_LEFT_FOLDED"),
    _seat("seat_rear_right", "SEAT_REAR_RIGHT_FOLDED"),
    _seat("rear_armrest", "SEAT_REAR_ARMREST_FOLDED"),
    _seat("ski_hatch", "SEAT_SKI_HATCH_FOLDED"),
    _seat("seat_third_row_left", "SEAT_THIRD_ROW_LEFT_FOLDED"),
    _seat("seat_third_row_right", "SEAT_THIRD_ROW_RIGHT_FOLDED"),
)


# A real car tyre lives around 180-350 kPa; nothing sane exceeds this. JLR
# returns a near-int16-max sentinel (~32750 -> 3275 kPa -> 475 psi on all
# four wheels) when a fresh read has no valid pressure, seen live on an XF
# (#7). Reject anything above this as unknown rather than showing garbage.
_TYRE_KPA_MAX = 600.0


def _tyre_kpa(value: Any) -> float | None:
    """Normalise the raw tyre value to kPa, rejecting sentinels.

    The scale differs by vehicle generation: an L405/L663 reports kPa*10
    (e.g. 2470) while an I-Pace reports plain kPa (e.g. 279). Real tyre
    pressures sit around 180-350 kPa, so anything above 1000 must be the
    *10 scale. A plausibility clamp on the *result* catches bad-read
    sentinels regardless of which scale the car uses.
    """
    raw = _to_float(value)
    if raw is None:
        return None
    kpa = round(raw / 10, 1) if raw > 1000 else round(raw, 1)
    if kpa <= 0 or kpa > _TYRE_KPA_MAX:
        return None
    return kpa


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
        suggested_display_precision=0,
        requires_ev=True,
    ),
    JlrSensorDescription(
        key="ev_range_combined",
        translation_key="ev_range_combined",
        status_key="EV_PHEV_RANGE_COMBINED_MILES",
        native_unit_of_measurement=UnitOfLength.MILES,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
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
        value_fn=_not_negative,
        requires_ev=True,
    ),
    JlrSensorDescription(
        key="ev_charging_status",
        translation_key="ev_charging_status",
        status_key="EV_CHARGING_STATUS",
        value_fn=_charging_status,
        device_class=SensorDeviceClass.ENUM,
        options=sorted(set(_CHARGING_STATES.values())),
        requires_ev=True,
    ),
    # EV preconditioning countdown. The ICE CLIMATE_STATUS_REMAINING_RUNTIME
    # sensor doesn't tick during ECC preconditioning, but this EV-specific key
    # does (#8).
    JlrSensorDescription(
        key="ev_precondition_remaining",
        translation_key="ev_precondition_remaining",
        status_key="EV_PRECONDITION_REMAINING_RUNTIME_MINUTES",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=_not_negative,
        requires_ev=True,
    ),
    # From here on, from JLR's Data Hub catalog: units as the catalog gives
    # them, not yet matched against an electric car's own display.
    _minutes(
        "ev_time_to_bulk",
        "EV_MINUTES_TO_BULK_CHARGED",
        value_fn=_not_negative,
        requires_ev=True,
    ),
    JlrSensorDescription(
        key="ev_energy_last_charge",
        translation_key="ev_energy_last_charge",
        status_key="EV_ENERGY_CONSUMED_LAST_CHARGE_KWH",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=1,
        requires_ev=True,
    ),
    JlrSensorDescription(
        key="ev_charge_limit",
        translation_key="ev_charge_limit",
        status_key="EV_CHARGE_LIMIT",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=_not_negative,
        requires_ev=True,
    ),
    JlrSensorDescription(
        key="ev_charge_limit_one_off",
        translation_key="ev_charge_limit_one_off",
        status_key="EV_CHARGE_LIMIT_ONE_OFF",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=_not_negative,
        requires_ev=True,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _wording(
        "ev_charge_limit_one_off_mode",
        "EV_CHARGE_LIMIT_ONE_OFF_MODE",
        requires_ev=True,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _wording(
        "ev_charge_limit_permanent_mode",
        "EV_CHARGE_LIMIT_PERMANENT_MODE",
        requires_ev=True,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _wording("ev_charging_mode", "EV_CHARGING_MODE_CHOICE", requires_ev=True),
    JlrSensorDescription(
        # Range added per hour of charging: km/hr in the catalog.
        key="ev_charging_rate",
        translation_key="ev_charging_rate",
        status_key="EV_CHARGING_RATE_KM_PER_HOUR",
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=_not_negative,
        requires_ev=True,
    ),
    JlrSensorDescription(
        key="ev_charging_rate_percent",
        translation_key="ev_charging_rate_percent",
        status_key="EV_CHARGING_RATE_SOC_PER_HOUR",
        native_unit_of_measurement="%/h",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_not_negative,
        requires_ev=True,
    ),
    JlrSensorDescription(
        # The battery's true charge, where ev_battery is the figure shown on
        # the dash. Off by default: two percentages that disagree slightly
        # are more confusing than one.
        key="ev_battery_actual",
        translation_key="ev_battery_actual",
        status_key="EV_STATE_OF_CHARGE_ACTUAL",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        requires_ev=True,
    ),
    # What the car's range estimate is built from. Off by default: it is
    # there for the curious, not for a dashboard.
    *(
        JlrSensorDescription(
            key=f"ev_consumption_band_{band}",
            translation_key=f"ev_consumption_band_{band}",
            status_key=f"EV_CONSUMPTION_SPEED_BAND_{band}",
            native_unit_of_measurement=(UnitOfEnergyDistance.KILO_WATT_HOUR_PER_100_KM),
            device_class=SensorDeviceClass.ENERGY_DISTANCE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
            value_fn=_per_100,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            requires_ev=True,
        )
        for band in range(1, 8)
    ),
    *(
        JlrSensorDescription(
            key=key,
            translation_key=key,
            status_key=status_key,
            native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            suggested_display_precision=0,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            requires_ev=True,
        )
        for key, status_key in (
            ("ev_consumption_ascent", "EV_CONSUMPTION_ASCENT"),
            ("ev_consumption_descent", "EV_CONSUMPTION_DESCENT"),
        )
    ),
    JlrSensorDescription(
        key="ev_consumption_standing",
        translation_key="ev_consumption_standing",
        status_key="EV_CONSUMPTION_STATIONARY",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=_watts,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        requires_ev=True,
    ),
    *(
        JlrSensorDescription(
            key=key,
            translation_key=key,
            status_key=status_key,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY_STORAGE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
            value_fn=scale,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            requires_ev=True,
        )
        for key, status_key, scale in (
            ("ev_usable_energy", "EV_USABLE_ENERGY_INITIAL", _centi_kwh),
            (
                "ev_usable_energy_regenerative",
                "EV_USABLE_ENERGY_REGENERATIVE",
                _wh_as_kwh,
            ),
            ("ev_usable_energy_revised", "EV_USABLE_ENERGY_REVISED", _centi_kwh),
        )
    ),
)

ALL_SENSORS = (*VEHICLE_SENSORS, *CABIN_SENSORS, *TYRE_SENSORS, *EV_SENSORS)


@dataclass(frozen=True, kw_only=True)
class JlrJourneyDescription(SensorEntityDescription):
    """One reading of a car's last journey, from the Data Hub."""

    field: str


# Keys, not ids: the unique ids are f"{vin}_{key}" like every other sensor.
JOURNEY_SENSORS: tuple[JlrJourneyDescription, ...] = (
    JlrJourneyDescription(
        key="last_journey_distance",
        translation_key="last_journey_distance",
        field="distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_display_precision=1,
    ),
    JlrJourneyDescription(
        key="last_journey_duration",
        translation_key="last_journey_duration",
        field="duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
    ),
    JlrJourneyDescription(
        key="last_journey_average_speed",
        translation_key="last_journey_average_speed",
        field="average_speed",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        suggested_display_precision=0,
    ),
    JlrJourneyDescription(
        key="last_journey_end",
        translation_key="last_journey_end",
        field="ended",
        device_class=SensorDeviceClass.TIMESTAMP,
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
    if description.suppress_for_ev and has_no_engine(attributes, status):
        return False
    if description.requires_diesel and is_not_diesel(attributes, status):
        return False
    if description.requires_ev:
        return is_electrified(attributes, status)
    return True


def _distance_unit_override(entry: JlrConfigEntry) -> str | None:
    """Return a distance unit override from options, or None for HA default."""
    unit = entry.options.get(OPT_DISTANCE_UNIT, DISTANCE_UNIT_DEFAULT)
    if unit == DISTANCE_UNIT_MILES:
        return UnitOfLength.MILES
    if unit == DISTANCE_UNIT_KM:
        return UnitOfLength.KILOMETERS
    return None


def _speed_for(distance_unit: str | None) -> str | None:
    """The per-hour unit that goes with a distance unit.

    Charging rate is range gained per hour, a speed to Home Assistant, so the
    distance option did not reach it: an owner who chose miles still saw km/h.
    """
    if distance_unit == UnitOfLength.MILES:
        return UnitOfSpeed.MILES_PER_HOUR
    if distance_unit == UnitOfLength.KILOMETERS:
        return UnitOfSpeed.KILOMETERS_PER_HOUR
    return None


def _pressure_unit_override(entry: JlrConfigEntry) -> str | None:
    """Return a pressure unit override from options, or None for HA default."""
    unit = entry.options.get(OPT_PRESSURE_UNIT, PRESSURE_UNIT_DEFAULT)
    if unit == PRESSURE_UNIT_KPA:
        return UnitOfPressure.KPA
    if unit == PRESSURE_UNIT_BAR:
        return UnitOfPressure.BAR
    if unit == PRESSURE_UNIT_PSI:
        return UnitOfPressure.PSI
    return None


# Coordinator-backed and read-only: there is nothing to serialise, and
# leaving it unset means Home Assistant assumes otherwise.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: JlrConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up JLR sensors.

    Only add a sensor if the vehicle actually reports its status key, so signals
    a given model doesn't have (e.g. AdBlue on a non-diesel) aren't created and
    left showing unknown.
    """
    coordinator = entry.runtime_data
    distance_unit = _distance_unit_override(entry)
    pressure_unit = _pressure_unit_override(entry)

    def build(vin: str) -> list[Any]:
        vehicle = coordinator.data.get("vehicles", {}).get(vin, {})
        status = vehicle.get("status", {})
        attributes = vehicle.get("attributes", {})
        if not status:
            # No snapshot yet. Most sensors are gated on the status keys this
            # model reports, so building now would create almost none and mark
            # the car done; leaving it produces nothing and it is reconsidered
            # on the next update.
            return []
        entities: list[Any] = []
        entities.extend(
            JlrVehicleSensor(
                coordinator, vin, description, distance_unit, pressure_unit
            )
            for description in ALL_SENSORS
            if _should_create_sensor(description, status, attributes)
        )
        if attributes.get("colour"):
            entities.append(JlrColourSensor(coordinator, vin))
        entities.append(JlrLastUpdatedSensor(coordinator, vin))
        if coordinator.datahub and vin in coordinator.datahub.vehicles:
            entities.append(JlrDataHubReportSensor(coordinator, vin))
        entities.append(JlrAllInfoSensor(coordinator, vin))
        if is_electrified(attributes, status) and "EV_CHARGING_METHOD" in status:
            entities.append(JlrEvccStatusSensor(coordinator, vin))
        if is_electrified(attributes, status) and "EV_CHARGE_NOW_SETTING" in status:
            entities.append(JlrChargeNowSettingSensor(coordinator, vin))
        return entities

    def protect(vin: str) -> set[str]:
        """Ids withheld from the prune because the EV judgement is a guess.

        Every one of these is gated on is_electrified, which reads fuelType
        when it has it and falls back to looking for EV_STATE_OF_CHARGE when
        it does not. The fallback is sound for deciding whether to *create*
        an entity and far too thin for deciding to delete one and its history.

        The journey sensors are withheld too, for another reason: see
        ``journeys``.
        """
        attributes = (
            coordinator.data.get("vehicles", {}).get(vin, {}).get("attributes", {})
        )
        if attributes.get("fuelType"):
            return journeys(vin)
        keys = [d.key for d in EV_SENSORS if d.requires_ev]
        keys += ["evcc_status", "charge_now_setting"]
        # has_no_engine guesses the same way when fuelType is missing.
        keys += [d.key for d in ALL_SENSORS if d.suppress_for_ev]
        return {f"{vin}_{key}" for key in keys} | journeys(vin)

    def journeys(vin: str) -> set[str]:
        """Journey sensors, which arrive after the rest: never pruned with it.

        They are built when the link has read a journey, which is after the
        status that builds everything else, so the prune that follows that
        status would delete them, history and all, on every restart.
        """
        if coordinator.datahub is None or vin not in coordinator.datahub.vehicles:
            return set()
        return {f"{vin}_{d.key}" for d in JOURNEY_SENSORS}

    async_add_vehicle_entities(
        entry, Platform.SENSOR, coordinator, async_add_entities, build, protect
    )
    _add_journey_sensors(entry, coordinator, async_add_entities, distance_unit)

    _apply_unit_overrides(hass, entry, distance_unit, pressure_unit)


def _add_journey_sensors(
    entry: JlrConfigEntry,
    coordinator: JlrCoordinator,
    async_add_entities: AddEntitiesCallback,
    distance_unit: str | None,
) -> None:
    """Add a car's journey sensors once the link has read a journey for it.

    Not before: JLR refused journeys to every owner until November 2026, and
    a car that is still refused would carry four sensors that never read.
    """
    link = coordinator.datahub
    if link is None:
        return
    added: set[str] = set()

    @callback
    def add() -> None:
        fresh: list[JlrJourneySensor] = []
        for vin in list(link.journeys):
            if vin in added or vin not in coordinator.data.get("vehicles", {}):
                continue
            added.add(vin)
            fresh.extend(
                JlrJourneySensor(coordinator, vin, description, distance_unit)
                for description in JOURNEY_SENSORS
            )
        if fresh:
            async_add_entities(fresh)

    add()
    entry.async_on_unload(link.async_add_listener(add))
    entry.async_on_unload(coordinator.async_add_listener(add))


def _apply_unit_overrides(
    hass: HomeAssistant,
    entry: JlrConfigEntry,
    distance_unit: str | None,
    pressure_unit: str | None,
) -> None:
    """Push a changed unit option into the entity registry, and only then.

    suggested_unit_of_measurement only applies when an entity is first
    registered, so changing the option after setup did nothing for entities
    that already existed (#4); hence writing to the registry at all. New
    entities need none of this — they are built with the option as their
    suggested unit.

    It used to run on every setup, and with the option on the default it
    removed whatever unit an entity had been given — including one the user
    picked by hand in the entity's own settings, which went back to miles on
    every restart and update (#40). So it now acts only when the option has
    changed since it last ran, and going back to the default removes only
    the unit this wrote, leaving anything the user has chosen since alone.
    """
    applied = dict(entry.data.get(CONF_APPLIED_UNITS) or {})
    # Speed is kept apart from distance, though it follows it, so an entry
    # whose distance was applied before speed followed still gets its speed.
    wanted = {
        "distance": distance_unit,
        "pressure": pressure_unit,
        "speed": _speed_for(distance_unit),
    }
    if all(applied.get(kind) == unit for kind, unit in wanted.items()):
        return

    ent_reg = er.async_get(hass)
    vins = entry.runtime_data.data.get("vehicles", {})
    for vin in vins:
        for description in (*ALL_SENSORS, *JOURNEY_SENSORS):
            if description.device_class == SensorDeviceClass.DISTANCE:
                kind = "distance"
            elif description.device_class == SensorDeviceClass.PRESSURE:
                kind = "pressure"
            elif description.device_class == SensorDeviceClass.SPEED:
                kind = "speed"
            else:
                continue
            unit, before = wanted[kind], applied.get(kind)
            if unit == before:
                continue
            entity_id = ent_reg.async_get_entity_id(
                "sensor", DOMAIN, f"{vin}_{description.key}"
            )
            reg_entry = ent_reg.async_get(entity_id) if entity_id else None
            if entity_id is None or reg_entry is None:
                continue
            sensor_options = dict(reg_entry.options.get("sensor", {}))
            current = sensor_options.get("unit_of_measurement")
            if unit is not None:
                # A newly forced unit applies to every sensor of its kind:
                # choosing it is the user saying so.
                sensor_options["unit_of_measurement"] = unit
            elif current == before:
                # Back to the default, and this is still the unit we wrote.
                sensor_options.pop("unit_of_measurement", None)
            else:
                continue
            ent_reg.async_update_entity_options(
                entity_id, "sensor", sensor_options or None
            )

    remembered = {kind: unit for kind, unit in wanted.items() if unit is not None}
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_APPLIED_UNITS: remembered}
    )


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
        if distance_unit and description.device_class == SensorDeviceClass.SPEED:
            self._attr_suggested_unit_of_measurement = _speed_for(distance_unit)
        if pressure_unit and description.device_class == SensorDeviceClass.PRESSURE:
            self._attr_suggested_unit_of_measurement = pressure_unit

    @property
    def native_value(self) -> Any:
        if self.entity_description.decays and self._vehicle.get("readings_decayed"):
            # It was true when the car said it and it is not any more. These
            # cars latch the coolant gauge at shutdown and keep pushing that
            # figure, so an engine that went cold overnight still reports a
            # perfectly ordinary 89 °C — and nothing in the number gives it
            # away.
            return None
        if self.entity_description.status_fn is not None:
            return self.entity_description.status_fn(self._vehicle.get("status", {}))
        raw = self._status_value(self.entity_description.status_key)
        if raw is None or raw == "":
            return None
        return self.entity_description.value_fn(raw)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.entity_description.attr_fn(self._vehicle.get("status", {}))


class JlrLastUpdatedSensor(JlrVehicleEntity, SensorEntity):
    """Timestamp of the vehicle's last reported position / status."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _attr_translation_key = "last_updated"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

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
        # Of the status, matching native_value. This reported position_stale,
        # so a car sending telemetry every few minutes from a spot it parked in
        # three days ago showed a timestamp from moments ago marked stale.
        return {"stale": self._vehicle.get("status_stale", False)}


class JlrDataHubReportSensor(JlrDataHubEntity, SensorEntity):
    """When the car last reported, as the JLR Data Hub stamps it.

    The first vehicle-side time anything here has had. Every other timestamp
    is when a message reached us — measured three hours after the car sent it,
    once — or inferred from the odometer.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "datahub_last_report"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: JlrCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin)
        self._attr_unique_id = f"{vin}_datahub_last_report"

    @property
    def native_value(self) -> datetime | None:
        link = self.coordinator.datahub
        return link.reports.get(self._vin) if link else None


class JlrJourneySensor(JlrDataHubEntity, SensorEntity):
    """The car's last journey: distance, duration, average speed, or its end.

    From the Data Hub's journeys, which give each in full once the journey is
    over. Nothing here is live: the values change when a journey ends.
    """

    entity_description: JlrJourneyDescription

    def __init__(
        self,
        coordinator: JlrCoordinator,
        vin: str,
        description: JlrJourneyDescription,
        distance_unit: str | None,
    ) -> None:
        super().__init__(coordinator, vin)
        self.entity_description = description
        self._attr_unique_id = f"{vin}_{description.key}"
        if distance_unit and description.device_class == SensorDeviceClass.DISTANCE:
            self._attr_suggested_unit_of_measurement = distance_unit
        if distance_unit and description.device_class == SensorDeviceClass.SPEED:
            self._attr_suggested_unit_of_measurement = _speed_for(distance_unit)

    @property
    def _journey(self) -> dict[str, Any]:
        link = self.coordinator.datahub
        return link.journeys.get(self._vin, {}) if link else {}

    @property
    def native_value(self) -> Any:
        return self._journey.get(self.entity_description.field)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.field != "ended":
            return None
        started = self._journey.get("started")
        return {"started": started.isoformat() if started else None}


class JlrAllInfoSensor(JlrVehicleEntity, SensorEntity):
    """Flattened vehicle status as attributes (disabled by default)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _attr_translation_key = "all_info"
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


class JlrColourSensor(JlrVehicleEntity, SensorEntity):
    """The paint, as JLR's vehicle details give it."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "colour"

    def __init__(self, coordinator: JlrCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin)
        self._attr_unique_id = f"{vin}_colour"

    @property
    def native_value(self) -> str | None:
        colour = self._vehicle.get("attributes", {}).get("colour")
        return str(colour)[:255] if colour else None


class JlrEvccStatusSensor(JlrVehicleEntity, SensorEntity):
    """IEC 61851-style connector state (A/B/C) for EVCC-style automations.

    Mapping verified live across all four wallbox states on an I-Pace (#1):
    A = disconnected, B = connected but not charging (incl. INITIALIZATION
    while the wallbox withholds power), C = charging.
    """

    _attr_translation_key = "evcc_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["A", "B", "C"]
    # Exists for wallbox controllers to read, not for a dashboard. Anyone
    # wiring up surplus charging can switch it on; everyone else should not
    # be paying recorder space for an IEC 61851 connector letter.
    _attr_entity_registry_enabled_default = False

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
    actively suppressed) — both looked like "off" (#6).

    Read-only in the strict sense: the buttons that used to set it were remote
    commands and are gone. What it is for now is telling an automation why a
    plugged-in car is not charging, which "off" never could.
    """

    _attr_translation_key = "charge_now_setting"
    _attr_device_class = SensorDeviceClass.ENUM
    # Lower case because Home Assistant will not translate an option that is
    # not [a-z0-9-_]+, and an untranslated enum shows the raw word.
    _attr_options = ["default", "force_on", "force_off"]
    # Niche: it answers "why is this plugged-in car not charging", which
    # matters when it matters and is noise the rest of the time.
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: JlrCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin)
        self._attr_unique_id = f"{vin}_charge_now_setting"

    @property
    def native_value(self) -> str | None:
        value = self.coordinator.charge_now_setting(self._vin)
        slug = value.lower() if value else None
        return slug if slug in self._attr_options else None

"""Every reading the Data Hub sends, as an entity.

Driven end to end: a Data Hub snapshot in JLR's own nested shape goes through
the translation, the coordinator and the platforms of a running Home
Assistant, and what comes out is read back from the state machine. The values
are made up, in the shapes JLR's catalog gives.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT, Doubles, FakeDataHub  # noqa: E402
from homeassistant.const import STATE_UNKNOWN  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.helpers.entity import Entity  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol import datahub_link  # noqa: E402
from custom_components.jlr_incontrol.const import DOMAIN  # noqa: E402

SETTINGS = {
    "client_id": "dsas-0123456789ab",
    "private_key": "not used by the fake",
    "certificate": "",
    "refresh_token": "offline-1",
    "vehicles": [KEPT],
    "account_id": "acct-1",
}


def seat(folded: str, held: str) -> dict[str, Any]:
    return {"IsFolded": folded, "IsMovementInhibited": held}


VEHICLE: dict[str, Any] = {
    "VehicleIdentification": {"VIN": KEPT},
    "TraveledDistance": 12345000,
    "Body": {
        "Hood": {"IsLocked": "LOCKED"},
        "Convertible": {"IsOpen": "FALSE"},
        "Lights": {"Beam": {"Low": {"IsOn": "TRUE"}}},
        "Security": {
            "Alarm": {"Mode": "ALARM_ARMED"},
            "IsEngineBlocked": "NORMAL_UNBLOCKED",
            "IsPanicAlarmTriggered": "UNKNOWN",
        },
        "Crash": {
            "Airbag": {"IsFired": "SRS_NOT_DEPLOYED"},
            "Front": {"IsImpactDetected": "FALSE"},
        },
    },
    "Connectivity": {"PrivacyMode": "FALSE"},
    "Chassis": {
        "Axle": {
            "Row1": {
                "Wheel": {
                    "Left": {"Tire": {"IsPressureLow": "NORMAL"}},
                    "Right": {"Tire": {"IsPressureLow": "LOW"}},
                }
            }
        }
    },
    "LowVoltageBattery": {
        "IsBatteryLow": "BATTERY_1_1",
        "StateOfHealth": "NONE",
        "StateOfChargeEstimate": "ACCURATE",
    },
    "System": {"Lifecycle": {"Mode": "CUSTOMER"}},
    "Cabin": {
        "HVAC": {
            "IsAirConditioningActive": "FALSE",
            "AirConditioning": {
                "Status": "OFF",
                "Mode": "TIMED",
                "PrioritySetting": "PRIORITIZE_COMFORT",
                "TimeRemaining": 30,
                "VentingTime": 30,
                "FuelFiredHeater": {"Setting": "ENABLED", "TimeRemaining": 0},
            },
            "Schedule": {
                "IsActive": "TRUE",
                "Primary": {"Month": 11, "Day": 9, "Hour": 6, "Minute": 45},
                "Secondary": {"Month": 0, "Day": 0, "Hour": 0, "Minute": 0},
            },
            "Cleaning": {
                "IsCycleActive": "INACTIVE",
                "CycleTimeRemaining": 40,
                "LastCycleStatus": "NO_LAST_CYCLE",
                "LastCycle": {"Error": "NONE"},
            },
        },
        "Seat": {
            "Row1": {"PassengerSide": {"Backrest": seat("UNFOLDED", "NOTINHIBITED")}},
            "Row2": {
                "DriverSide": {"Backrest": seat("FOLDED", "NOTINHIBITED")},
                "PassengerSide": {"Backrest": seat("MIDTRAVELSTATIONARY", "INHIBITED")},
                "Middle": {
                    "Armrest": seat("UNFOLDED", "NOTINHIBITED"),
                    "SkiHatch": seat("SOMETHING_NEW", "NOTINHIBITED"),
                },
            },
        },
    },
    "Schedule": {
        "Departure": {
            "IsSet": "TRUE",
            "Year": 2026,
            "Month": 11,
            "Day": 8,
            "Hour": 7,
            "Minute": 30,
        }
    },
    "Powertrain": {
        "Range": 640,
        "FuelSystem": {"RelativeLevel": 42, "AbsoluteLevel": 33, "Range": 500},
        "CombustionEngine": {
            "DieselExhaustFluid": {"Level": 80},
            "Diesel": {"ParticulateFilter": {"IsLevelLow": "CLEAR"}},
        },
    },
}

ELECTRIC: dict[str, Any] = {
    "VehicleIdentification": {"VIN": KEPT},
    "TraveledDistance": 12345000,
    "Powertrain": {
        # An electric car can still send the fuel signals, empty.
        "FuelSystem": {"AbsoluteLevel": 0},
        "TractionBattery": {
            "StateOfCharge": {"Displayed": 80, "Current": 78},
            "Range": 300,
            "BatteryConditioning": {"IsActive": "TRUE"},
            "Charging": {
                "Status": "CHARGING",
                "Method": "WIRED",
                "IsCharging": "TRUE",
                "ChargingPort": {"AnyPosition": {"IsChargingCableConnected": "TRUE"}},
                "TimeToBulkCharged": 45,
                "Session": {"Energy": 12},
                "ChargeLimit": 90,
                "ChargeLimitSetting": {
                    "OneOffValue": 100,
                    "OneOffMode": "INACTIVE",
                    "PermanentMode": "ACTIVE",
                },
                "ChargeRate": 40,
                "StateOfChargeRate": 9.5,
                "ChargingModeChoice": "SMART",
            },
            "RangeEstimation": {
                "Consumption": {
                    "SpeedBracket": {f"SB{n}": 100 + 20 * n for n in range(1, 8)},
                    "Ascent": 300.0,
                    "Descent": 120.0,
                    "Stationary": 0.1,
                },
                "UsableEnergy": {
                    "Initial": 84.5,
                    "Regenerative": 2.5,
                    "Revised": 83.0,
                },
            },
        },
    },
}


@pytest.fixture
def snapshot() -> dict[str, Any]:
    return VEHICLE


@pytest.fixture
def fuel(snapshot: dict[str, Any]) -> str:
    return "electric" if snapshot is ELECTRIC else "diesel"


@pytest.fixture
def body() -> str:
    return "5 Door"


@pytest.fixture
def ev_answer() -> dict[str, Any] | None:
    """What the EV endpoint answers; None keeps the fake's refusal."""
    return None


@pytest.fixture
def every_entity_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # Most of these are off by default, and an entity that is off has no
    # state to read. Core's own tests switch them on the same way.
    monkeypatch.setattr(
        Entity, "entity_registry_enabled_default", property(lambda self: True)
    )


@pytest.fixture
async def car(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    doubles: Doubles,
    monkeypatch: pytest.MonkeyPatch,
    every_entity_on: None,
    snapshot: dict[str, Any],
    fuel: str,
    body: str,
    ev_answer: dict[str, Any] | None,
) -> AsyncIterator[None]:
    """A car on the Data Hub, every entity switched on, holding ``snapshot``."""

    class Hub(FakeDataHub):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.status_result = {
                "timestamp": "2026-11-07T09:00:00.000Z",
                "Vehicle": snapshot,
            }
            self.vehicle_result["Vehicle"]["Powertrain"] = {"FuelType": fuel}
            self.vehicle_result["Vehicle"]["Exterior"] = {"Colour": "Santorini Black"}
            self.vehicle_result["Vehicle"]["VehicleIdentification"]["BodyType"] = body
            if ev_answer is not None:
                self.ev_result = ev_answer

    monkeypatch.setattr(datahub_link, "DataHubClient", doubles.builder("datahub", Hub))

    async def parked(**kwargs: Any) -> str:
        await asyncio.Event().wait()
        return "unreachable"

    monkeypatch.setattr(datahub_link, "run_session", parked)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "datahub": dict(SETTINGS)}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield
    await hass.config_entries.async_unload(entry.entry_id)


def state(hass: HomeAssistant, platform: str, key: str) -> str | None:
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{KEPT}_{key}"
    )
    if entity_id is None:
        return None
    found = hass.states.get(entity_id)
    return None if found is None else found.state


def local(hass: HomeAssistant, key: str) -> tuple[int, ...]:
    value = state(hass, "sensor", key)
    parsed = dt_util.parse_datetime(value or "")
    assert parsed is not None, value
    when = dt_util.as_local(parsed)
    return (when.year, when.month, when.day, when.hour, when.minute)


@pytest.mark.usefixtures("car")
class TestTheCarsOwnReadings:
    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("fuel_volume", "33.0"),
            # Tenths of a litre on the wire.
            ("adblue_to_refill", "8.0"),
            ("engine_immobiliser", "Normal unblocked"),
            ("lifecycle_mode", "Customer"),
            ("battery_12v_status", "Battery 1 1"),
            ("battery_12v_health", "None"),
            ("battery_12v_estimate", "Accurate"),
            ("climate_mode", "timed"),
            ("climate_priority", "comfort"),
            ("climate_run_time", "30.0"),
            ("climate_venting_time", "30.0"),
            ("heater_time_remaining", "0.0"),
            ("air_cleaning_cycle_time", "40.0"),
            ("air_cleaning_last_cycle", "No last cycle"),
            ("air_cleaning_last_error", "None"),
            ("colour", "Santorini Black"),
            ("seat_front_right", "unfolded"),
            ("seat_rear_left", "folded"),
            ("seat_rear_right", "part_folded"),
            ("rear_armrest", "unfolded"),
        ],
    )
    def test_a_sensor(self, hass: HomeAssistant, key: str, expected: str) -> None:
        assert state(hass, "sensor", key) == expected

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("bonnet_lock", "off"),
            ("headlights", "on"),
            # UNKNOWN from the car is unknown here, not "no panic".
            ("panic_alarm", STATE_UNKNOWN),
            ("airbag_deployed", "off"),
            ("front_impact", "off"),
            ("privacy_mode", "off"),
            ("particulate_filter_warning", "off"),
            ("tyre_warning_fl", "off"),
            ("tyre_warning_fr", "on"),
            ("climate", "off"),
            ("air_conditioning", "off"),
            ("climate_timer", "on"),
            ("departure_timer", "on"),
            ("heater_enabled", "on"),
            ("air_cleaning", "off"),
            ("seat_rear_left_held", "off"),
            ("seat_rear_right_held", "on"),
        ],
    )
    def test_a_binary_sensor(
        self, hass: HomeAssistant, key: str, expected: str
    ) -> None:
        assert state(hass, "binary_sensor", key) == expected

    def test_the_departure_timer_is_one_time(self, hass: HomeAssistant) -> None:
        # Read off the car's clock, which is the owner's: Home Assistant's
        # time zone, not UTC.
        assert local(hass, "departure_time") == (2026, 11, 8, 7, 30)

    def test_a_climate_timer_takes_the_year_nearest_now(
        self, hass: HomeAssistant
    ) -> None:
        # No year comes with it, so only the rest can be checked here.
        assert local(hass, "climate_timer_1")[1:] == (11, 9, 6, 45)

    def test_an_unset_timer_reads_unknown_not_new_year(
        self, hass: HomeAssistant
    ) -> None:
        assert state(hass, "sensor", "climate_timer_2") == STATE_UNKNOWN

    def test_a_fold_position_outside_the_catalog_is_unknown(
        self, hass: HomeAssistant
    ) -> None:
        assert state(hass, "sensor", "ski_hatch") == STATE_UNKNOWN

    def test_a_reading_the_car_does_not_send_has_no_entity(
        self, hass: HomeAssistant
    ) -> None:
        # No third row on this car, so no third-row entities.
        assert state(hass, "sensor", "seat_third_row_left") is None
        assert state(hass, "binary_sensor", "seat_third_row_left_held") is None

    def test_a_car_with_no_charge_port_gets_none_of_the_electric_ones(
        self, hass: HomeAssistant
    ) -> None:
        assert state(hass, "sensor", "ev_charge_limit") is None


@pytest.mark.usefixtures("car")
class TestWhatIsLeftOff:
    """A reading that means nothing for this kind of car has no entity."""

    def test_a_diesel_has_no_total_range_or_soft_top(self, hass: HomeAssistant) -> None:
        # Its total range is its fuel range, and a hard-top still reports the
        # convertible roof key.
        assert state(hass, "sensor", "total_range") is None
        assert state(hass, "binary_sensor", "convertible_roof") is None
        assert state(hass, "sensor", "adblue_to_refill") is not None

    @pytest.mark.parametrize("fuel", ["petrol"])
    def test_a_petrol_car_has_no_adblue(self, hass: HomeAssistant) -> None:
        # It sends the AdBlue keys all the same.
        assert state(hass, "sensor", "adblue_to_refill") is None
        assert state(hass, "binary_sensor", "particulate_filter_warning") is not None

    @pytest.mark.parametrize("fuel", ["petrol plug-in hybrid"])
    def test_a_hybrid_has_its_combined_range(self, hass: HomeAssistant) -> None:
        assert state(hass, "sensor", "total_range") == "640.0"

    @pytest.mark.parametrize("body", ["2 Door Convertible"])
    def test_a_soft_top_has_its_roof(self, hass: HomeAssistant) -> None:
        assert state(hass, "binary_sensor", "convertible_roof") == "off"


@pytest.mark.usefixtures("car")
@pytest.mark.parametrize("snapshot", [ELECTRIC])
class TestAnElectricCar:
    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("ev_time_to_bulk", "45.0"),
            ("ev_energy_last_charge", "12.0"),
            ("ev_charge_limit", "90.0"),
            ("ev_charge_limit_one_off", "100.0"),
            ("ev_charge_limit_one_off_mode", "Inactive"),
            ("ev_charge_limit_permanent_mode", "Active"),
            ("ev_charging_mode", "Smart"),
            ("ev_charging_rate", "40.0"),
            ("ev_charging_rate_percent", "9.5"),
            ("ev_battery_actual", "78.0"),
            # 120 Wh/km is 12 kWh/100 km.
            ("ev_consumption_band_1", "12.0"),
            ("ev_consumption_band_7", "24.0"),
            ("ev_consumption_ascent", "300.0"),
            ("ev_consumption_descent", "120.0"),
            # 0.1 Wh every second is 360 W.
            ("ev_consumption_standing", "360.0"),
            ("ev_usable_energy", "84.5"),
            ("ev_usable_energy_regenerative", "2.5"),
            ("ev_usable_energy_revised", "83.0"),
        ],
    )
    def test_a_sensor(self, hass: HomeAssistant, key: str, expected: str) -> None:
        assert state(hass, "sensor", key) == expected

    @pytest.mark.parametrize(
        "key", ["ev_charging_flag", "ev_cable_connected", "ev_battery_conditioning"]
    )
    def test_a_binary_sensor(self, hass: HomeAssistant, key: str) -> None:
        assert state(hass, "binary_sensor", key) == "on"

    def test_a_fuel_reading_is_not_made_for_it(self, hass: HomeAssistant) -> None:
        assert state(hass, "sensor", "fuel_volume") is None


def ev_endpoint(charging: bool, plugged: bool, **extra: Any) -> dict[str, Any]:
    """The EV endpoint's answer, in the shape JLR's API spec gives it."""
    return {
        "timestamp": "2026-11-07T09:00:00Z",
        "Vehicle": {
            "Powertrain": {
                "TractionBattery": {
                    "StateOfCharge": {"Current": 64},
                    "Range": 40,
                    "Charging": {
                        "IsCharging": charging,
                        "IsPlugConnected": plugged,
                        "ChargeLimit": 80,
                        **extra,
                    },
                }
            }
        },
    }


@pytest.mark.usefixtures("car")
@pytest.mark.parametrize("fuel", ["petrol plug-in hybrid"])
class TestAPlugInHybridOnTheEvEndpoint:
    """The battery from the EV endpoint, the status snapshot having none.

    What an owner on a Range Rover Sport plug-in hybrid reported: on the Data
    Hub the battery, charging status and time to full were gone, because the
    status snapshot carries none of them and nothing asked the EV endpoint.
    """

    @pytest.mark.parametrize("ev_answer", [ev_endpoint(True, True)])
    def test_charging(self, hass: HomeAssistant) -> None:
        assert state(hass, "sensor", "ev_battery") == "64.0"
        assert state(hass, "sensor", "ev_charging_status") == "charging"
        assert state(hass, "sensor", "ev_charge_limit") == "80.0"
        assert state(hass, "binary_sensor", "ev_charging") == "on"
        assert state(hass, "binary_sensor", "ev_plugged_in") == "on"
        assert state(hass, "sensor", "evcc_status") == "C"

    @pytest.mark.parametrize("ev_answer", [ev_endpoint(False, True)])
    def test_plugged_in_and_not_charging(self, hass: HomeAssistant) -> None:
        assert state(hass, "sensor", "ev_charging_status") == "not_charging"
        assert state(hass, "binary_sensor", "ev_charging") == "off"
        assert state(hass, "binary_sensor", "ev_plugged_in") == "on"
        assert state(hass, "sensor", "evcc_status") == "B"

    @pytest.mark.parametrize("ev_answer", [ev_endpoint(False, False)])
    def test_unplugged(self, hass: HomeAssistant) -> None:
        assert state(hass, "sensor", "ev_charging_status") == "not_connected"
        assert state(hass, "binary_sensor", "ev_plugged_in") == "off"
        assert state(hass, "sensor", "evcc_status") == "A"

    @pytest.mark.parametrize(
        "ev_answer",
        [ev_endpoint(False, False, ChargeRate=0, StateOfChargeRate=-1)],
    )
    def test_a_negative_rate_is_no_rate(self, hass: HomeAssistant) -> None:
        # What a Range Rover Sport plug-in hybrid sent sitting full and not
        # charging: -1 %/h, which read as the battery draining by the hour.
        assert state(hass, "sensor", "ev_charging_rate_percent") == STATE_UNKNOWN
        # A real zero stays a zero.
        assert state(hass, "sensor", "ev_charging_rate") == "0.0"

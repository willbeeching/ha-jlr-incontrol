"""The translation from Data Hub signals to the socket's status keys.

The pairs checked here were taken from two cars read through both sources at
the same moment in November 2026: the Data Hub's values and the socket's sat
side by side and matched. The numbers below are stand-ins of the same shape,
not those cars' readings.
"""

from __future__ import annotations

import pytest

from custom_components.jlr_incontrol.vss import (
    signals_to_position,
    to_attributes,
    to_position,
    to_status,
)

REPORTED = "2026-11-07T09:00:00.000Z"

# One of every kind of signal a car sent, in the Data Hub's own names.
SIGNALS = {
    "Vehicle.Body.Door.Lock.Status": "TRUE",
    "Vehicle.Body.Trunk.Rear.IsLocked": "TRUE",
    "Vehicle.Body.Trunk.Rear.IsOpen": "CLOSED",
    "Vehicle.Body.Hood.IsLocked": "LOCKED",
    "Vehicle.Body.Hood.IsOpen": "CLOSED",
    "Vehicle.Body.Roof.IsOpen": "FALSE",
    "Vehicle.Body.Security.Alarm.Mode": "ALARM_OFF",
    "Vehicle.Cabin.Door.Row1.DriverSide.IsLocked": "LOCKED",
    "Vehicle.Cabin.Door.Row1.DriverSide.IsOpen": "CLOSED",
    "Vehicle.Cabin.Door.Row1.DriverSide.Window.Position": "OPEN",
    "Vehicle.Cabin.Door.Row1.PassengerSide.Window.Position": "CLOSED",
    "Vehicle.Cabin.Door.Row2.PassengerSide.IsLocked": "UNLOCKED",
    "Vehicle.Chassis.Axle.Row1.Wheel.Left.Tire.Pressure": 2300,
    "Vehicle.Chassis.Axle.Row2.Wheel.Right.Tire.Pressure": 2450,
    "Vehicle.Chassis.Axle.Row1.Wheel.Left.Tire.IsPressureLow": "NORMAL",
    "Vehicle.Chassis.Brake.FluidLevel.IsLevelLow": "NORMAL",
    "Vehicle.LowVoltageBattery.CurrentVoltage": 14.0,
    "Vehicle.LowVoltageBattery.StateOfCharge": 90,
    "Vehicle.Powertrain.CombustionEngine.EngineCoolant.Temperature": 88,
    "Vehicle.Powertrain.CombustionEngine.DieselExhaustFluid.Level": 60,
    "Vehicle.Powertrain.FuelSystem.RelativeLevel": 40,
    "Vehicle.Powertrain.FuelSystem.Range": 300,
    "Vehicle.Service.DistanceToService": 12000,
    "Vehicle.System.DriveCycle.Status": "KEY_ON_ENGINE_OFF",
    "Vehicle.TraveledDistance": 12345000,
    "Vehicle.Cabin.HVAC.Schedule.Secondary.Minute": 0,
}


class TestRenaming:
    def test_values_cross_unchanged_under_the_sockets_names(self) -> None:
        status = to_status(SIGNALS, REPORTED)
        assert status["DOOR_IS_ALL_DOORS_LOCKED"] == "TRUE"
        assert status["DOOR_ENGINE_HOOD_LOCK_STATUS"] == "LOCKED"
        assert status["THEFT_ALARM_STATUS"] == "ALARM_OFF"
        assert status["TYRE_PRESSURE_FRONT_LEFT"] == "2300"
        assert status["TYRE_PRESSURE_REAR_RIGHT"] == "2450"
        assert status["TYRE_STATUS_FRONT_LEFT"] == "NORMAL"
        assert status["FUEL_LEVEL_PERC"] == "40"
        assert status["DISTANCE_TO_EMPTY_FUEL"] == "300"
        assert status["EXT_KILOMETERS_TO_SERVICE"] == "12000"
        assert status["EXT_EXHAUST_FLUID_VOLUME_REFILL_LITRESX10"] == "60"
        assert status["ENGINE_COOLANT_TEMP"] == "88"
        assert status["VEHICLE_STATE_TYPE"] == "KEY_ON_ENGINE_OFF"
        assert status["CLIMATE_STATUS_TIMER2_MINUTE"] == "0"

    def test_numbers_arrive_as_the_socket_sent_them(self) -> None:
        # Strings, as the socket sent everything; a float keeps its ".0".
        status = to_status(SIGNALS, REPORTED)
        assert status["BATTERY_VOLTAGE"] == "14.0"
        assert status["BATTERY_STATUS_12V_SOC"] == "90"
        assert all(isinstance(value, str) for value in status.values())

    def test_the_driver_side_is_the_sockets_left(self) -> None:
        # Measured on a right-hand-drive car: its driver-side window open on
        # the Data Hub was WINDOW_FRONT_LEFT_STATUS open on the socket.
        status = to_status(SIGNALS, REPORTED)
        assert status["WINDOW_FRONT_LEFT_STATUS"] == "OPEN"
        assert status["WINDOW_FRONT_RIGHT_STATUS"] == "CLOSED"
        assert status["DOOR_FRONT_LEFT_LOCK_STATUS"] == "LOCKED"
        assert status["DOOR_REAR_RIGHT_LOCK_STATUS"] == "UNLOCKED"

    def test_a_signal_with_no_socket_name_is_left_out(self) -> None:
        status = to_status({"Vehicle.Something.New": "X"}, None)
        assert status == {}

    def test_a_missing_value_is_left_out_not_written_as_none(self) -> None:
        status = to_status({"Vehicle.Powertrain.FuelSystem.RelativeLevel": None}, None)
        assert "FUEL_LEVEL_PERC" not in status

    def test_the_data_hubs_own_signals_get_names_in_the_sockets_style(self) -> None:
        status = to_status(
            {
                "Vehicle.Powertrain.FuelSystem.AbsoluteLevel": 41,
                "Vehicle.Cabin.Seat.Row2.DriverSide.Backrest.IsFolded": "FOLDED",
                "Vehicle.Schedule.Departure.Hour": 7,
                "Vehicle.Powertrain.TractionBattery.RangeEstimation."
                "Consumption.SpeedBracket.SB3": 180,
            },
            None,
        )
        assert status == {
            "FUEL_LEVEL_LITRES": "41",
            "SEAT_REAR_LEFT_FOLDED": "FOLDED",
            "DEPARTURE_TIMER_HOUR": "7",
            "EV_CONSUMPTION_SPEED_BAND_3": "180",
        }

    def test_a_true_boolean_reads_as_the_socket_wrote_it(self) -> None:
        status = to_status({"Vehicle.Body.Roof.IsOpen": True}, None)
        assert status["IS_SUNROOF_OPEN"] == "TRUE"


class TestDerived:
    def test_the_odometer_in_metres_and_whole_miles(self) -> None:
        # The socket truncated rather than rounded: 12,345,000 m is 7,670.86 miles.
        status = to_status({"Vehicle.TraveledDistance": 12345000}, None)
        assert status["ODOMETER"] == status["ODOMETER_METER"] == "12345000"
        assert status["ODOMETER_MILES"] == "7670"

    def test_an_unreadable_odometer_is_left_out(self) -> None:
        for value in ("not a number", True, None):
            status = to_status({"Vehicle.TraveledDistance": value}, None)
            assert "ODOMETER_MILES" not in status

    def test_the_boot_lock_gets_its_word(self) -> None:
        locked = to_status({"Vehicle.Body.Trunk.Rear.IsLocked": "TRUE"}, None)
        open_ = to_status({"Vehicle.Body.Trunk.Rear.IsLocked": "FALSE"}, None)
        odd = to_status({"Vehicle.Body.Trunk.Rear.IsLocked": "UNKNOWN"}, None)
        assert locked["DOOR_BOOT_LOCK_STATUS"] == "LOCKED"
        assert open_["DOOR_BOOT_LOCK_STATUS"] == "UNLOCKED"
        assert "DOOR_BOOT_LOCK_STATUS" not in odd

    def test_electric_range_in_both_units(self) -> None:
        status = to_status({"Vehicle.Powertrain.TractionBattery.Range": 400}, None)
        assert status["EV_RANGE_ON_BATTERY_KM"] == "400"
        assert status["EV_RANGE_ON_BATTERY_MILES"] == "249"

    def test_the_report_time_is_the_cars_own(self) -> None:
        assert to_status({}, REPORTED) == {"LAST_UPDATED_TIME": REPORTED}
        assert "LAST_UPDATED_TIME" not in to_status({}, None)


class TestLocation:
    def test_a_fix_is_shaped_like_the_portals(self) -> None:
        body = {
            "Vehicle": {
                "CurrentLocation": {
                    "Latitude": 51.5,
                    "Longitude": -0.12,
                    "Heading": 245,
                    "Timestamp": "2026-11-07T09:00:00Z",
                },
                "Speed": 0,
            },
            "timestamp": "2026-11-07T09:05:00Z",
        }
        assert to_position(body) == {
            "latitude": 51.5,
            "longitude": -0.12,
            "heading": 245.0,
            "speed": 0.0,
            # The fix's own time, not when the location was last updated.
            "timestamp": "2026-11-07T09:00:00Z",
        }

    def test_without_the_fixs_own_time_the_responses_is_used(self) -> None:
        body = {
            "Vehicle": {"CurrentLocation": {"Latitude": 1, "Longitude": 2}},
            "timestamp": "2026-11-07T09:05:00Z",
        }
        position = to_position(body)
        assert position is not None
        assert position["timestamp"] == "2026-11-07T09:05:00Z"
        assert position["heading"] is None

    def test_the_streams_separate_signals_make_the_same_fix(self) -> None:
        signals = {
            "Vehicle.CurrentLocation.Latitude": 51.5,
            "Vehicle.CurrentLocation.Longitude": -0.12,
            "Vehicle.Speed": 0,
        }
        assert signals_to_position(signals, "2026-11-07T09:00:00Z") == {
            "latitude": 51.5,
            "longitude": -0.12,
            "heading": None,
            "speed": 0.0,
            "timestamp": "2026-11-07T09:00:00Z",
        }
        assert signals_to_position({"Vehicle.Speed": 0}, None) is None

    def test_no_fix_is_none(self) -> None:
        assert to_position({}) is None
        assert to_position({"Vehicle": {}}) is None
        assert to_position({"Vehicle": {"CurrentLocation": {"Latitude": 1}}}) is None


class TestDetails:
    def test_details_land_under_the_attribute_names_the_entities_read(self) -> None:
        body = {
            "Vehicle": {
                "VehicleIdentification": {
                    "VIN": "SALXX0000000000XX",
                    "Brand": "Land Rover",
                    "Model": "Range Rover",
                    "Year": 2023,
                    "BodyType": "5 Door SWB",
                    "LicensePlate": None,
                },
                "Powertrain": {"FuelType": "diesel"},
                "Exterior": {"Colour": "Black"},
            }
        }
        assert to_attributes(body) == {
            "vehicleBrand": "Land Rover",
            "vehicleType": "Range Rover",
            "model": "Range Rover",
            "modelYear": 2023,
            "fuelType": "diesel",
            "numberOfDoors": "5",
            "bodyType": "5 Door SWB",
            "colour": "Black",
        }

    def test_the_colour_is_read_under_either_spelling(self) -> None:
        body = {"Vehicle": {"Exterior": {"Color": "Blue"}}}
        assert to_attributes(body) == {"colour": "Blue"}

    def test_a_body_that_does_not_say_how_many_doors_says_nothing(self) -> None:
        body = {"Vehicle": {"VehicleIdentification": {"BodyType": "90 Standard"}}}
        assert "numberOfDoors" not in to_attributes(body)
        assert to_attributes(body)["bodyType"] == "90 Standard"

    def test_nothing_usable_is_nothing(self) -> None:
        assert to_attributes({}) == {}
        assert to_attributes({"Vehicle": {"VehicleIdentification": "x"}}) == {}


EV = "Vehicle.Powertrain.TractionBattery."


class TestTheEvEndpointInSocketTerms:
    """The EV endpoint's answer, put into the keys the entities read."""

    def ev(self, **charging: object) -> dict[str, object]:
        signals: dict[str, object] = {f"{EV}StateOfCharge.Current": 78.5}
        signals.update({f"{EV}Charging.{k}": v for k, v in charging.items()})
        return signals

    def test_the_true_charge_stands_in_for_the_shown_one(self) -> None:
        status = to_status(self.ev(), None)
        assert status["EV_STATE_OF_CHARGE"] == "78.5"
        assert status["EV_STATE_OF_CHARGE_ACTUAL"] == "78.5"

    def test_the_shown_charge_wins_when_it_comes(self) -> None:
        signals = {**self.ev(), f"{EV}StateOfCharge.Displayed": 80}
        assert to_status(signals, None)["EV_STATE_OF_CHARGE"] == "80"

    @pytest.mark.parametrize(
        ("charging", "status", "method"),
        [
            ({"IsCharging": True, "IsPlugConnected": True}, "CHARGING", "WIRED"),
            (
                {"IsCharging": False, "IsPlugConnected": False},
                "NOTCONNECTED",
                "NOTCONNECTED",
            ),
            ({"IsCharging": False, "IsPlugConnected": True}, "NOTCHARGING", "WIRED"),
            ({"IsPlugConnected": True, "Mode": "fault"}, "FAULT", "WIRED"),
        ],
    )
    def test_charging_in_the_sockets_words(
        self, charging: dict[str, object], status: str, method: str
    ) -> None:
        result = to_status(self.ev(**charging), None)
        assert result["EV_CHARGING_STATUS"] == status
        assert result["EV_CHARGING_METHOD"] == method

    def test_a_charging_status_the_car_sent_is_not_overwritten(self) -> None:
        signals = {
            **self.ev(IsCharging=False, IsPlugConnected=True),
            f"{EV}Charging.Status": "FULLYCHARGED",
        }
        assert to_status(signals, None)["EV_CHARGING_STATUS"] == "FULLYCHARGED"

    def test_nothing_is_said_when_nothing_was_sent(self) -> None:
        # Plugged in, with no word on whether it is charging: no status.
        result = to_status(self.ev(IsPlugConnected=True), None)
        assert "EV_CHARGING_STATUS" not in result
        assert to_status({}, None) == {}

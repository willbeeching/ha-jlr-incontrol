"""The translation from Data Hub signals to the socket's status keys.

The pairs checked here were taken from two cars read through both sources at
the same moment in November 2026: the Data Hub's values and the socket's sat
side by side and matched. The numbers below are stand-ins of the same shape,
not those cars' readings.
"""

from __future__ import annotations

from custom_components.jlr_incontrol.vss import to_position, to_status

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

    def test_a_true_boolean_reads_as_the_socket_wrote_it(self) -> None:
        status = to_status({"Vehicle.Body.Roof.IsOpen": True}, None)
        assert status["IS_SUNROOF_OPEN"] == "TRUE"


class TestDerived:
    def test_the_odometer_in_metres_and_whole_miles(self) -> None:
        # The socket truncated: 58,328,000 m arrived as 36243 miles, not 36244.
        status = to_status({"Vehicle.TraveledDistance": 58328000}, None)
        assert status["ODOMETER"] == status["ODOMETER_METER"] == "58328000"
        assert status["ODOMETER_MILES"] == "36243"

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

    def test_no_fix_is_none(self) -> None:
        assert to_position({}) is None
        assert to_position({"Vehicle": {}}) is None
        assert to_position({"Vehicle": {"CurrentLocation": {"Latitude": 1}}}) is None

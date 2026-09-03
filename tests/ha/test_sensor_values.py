"""What each sensor turns a raw JLR value into.

Most of these are guards against sentinels. JLR report "not fitted" and "bad
read" as in-band values — a coolant temperature of -40, a negative combined
range, a tyre pressure of zero — and passing those through produces a
confident wrong reading rather than an absent one.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT  # noqa: E402

from custom_components.jlr_incontrol.sensor import (  # noqa: E402
    EV_SENSORS,
    TYRE_SENSORS,
    VEHICLE_SENSORS,
    JlrAllInfoSensor,
    JlrChargeNowSettingSensor,
    JlrEvccStatusSensor,
    JlrLastUpdatedSensor,
    JlrVehicleSensor,
    _combined_range,
    _coolant_temp,
    _odometer_attrs,
    _to_float,
    _tyre_kpa,
)

ALL = VEHICLE_SENSORS + TYRE_SENSORS + EV_SENSORS


class StandInCoordinator:
    """Only what a sensor entity reads."""

    def __init__(self, **vehicle: Any) -> None:
        self.data = {"vehicles": {KEPT: vehicle}}
        self._charge_now: str | None = None

    def charge_now_setting(self, vin: str) -> str | None:
        return self._charge_now


def build(cls: type, **vehicle: Any) -> Any:
    """A sensor with a coordinator but no Home Assistant behind it."""
    made = cls.__new__(cls)
    made._vin = KEPT
    made.coordinator = StandInCoordinator(**vehicle)
    return made


def description(key: str) -> Any:
    return next(item for item in ALL if item.key == key)


class TestReadingNumbers:
    @pytest.mark.parametrize(
        ("raw", "expected"), [("42", 42.0), ("42.5", 42.5), (7, 7.0)]
    )
    def test_a_number_is_a_number(self, raw: Any, expected: float) -> None:
        assert _to_float(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "UNKNOWN", [], {}])
    def test_anything_else_is_not_guessed_at(self, raw: Any) -> None:
        assert _to_float(raw) is None


class TestSentinels:
    @pytest.mark.parametrize("raw", ["-40", "-50", "-273"])
    def test_a_coolant_sentinel_is_unknown_not_freezing(self, raw: str) -> None:
        # -40 is what an unfitted or unread sensor reports. Shown as a
        # temperature it looks like a real and alarming one.
        assert _coolant_temp(raw) is None

    @pytest.mark.parametrize(("raw", "expected"), [("90", 90.0), ("-39", -39.0)])
    def test_a_real_temperature_survives(self, raw: str, expected: float) -> None:
        assert _coolant_temp(raw) == expected

    @pytest.mark.parametrize("raw", ["-1", "-100"])
    def test_a_negative_combined_range_is_unknown(self, raw: str) -> None:
        assert _combined_range(raw) is None

    def test_a_real_combined_range_survives(self) -> None:
        assert _combined_range("410") == 410.0

    def test_an_unreadable_value_is_unknown_for_both(self) -> None:
        assert _coolant_temp("UNKNOWN") is None
        assert _combined_range("UNKNOWN") is None


class TestTyrePressure:
    def test_kilopascals_are_taken_as_they_are(self) -> None:
        assert _tyre_kpa("240") == 240.0

    def test_a_tenth_scale_reading_is_divided(self) -> None:
        # Some cars report kPa*10. The clamp on the result is what catches it
        # rather than a per-model table nobody can maintain.
        assert _tyre_kpa("2400") == 240.0

    @pytest.mark.parametrize("raw", ["0", "-10", "99999", "UNKNOWN", None])
    def test_an_implausible_reading_is_unknown(self, raw: Any) -> None:
        assert _tyre_kpa(raw) is None

    def test_bar_comes_along_as_an_attribute(self) -> None:
        attrs = description("tyre_pressure_fl").attr_fn(
            {"TYRE_PRESSURE_FRONT_LEFT": "240"}
        )
        assert attrs["bar"] == 2.4

    def test_no_bar_attribute_without_a_pressure(self) -> None:
        assert description("tyre_pressure_fl").attr_fn({}) == {}


class TestOdometer:
    def test_kilometres_are_offered_alongside_miles(self) -> None:
        assert _odometer_attrs({"ODOMETER": "123456"})["kilometers"] == 123.5

    def test_a_car_that_did_not_report_one_gets_no_attribute(self) -> None:
        assert _odometer_attrs({}) == {}


class TestTheSensorItself:
    def test_a_value_is_passed_through_its_function(self) -> None:
        sensor = build(JlrVehicleSensor, status={"FUEL_LEVEL_PERC": "42"})
        sensor.entity_description = description("fuel_level")
        assert sensor.native_value == 42.0

    @pytest.mark.parametrize("raw", [None, ""])
    def test_a_missing_reading_is_unknown(self, raw: Any) -> None:
        sensor = build(JlrVehicleSensor, status={"FUEL_LEVEL_PERC": raw})
        sensor.entity_description = description("fuel_level")
        assert sensor.native_value is None


class TestLastUpdated:
    def test_a_timestamp_is_parsed(self) -> None:
        sensor = build(JlrLastUpdatedSensor, status_ts="2026-08-26T08:00:00.000Z")
        assert sensor.native_value is not None

    def test_a_naive_timestamp_is_assumed_utc(self) -> None:
        # A naive datetime reaching Home Assistant raises; JLR are not
        # consistent about the suffix.
        sensor = build(JlrLastUpdatedSensor, status_ts="2026-08-26T08:00:00")
        assert sensor.native_value.tzinfo is not None

    @pytest.mark.parametrize("raw", [None, "", "not a date"])
    def test_anything_unparseable_is_unknown(self, raw: Any) -> None:
        assert build(JlrLastUpdatedSensor, status_ts=raw).native_value is None


class TestAllInfo:
    def test_it_carries_the_whole_status(self) -> None:
        sensor = build(JlrAllInfoSensor, status={"ODOMETER": "1", "FUEL": "2"})
        assert sensor.native_value == "ok"
        assert sensor.extra_state_attributes == {"ODOMETER": "1", "FUEL": "2"}

    def test_a_car_with_no_status_yet_is_not_a_crash(self) -> None:
        assert build(JlrAllInfoSensor).extra_state_attributes == {}


class TestEvccConnectorState:
    """Verified live across all four wallbox states on an I-PACE."""

    @pytest.mark.parametrize(
        ("method", "charging", "expected"),
        [
            ("NOTCONNECTED", "", "A"),
            ("WIRED", "INITIALIZATION", "B"),
            ("WIRED", "WAITINGTOCHARGE", "B"),
            ("WIRED", "CHARGING", "C"),
            ("WIRED", "BULKCHARGED", "C"),
        ],
    )
    def test_the_connector_letter(
        self, method: str, charging: str, expected: str
    ) -> None:
        sensor = build(
            JlrEvccStatusSensor,
            status={"EV_CHARGING_METHOD": method, "EV_CHARGING_STATUS": charging},
        )
        assert sensor.native_value == expected

    @pytest.mark.parametrize("method", ["", "UNKNOWN", None])
    def test_a_car_that_has_not_said_reports_nothing(self, method: Any) -> None:
        # Not "A": claiming disconnected when the car has not answered would
        # tell a wallbox controller to stop.
        sensor = build(JlrEvccStatusSensor, status={"EV_CHARGING_METHOD": method})
        assert sensor.native_value is None


class TestChargeNowOverride:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("DEFAULT", "default"), ("FORCE_ON", "force_on"), ("FORCE_OFF", "force_off")],
    )
    def test_the_override_is_reported_as_an_option(
        self, raw: str, expected: str
    ) -> None:
        sensor = build(JlrChargeNowSettingSensor)
        sensor.coordinator._charge_now = raw
        assert sensor.native_value == expected

    @pytest.mark.parametrize("raw", [None, "", "SOMETHING_NEW"])
    def test_anything_outside_the_options_is_unknown(self, raw: Any) -> None:
        # An enum reporting a value not in its options errors on every update.
        sensor = build(JlrChargeNowSettingSensor)
        sensor.coordinator._charge_now = raw
        assert sensor.native_value is None

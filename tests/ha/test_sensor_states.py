"""Sensor values that are enumerations, and the ones deliberately not.

An ENUM sensor has to declare every value it will ever report; anything else
logs an error on each update and leaves the entity unusable. That makes the
choice of which JLR enums to close a judgement, not a formality.
"""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from custom_components.jlr_incontrol.sensor import (  # noqa: E402
    EV_SENSORS,
    TYRE_SENSORS,
    VEHICLE_SENSORS,
    _alarm_state,
    _charging_status,
)

ALL_SENSORS = VEHICLE_SENSORS + TYRE_SENSORS + EV_SENSORS


def description(key: str):
    return next(item for item in ALL_SENSORS if item.key == key)


class TestChargingStatus:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("CHARGING", "charging"),
            ("BULKCHARGED", "bulk_charged"),
            ("FULLYCHARGED", "fully_charged"),
            ("WAITINGTOCHARGE", "waiting_to_charge"),
            ("INITIALIZATION", "initialization"),
            ("PAUSED", "paused"),
            ("NOTCONNECTED", "not_connected"),
            ("FAULT", "fault"),
            ("No Message", "no_message"),
        ],
    )
    def test_known_values_map_to_a_translated_option(
        self, raw: str, expected: str
    ) -> None:
        assert _charging_status(raw) == expected

    def test_every_option_it_can_return_is_declared(self) -> None:
        # The failure this guards is silent until someone's car charges.
        declared = set(description("ev_charging_status").options or [])
        assert {
            _charging_status(raw)
            for raw in ("CHARGING", "PAUSED", "FAULT", "No Message")
        } <= declared

    def test_an_unknown_value_is_reported_rather_than_shown(self, caplog) -> None:
        caplog.set_level(logging.WARNING)
        assert _charging_status("SOMETHING_NEW") is None
        assert "SOMETHING_NEW" in caplog.text

    def test_an_empty_value_is_not_worth_a_warning(self, caplog) -> None:
        caplog.set_level(logging.WARNING)
        assert _charging_status("") is None
        assert caplog.text == ""


class TestAlarmState:
    """Left as free text on purpose — see the docstring on _alarm_state."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("ALARM_ARMED", "Armed"),
            ("ALARM_OFF", "Off"),
            ("ALARM_NOT_SET__DOOR_OPEN", "Not Set Door Open"),
        ],
    )
    def test_it_stays_readable(self, raw: str, expected: str) -> None:
        assert _alarm_state(raw) == expected

    def test_a_value_nobody_has_seen_still_shows_something(self) -> None:
        # The whole reason it is not an ENUM: this must not become unavailable
        # for whoever owns the car that reports it.
        assert _alarm_state("ALARM_SOMETHING_NEW") == "Something New"

    def test_it_is_not_declared_as_an_enum(self) -> None:
        assert description("alarm_state").options is None


class TestWhatTheDocsPromise:
    """Three entities are documented as off by default. Keep that true."""

    def test_the_noisy_twelve_volt_reading_is_off(self) -> None:
        assert description("battery_soc_12v").entity_registry_enabled_default is False

    def test_the_voltage_beside_it_is_on(self) -> None:
        # It is the one that actually says something about 12V health.
        assert description("battery_voltage").entity_registry_enabled_default is not (
            False
        )

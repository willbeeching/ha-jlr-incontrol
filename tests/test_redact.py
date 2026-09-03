"""Nothing identifying may reach a log or a diagnostics download.

These are regression tests for a real leak: a reporter's VIN reached a public
issue because a debug line printed it and the maintainer asked for the log, and
the telematics IMEI shipped in clear in every diagnostics download because the
redaction named ``imei`` while the payload calls it ``TU_STATUS_IMEI``.
"""

from __future__ import annotations

import json

from jlr.redact import REDACTED, scrub, scrub_text, vehicle_label

VIN = "SALEA6AU6P2152296"
OTHER_VIN = "SADHA2B14M1617505"


class TestVehicleLabel:
    def test_is_stable_for_a_car(self) -> None:
        assert vehicle_label(VIN) == vehicle_label(VIN)

    def test_distinguishes_cars(self) -> None:
        assert vehicle_label(VIN) != vehicle_label(OTHER_VIN)

    def test_does_not_contain_the_vin_or_any_part_of_it(self) -> None:
        label = vehicle_label(VIN)
        assert VIN not in label
        # Not even the tail, which is the serial and narrows a car a long way
        # when the make and model are sitting next to it.
        assert VIN[-4:] not in label
        assert VIN[-6:] not in label

    def test_survives_a_missing_vin(self) -> None:
        assert vehicle_label("") == "vehicle_unknown"


class TestSensitiveKeys:
    def test_redacts_the_names_the_payload_actually_uses(self) -> None:
        # The exact keys that defeated the previous field-name approach.
        payload = {
            "vin": VIN,
            "fullVin": VIN,
            "TU_STATUS_IMEI": "359972461763135",
            "TU_STATUS_SERIAL_NUMBER": "207VIHJB69057",
            "registrationNumber": "AB12 CDE",
            "serial_number": "abc",
            "latitude": 51.5,
            "longitude": -0.1,
            "portal_id": "ENC123",
        }
        assert all(value == REDACTED for value in scrub(payload).values())

    def test_redacts_credentials_wherever_they_appear(self) -> None:
        scrubbed = scrub(
            {
                "refresh_token": "t",
                "sso_cookies": {"SSOSession": "s"},
                "Authorization": "Bearer x",
                "password": "hunter2",
            }
        )
        assert all(value == REDACTED for value in scrubbed.values())

    def test_leaves_ordinary_telemetry_alone(self) -> None:
        # A substring rule would redact IS_DRIVING because it contains "VIN".
        payload = {
            "IS_DRIVING": "TRUE",
            "BATTERY_STATUS": "BATTERY_1_1",
            "DOOR_IS_ALL_DOORS_LOCKED": "TRUE",
            "ODOMETER_MILES": "18513",
        }
        assert scrub(payload) == payload


class TestVinShapedText:
    def test_catches_a_vin_with_no_key_to_recognise(self) -> None:
        # An unrecognised message is logged whole precisely because its shape
        # is unknown; it must not carry a VIN while we work out what it is.
        assert VIN not in scrub_text(f"GET /vehicles/{VIN}/status -> 498")

    def test_recurses_through_nested_payloads(self) -> None:
        nested = {"a": [{"b": {"c": f"vehicle {VIN} refused"}}]}
        assert VIN not in json.dumps(scrub(nested))

    def test_leaves_values_that_only_look_long_alone(self) -> None:
        # 17 characters but not VIN-shaped: version strings keep their meaning.
        assert scrub_text("L8B2-70712-AAC") == "L8B2-70712-AAC"


class TestDiagnosticsShape:
    def test_no_identifier_survives_a_realistic_dump(self) -> None:
        """The whole point: serialise a dump and grep it for the real values."""
        dump = scrub(
            {
                "telemetry": {"last_push": {VIN: "2026-09-01T22:00:31Z"}},
                "vehicles": {
                    vehicle_label(VIN): {
                        "attributes": {
                            "nickname": "Defender",
                            "registrationNumber": "AB12 CDE",
                        },
                        "status": {
                            "TU_STATUS_IMEI": "359972461763135",
                            "TU_STATUS_SERIAL_NUMBER": "207VIHJB69057",
                            "ODOMETER_MILES": "18513",
                        },
                        "position": {"latitude": 51.5, "longitude": -0.1},
                    }
                },
            }
        )
        serialised = json.dumps(dump)
        for secret in (
            VIN,
            "AB12 CDE",
            "359972461763135",
            "207VIHJB69057",
            "51.5",
            "-0.1",
        ):
            assert secret not in serialised, f"{secret} survived redaction"
        # And the dump is still worth having.
        assert "Defender" in serialised
        assert "18513" in serialised

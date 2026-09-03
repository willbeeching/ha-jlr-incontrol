"""The STOMP codec and the shape of what the broker actually sends.

Vehicle data arrives over a hand-rolled STOMP client because there is no
library for JLR's dialect of it, so the framing is ours to get right. These
cover the parts that bit: heart-beats arriving as bare newlines, several frames
in one websocket payload, header escaping, and the doubly-encoded message body.
"""

from __future__ import annotations

import json

from jlr import telemetry as tel
from jlr.api import flatten_status

# VIN-shaped but not a real vehicle. Committed tests must not carry one.
VIN = "SAJAA1234567890AB"

CONNECTED = "CONNECTED\nversion:1.2\nheart-beat:10000,10000\nuser-name:9f3c\n\n\x00"


class TestFraming:
    def test_encodes_a_frame(self) -> None:
        assert tel._encode(
            "CONNECT", {"accept-version": "1.2", "heart-beat": "20000,20000"}
        ) == ("CONNECT\naccept-version:1.2\nheart-beat:20000,20000\n\n\x00")

    def test_escapes_header_values(self) -> None:
        # STOMP 1.2 §3.1: a colon in a value would otherwise end the header.
        assert tel._escape("Bearer a:b\\c") == "Bearer a\\cb\\\\c"

    def test_escaping_round_trips(self) -> None:
        assert tel._unescape(tel._escape("a:b\nc\\d")) == "a:b\nc\\d"

    def test_decodes_a_frame(self) -> None:
        (frame,) = tel._decode(CONNECTED)
        assert frame.command == "CONNECTED"
        assert frame.headers["heart-beat"] == "10000,10000"

    def test_a_bare_newline_is_a_heartbeat_not_a_frame(self) -> None:
        # The broker beats every 10s down the same socket; treating one as
        # content would desynchronise the stream.
        assert tel._decode("\n") == []

    def test_a_heartbeat_before_a_frame_is_skipped(self) -> None:
        assert len(tel._decode("\n" + CONNECTED)) == 1

    def test_several_frames_can_share_one_payload(self) -> None:
        assert len(tel._decode(CONNECTED + CONNECTED)) == 2


class TestVehicleMessages:
    """The VIN-topic envelope, whose body is JSON inside a JSON string."""

    envelope = {
        "eid": "e-1",
        "st": "VHS",
        "t": "2026-08-26T08:12:41.589Z",
        "v": VIN,
        "a": {
            "b": json.dumps(
                {
                    "vehicleStatus": {
                        "coreStatus": [
                            {
                                "key": "ODOMETER",
                                "value": "123456",
                                "lastUpdatedTime": "2026-08-26T07:00:00.000Z",
                            },
                            {
                                "key": "FUEL_LEVEL_PERC",
                                "value": "42",
                                "lastUpdatedTime": "2026-08-26T08:00:00.000Z",
                            },
                        ],
                        "evStatus": [
                            {
                                "key": "EV_STATE_OF_CHARGE",
                                "value": "80",
                                "lastUpdatedTime": "2026-08-26T06:00:00.000Z",
                            }
                        ],
                    }
                }
            )
        },
    }

    def test_unwraps_the_double_encoding(self) -> None:
        payload = tel._inner_payload(self.envelope)
        assert "vehicleStatus" in payload

    def test_flattens_core_and_ev_status_together(self) -> None:
        status = flatten_status(tel._inner_payload(self.envelope))
        assert status["ODOMETER"] == "123456"
        assert status["EV_STATE_OF_CHARGE"] == "80"

    def test_takes_the_newest_per_key_stamp_not_the_push_time(self) -> None:
        # The envelope's "t" advances on every reconnect even when the payload
        # is unchanged, which would report a permanently fresh vehicle.
        status = flatten_status(tel._inner_payload(self.envelope))
        assert status["LAST_UPDATED_TIME"] == "2026-08-26T08:00:00.000Z"
        assert status["LAST_UPDATED_TIME"] != self.envelope["t"]

    def test_a_body_that_is_not_json_is_not_fatal(self) -> None:
        assert tel._inner_payload({"v": VIN, "a": {"b": "not json"}}) is None

    def test_a_message_with_no_body_is_not_fatal(self) -> None:
        assert tel._inner_payload({"v": VIN}) is None

"""The supervisor that keeps the telemetry socket alive, and what it does wrong.

Every reading in this integration arrives through here, and when it stops
arriving nothing else notices for half an hour. The reconnect loop is also the
one piece that talks to JLR's servers unprompted, so its backoff is a
politeness question as much as a correctness one: a client that retries a
refused connection every five seconds forever is how an unofficial API stops
being available to everybody.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from jlr import telemetry as tel
from jlr.api import JlrApiError, JlrRateLimitError
from jlr.const import WS_BACKOFF_MAX, WS_BACKOFF_START, WS_TYPE_STATUS
from jlr.telemetry import Frame, JlrTelemetry, JlrTelemetryError

VIN = "SAJAA1234567890AB"
DEVICE = "3f2c9a71-5d84-4c1e-9a30-6b7d8e5f4a21"

CONNECTED = "CONNECTED\nversion:1.2\nheart-beat:10000,10000\nuser-name:9f3c\n\n\x00"

# What a STOMP broker actually stamps on a MESSAGE frame. The destination is
# the identifying one: it is the topic the frame was addressed to, and this
# integration's own topics are named after the device id and the VIN.
BROKER_HEADERS = {
    "destination": f"/user/topic/DEVICE.{DEVICE}",
    "subscription": "sub-0",
    "message-id": "3c9e1f20-7a41-4b8e-9d02-5f6a7b8c9d0e-101772409",
    "content-length": "512",
    "timestamp": "2026-08-26T08:12:41.589Z",
}

# And the same for a frame on a vehicle topic, which is the only kind that is
# acknowledged. Shaped like the app capture on issue #30, where the device
# topic's message-id is a uuid with a counter on the end and this one is a
# bare uuid.
VIN_HEADERS = {
    "destination": f"/user/topic/VIN.{VIN}",
    "subscription": "sub-vin-0",
    "message-id": "8d2f4a61-3b7c-4e95-a0d8-1c6e9f2b5a73",
    "content-length": "512",
}

START = WS_BACKOFF_START.total_seconds()
MAX = WS_BACKOFF_MAX.total_seconds()


class FakeWebSocket:
    """Records what we sent it. Nothing here reads."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_str(self, data: str) -> None:
        self.sent.append(data)


def telemetry(**handlers: Any) -> JlrTelemetry:
    """A telemetry client with no session and recording callbacks."""
    made = JlrTelemetry.__new__(JlrTelemetry)
    made._vins = [VIN]
    made._subscribed = {}
    made._task = None
    made._connected = False
    made._outage_logged = False
    made.status: list[tuple] = []
    made.positions: list[tuple] = []
    made.connections: list[bool] = []
    made._on_status = handlers.get("on_status", lambda *args: made.status.append(args))
    made._on_position = handlers.get(
        "on_position", lambda *args: made.positions.append(args)
    )
    made._on_connected = handlers.get(
        "on_connected", lambda state: made.connections.append(state)
    )
    return made


class _NoSleep:
    """asyncio, with sleep recorded instead of served.

    A proxy rather than monkeypatching asyncio.sleep itself: that module
    object is shared with the test, so replacing the attribute makes the
    replacement call itself, and the test's own yields get counted as waits.
    """

    def __init__(self, recorded: list[float]) -> None:
        self._recorded = recorded

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def sleep(self, seconds: float) -> None:
        self._recorded.append(seconds)
        # Yield so the test can cancel the loop between attempts.
        await asyncio.sleep(0)


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Capture what the supervisor would sleep for, without sleeping."""
    recorded: list[float] = []
    monkeypatch.setattr(tel, "asyncio", _NoSleep(recorded))
    return recorded


async def supervise(client: JlrTelemetry, attempts: int, waits: list[float]) -> None:
    """Run the supervisor until it has slept `attempts` times, then stop."""
    task = asyncio.create_task(client._async_supervise())
    for _ in range(2000):
        if len(waits) >= attempts:
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class TestBackoff:
    async def test_it_doubles_between_attempts(self, waits) -> None:
        client = telemetry()

        async def always_fails() -> None:
            raise JlrTelemetryError("the socket dropped")

        client._async_session = always_fails
        await supervise(client, 4, waits)
        assert waits[:4] == [START, START * 2, START * 4, START * 8]

    async def test_it_stops_doubling_at_the_ceiling(self, waits) -> None:
        client = telemetry()

        async def always_fails() -> None:
            raise JlrTelemetryError("the socket dropped")

        client._async_session = always_fails
        await supervise(client, 20, waits)
        assert max(waits) == MAX
        assert waits[-1] == MAX

    async def test_a_planned_reconnect_is_not_penalised(self, waits) -> None:
        # Returning cleanly is the token-expiry reconnect, which happens every
        # few minutes by design. Backing off for it would throttle normal use.
        client = telemetry()
        rounds = 0

        async def expires_then_fails() -> None:
            nonlocal rounds
            rounds += 1
            if rounds <= 3:
                return
            raise JlrTelemetryError("the socket dropped")

        client._async_session = expires_then_fails
        await supervise(client, 1, waits)
        # Three clean returns slept not at all; the first real failure starts
        # from the bottom of the ladder rather than part-way up it.
        assert waits[0] == START

    async def test_a_clean_return_resets_a_climbed_backoff(self, waits) -> None:
        client = telemetry()
        rounds = 0

        async def fails_then_recovers() -> None:
            nonlocal rounds
            rounds += 1
            if rounds in (1, 2, 3):
                raise JlrTelemetryError("the socket dropped")
            if rounds == 4:
                return
            raise JlrTelemetryError("dropped again")

        client._async_session = fails_then_recovers
        await supervise(client, 4, waits)
        assert waits[:3] == [START, START * 2, START * 4]
        assert waits[3] == START, "a good connection should clear the penalty"


class TestBeingToldToWait:
    async def test_a_rate_limit_is_obeyed_rather_than_guessed(self, waits) -> None:
        # Ignoring Retry-After is how a client stops being throttled and
        # starts being blocked.
        client = telemetry()

        async def limited() -> None:
            raise JlrRateLimitError("slow down", 900)

        client._async_session = limited
        await supervise(client, 1, waits)
        assert waits[0] == 900

    async def test_a_rate_limit_without_a_delay_falls_back_to_backoff(
        self, waits
    ) -> None:
        client = telemetry()

        async def limited() -> None:
            raise JlrRateLimitError("slow down", None)

        client._async_session = limited
        await supervise(client, 2, waits)
        assert waits[:2] == [START, START * 2]

    async def test_being_told_to_wait_does_not_reset_the_ladder(self, waits) -> None:
        client = telemetry()
        rounds = 0

        async def limited_then_dropped() -> None:
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                raise JlrRateLimitError("slow down", 900)
            raise JlrTelemetryError("the socket dropped")

        client._async_session = limited_then_dropped
        await supervise(client, 2, waits)
        assert waits == [900, START * 2]


class TestTheSupervisorNeverDies:
    async def test_an_unexpected_error_is_survived(self, waits) -> None:
        # Anything that escapes here stops every reading in the integration
        # until Home Assistant is restarted.
        client = telemetry()

        async def explodes() -> None:
            raise ZeroDivisionError("something nobody predicted")

        client._async_session = explodes
        await supervise(client, 3, waits)
        assert len(waits) >= 3

    async def test_an_api_error_is_survived(self, waits) -> None:
        client = telemetry()

        async def refused() -> None:
            raise JlrApiError("Jaguar Land Rover returned 503")

        client._async_session = refused
        await supervise(client, 2, waits)
        assert len(waits) >= 2

    async def test_cancellation_is_not_swallowed(self, waits) -> None:
        # async_stop relies on it: a supervisor that catches CancelledError
        # would hang the unload.
        client = telemetry()

        async def cancelled() -> None:
            raise asyncio.CancelledError

        client._async_session = cancelled
        task = asyncio.create_task(client._async_supervise())
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_every_failure_reports_the_socket_down(self, waits) -> None:
        client = telemetry()
        client._connected = True

        async def always_fails() -> None:
            raise JlrTelemetryError("the socket dropped")

        client._async_session = always_fails
        await supervise(client, 1, waits)
        assert client.connections == [False]
        assert client.connected is False


class TestStartAndStop:
    async def test_starting_twice_runs_one_supervisor(self) -> None:
        client = telemetry()
        started = 0

        async def counts() -> None:
            nonlocal started
            started += 1
            await asyncio.Event().wait()

        client._async_supervise = counts
        await client.async_start()
        # Let it actually begin, or the second start has nothing to collide
        # with and the test passes for the wrong reason.
        await asyncio.sleep(0)
        first = client._task
        await client.async_start()
        assert client._task is first
        await client.async_stop()
        assert started == 1

    async def test_stopping_unwinds_the_task(self) -> None:
        client = telemetry()

        async def forever() -> None:
            await asyncio.Event().wait()

        client._async_supervise = forever
        await client.async_start()
        await client.async_stop()
        assert client._task is None

    async def test_stopping_when_never_started_is_harmless(self) -> None:
        await telemetry().async_stop()

    async def test_stopping_reports_the_socket_down(self) -> None:
        client = telemetry()
        client._connected = True

        async def forever() -> None:
            await asyncio.Event().wait()

        client._async_supervise = forever
        await client.async_start()
        await client.async_stop()
        assert client.connections == [False]


def message(_headers: dict[str, str] | None = None, **envelope: Any) -> Frame:
    return Frame("MESSAGE", _headers or {}, json.dumps(envelope))


def vhs(vin: str = VIN, **extra: Any) -> Frame:
    body = json.dumps(
        {
            "vehicleStatus": {
                "coreStatus": [
                    {
                        "key": "ODOMETER",
                        "value": "123456",
                        "lastUpdatedTime": "2026-08-26T07:00:00.000Z",
                    }
                ]
            }
        }
    )
    return message(
        extra.pop("_headers", None),
        eid="e-1",
        st=WS_TYPE_STATUS,
        v=vin,
        a={"b": body},
        **extra,
    )


class TestFramesWeCanUse:
    async def test_a_status_message_reaches_the_coordinator(self) -> None:
        client, ws = telemetry(), FakeWebSocket()
        await client._async_handle(ws, vhs(t="2026-08-26T08:12:41.589Z"), DEVICE)
        vin, status, sent = client.status[0]
        assert vin == VIN
        assert status["ODOMETER"] == "123456"
        assert sent == "2026-08-26T08:12:41.589Z"

    async def test_a_position_message_reaches_the_coordinator(self) -> None:
        client, ws = telemetry(), FakeWebSocket()
        body = json.dumps({"position": {"latitude": 51.5074, "longitude": -0.1278}})
        await client._async_handle(
            ws, message(eid="e-2", st="VHP", v=VIN, a={"b": body}), DEVICE
        )
        assert client.positions[0][0] == VIN
        assert client.positions[0][1]["latitude"] == 51.5074


class TestFramesWeCannot:
    async def test_a_broker_error_frame_drops_the_connection(self) -> None:
        # It has to reach the supervisor, which reconnects; swallowing it
        # leaves a socket that will never deliver anything again.
        client, ws = telemetry(), FakeWebSocket()
        with pytest.raises(JlrTelemetryError, match="rejected"):
            await client._async_handle(
                ws, Frame("ERROR", {"message": "rejected"}, ""), DEVICE
            )

    @pytest.mark.parametrize("command", ["RECEIPT", "CONNECTED", "SUBSCRIBE"])
    async def test_other_commands_are_ignored(self, command: str) -> None:
        client, ws = telemetry(), FakeWebSocket()
        await client._async_handle(ws, Frame(command, {}, ""), DEVICE)
        assert client.status == []

    @pytest.mark.parametrize(
        "body", ["not json", "", "[1, 2, 3]", '"a bare string"', "null"]
    )
    async def test_a_body_we_cannot_read_is_not_fatal(self, body: str) -> None:
        client, ws = telemetry(), FakeWebSocket()
        await client._async_handle(ws, Frame("MESSAGE", {}, body), DEVICE)
        assert client.status == []

    async def test_a_message_with_no_vin_is_dropped(self) -> None:
        client, ws = telemetry(), FakeWebSocket()
        await client._async_handle(
            ws, message(eid="e-3", st=WS_TYPE_STATUS, a={"b": "{}"}), DEVICE
        )
        assert client.status == []

    async def test_a_vin_message_with_an_unreadable_body_is_dropped(self) -> None:
        client, ws = telemetry(), FakeWebSocket()
        await client._async_handle(
            ws, message(eid="e-4", st=WS_TYPE_STATUS, v=VIN, a={"b": "{{{"}), DEVICE
        )
        assert client.status == []

    async def test_an_unhandled_type_reaches_neither_callback(self) -> None:
        client, ws = telemetry(), FakeWebSocket()
        body = json.dumps({"serviceStatus": {"status": "Started"}})
        await client._async_handle(
            ws, message(eid="e-5", st="RDL", v=VIN, a={"b": body}), DEVICE
        )
        assert client.status == []
        assert client.positions == []

    async def test_an_unacknowledgeable_message_is_still_read(self) -> None:
        # No message-id means nothing to acknowledge, not nothing to do.
        client, ws = await subscribed()
        headers = {k: v for k, v in VIN_HEADERS.items() if k != "message-id"}
        await client._async_handle(ws, vhs(_headers=headers), DEVICE)
        assert client.status
        assert acks(ws) == []


class TestRepeatedAndReorderedFrames:
    """The broker redelivers until acknowledged, so both really happen."""

    async def test_a_redelivered_frame_is_acknowledged_again(self) -> None:
        client, ws = await subscribed()
        await client._async_handle(ws, vhs(_headers=VIN_HEADERS), DEVICE)
        await client._async_handle(ws, vhs(_headers=VIN_HEADERS), DEVICE)
        assert len(acks(ws)) == 2
        assert len(client.status) == 2

    async def test_the_last_frame_delivered_wins(self) -> None:
        # Documented, not accidental: the timestamp is per status key and is
        # often absent, so there is nothing dependable to order two snapshots
        # by. The coordinator takes the newest delivery, and a redelivery of
        # an older one after a reconnect would briefly show the older values.
        client, ws = telemetry(), FakeWebSocket()
        newer = json.dumps(
            {
                "vehicleStatus": {
                    "coreStatus": [
                        {
                            "key": "ODOMETER",
                            "value": "999999",
                            "lastUpdatedTime": "2026-08-26T09:00:00.000Z",
                        }
                    ]
                }
            }
        )
        await client._async_handle(
            ws, message(eid="e-9", st=WS_TYPE_STATUS, v=VIN, a={"b": newer}), DEVICE
        )
        await client._async_handle(ws, vhs(), DEVICE)
        assert client.status[-1][1]["ODOMETER"] == "123456"


async def subscribed(
    vins: list[str] | None = None,
) -> tuple[JlrTelemetry, FakeWebSocket]:
    """A client that has subscribed the way a real connection does."""
    client, ws = telemetry(), FakeWebSocket()
    client._vins = vins or [VIN]
    await client._async_subscribe(ws, DEVICE)
    ws.sent.clear()
    return client, ws


def acks(ws: FakeWebSocket) -> list[Frame]:
    """Every acknowledgement sent, decoded rather than substring-matched."""
    frames = [frame for raw in ws.sent for frame in tel._decode(raw)]
    return [
        frame
        for frame in frames
        if frame.command == "SEND"
        and frame.headers.get("destination") == "/app/messageReceived"
    ]


class TestAcknowledging:
    """In the iOS app's exact form, which issue #30 captured.

    A broker redelivers what it does not consider acknowledged, so the form
    matters even though nobody has measured the old one failing.
    """

    async def test_a_vehicle_message_is_acknowledged_as_the_app_does(self) -> None:
        client, ws = await subscribed()
        await client._async_handle(ws, vhs(_headers=VIN_HEADERS), DEVICE)
        (ack,) = acks(ws)
        assert ack.headers["vin"] == VIN
        assert ack.headers["device"] == DEVICE
        assert ack.headers["content-type"] == "application/json;charset=UTF-8"
        assert json.loads(ack.body) == {"message-id": VIN_HEADERS["message-id"]}

    async def test_it_echoes_the_header_not_the_envelope_id(self) -> None:
        # 1.7.4 sent the envelope's eid. The app never does.
        client, ws = await subscribed()
        await client._async_handle(ws, vhs(_headers=VIN_HEADERS), DEVICE)
        (ack,) = acks(ws)
        assert "e-1" not in ack.body
        assert "eventIds" not in ack.body

    async def test_the_device_topic_is_not_acknowledged(self) -> None:
        # The app leaves its receipts alone. They carry a message-id like
        # every other frame, so acknowledging from the headers would reach
        # them without the subscription check. The body is the capture's.
        client, ws = await subscribed()
        frame = message(
            {
                "destination": f"/user/topic/DEVICE.{DEVICE}",
                "subscription": "sub-dev",
                "message-id": "3c9e1f20-7a41-4b8e-9d02-5f6a7b8c9d0e-101772410",
            },
            messageType="SubscriptionAccepted",
            vin=VIN,
        )
        await client._async_handle(ws, frame, DEVICE)
        assert acks(ws) == []

    async def test_a_subscription_we_did_not_make_is_not_acknowledged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Failing visibly: if the broker ever names subscriptions differently,
        # the debug log says so instead of everything silently going unacked.
        caplog.set_level(logging.DEBUG)
        client, ws = await subscribed()
        headers = {**VIN_HEADERS, "subscription": "sub-7"}
        await client._async_handle(ws, vhs(_headers=headers), DEVICE)
        assert acks(ws) == []
        assert "'sub-7', which is not one of ours" in caplog.text
        assert client.status, "the reading is still used"

    async def test_an_unreadable_body_is_still_acknowledged(self) -> None:
        # The acknowledgement comes from the headers. A payload we cannot
        # parse is still a delivery, and leaving it unacked has the broker
        # send it again for as long as the connection lasts.
        client, ws = await subscribed()
        await client._async_handle(ws, Frame("MESSAGE", VIN_HEADERS, "{{{"), DEVICE)
        assert len(acks(ws)) == 1

    async def test_each_vehicle_is_named_in_its_own_acknowledgement(self) -> None:
        other = "SALBB9876543210CD"
        client, ws = await subscribed([VIN, other])
        headers = {**VIN_HEADERS, "subscription": "sub-vin-1", "message-id": "m-9"}
        await client._async_handle(ws, vhs(vin=other, _headers=headers), DEVICE)
        (ack,) = acks(ws)
        assert ack.headers["vin"] == other

    async def test_a_vehicle_list_change_mid_connection_names_the_right_car(
        self,
    ) -> None:
        # The ids were handed out by the connection that is still open. A car
        # added to the account reorders the list for the next one, and must
        # not change which VIN sub-vin-0 stands for on this one.
        client, ws = await subscribed([VIN])
        client.async_set_vehicles({VIN, "SAAAA0000000000AA"})
        await client._async_handle(ws, vhs(_headers=VIN_HEADERS), DEVICE)
        (ack,) = acks(ws)
        assert ack.headers["vin"] == VIN

    async def test_the_message_id_never_reaches_the_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        client, ws = await subscribed()
        await client._async_handle(ws, vhs(_headers=VIN_HEADERS), DEVICE)
        assert acks(ws)
        assert VIN_HEADERS["message-id"] not in caplog.text
        assert VIN not in caplog.text

    async def test_a_missing_message_id_names_only_the_vehicle_label(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        client, ws = await subscribed()
        headers = {k: v for k, v in VIN_HEADERS.items() if k != "message-id"}
        await client._async_handle(ws, vhs(_headers=headers), DEVICE)
        assert "no message-id to acknowledge" in caplog.text
        assert VIN not in caplog.text


class TestSubscribing:
    async def test_the_device_topic_comes_before_the_vehicles(self) -> None:
        # The app's own order. The device topic carries the receipts for each
        # vehicle subscription, so subscribing to it second loses them.
        client, ws = telemetry(), FakeWebSocket()
        client._vins = [VIN, "SALBB9876543210CD"]
        await client._async_subscribe(ws, DEVICE)
        assert "sub-dev" in ws.sent[0]
        assert DEVICE in ws.sent[0]

    async def test_every_vehicle_gets_its_own_subscription(self) -> None:
        client, ws = telemetry(), FakeWebSocket()
        client._vins = [VIN, "SALBB9876543210CD"]
        await client._async_subscribe(ws, DEVICE)
        assert sum("sub-vin-" in frame for frame in ws.sent) == 2
        assert any(VIN in frame for frame in ws.sent)
        assert any("SALBB9876543210CD" in frame for frame in ws.sent)

    async def test_an_account_with_no_vehicles_still_subscribes_to_the_device(
        self,
    ) -> None:
        client, ws = telemetry(), FakeWebSocket()
        client._vins = []
        await client._async_subscribe(ws, DEVICE)
        assert len(ws.sent) == 1

    async def test_a_vehicle_added_later_is_picked_up_on_reconnect(self) -> None:
        # async_set_vehicles only changes the list; the next connection is
        # what acts on it. A car added to the account mid-session would
        # otherwise never be subscribed.
        client, ws = telemetry(), FakeWebSocket()
        client.async_set_vehicles({VIN, "SALBB9876543210CD"})
        await client._async_subscribe(ws, DEVICE)
        assert sum("sub-vin-" in frame for frame in ws.sent) == 2

    async def test_each_connection_hands_out_its_own_ids(self) -> None:
        # A car removed from the account must not keep a subscription id that
        # the next connection would still acknowledge in its name.
        other = "SALBB9876543210CD"
        client, ws = await subscribed([VIN, other])
        client.async_set_vehicles({VIN})
        await client._async_subscribe(ws, DEVICE)
        ws.sent.clear()
        headers = {**VIN_HEADERS, "subscription": "sub-vin-1"}
        await client._async_handle(ws, vhs(vin=other, _headers=headers), DEVICE)
        assert acks(ws) == []

    async def test_the_vehicle_list_is_ordered_so_ids_are_stable(self) -> None:
        client = telemetry()
        client.async_set_vehicles({"SALBB9876543210CD", VIN})
        assert client._vins == sorted([VIN, "SALBB9876543210CD"])


class TestConnectionReporting:
    def test_it_only_reports_a_change(self) -> None:
        client = telemetry()
        client._set_connected(True)
        client._set_connected(True)
        client._set_connected(False)
        assert client.connections == [True, False]


class FakeReceiver(FakeWebSocket):
    """A socket that also answers, so the handshake can be driven."""

    def __init__(self, *frames: Any) -> None:
        super().__init__()
        self._frames = list(frames)

    async def receive(self, timeout: float | None = None) -> Any:
        if not self._frames:
            raise TimeoutError
        item = self._frames.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Message:
    def __init__(self, kind: Any, data: str = "") -> None:
        self.type, self.data = kind, data


def text(data: str) -> Message:
    import aiohttp

    return Message(aiohttp.WSMsgType.TEXT, data)


class TestHandshake:
    async def test_a_connected_frame_completes_it(self) -> None:
        client = telemetry()
        client._client = type("C", (), {"username": "someone@example.com"})()
        ws = FakeReceiver(text(CONNECTED))
        await client._async_handshake(ws, "a-token", DEVICE)
        assert "CONNECT" in ws.sent[0]
        # The bearer is what the broker binds the session to.
        assert "a-token" in ws.sent[0]

    async def test_a_refusal_is_reported_with_its_reason(self) -> None:
        client = telemetry()
        client._client = type("C", (), {"username": "someone@example.com"})()
        ws = FakeReceiver(text("ERROR\nmessage:bad token\n\n\x00"))
        with pytest.raises(JlrTelemetryError, match="bad token"):
            await client._async_handshake(ws, "a-token", DEVICE)

    async def test_an_unexpected_frame_is_not_assumed_to_be_fine(self) -> None:
        client = telemetry()
        client._client = type("C", (), {"username": "someone@example.com"})()
        ws = FakeReceiver(text("RECEIPT\nreceipt-id:1\n\n\x00"))
        with pytest.raises(JlrTelemetryError, match="instead of CONNECTED"):
            await client._async_handshake(ws, "a-token", DEVICE)

    async def test_silence_is_reported_rather_than_waited_on_forever(self) -> None:
        client = telemetry()
        with pytest.raises(JlrTelemetryError, match="did not answer"):
            await client._async_next_frame(FakeReceiver())

    async def test_a_socket_that_closes_mid_handshake_says_so(self) -> None:
        import aiohttp

        client = telemetry()
        ws = FakeReceiver(Message(aiohttp.WSMsgType.CLOSED))
        with pytest.raises(JlrTelemetryError, match="closed during handshake"):
            await client._async_next_frame(ws)

    async def test_a_heartbeat_before_the_answer_is_skipped(self) -> None:
        client = telemetry()
        ws = FakeReceiver(text("\n"), text(CONNECTED))
        assert (await client._async_next_frame(ws)).command == "CONNECTED"


class TestFindingAPositionInWhateverShape:
    """Position has not been seen live here, so several spellings are accepted."""

    @pytest.mark.parametrize(
        "payload",
        [
            {"latitude": 51.5074, "longitude": -0.1278},
            {"position": {"latitude": 51.5074, "longitude": -0.1278}},
            {"vehiclePosition": {"lat": 51.5074, "lng": -0.1278}},
            {"position": {"position": {"lat": 51.5074, "lon": -0.1278}}},
        ],
    )
    def test_each_shape_yields_the_same_fix(self, payload: dict) -> None:
        found = tel._extract_position(payload)
        assert found["latitude"] == 51.5074
        assert found["longitude"] == -0.1278

    def test_the_extras_come_along_when_they_are_there(self) -> None:
        found = tel._extract_position(
            {
                "latitude": 51.5074,
                "longitude": -0.1278,
                "bearing": 90,
                "speed": 30,
                "ts": "2026-08-26T08:00:00.000Z",
            }
        )
        assert found["heading"] == 90
        assert found["speed"] == 30
        assert found["timestamp"] == "2026-08-26T08:00:00.000Z"

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"latitude": 51.5074},
            {"longitude": -0.1278},
            {"position": "not a mapping"},
            {"position": {"latitude": None, "longitude": None}},
        ],
    )
    def test_half_a_fix_is_no_fix(self, payload: dict) -> None:
        # Reporting a coordinate we are not sure of would put someone's car on
        # the map in the wrong place, which is worse than reporting nothing.
        assert tel._extract_position(payload) == {}


class TestIdentifyingAnUnknownField:
    def test_it_hands_back_one_raw_item_to_look_at(self) -> None:
        payload = {"vehicleStatus": {"coreStatus": [{"key": "ODOMETER"}]}}
        assert tel._first_item(payload) == {"key": "ODOMETER"}

    def test_an_unwrapped_payload_works_too(self) -> None:
        assert tel._first_item({"coreStatus": [{"key": "ODOMETER"}]})["key"] == (
            "ODOMETER"
        )

    @pytest.mark.parametrize("payload", [{}, {"vehicleStatus": {}}, {"coreStatus": []}])
    def test_nothing_to_show_is_not_a_crash(self, payload: dict) -> None:
        assert tel._first_item(payload) is None


class _Clock:
    """time, with monotonic under the test's control.

    A proxy for the same reason as _NoSleep: the module object is shared, so
    replacing time.monotonic replaces it for pytest as well.
    """

    def __init__(self) -> None:
        self.now = 1000.0

    def __getattr__(self, name: str) -> Any:
        import time as real

        return getattr(real, name)

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    made = _Clock()
    monkeypatch.setattr(tel, "time", made)
    return made


class FakeSocket(FakeWebSocket):
    """A websocket that hands out scripted messages and records replies.

    Every receive advances the clock, scripted or not. The read loop only ever
    leaves through the passage of time — a deadline or a silence — so a socket
    that answers instantly and a clock that never moves is an infinite loop.
    """

    def __init__(
        self,
        *messages: Any,
        error: Exception | None = None,
        clock: _Clock | None = None,
        tick: float = 1.0,
    ) -> None:
        super().__init__()
        self._messages = list(messages)
        self._error = error
        self._clock = clock
        self._tick = tick
        self.pongs: list[Any] = []
        self.closed = False

    async def __aenter__(self) -> FakeSocket:
        return self

    async def __aexit__(self, *args: object) -> bool:
        self.closed = True
        return False

    async def pong(self, data: Any = None) -> None:
        self.pongs.append(data)

    def exception(self) -> Exception | None:
        return self._error

    async def receive(self, timeout: float | None = None) -> Any:
        if self._clock is not None:
            self._clock.now += self._tick
        if not self._messages:
            raise TimeoutError
        item = self._messages.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    username = "someone@example.com"
    device_id = DEVICE

    def __init__(self, token: str | None = "a-token", renewal: float = 300.0) -> None:
        self.access_token = token
        self._renewal = renewal
        self.connects = 0
        self.connect_error: Exception | None = None

    async def async_connect(self) -> None:
        self.connects += 1
        if self.connect_error is not None:
            raise self.connect_error

    def seconds_until_renewal(self) -> float:
        return self._renewal


def session_client(ws: Any, client: FakeClient | None = None) -> JlrTelemetry:
    made = telemetry()
    made._client = client or FakeClient()

    class Session:
        async def ws_connect(self, url: str, **kwargs: Any) -> Any:
            self.url, self.kwargs = url, kwargs
            if isinstance(ws, Exception):
                raise ws
            return ws

    made._session = Session()
    return made


class TestOpeningASession:
    async def test_it_renews_the_token_before_connecting(self, clock) -> None:
        # The session is bound to the bearer presented at CONNECT, so a stale
        # one means a socket that opens and is immediately refused.
        client = FakeClient(renewal=2)
        made = session_client(FakeSocket(text(CONNECTED), clock=clock), client)
        await made._async_session()
        assert client.connects == 1

    async def test_with_no_token_it_does_not_even_try(self, clock) -> None:
        made = session_client(FakeSocket(), FakeClient(token=None))
        with pytest.raises(JlrTelemetryError, match="no access token"):
            await made._async_session()

    async def test_a_refused_connection_is_reported_not_raised_raw(self, clock) -> None:
        import aiohttp

        made = session_client(aiohttp.ClientError("connection refused"))
        with pytest.raises(JlrTelemetryError, match="could not open"):
            await made._async_session()

    async def test_it_handshakes_subscribes_and_reports_connected(self, clock) -> None:
        socket = FakeSocket(text(CONNECTED), clock=clock)
        made = session_client(socket, FakeClient(renewal=2))
        await made._async_session()
        assert "CONNECT\n" in socket.sent[0]
        assert any("sub-dev" in frame for frame in socket.sent)
        assert made.connections == [True]
        assert socket.closed, "the socket has to be closed on the way out"


class TestTheReadLoop:
    def running(self, socket: Any) -> JlrTelemetry:
        made = telemetry()
        made._client = FakeClient()
        return made

    async def test_it_returns_when_the_token_is_due_for_renewal(self, clock) -> None:
        # A clean return, which the supervisor treats as routine rather than
        # as a failure worth backing off from.
        made = self.running(None)
        await made._async_read(FakeSocket(clock=clock), DEVICE, clock.now - 1)

    async def test_a_silent_socket_is_eventually_declared_dead(self, clock) -> None:
        # Otherwise a half-open connection sits there delivering nothing and
        # nobody notices for half an hour.
        made = self.running(None)
        socket = FakeSocket(clock=clock, tick=5)
        with pytest.raises(JlrTelemetryError, match="no telemetry traffic"):
            await made._async_read(socket, DEVICE, clock.now + 10_000)

    async def test_it_sends_its_half_of_the_heartbeat(self, clock) -> None:
        # On a clock, not on an idle read: the broker beats every 10s, so
        # waiting for a quiet socket means never sending one and being closed
        # for inactivity.
        made = self.running(None)
        socket = FakeSocket(clock=clock, tick=9)
        with pytest.raises(JlrTelemetryError):
            await made._async_read(socket, DEVICE, clock.now + 10_000)
        assert "\n" in socket.sent

    async def test_a_status_frame_is_delivered(self, clock) -> None:
        made = self.running(None)
        body = vhs().body
        frame = f"MESSAGE\n\n{body}\x00"
        socket = FakeSocket(text(frame), clock=clock)
        await made._async_read(socket, DEVICE, clock.now + 1.5)
        assert made.status

    async def test_a_ping_is_answered(self, clock) -> None:
        import aiohttp

        made = self.running(None)
        socket = FakeSocket(Message(aiohttp.WSMsgType.PING, "ping"), clock=clock)
        await made._async_read(socket, DEVICE, clock.now + 1.5)
        assert socket.pongs == ["ping"]

    async def test_a_pong_counts_as_traffic(self, clock) -> None:
        import aiohttp

        made = self.running(None)
        socket = FakeSocket(Message(aiohttp.WSMsgType.PONG, ""), clock=clock)
        await made._async_read(socket, DEVICE, clock.now + 1.5)

    async def test_a_socket_error_drops_the_connection(self, clock) -> None:
        import aiohttp

        made = self.running(None)
        socket = FakeSocket(
            Message(aiohttp.WSMsgType.ERROR),
            error=OSError("reset by peer"),
            clock=clock,
        )
        with pytest.raises(JlrTelemetryError, match="socket error"):
            await made._async_read(socket, DEVICE, clock.now + 10_000)

    async def test_a_closed_socket_drops_the_connection(self, clock) -> None:
        import aiohttp

        made = self.running(None)
        socket = FakeSocket(Message(aiohttp.WSMsgType.CLOSED), clock=clock)
        with pytest.raises(JlrTelemetryError, match="closed"):
            await made._async_read(socket, DEVICE, clock.now + 10_000)


class TestStatusWithoutATimestamp:
    async def test_it_is_still_delivered(self) -> None:
        # Logged so the unrecognised field name can be identified, but a
        # snapshot with no timestamp is better than no snapshot.
        client, ws = telemetry(), FakeWebSocket()
        body = json.dumps(
            {"vehicleStatus": {"coreStatus": [{"key": "ODOMETER", "value": "1"}]}}
        )
        await client._async_handle(
            ws, message(eid="e-8", st=WS_TYPE_STATUS, v=VIN, a={"b": body}), DEVICE
        )
        assert client.status[0][1]["ODOMETER"] == "1"


class TestDoublyEncodedBodies:
    def test_a_body_already_decoded_is_taken_as_is(self) -> None:
        # The envelope's "b" is normally a JSON string, but the broker has
        # been seen to send the object directly.
        assert tel._inner_payload({"a": {"b": {"vehicleStatus": {}}}}) == {
            "vehicleStatus": {}
        }

    @pytest.mark.parametrize(
        "envelope",
        [{}, {"a": "not a mapping"}, {"a": {}}, {"a": {"b": 42}}, {"a": {"b": "[]"}}],
    )
    def test_anything_else_yields_nothing(self, envelope: dict) -> None:
        assert tel._inner_payload(envelope) is None


class TestRepeatedHeaders:
    def test_the_first_occurrence_wins(self) -> None:
        # STOMP 1.2 §3.1. Taking the last would let a second header override
        # the one the broker meant.
        raw = "CONNECTED\nversion:1.2\nversion:1.0\n\n\x00"
        (frame,) = tel._decode(raw)
        assert frame.headers["version"] == "1.2"

    def test_a_line_without_a_colon_is_skipped(self) -> None:
        raw = "CONNECTED\nnonsense\nversion:1.2\n\n\x00"
        (frame,) = tel._decode(raw)
        assert frame.headers == {"version": "1.2"}


class TestTheSnapshotAgeDiagnostic:
    """Instrumentation for the one question this integration cannot answer.

    These cars send no timestamp with a status, so nothing distinguishes a
    snapshot recorded an hour ago from one recorded last night — and JLR
    deliver them late and out of order, so the last to arrive is not the
    newest. The envelope's ``t`` is read and then distrusted as the broker's
    send time; whether that is true decides whether ordering is possible at
    all, and it is answerable only from a live log.

    The line has to carry ``t`` next to something that betrays the payload's
    real age. Coolant temperature does: a car parked overnight cannot honestly
    report ninety-one degrees.
    """

    async def test_it_logs_t_beside_the_markers_that_date_the_payload(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        client, ws = telemetry(), FakeWebSocket()
        body = json.dumps(
            {
                "vehicleStatus": {
                    "coreStatus": [
                        {"key": "ENGINE_COOLANT_TEMP", "value": "91"},
                        {"key": "BATTERY_VOLTAGE", "value": "13.3"},
                        {"key": "VEHICLE_STATE_TYPE", "value": "KEY_REMOVED"},
                        {"key": "ODOMETER_MILES", "value": "34650"},
                    ]
                }
            }
        )
        await client._async_handle(
            ws,
            message(
                eid="e-1",
                st=WS_TYPE_STATUS,
                v=VIN,
                a={"b": body},
                t="2026-09-08T19:58:34.000Z",
            ),
            DEVICE,
        )
        line = next(m for m in caplog.messages if " t=" in m)
        assert "t=2026-09-08T19:58:34.000Z" in line
        assert "coolant=91" in line
        assert "volts=13.3" in line
        assert "state=KEY_REMOVED" in line
        assert "odo=34650" in line

    async def test_it_does_not_put_the_vin_in_the_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Debug logs get pasted into issues.
        caplog.set_level(logging.DEBUG)
        client, ws = telemetry(), FakeWebSocket()
        await client._async_handle(ws, vhs(t="2026-08-26T08:12:41.589Z"), DEVICE)
        assert VIN not in caplog.text

    async def test_it_reports_the_envelope_and_header_names(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Both are places a timestamp could be hiding that nothing has looked
        # at; the point is to find out what is actually there.
        caplog.set_level(logging.DEBUG)
        client, ws = telemetry(), FakeWebSocket()
        await client._async_handle(ws, vhs(t="...", _headers=BROKER_HEADERS), DEVICE)
        line = next(m for m in caplog.messages if " t=" in m)
        assert "envelope=" in line and "'t'" in line
        assert "'destination'" in line and "'message-id'" in line

    async def test_it_reports_the_value_of_a_header_that_carries_a_time(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Finding one of these is the whole point of the diagnostic.
        caplog.set_level(logging.DEBUG)
        client, ws = telemetry(), FakeWebSocket()
        await client._async_handle(ws, vhs(t="...", _headers=BROKER_HEADERS), DEVICE)
        line = next(m for m in caplog.messages if " t=" in m)
        assert "2026-08-26T08:12:41.589Z" in line

    async def test_it_does_not_report_identifiers_from_header_values(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A header value is free-form text, and the broker puts the device id
        # and the VIN inside the topic it addresses a frame to. Redaction
        # matches on key names, so a whole-header dump walks straight past
        # both — and these lines get pasted into public issues.
        caplog.set_level(logging.DEBUG)
        client, ws = telemetry(), FakeWebSocket()
        await client._async_handle(ws, vhs(t="...", _headers=BROKER_HEADERS), DEVICE)
        assert DEVICE not in caplog.text
        assert VIN not in caplog.text


class TestAnOutageIsAnnouncedOnceAndSoIsItsEnd:
    """Home Assistant's rule, which the quality scale claimed we met.

    One message when a thing becomes unavailable and one when it comes back,
    with the retries in between at debug. Every failed attempt used to file
    its own warning while recovery was logged at debug only — so an outage
    announced loudly appeared never to end, and a long one buried whatever
    else somebody had opened the log to read.
    """

    def announced(self, caplog: pytest.LogCaptureFixture) -> list[str]:
        """The messages a person is meant to see.

        Info, per the rule: "Logging should happen at info level", and its
        example logs the outage and the recovery both there. A socket that
        drops and comes back is ordinary for a car parked underground.
        """
        return [r.message for r in caplog.records if r.levelname == "INFO"]

    def warnings(self, caplog: pytest.LogCaptureFixture) -> list[str]:
        return [r.message for r in caplog.records if r.levelname == "WARNING"]

    async def test_a_long_outage_says_so_once(
        self, caplog: pytest.LogCaptureFixture, waits
    ) -> None:
        caplog.set_level(logging.DEBUG)
        client = telemetry()

        async def refused() -> None:
            raise JlrApiError("Jaguar Land Rover returned 503")

        client._async_session = refused
        await supervise(client, 4, waits)

        assert len(self.announced(caplog)) == 1, self.announced(caplog)
        assert not self.warnings(caplog), "an outage is not a warning"
        # The rest are still reported, just not at a level that buries the log.
        retries = [r for r in caplog.records if r.levelname == "DEBUG"]
        assert len(retries) >= 3

    async def test_coming_back_is_reported(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = telemetry()
        client._outage_logged = True
        client._connected = False
        caplog.set_level(logging.DEBUG)

        client._set_connected(True)

        # Info, not warning: recovery is good news and the rule asks for it
        # there. A warning would also mean an outage that ended still left two
        # warnings in the log.
        assert [m for m in self.announced(caplog) if "back" in m], [
            r.levelname for r in caplog.records
        ]
        assert not self.warnings(caplog)
        assert client._outage_logged is False

    async def test_being_rate_limited_is_reported_the_same_way(
        self, caplog: pytest.LogCaptureFixture, waits
    ) -> None:
        # It had its own warning that fired on every attempt and never set the
        # flag, so being throttled filed one a minute and never announced
        # recovering — the one branch the guard did not cover.
        caplog.set_level(logging.DEBUG)
        client = telemetry()

        async def throttled() -> None:
            raise JlrRateLimitError("slow down", retry_after=30)

        client._async_session = throttled
        await supervise(client, 3, waits)

        assert len(self.announced(caplog)) == 1, self.announced(caplog)
        assert not self.warnings(caplog)
        assert client._outage_logged is True

    async def test_the_wait_the_broker_asked_for_is_still_honoured(
        self, caplog: pytest.LogCaptureFixture, waits
    ) -> None:
        # Quieter logging must not quietly drop the Retry-After.
        caplog.set_level(logging.DEBUG)
        client = telemetry()

        async def throttled() -> None:
            raise JlrRateLimitError("slow down", retry_after=30)

        client._async_session = throttled
        await supervise(client, 2, waits)

        assert 30 in [int(w) for w in waits]

    async def test_a_recovery_nobody_was_told_about_is_not_announced(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Connecting for the first time is not a recovery.
        client = telemetry()
        client._outage_logged = False
        client._connected = False
        caplog.set_level(logging.DEBUG)

        client._set_connected(True)

        assert not self.announced(caplog)
        assert not self.warnings(caplog)

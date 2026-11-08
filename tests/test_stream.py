"""The MQTT session, against a broker run in-process.

The broker here speaks real MQTT 3.1.1 bytes, so what is tested is the framing
the client puts on the wire and how it reads what comes back — the parts a
hand-written client gets wrong. The connection is in memory rather than a
socket, because the Home Assistant test plugin blocks sockets; the client is
pointed at it by swapping the one call that opens the connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from custom_components.jlr_incontrol import stream
from custom_components.jlr_incontrol.stream import (
    MqttDropped,
    MqttError,
    MqttRefused,
    MqttRejected,
    connect_packet,
    parse_publish,
    run_session,
    subscribe_packet,
)

Script = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


def publish(topic: str, payload: bytes, packet_id: int | None = None) -> bytes:
    body = struct.pack("!H", len(topic)) + topic.encode()
    kind = 0x30
    if packet_id is not None:
        kind |= 0x02
        body += struct.pack("!H", packet_id)
    return stream._packet(kind, body + payload)


CONNACK_OK = bytes([0x20, 2, 0, 0])
SUBACK_OK = bytes([0x90, 3, 0, 1, 0])


# Held before any test swaps it, so the broker's side keeps reading properly
# while a test makes the client's reads fail.
_read = stream._read_packet


async def read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    return await _read(reader)


class Pipe(asyncio.Transport):
    """One direction of an in-memory connection: writes land in the peer's reader."""

    def __init__(self, peer: asyncio.StreamReader) -> None:
        super().__init__()
        self.peer = peer
        self.closed = False
        self.gone = False
        self.other: Pipe | None = None
        self.protocol: asyncio.StreamReaderProtocol | None = None

    def write(self, data: bytes | bytearray | memoryview) -> None:
        # Writing to a peer that has hung up goes nowhere, as on a socket.
        if not self.closed and not self.peer.at_eof() and not self.gone:
            self.peer.feed_data(bytes(data))

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.other is not None:
            self.other.gone = True
        self.peer.feed_eof()
        assert self.protocol is not None
        self.protocol.connection_lost(None)

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return default


def connection() -> tuple[tuple[Any, Any], tuple[Any, Any]]:
    """Two connected (reader, writer) pairs, without a socket."""
    loop = asyncio.get_running_loop()
    ends = []
    pipes = []
    readers = (asyncio.StreamReader(), asyncio.StreamReader())
    for mine, theirs in ((readers[0], readers[1]), (readers[1], readers[0])):
        protocol = asyncio.StreamReaderProtocol(mine)
        transport = Pipe(theirs)
        transport.protocol = protocol
        protocol.connection_made(transport)
        pipes.append(transport)
        ends.append((mine, asyncio.StreamWriter(transport, protocol, mine, loop)))
    pipes[0].other, pipes[1].other = pipes[1], pipes[0]
    return ends[0], ends[1]


BROKER: Broker | None = None


@pytest.fixture(autouse=True)
def in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the client's one connection call at the scripted broker.

    In memory rather than over a socket: the Home Assistant test plugin
    blocks sockets, and nothing here is about the socket itself.
    """
    global BROKER
    BROKER = None

    async def open_connection(host: str, port: int, **kwargs: Any) -> Any:
        if BROKER is None:
            raise ConnectionRefusedError("nothing listening")
        client, server = connection()
        BROKER.task = asyncio.ensure_future(BROKER.serve(*server))
        return client

    monkeypatch.setattr(stream.asyncio, "open_connection", open_connection)


class Broker:
    """One connection, scripted. Records the packets the client sent."""

    def __init__(self, script: Script) -> None:
        self.script = script
        self.received: list[tuple[int, bytes]] = []
        self.task: asyncio.Future[None] | None = None

    async def serve(self, reader: asyncio.StreamReader, writer: Any) -> None:
        try:
            await self.script(reader, writer)
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()

    async def __aenter__(self) -> Broker:
        global BROKER
        BROKER = self
        return self

    async def __aexit__(self, *args: object) -> None:
        global BROKER
        BROKER = None
        if self.task is not None:
            # Let the script read what the client sent on its way out.
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self.task, timeout=1)

    async def take(self, reader: asyncio.StreamReader) -> tuple[int, bytes]:
        packet = await read_packet(reader)
        self.received.append(packet)
        return packet


async def session(
    broker: Broker, **kwargs: object
) -> tuple[str, list[tuple[str, bytes]]]:
    messages: list[tuple[str, bytes]] = []
    options: dict = {
        "host": "broker.invalid",
        "port": 8883,
        "ssl_context": None,
        "client_id": "ha-test",
        "username": "DSAS-0123",
        "password": "token",
        "topics": ["account/a/vehicles/V/#"],
        "on_message": lambda t, p: messages.append((t, p)),
        "keepalive": 60,
        **kwargs,
    }
    reason = await asyncio.wait_for(run_session(**options), timeout=10)
    return reason, messages


class TestTheWireFormat:
    def test_connect_carries_the_credentials_and_a_clean_session(self) -> None:
        packet = connect_packet("cid", "user", "pass", 60)
        assert packet[0] == 0x10
        assert b"\x00\x04MQTT\x04\xc2\x00\x3c" in packet
        assert packet.endswith(b"\x00\x03cid\x00\x04user\x00\x04pass")

    def test_a_long_password_gets_a_multi_byte_length(self) -> None:
        # A JWT is well over 127 bytes; a one-byte length would truncate it.
        packet = connect_packet("c", "u", "p" * 1200, 60)
        size, shift, i = 0, 0, 1
        while True:
            byte = packet[i]
            size |= (byte & 0x7F) << shift
            i += 1
            if not byte & 0x80:
                break
            shift += 7
        assert size == len(packet) - i

    def test_subscribe_asks_for_qos_0(self) -> None:
        packet = subscribe_packet(
            1, ["account/a/vehicles/V/#", "account/a/vehicles/W/#"]
        )
        assert packet[0] == 0x82
        assert packet.endswith(b"V/#\x00\x00\x16account/a/vehicles/W/#\x00")

    def test_a_qos_1_publish_has_a_packet_id(self) -> None:
        raw = publish("t", b"{}", packet_id=7)
        topic, payload, packet_id = parse_publish(raw[0], raw[2:])
        assert (topic, payload, packet_id) == ("t", b"{}", 7)


class TestASession:
    async def test_messages_are_delivered_until_the_broker_closes(self) -> None:
        subscribed: list[bool] = []

        async def script(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await broker.take(r)
            w.write(CONNACK_OK)
            await broker.take(r)
            w.write(SUBACK_OK)
            w.write(publish("account/a/vehicles/V/status", b'{"name":"x"}'))
            w.write(publish("account/a/vehicles/V/status", b'{"name":"y"}', 9))
            await w.drain()
            await broker.take(r)  # the PUBACK for the QoS 1 delivery

        broker = Broker(script)
        async with broker:
            reason, messages = await session(
                broker, on_subscribed=lambda: subscribed.append(True)
            )
        assert reason == "closed by broker"
        assert [p for _, p in messages] == [b'{"name":"x"}', b'{"name":"y"}']
        assert subscribed == [True]
        kinds = [k for k, _ in broker.received]
        assert kinds == [0x10, 0x82, 0x40]
        assert broker.received[2][1] == struct.pack("!H", 9)

    async def test_a_refusal_says_which(self) -> None:
        async def script(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await broker.take(r)
            w.write(bytes([0x20, 2, 0, 5]))
            await w.drain()

        broker = Broker(script)
        async with broker:
            with pytest.raises(MqttRefused, match="not authorised") as raised:
                await session(broker)
        assert raised.value.code == 5

    async def test_an_unknown_refusal_code_is_still_reported(self) -> None:
        async def script(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await broker.take(r)
            w.write(bytes([0x20, 2, 0, 0x87]))
            await w.drain()

        broker = Broker(script)
        async with broker:
            with pytest.raises(MqttRefused, match="135"):
                await session(broker)

    async def test_something_other_than_connack_is_an_error(self) -> None:
        async def script(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await broker.take(r)
            w.write(bytes([0xD0, 0]))
            await w.drain()

        broker = Broker(script)
        async with broker:
            with pytest.raises(MqttError, match="CONNACK"):
                await session(broker)

    async def test_a_refused_subscription_is_not_a_quiet_car(self) -> None:
        async def script(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await broker.take(r)
            w.write(CONNACK_OK)
            await broker.take(r)
            w.write(bytes([0x90, 3, 0, 1, 0x80]))
            await w.drain()
            await asyncio.sleep(1)

        broker = Broker(script)
        async with broker:
            with pytest.raises(MqttRejected, match="subscription refused"):
                await session(broker)

    async def test_a_hang_up_before_connack_is_a_rejected_login(self) -> None:
        # How AWS IoT turns a login down: no refusal code, just a close.
        async def script(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await broker.take(r)

        broker = Broker(script)
        async with broker:
            with pytest.raises(MqttDropped, match="login") as raised:
                await session(broker)
        assert isinstance(raised.value, MqttRejected)

    async def test_a_hang_up_before_suback_is_a_rejected_subscription(self) -> None:
        async def script(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await broker.take(r)
            w.write(CONNACK_OK)
            await broker.take(r)

        broker = Broker(script)
        async with broker:
            with pytest.raises(MqttDropped, match="subscription"):
                await session(broker)

    async def test_one_filter_refused_of_several_is_not_a_fault(self) -> None:
        # Two cars, one of which the broker will not serve: the other still
        # deserves its updates.
        async def script(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await broker.take(r)
            w.write(CONNACK_OK)
            await broker.take(r)
            w.write(bytes([0x90, 4, 0, 1, 0x80, 0]))
            await w.drain()

        broker = Broker(script)
        async with broker:
            reason, _ = await session(broker, topics=["a/#", "b/#"])
        assert reason == "closed by broker"

    async def test_it_leaves_at_its_time_limit(self) -> None:
        async def script(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await broker.take(r)
            w.write(CONNACK_OK)
            await broker.take(r)
            w.write(SUBACK_OK)
            await w.drain()
            await broker.take(r)  # DISCONNECT, sent on the way out

        broker = Broker(script)
        async with broker:
            reason, _ = await session(broker, max_seconds=0.3)
        assert reason == "time limit"
        assert broker.received[-1][0] == 0xE0

    async def test_a_quiet_line_is_kept_alive_with_a_ping(self) -> None:
        async def script(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await broker.take(r)
            w.write(CONNACK_OK)
            await broker.take(r)
            w.write(SUBACK_OK)
            await w.drain()
            kind, _ = await broker.take(r)
            assert kind == 0xC0
            w.write(bytes([0xD0, 0]))
            await w.drain()

        broker = Broker(script)
        async with broker:
            reason, _ = await session(broker, keepalive=0.2)
        assert reason == "closed by broker"
        assert 0xC0 in [k for k, _ in broker.received]

    async def test_a_busy_line_still_pings_on_time(self) -> None:
        # The keepalive is the client's to keep: the broker hangs up on a
        # client silent for one and a half keepalives, however much the broker
        # itself is sending. A message every 0.05s once reset the wait, so
        # the client never sent anything at all.
        async def script(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await broker.take(r)
            w.write(CONNACK_OK)
            await broker.take(r)
            w.write(SUBACK_OK)
            await w.drain()
            loop = asyncio.get_running_loop()
            pinged = asyncio.ensure_future(broker.take(r))
            until = loop.time() + 0.6
            while loop.time() < until and not pinged.done():
                w.write(publish("account/a/vehicles/V/status", b"[]"))
                await w.drain()
                await asyncio.sleep(0.05)
            if pinged.done():
                w.write(bytes([0xD0, 0]))
                await w.drain()
            else:
                pinged.cancel()

        broker = Broker(script)
        async with broker:
            reason, messages = await session(broker, keepalive=0.2)
        assert reason == "closed by broker"
        assert messages
        assert 0xC0 in [k for k, _ in broker.received]

    async def test_an_unanswered_ping_ends_the_session(self) -> None:
        async def script(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await broker.take(r)
            w.write(CONNACK_OK)
            await broker.take(r)
            w.write(SUBACK_OK)
            await w.drain()
            await asyncio.sleep(2)

        broker = Broker(script)
        async with broker:
            with pytest.raises(MqttError, match="keepalive"):
                await session(broker, keepalive=0.2)

    async def test_a_deadline_reached_while_waiting_is_a_time_limit(self) -> None:
        async def script(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await broker.take(r)
            w.write(CONNACK_OK)
            await broker.take(r)
            w.write(SUBACK_OK)
            await w.drain()
            await asyncio.sleep(2)

        broker = Broker(script)
        async with broker:
            reason, _ = await session(broker, keepalive=5, max_seconds=0.3)
        assert reason == "time limit"

    async def test_a_malformed_length_is_an_error(self) -> None:
        async def script(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await broker.take(r)
            w.write(bytes([0x20, 0xFF, 0xFF, 0xFF, 0xFF, 0x01]))
            await w.drain()

        broker = Broker(script)
        async with broker:
            with pytest.raises(MqttError, match="malformed"):
                await session(broker)

    async def test_an_enormous_packet_is_refused(self) -> None:
        async def script(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await broker.take(r)
            w.write(bytes([0x20]) + stream._length(stream.MAX_PACKET + 1))
            await w.drain()

        broker = Broker(script)
        async with broker:
            with pytest.raises(MqttError, match="refused"):
                await session(broker)

    async def test_nothing_listening_is_an_error(self) -> None:
        # No broker: the connection is refused.
        with pytest.raises(MqttError, match="could not connect"):
            await asyncio.wait_for(
                run_session(
                    host="broker.invalid",
                    port=8883,
                    ssl_context=None,
                    client_id="c",
                    username="u",
                    password="p",
                    topics=["t"],
                    on_message=lambda t, p: None,
                ),
                timeout=10,
            )

    async def test_a_reset_mid_session_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def script(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await broker.take(r)
            w.write(CONNACK_OK)
            await w.drain()
            await asyncio.sleep(1)

        async def reset(reader: asyncio.StreamReader) -> tuple[int, bytes]:
            raise ConnectionResetError("reset")

        broker = Broker(script)
        async with broker:
            calls = 0
            original = stream._read_packet

            async def first_then_reset(
                reader: asyncio.StreamReader,
            ) -> tuple[int, bytes]:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return await original(reader)
                return await reset(reader)

            monkeypatch.setattr(stream, "_read_packet", first_then_reset)
            with pytest.raises(MqttError, match="connection lost"):
                await session(broker)

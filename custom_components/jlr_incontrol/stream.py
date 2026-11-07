"""Just enough MQTT 3.1.1 to listen to the JLR Data Hub stream.

Written here rather than taken from paho-mqtt because what the stream needs is
small and fixed: connect with a username and password, subscribe once, receive
QoS 0 publishes, answer keepalive. The broker downgrades anything else to QoS 0
regardless. paho would add a requirement to install, a thread of its own per
connection, and a callback API to marshal back onto the event loop — all for
the parts of MQTT this never uses.

Nothing here knows about JLR or Home Assistant: it is a session that ends, and
says why. Reconnecting, and how politely, is the caller's business.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import struct
from collections.abc import Callable, Sequence

CONNECT = 0x10
CONNACK = 0x20
PUBLISH = 0x30
PUBACK = 0x40
SUBSCRIBE = 0x82
SUBACK = 0x90
PINGREQ = 0xC0
PINGRESP = 0xD0
DISCONNECT = 0xE0

# CONNACK return codes, for a log line a person can act on.
_REFUSALS = {
    1: "unacceptable protocol version",
    2: "client id rejected",
    3: "server unavailable",
    4: "bad username or password",
    5: "not authorised",
}

# What a hang-up names when the broker accepted the login but not the
# subscription: the login was right, the topics were not.
LOGIN_STAGE = "the login was accepted"
SUBSCRIPTION_STAGE = "the subscription was confirmed"

# Large enough for any signal message seen; small enough that a corrupt length
# cannot make us buffer the broker's whole stream.
MAX_PACKET = 1 << 20


class MqttError(Exception):
    """The session ended abnormally."""


class MqttRejected(MqttError):
    """The broker was reached and turned this session down.

    As distinct from not reaching it at all: a different username or client
    id might be accepted where a different host would not help.
    """


class MqttRefused(MqttRejected):
    """The broker answered CONNECT with a refusal code."""

    def __init__(self, code: int) -> None:
        super().__init__(_REFUSALS.get(code, f"refused ({code})"))
        self.code = code


class MqttDropped(MqttRejected):
    """The broker hung up before confirming a stage.

    How AWS IoT, which carries the Data Hub stream, turns down a login or a
    subscription its policy does not allow: no refusal code, just a closed
    connection. Which stage it was is the clue to what it disliked.
    """

    def __init__(self, stage: str) -> None:
        super().__init__(f"closed by the broker before {stage}")
        self.stage = stage


def _string(value: str) -> bytes:
    raw = value.encode()
    return struct.pack("!H", len(raw)) + raw


def _length(size: int) -> bytes:
    out = bytearray()
    while True:
        byte, size = size % 128, size // 128
        out.append(byte | 0x80 if size else byte)
        if not size:
            return bytes(out)


def _packet(kind: int, body: bytes) -> bytes:
    return bytes([kind]) + _length(len(body)) + body


def connect_packet(
    client_id: str, username: str, password: str, keepalive: int
) -> bytes:
    """CONNECT with a clean session and a username and password."""
    variable = _string("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", keepalive)
    return _packet(
        CONNECT, variable + _string(client_id) + _string(username) + _string(password)
    )


def subscribe_packet(packet_id: int, topics: Sequence[str]) -> bytes:
    """SUBSCRIBE to topic filters, all at QoS 0."""
    filters = b"".join(_string(topic) + b"\x00" for topic in topics)
    return _packet(SUBSCRIBE, struct.pack("!H", packet_id) + filters)


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = (await reader.readexactly(1))[0]
    size, shift = 0, 0
    for _ in range(4):
        byte = (await reader.readexactly(1))[0]
        size |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
    else:
        raise MqttError("malformed packet length")
    if size > MAX_PACKET:
        raise MqttError(f"packet of {size} bytes refused")
    return header, await reader.readexactly(size) if size else b""


def parse_publish(header: int, body: bytes) -> tuple[str, bytes, int | None]:
    """Topic, payload, and the packet id a QoS 1 delivery must be acknowledged with."""
    (topic_len,) = struct.unpack("!H", body[:2])
    topic = body[2 : 2 + topic_len].decode(errors="replace")
    offset = 2 + topic_len
    packet_id = None
    if (header >> 1) & 0x03:
        (packet_id,) = struct.unpack("!H", body[offset : offset + 2])
        offset += 2
    return topic, body[offset:], packet_id


async def run_session(
    *,
    host: str,
    port: int,
    ssl_context: ssl.SSLContext | None,
    client_id: str,
    username: str,
    password: str,
    topics: Sequence[str],
    on_message: Callable[[str, bytes], None],
    on_subscribed: Callable[[], None] | None = None,
    keepalive: float = 60,
    max_seconds: float | None = None,
) -> str:
    """Connect, subscribe, and deliver messages until the session ends.

    Returns why it ended when that was expected (the time limit, or the broker
    closing a healthy connection, which is how the stream's one-hour cap and
    token expiry arrive). Raises for anything that is a fault.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host,
                port,
                ssl=ssl_context,
                server_hostname=host if ssl_context else None,
            ),
            timeout=30,
        )
    except (OSError, TimeoutError) as err:
        raise MqttError(f"could not connect: {err!r}") from err
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds if max_seconds else None
    # One read in flight across keepalive timeouts. Cancelling a read on
    # timeout instead could abandon a packet half-consumed — its header byte
    # gone, its body left — and every packet after it misframed.
    reading: asyncio.Future[tuple[int, bytes]] | None = None
    # What the broker has yet to confirm. A hang-up before it is a rejection;
    # after it, the normal end of a session.
    stage = LOGIN_STAGE
    try:
        writer.write(
            connect_packet(client_id, username, password, max(1, round(keepalive)))
        )
        await writer.drain()
        kind, body = await asyncio.wait_for(_read_packet(reader), timeout=30)
        if kind & 0xF0 != CONNACK or len(body) < 2:
            raise MqttError("no CONNACK")
        if body[1]:
            raise MqttRefused(body[1])

        stage = SUBSCRIPTION_STAGE
        writer.write(subscribe_packet(1, topics))
        await writer.drain()
        awaiting_ping = False
        subscribed = False
        while True:
            wait = float(keepalive)
            if deadline is not None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return "time limit"
                wait = min(wait, remaining)
            if reading is None:
                reading = asyncio.ensure_future(_read_packet(reader))
            done, _ = await asyncio.wait({reading}, timeout=wait)
            if not done:
                if deadline is not None and loop.time() >= deadline:
                    return "time limit"
                if awaiting_ping:
                    raise MqttError("keepalive went unanswered")
                writer.write(bytes([PINGREQ, 0]))
                await writer.drain()
                awaiting_ping = True
                continue
            kind, body = reading.result()
            reading = None
            awaiting_ping = False
            kind_type = kind & 0xF0
            if kind_type == PUBLISH:
                topic_name, payload, packet_id = parse_publish(kind, body)
                if packet_id is not None:
                    writer.write(_packet(PUBACK, struct.pack("!H", packet_id)))
                    await writer.drain()
                on_message(topic_name, payload)
            elif kind_type == SUBACK and not subscribed:
                # 0x80 is the broker refusing a filter. Every filter refused
                # would sit connected and silent forever, which looks like a
                # parked car rather than a fault.
                if all(code == 0x80 for code in body[2:]):
                    raise MqttRejected("subscription refused")
                subscribed = True
                stage = ""
                if on_subscribed:
                    on_subscribed()
    except asyncio.IncompleteReadError:
        if stage:
            raise MqttDropped(stage) from None
        return "closed by broker"
    except (ConnectionError, ssl.SSLError) as err:
        raise MqttError(f"connection lost: {err!r}") from err
    finally:
        if reading is not None and not reading.done():
            reading.cancel()
        if not writer.is_closing():
            with contextlib.suppress(ConnectionError, RuntimeError):
                writer.write(bytes([DISCONNECT, 0]))
        writer.close()
        with contextlib.suppress(ConnectionError, ssl.SSLError, OSError):
            await writer.wait_closed()

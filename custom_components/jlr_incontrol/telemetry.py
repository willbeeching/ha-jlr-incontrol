"""Real-time vehicle telemetry over JLR's STOMP websocket.

Why this exists: in August 2026 JLR put Approov attestation on the REST
vehicle-data endpoints (``if9/webview/vehicles/{vin}/*``). They now answer 498
to anything that cannot produce a device-bound attestation token, and the
webview ``Origin`` bypass that carried this integration through the previous
block no longer helps. Polling is simply over.

The app's telemetry socket is not attested. It takes the plain ForgeRock bearer,
and the moment you subscribe to a VIN it pushes the current ``VHS`` payload —
the same ``coreStatus`` / ``evStatus`` key lists the REST ``/status`` used to
return — and then streams updates as they happen. So this is not a workaround
that gets us back to where we were; it is better than what we had.

The protocol is STOMP 1.2 carried in websocket text frames:

    CONNECT                         -> CONNECTED
    SUBSCRIBE /user/topic/DEVICE.x  -> SubscriptionAccepted, one per vehicle
    SUBSCRIBE /user/topic/VIN.y     -> MESSAGE (current VHS), then live updates
    SEND /app/messageReceived       <- acknowledge, or the broker redelivers

One connection covers every vehicle on the account.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any, NamedTuple

import aiohttp

from .api import JlrApiError, JlrClient, JlrRateLimitError, flatten_status
from .const import (
    USER_AGENT,
    WS_ACK_DESTINATION,
    WS_BACKOFF_MAX,
    WS_BACKOFF_START,
    WS_DEVICE_TOPIC,
    WS_HEARTBEAT_MS,
    WS_HEARTBEAT_SEND,
    WS_HOST,
    WS_READ_TIMEOUT,
    WS_TYPE_STATUS,
    WS_URL,
    WS_VIN_TOPIC,
)
from .redact import scrub, scrub_text

_LOGGER = logging.getLogger(__name__)

# STOMP frames are NUL-terminated; a lone newline between them is a heart-beat.
_NUL = "\x00"

# Header escaping, STOMP 1.2 section 3.1. The backslash must be substituted
# first or it would re-escape the escapes introduced after it.
_ESCAPES = (("\\", "\\\\"), ("\r", "\\r"), ("\n", "\\n"), (":", "\\c"))


def _escape(value: str) -> str:
    for raw, encoded in _ESCAPES:
        value = value.replace(raw, encoded)
    return value


def _unescape(value: str) -> str:
    out: list[str] = []
    iterator = iter(value)
    for char in iterator:
        if char != "\\":
            out.append(char)
            continue
        nxt = next(iterator, "")
        out.append({"r": "\r", "n": "\n", "c": ":", "\\": "\\"}.get(nxt, nxt))
    return "".join(out)


class Frame(NamedTuple):
    """One decoded STOMP frame."""

    command: str
    headers: dict[str, str]
    body: str


def _encode(command: str, headers: dict[str, str], body: str = "") -> str:
    lines = [command]
    lines.extend(f"{_escape(key)}:{_escape(value)}" for key, value in headers.items())
    return "\n".join(lines) + "\n\n" + body + _NUL


def _decode(raw: str) -> list[Frame]:
    """Split a websocket payload into frames, ignoring heart-beats."""
    frames: list[Frame] = []
    for chunk in raw.split(_NUL):
        # Leading EOLs are heart-beats sitting between frames, not content.
        chunk = chunk.lstrip("\r\n")
        if not chunk:
            continue
        head, _, body = chunk.partition("\n\n")
        lines = head.split("\n")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            # Repeated headers: the first occurrence wins (STOMP 1.2, 3.1).
            headers.setdefault(_unescape(key.strip()), _unescape(value.strip()))
        frames.append(Frame(lines[0].strip(), headers, body))
    return frames


class JlrTelemetryError(Exception):
    """Raised when the telemetry socket cannot be established or is refused."""


class JlrTelemetry:
    """Keeps one STOMP subscription alive and hands messages to the coordinator.

    Owns a supervisor task that reconnects with backoff. The STOMP session is
    bound to the bearer presented at CONNECT and the ForgeRock access token only
    lives about five minutes, so a reconnect shortly before expiry is the normal
    case rather than a fault — each one also re-delivers a fresh snapshot.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        client: JlrClient,
        *,
        on_status: Callable[[str, dict[str, Any], str | None], None],
        on_position: Callable[[str, dict[str, Any]], None],
        on_connected: Callable[[bool], None],
    ) -> None:
        self._session = session
        self._client = client
        self._on_status = on_status
        self._on_position = on_position
        self._on_connected = on_connected
        self._vins: list[str] = []
        self._task: asyncio.Task[None] | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        """Whether a subscribed session is currently up."""
        return self._connected

    def async_set_vehicles(self, vins: Iterable[str]) -> None:
        """Update the VIN list used on the next connection."""
        self._vins = sorted(vins)

    async def async_start(self) -> None:
        """Start the supervisor task (idempotent)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._async_supervise())

    async def async_stop(self) -> None:
        """Cancel the supervisor and wait for it to unwind."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._set_connected(False)

    # ------------------------------------------------------------- supervisor
    async def _async_supervise(self) -> None:
        backoff = WS_BACKOFF_START.total_seconds()
        while True:
            wait = backoff
            try:
                await self._async_session()
            except asyncio.CancelledError:
                raise
            except JlrRateLimitError as err:
                # Being told how long to wait beats guessing, and ignoring the
                # instruction is how a client stops being throttled and starts
                # being blocked.
                wait = backoff if err.retry_after is None else err.retry_after
                _LOGGER.warning(
                    "telemetry rate limited: %s; waiting %ss as asked",
                    err,
                    int(wait),
                )
            except (JlrTelemetryError, JlrApiError, aiohttp.ClientError) as err:
                _LOGGER.warning(
                    "telemetry socket dropped: %s; retrying in %ss", err, int(backoff)
                )
            except Exception:  # noqa: BLE001 - the supervisor must never die
                _LOGGER.exception(
                    "unexpected telemetry failure; retrying in %ss", int(backoff)
                )
            else:
                # A clean return is the planned token-expiry reconnect: go
                # straight back round without penalising it with a backoff.
                backoff = WS_BACKOFF_START.total_seconds()
                continue
            finally:
                self._set_connected(False)
            await asyncio.sleep(wait)
            backoff = min(backoff * 2, WS_BACKOFF_MAX.total_seconds())

    # ---------------------------------------------------------------- session
    async def _async_session(self) -> None:
        """Run one connection until the token needs renewing or it drops."""
        # Renews the access token and re-registers the device if needed.
        await self._client.async_connect()
        token = self._client.access_token
        if not token:
            raise JlrTelemetryError("no access token to open the telemetry socket")
        device_id = self._client.device_id
        # Reconnect before the bearer we are about to present goes stale.
        deadline = time.monotonic() + self._client.seconds_until_renewal()

        try:
            ws = await self._session.ws_connect(
                WS_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "deviceId": device_id,
                    "User-Agent": USER_AGENT,
                },
                heartbeat=None,
                autoping=False,
            )
        except aiohttp.ClientError as err:
            raise JlrTelemetryError(
                f"could not open the telemetry socket: {err}"
            ) from err

        async with ws:
            await self._async_handshake(ws, token, device_id)
            await self._async_subscribe(ws, device_id)
            self._set_connected(True)
            await self._async_read(ws, device_id, deadline)

    async def _async_handshake(
        self, ws: aiohttp.ClientWebSocketResponse, token: str, device_id: str
    ) -> None:
        await ws.send_str(
            _encode(
                "CONNECT",
                {
                    "accept-version": "1.2",
                    "host": WS_HOST,
                    "Authorization": f"Bearer {token}",
                    "deviceId": device_id,
                    "userName": self._client.username,
                    "heart-beat": f"{WS_HEARTBEAT_MS},{WS_HEARTBEAT_MS}",
                },
            )
        )
        frame = await self._async_next_frame(ws)
        if frame.command == "ERROR":
            raise JlrTelemetryError(
                f"telemetry broker refused the connection: "
                f"{frame.headers.get('message') or frame.body[:200] or 'no reason given'}"
            )
        if frame.command != "CONNECTED":
            raise JlrTelemetryError(
                f"telemetry broker answered {frame.command} instead of CONNECTED"
            )

    async def _async_subscribe(
        self, ws: aiohttp.ClientWebSocketResponse, device_id: str
    ) -> None:
        """Subscribe to the device topic first, then one topic per vehicle.

        The order is the app's own: the device topic is what acknowledges each
        vehicle subscription, so subscribing to it second loses those receipts.
        """
        await ws.send_str(
            _encode(
                "SUBSCRIBE",
                {
                    "id": "sub-dev",
                    "destination": WS_DEVICE_TOPIC.format(device_id=device_id),
                    "ack": "auto",
                },
            )
        )
        for index, vin in enumerate(self._vins):
            await ws.send_str(
                _encode(
                    "SUBSCRIBE",
                    {
                        "id": f"sub-vin-{index}",
                        "destination": WS_VIN_TOPIC.format(vin=vin),
                        "ack": "auto",
                    },
                )
            )
        _LOGGER.debug("telemetry subscribed to %s vehicle(s)", len(self._vins))

    # ------------------------------------------------------------------- read
    async def _async_read(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        device_id: str,
        deadline: float,
    ) -> None:
        """Pump frames until the token deadline or the socket drops."""
        beat = WS_HEARTBEAT_SEND.total_seconds()
        last_rx = last_tx = time.monotonic()
        while True:
            now = time.monotonic()
            if now >= deadline:
                _LOGGER.debug("telemetry reconnecting for a fresh access token")
                return
            if now - last_rx > WS_READ_TIMEOUT.total_seconds():
                raise JlrTelemetryError(
                    f"no telemetry traffic for {WS_READ_TIMEOUT.total_seconds():.0f}s"
                )
            # Our half of the heart-beat contract, on a clock rather than on an
            # idle read: the broker sends every 10s, so waiting for a quiet
            # socket to prompt us means never sending one, and it closes the
            # session for inactivity after a couple of minutes.
            if now - last_tx >= beat:
                await ws.send_str("\n")
                last_tx = now
            window = min(beat - (now - last_tx), max(deadline - now, 0.1))
            try:
                msg = await ws.receive(timeout=max(window, 0.1))
            except TimeoutError:
                continue

            if msg.type is aiohttp.WSMsgType.TEXT:
                last_rx = time.monotonic()
                for frame in _decode(msg.data):
                    await self._async_handle(ws, frame, device_id)
            elif msg.type is aiohttp.WSMsgType.PING:
                last_rx = time.monotonic()
                await ws.pong(msg.data)
            elif msg.type is aiohttp.WSMsgType.PONG:
                last_rx = time.monotonic()
            elif msg.type is aiohttp.WSMsgType.ERROR:
                raise JlrTelemetryError(f"telemetry socket error: {ws.exception()}")
            else:
                raise JlrTelemetryError(f"telemetry socket closed ({msg.type.name})")

    async def _async_next_frame(self, ws: aiohttp.ClientWebSocketResponse) -> Frame:
        """Wait for the next real frame, skipping heart-beats."""
        deadline = time.monotonic() + WS_READ_TIMEOUT.total_seconds()
        while time.monotonic() < deadline:
            try:
                msg = await ws.receive(timeout=WS_READ_TIMEOUT.total_seconds())
            except TimeoutError:
                break
            if msg.type is not aiohttp.WSMsgType.TEXT:
                raise JlrTelemetryError(
                    f"telemetry socket closed during handshake ({msg.type.name})"
                )
            frames = _decode(msg.data)
            if frames:
                return frames[0]
        raise JlrTelemetryError("telemetry broker did not answer the handshake")

    # --------------------------------------------------------------- messages
    async def _async_handle(
        self, ws: aiohttp.ClientWebSocketResponse, frame: Frame, device_id: str
    ) -> None:
        """Dispatch one frame."""
        if frame.command == "ERROR":
            raise JlrTelemetryError(
                f"telemetry broker sent ERROR: "
                f"{frame.headers.get('message') or frame.body[:200]}"
            )
        if frame.command != "MESSAGE":
            return

        try:
            envelope = json.loads(frame.body)
        except ValueError:
            _LOGGER.debug(
                "telemetry sent a non-JSON message: %.200s", scrub_text(frame.body)
            )
            return
        if not isinstance(envelope, dict):
            return

        event_id = envelope.get("eid")
        if event_id:
            await self._async_ack(ws, device_id, [str(event_id)])

        message_type = str(envelope.get("st") or envelope.get("messageType") or "")
        vin = envelope.get("v") or envelope.get("vin")
        if not vin:
            _LOGGER.debug("telemetry message without a VIN: %s", message_type or "?")
            return

        payload = _inner_payload(envelope)
        if payload is None:
            _LOGGER.debug("telemetry %s message carried no readable body", message_type)
            return

        if message_type == WS_TYPE_STATUS:
            status = flatten_status(payload)
            if not status:
                return
            if "LAST_UPDATED_TIME" not in status:
                _LOGGER.debug(
                    "VHS items carry no recognised timestamp; first item is %s",
                    scrub(_first_item(payload)),
                )
            self._on_status(vin, status, str(envelope.get("t") or "") or None)
            return

        position = _extract_position(payload)
        if position:
            self._on_position(vin, position)
            return

        # Everything else is a command/service notification. Log the shape so an
        # unrecognised message type can be identified from a debug log rather
        # than guessed at.
        _LOGGER.debug(
            "telemetry %s message for a vehicle, unhandled: %.300s",
            message_type or "(untyped)",
            json.dumps(scrub(payload)),
        )

    async def _async_ack(
        self, ws: aiohttp.ClientWebSocketResponse, device_id: str, event_ids: list[str]
    ) -> None:
        """Acknowledge messages, or the broker keeps redelivering them."""
        await ws.send_str(
            _encode(
                "SEND",
                {"destination": WS_ACK_DESTINATION, "content-type": "application/json"},
                json.dumps({"deviceId": device_id, "eventIds": event_ids}),
            )
        )

    def _set_connected(self, connected: bool) -> None:
        if connected != self._connected:
            self._connected = connected
            self._on_connected(connected)


def _first_item(payload: dict[str, Any]) -> Any:
    """One raw coreStatus item, for identifying an unrecognised field name."""
    group = (payload.get("vehicleStatus", payload) or {}).get("coreStatus") or []
    return group[0] if group else None


def _inner_payload(envelope: dict[str, Any]) -> dict[str, Any] | None:
    """Unwrap the double-encoded body: envelope["a"]["b"] is a JSON *string*."""
    body = envelope.get("a")
    if isinstance(body, dict):
        inner = body.get("b")
        if isinstance(inner, str):
            try:
                parsed = json.loads(inner)
            except ValueError:
                return None
            return parsed if isinstance(parsed, dict) else None
        if isinstance(inner, dict):
            return inner
    return None


# Position has not been observed live on this socket yet (the REST /position it
# replaces is behind the same wall), so accept the spellings the app's own model
# and the old REST payload both use rather than betting on one.
_LATITUDE_KEYS = ("latitude", "lat")
_LONGITUDE_KEYS = ("longitude", "longitude_", "lon", "lng")


def _extract_position(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull a {latitude, longitude, ...} dict out of a position message."""
    for candidate in (
        payload,
        payload.get("position"),
        payload.get("vehiclePosition"),
        (payload.get("position") or {}).get("position"),
    ):
        if not isinstance(candidate, dict):
            continue
        latitude = _first(candidate, _LATITUDE_KEYS)
        longitude = _first(candidate, _LONGITUDE_KEYS)
        if latitude is None or longitude is None:
            continue
        return {
            "latitude": latitude,
            "longitude": longitude,
            "heading": _first(candidate, ("heading", "bearing")),
            "speed": _first(candidate, ("speed",)),
            "timestamp": _first(candidate, ("timestamp", "ts")),
        }
    return {}


def _first(source: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if source.get(key) is not None:
            return source[key]
    return None

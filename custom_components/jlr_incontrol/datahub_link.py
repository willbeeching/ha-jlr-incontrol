"""The running link to the JLR Data Hub, for an entry that has one set up.

Optional and additive. Everything the integration already does carries on
exactly as before; this runs alongside, and for now what it adds is the thing
nothing else in JLR's data has ever had — the time the car actually reported.

Two sources, both JLR's documented API:

- REST status, read when the link starts and then hourly. Cheap insurance, and
  the only source until the owner turns the stream on in the portal.
- The MQTT stream, held open continuously. JLR push signals as the car sends
  them, each stamped with when it sent them.

Politeness is the same rule as the socket's: a floor between connection
attempts, backoff on failure, and nothing that retries in a loop when the
answer cannot change (a link the owner has revoked).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import ssl
import time
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from homeassistant.util.ssl import client_context

from .const import (
    CONF_DATAHUB,
    DATAHUB_ACCOUNT_ID,
    DATAHUB_CLIENT_ID,
    DATAHUB_PRIVATE_KEY,
    DATAHUB_REFRESH_TOKEN,
    DATAHUB_VEHICLES,
    DOMAIN,
    ISSUE_DATAHUB_SIGNED_OUT,
)
from .datahub import (
    DataHubAuthError,
    DataHubClient,
    DataHubError,
    DataHubForbidden,
    DataHubRateLimited,
    Tokens,
)
from .redact import vehicle_label
from .stream import MqttError, MqttRejected, run_session

_LOGGER = logging.getLogger(__name__)

# The developer docs name the first; the portal shows the second, which did
# not resolve in public DNS when this was written (November 2026). Both are
# kept because the portal is JLR's own word on it and may yet go live;
# whichever connects is remembered.
BROKERS = ("stream-data.jlr-vcdp.com", "stream-data.b2b-prod.jlr-vcdp.com")
BROKER_PORT = 8883
# AWS IoT, which carries the stream, also takes a username and password on
# 443 when the TLS handshake asks for the "mqtt" protocol. Tried only after
# every login on the documented port has been turned down.
ALPN_PORT = 443

# The broker closes a session at one hour. Leaving at 55 minutes reconnects on
# our schedule rather than mid-message on theirs.
SESSION_SECONDS = 55 * 60
# Minimum gap between connection attempts, however they ended. The docs warn
# that rapid reconnects are rate-limited; this keeps a broker that closes us
# instantly from becoming a tight loop.
RECONNECT_FLOOR = 30.0
RECONNECT_CEILING = 15 * 60.0
# A session shorter than this counts as a failure for backoff purposes.
HEALTHY_SESSION = 60.0
# REST status, alongside the stream. The stream carries changes; this is the
# full snapshot, and the only source while the stream is switched off.
STATUS_INTERVAL = 60 * 60.0


@dataclass(frozen=True)
class _Login:
    """One way of presenting ourselves to the broker."""

    username: str
    own_id: bool
    label: str
    port: int = BROKER_PORT

    def client_id(self, entry_id: str, started: float) -> str:
        """The username itself, or an id of our own unique to this session."""
        if self.own_id:
            return self.username
        return f"ha-{entry_id[:8]}-{int(started)}"


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    return dt_util.parse_datetime(value)


def _flatten(tree: Any, prefix: str = "") -> dict[str, Any]:
    """The nested VSS tree REST returns, as dotted signal names."""
    flat: dict[str, Any] = {}
    if isinstance(tree, dict):
        for key, value in tree.items():
            name = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                flat.update(_flatten(value, name))
            else:
                flat[name] = value
    return flat


class DataHubLink:
    """Sign-in upkeep, REST status and the stream, for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        settings: dict[str, Any] = entry.data[CONF_DATAHUB]
        self.client = DataHubClient(
            async_get_clientsession(hass),
            settings[DATAHUB_CLIENT_ID],
            settings[DATAHUB_PRIVATE_KEY],
        )
        self.vehicles: list[str] = list(settings.get(DATAHUB_VEHICLES, []))
        self.account_id: str | None = settings.get(DATAHUB_ACCOUNT_ID)
        self._tokens: Tokens | None = None
        self._task: asyncio.Task[None] | None = None
        self._listeners: list[CALLBACK_TYPE] = []

        self.connected = False
        self.signed_out = False
        # None until the stream has been tried; False when the portal has not
        # had it switched on, which is a setting the owner can change.
        self.stream_enabled: bool | None = None
        self.broker: str | None = None
        self._login: _Login | None = None
        self._alpn_context: ssl.SSLContext | None = None
        # The newest time the car itself reported, per VIN, from either source.
        self.reports: dict[str, datetime] = {}
        # Latest value and vehicle time per signal, per VIN. Names and times go
        # into diagnostics; the values stay here.
        self.signals: dict[str, dict[str, dict[str, Any]]] = {}
        self.forbidden: dict[str, list[str]] = {}
        self.stats: dict[str, Any] = {
            "sessions": 0,
            "messages": 0,
            "last_session_seconds": None,
            "last_session_end": None,
            "last_error": None,
        }
        self._last_status_read = 0.0

    # -- lifecycle --------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Start the background loop. Returns at once."""
        self._task = self.entry.async_create_background_task(
            self.hass, self._async_run(), f"{DOMAIN} data hub"
        )

    async def async_stop(self) -> None:
        """Stop the loop and close the stream."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        self._set_connected(False)

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call ``update`` whenever something an entity shows has changed."""
        self._listeners.append(update)

        @callback
        def remove() -> None:
            self._listeners.remove(update)

        return remove

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    def _set_connected(self, connected: bool) -> None:
        if self.connected != connected:
            self.connected = connected
            self._notify()

    # -- the loop ---------------------------------------------------------

    async def _async_run(self) -> None:
        delay = RECONNECT_FLOOR
        while True:
            try:
                if time.monotonic() - self._last_status_read >= STATUS_INTERVAL or (
                    not self._last_status_read
                ):
                    await self._async_read_status()
                if self.stream_enabled is False:
                    # Nothing to connect to until the owner flips the toggle;
                    # asking for a stream token every hour finds out when.
                    await self._pause(STATUS_INTERVAL)
                    self.stream_enabled = None
                    continue
                lasted = await self._async_stream_once()
                delay = (
                    RECONNECT_FLOOR
                    if lasted >= HEALTHY_SESSION
                    else min(delay * 2, RECONNECT_CEILING)
                )
            except DataHubAuthError as err:
                self._signed_out(err)
                return
            except DataHubRateLimited as err:
                _LOGGER.warning(
                    "Data Hub asked us to slow down for %.0fs", err.retry_after
                )
                delay = max(err.retry_after, RECONNECT_FLOOR)
            except (DataHubError, MqttError) as err:
                self.stats["last_error"] = str(err)
                delay = min(delay * 2, RECONNECT_CEILING)
                _LOGGER.warning("Data Hub stream: %s; retrying in %.0fs", err, delay)
            # Jitter, so a fleet of installations that all lost the broker at
            # the same moment do not all come back at the same moment.
            await self._pause(delay * random.uniform(1.0, 1.2))

    async def _pause(self, seconds: float) -> None:
        """Every wait in the loop, in one place a test can stand in for."""
        await asyncio.sleep(seconds)

    def _signed_out(self, err: Exception) -> None:
        _LOGGER.warning(
            "JLR Data Hub no longer accepts this link (%s). Set it up again from "
            "the integration's options.",
            err,
        )
        self.signed_out = True
        self._set_connected(False)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_DATAHUB_SIGNED_OUT}_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_DATAHUB_SIGNED_OUT,
            translation_placeholders={"title": self.entry.title},
        )
        self._notify()

    async def _async_access_token(self) -> str:
        if self._tokens and self._tokens.expires_at - time.time() > 30:
            return self._tokens.access_token
        settings: dict[str, Any] = self.entry.data[CONF_DATAHUB]
        tokens = await self.client.async_refresh(settings[DATAHUB_REFRESH_TOKEN])
        self._tokens = tokens
        if tokens.vehicles:
            self.vehicles = tokens.vehicles
        if tokens.account_id:
            self.account_id = tokens.account_id
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{ISSUE_DATAHUB_SIGNED_OUT}_{self.entry.entry_id}"
        )
        self._persist(tokens)
        return tokens.access_token

    def _persist(self, tokens: Tokens) -> None:
        """Keep the rotated refresh token, and what it says the link covers.

        The old refresh token stops working once spent, so a renewal that is
        not written down is a restart away from asking the owner to approve
        the link again on their phone.
        """
        settings: dict[str, Any] = dict(self.entry.data[CONF_DATAHUB])
        updated = {
            **settings,
            DATAHUB_REFRESH_TOKEN: tokens.refresh_token
            or settings[DATAHUB_REFRESH_TOKEN],
            DATAHUB_VEHICLES: self.vehicles,
            DATAHUB_ACCOUNT_ID: self.account_id,
        }
        if updated != settings:
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, CONF_DATAHUB: updated}
            )

    async def _async_read_status(self) -> None:
        token = await self._async_access_token()
        self._last_status_read = time.monotonic()
        for vin in self.vehicles:
            try:
                body = await self.client.async_status(vin, token)
            except DataHubForbidden as err:
                _LOGGER.debug("Data Hub status for %s: %s", vehicle_label(vin), err)
                continue
            snapshot = _parse_time(body.get("timestamp"))
            signals = self.signals.setdefault(vin, {})
            for name, value in _flatten(body.get("Vehicle"), "Vehicle").items():
                if name == "Vehicle.VehicleIdentification.VIN":
                    continue
                seen = signals.get(name, {}).get("time")
                # A streamed value stamped later than this snapshot is newer.
                if seen and snapshot and seen > snapshot:
                    continue
                signals[name] = {"value": value, "time": snapshot}
            if snapshot:
                self._note_report(vin, snapshot)
            _LOGGER.debug(
                "Data Hub status for %s: reported %s, %d signals",
                vehicle_label(vin),
                snapshot,
                len(signals),
            )
        self._notify()

    def _note_report(self, vin: str, when: datetime) -> bool:
        if vin not in self.reports or when > self.reports[vin]:
            self.reports[vin] = when
            return True
        return False

    async def _async_stream_once(self) -> float:
        """One session. Returns how long it lasted."""
        try:
            stream = await self.client.async_stream_token()
        except DataHubForbidden:
            if self.stream_enabled is not False:
                _LOGGER.info(
                    "Data Hub stream is not switched on for this client; turn it "
                    "on at data.jlr.com to receive updates as they happen"
                )
            self.stream_enabled = False
            self._notify()
            return 0.0
        self.stream_enabled = True
        account = stream.account_id or self.account_id
        if not account:
            raise DataHubError("the stream token names no account")
        context: ssl.SSLContext = client_context()
        hosts = (self.broker,) if self.broker else BROKERS
        # One filter per car, as in JLR's own example, rather than the whole
        # account: the narrowest thing that delivers everything wanted.
        topics = [f"account/{account}/vehicles/{vin}/#" for vin in self.vehicles] or [
            f"account/{account}/#"
        ]
        last_error: MqttError | None = None
        for host in hosts:
            for login in self._logins():
                started = time.monotonic()
                try:
                    reason = await run_session(
                        host=host,
                        port=login.port,
                        ssl_context=(
                            context if login.port == BROKER_PORT else await self._alpn()
                        ),
                        client_id=login.client_id(self.entry.entry_id, started),
                        username=login.username,
                        password=stream.access_token,
                        topics=topics,
                        on_message=self._on_message,
                        on_subscribed=partial(self._on_subscribed, host, login),
                        max_seconds=SESSION_SECONDS,
                    )
                except MqttRejected as err:
                    # The broker is there and said no: the next way of
                    # logging in, not the next host.
                    last_error = err
                    self.stats["last_rejection"] = f"{login.label}: {err}"
                    _LOGGER.debug("Data Hub broker %s, %s: %s", host, login.label, err)
                    continue
                except MqttError as err:
                    last_error = err
                    if self.broker:
                        raise
                    _LOGGER.debug("Data Hub broker %s: %s", host, err)
                    break
                finally:
                    self._set_connected(False)
                lasted = time.monotonic() - started
                self.stats["last_session_seconds"] = round(lasted)
                self.stats["last_session_end"] = reason
                _LOGGER.debug(
                    "Data Hub stream session ended after %.0fs: %s", lasted, reason
                )
                return lasted
            if isinstance(last_error, MqttRejected):
                # Turned down by a broker that answered: the other host would
                # only be more refusals for JLR to count.
                break
        assert last_error is not None
        raise last_error

    def _logins(self) -> list[_Login]:
        """The ways of logging in to try, best guess first.

        The docs give a username and say to keep whatever client id a tool
        generates. The portal shows the username in capitals — but it shows
        that whole panel in capitals, the broker's hostname included — and AWS
        IoT policies commonly tie the client id to the identity. So four
        combinations, then the other port AWS IoT accepts a password on, each
        tried once until one works, after which only that one is used.
        """
        if self._login is not None:
            return [self._login]
        issued = self.client.client_id
        logins = [
            _Login(issued, own_id=False, label="username as issued"),
            _Login(issued, own_id=True, label="username as issued, as client id"),
        ]
        if issued.upper() != issued:
            logins += [
                _Login(issued.upper(), own_id=False, label="username in capitals"),
                _Login(
                    issued.upper(),
                    own_id=True,
                    label="username in capitals, as client id",
                ),
            ]
        logins.append(
            _Login(issued, own_id=False, label="port 443 with ALPN", port=ALPN_PORT)
        )
        return logins

    async def _alpn(self) -> ssl.SSLContext:
        """A context that asks for "mqtt" in the handshake, made once.

        Its own, not the shared client context: setting ALPN on that one
        would change every other TLS connection Home Assistant makes.
        """
        if self._alpn_context is None:

            def build() -> ssl.SSLContext:
                context = ssl.create_default_context()
                context.set_alpn_protocols(["mqtt"])
                return context

            self._alpn_context = await self.hass.async_add_executor_job(build)
        return self._alpn_context

    def _on_subscribed(self, host: str, login: _Login) -> None:
        if self.broker != host or self._login != login:
            _LOGGER.info("Data Hub stream connected (%s, %s)", host, login.label)
        self.broker = host
        self._login = login
        self.stats["sessions"] += 1
        self._set_connected(True)

    def _on_message(self, topic: str, payload: bytes) -> None:
        self.stats["messages"] += 1
        # account/{account}/vehicles/{vin}/{category}
        parts = topic.split("/")
        vin = parts[3] if len(parts) > 3 and parts[2] == "vehicles" else None
        try:
            decoded = json.loads(payload)
        except ValueError:
            _LOGGER.debug("Data Hub message on %s was not JSON", parts[-1])
            return
        items = decoded if isinstance(decoded, list) else [decoded]
        changed = False
        for item in items:
            if not isinstance(item, dict) or vin is None:
                continue
            name = item.get("name")
            when = _parse_time(item.get("timestamp"))
            if not isinstance(name, str):
                continue
            self.signals.setdefault(vin, {})[name] = {
                "value": item.get("value"),
                "time": when,
                "unit": item.get("unit"),
            }
            if when and self._note_report(vin, when):
                changed = True
            _LOGGER.debug(
                "Data Hub %s %s at %s", vehicle_label(vin), name, item.get("timestamp")
            )
        if changed:
            self._notify()

    # -- reporting --------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """What the link has seen, without values or identifiers."""
        return {
            "connected": self.connected,
            "signed_out": self.signed_out,
            "stream_enabled": self.stream_enabled,
            "broker": self.broker,
            "login": self._login.label if self._login else None,
            "stats": dict(self.stats),
            "vehicles": {
                vehicle_label(vin): {
                    "last_report": self.reports.get(vin),
                    "signals": {
                        name: entry.get("time")
                        for name, entry in sorted(self.signals.get(vin, {}).items())
                    },
                }
                for vin in self.vehicles
            },
        }

"""The running link to the JLR Data Hub, for an entry that has one set up.

Optional. When it is set up and covers every car on the account, it is where
vehicle status comes from, in place of the telemetry socket: the coordinator
hands it a callback and it delivers socket-shaped status (see vss.py), so the
entities neither know nor care which one is talking. Location still comes from
the owner portal; the link asks JLR for it hourly in case that changes.

Two sources, both JLR's documented API:

- REST status, read when the link starts and then on a timer: every fifteen
  minutes until the stream has shown it delivers, hourly after that.
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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from homeassistant.util.ssl import client_context

from .const import (
    CONF_DATAHUB,
    CONF_REFRESH_TOKEN,
    CONF_SSO_COOKIES,
    DATAHUB_ACCOUNT_ID,
    DATAHUB_CERTIFICATE,
    DATAHUB_CLIENT_ID,
    DATAHUB_PRIVATE_KEY,
    DATAHUB_REFRESH_TOKEN,
    DATAHUB_RENEW_WITHIN,
    DATAHUB_VEHICLES,
    DOMAIN,
    ISSUE_DATAHUB_CERTIFICATE,
    ISSUE_DATAHUB_LOCATION_REFUSED,
    ISSUE_DATAHUB_SIGNED_OUT,
    ISSUE_DATAHUB_STATUS_REFUSED,
    ISSUE_DATAHUB_STREAM_OFF,
)
from .datahub import (
    DataHubAuthError,
    DataHubClient,
    DataHubError,
    DataHubForbidden,
    DataHubRateLimited,
    Tokens,
    certificate_expiry,
)
from .redact import vehicle_label
from .stream import (
    SUBSCRIPTION_STAGE,
    MqttDropped,
    MqttError,
    MqttRejected,
    run_session,
)
from .vss import (
    LATITUDE,
    LONGITUDE,
    POSITION_SIGNALS,
    signals_to_position,
    to_attributes,
    to_position,
    to_status,
)

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
# Until the stream has delivered a message, REST is the only source of change,
# and it replaces a socket that pushed within minutes. Fifteen minutes is the
# housekeeping cadence this integration already keeps, for two requests.
STATUS_INTERVAL_UNPROVEN = 15 * 60.0
# Location is refused per car until the owner declares how the car is used at
# data.jlr.com (sole user, shared user, other drivers' consent). A refused car
# is asked hourly, so a declaration made later is noticed, and no more often;
# a permitted one is read with its status, as the portal's position was.
LOCATION_INTERVAL = 60 * 60.0
# Battery and charging come from their own endpoint, which JLR refuse for a car
# without a charge port. Read with each status read for a car that answers; a
# car that is refused is asked again daily rather than every read, since what
# it is refused for is its hardware, not anything an owner can change.
EV_REFUSED_RETRY = 24 * 60 * 60.0
# How long without a good status read before the link stops vouching for the
# readings. Three missed hourly reads: one failure is not an outage.
HEALTHY_FOR = 3 * 60 * 60.0
# After every way of logging in has been turned down. The broker answered and
# said no five times; asking again in minutes would only add to the refusals.
# Measured in November 2026: the stream refused every login for owner
# accounts, and the broker the portal names was not yet in public DNS.
REJECTED_RETRY = 6 * 60 * 60.0


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

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        on_status: Callable[[str, dict[str, str]], None] | None = None,
        on_position: Callable[[str, dict[str, Any]], None] | None = None,
        on_signed_out: Callable[[], None] | None = None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._on_status = on_status
        self._on_position = on_position
        self._on_signed_out = on_signed_out
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
        self._status_task: asyncio.Task[None] | None = None
        self._listeners: list[CALLBACK_TYPE] = []

        self.connected = False
        self.signed_out = False
        # None until the stream has been tried; False when the portal has not
        # had it switched on, which is a setting the owner can change.
        self.stream_enabled: bool | None = None
        self.broker: str | None = None
        self._login: _Login | None = None
        # A login the broker accepted, even if the subscription then was not.
        self._accepted: _Login | None = None
        # The topic layout that the broker let us subscribe with.
        self._shape: str | None = None
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
        # When a status read last succeeded, on the monotonic clock.
        self._status_ok_at: float | None = None
        # What JLR said the last time each car's location was asked for.
        self.location: dict[str, str] = {}
        self._location_due: dict[str, float] = {}
        self.ev: dict[str, str] = {}
        self._status_refused: set[str] = set()
        self._ev_due: dict[str, float] = {}
        # Cars with a full REST snapshot behind their signals. Until a car has
        # one, what the stream has sent is a few signals, not its status.
        self._read_in_full: set[str] = set()
        # When the stream may next be tried, after a round of rejections.
        self._stream_after = 0.0

    # -- lifecycle --------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Start the status timer and the stream. Returns at once.

        Safe to call twice: the second call finds both running and leaves
        them alone, so setup can start it early when it is the status source
        and again unconditionally once the platforms are up.
        """
        if self._task is not None:
            return
        self._status_task = self.entry.async_create_background_task(
            self.hass, self._async_status_loop(), f"{DOMAIN} data hub status"
        )
        self._task = self.entry.async_create_background_task(
            self.hass, self._async_run(), f"{DOMAIN} data hub"
        )

    async def async_stop(self) -> None:
        """Stop both loops and close the stream."""
        for task in (self._task, self._status_task):
            if task is not None:
                task.cancel()
                # Whatever the task died of, an unload must still finish.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._task = self._status_task = None
        self._set_connected(False)

    @property
    def healthy(self) -> bool:
        """Whether the readings this link delivered can still be vouched for."""
        if self.signed_out:
            return False
        if self.connected:
            return True
        return (
            self._status_ok_at is not None
            and time.monotonic() - self._status_ok_at < HEALTHY_FOR
        )

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

    async def _async_status_loop(self) -> None:
        """REST status on a timer, and location with it."""
        while not self.signed_out:
            wait = (
                STATUS_INTERVAL if self.stats["messages"] else STATUS_INTERVAL_UNPROVEN
            )
            try:
                await self._async_read_status()
                await self._async_probe_location()
            except DataHubAuthError as err:
                self._signed_out(err)
                return
            except DataHubRateLimited as err:
                _LOGGER.warning(
                    "Data Hub asked us to slow down for %.0fs", err.retry_after
                )
                wait = max(err.retry_after, wait)
            except DataHubError as err:
                _LOGGER.warning("Data Hub status read failed: %s", err)
            except Exception:  # noqa: BLE001 - a background loop must not die
                _LOGGER.exception("unexpected failure reading Data Hub status")
            await self._pause(wait)

    async def _async_run(self) -> None:
        """The stream: connect, hold, reconnect politely."""
        delay = RECONNECT_FLOOR
        while not self.signed_out:
            try:
                if (
                    self.stream_enabled is False
                    or time.monotonic() < self._stream_after
                ):
                    # Nothing to connect to until the owner flips the toggle,
                    # or the broker turned every login down. Status carries
                    # on on its own timer meanwhile.
                    await self._pause(STATUS_INTERVAL)
                    if self.stream_enabled is False:
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
            except MqttRejected as err:
                self.stats["last_error"] = str(err)
                self._stream_after = time.monotonic() + REJECTED_RETRY
                _LOGGER.warning(
                    "Data Hub stream turned down every way of logging in (%s); "
                    "trying again in %.0f hours. Status still updates on a timer",
                    err,
                    REJECTED_RETRY / 3600,
                )
                continue
            except (DataHubError, MqttError) as err:
                self.stats["last_error"] = str(err)
                delay = min(delay * 2, RECONNECT_CEILING)
                _LOGGER.warning("Data Hub stream: %s; retrying in %.0fs", err, delay)
            except Exception:  # noqa: BLE001 - a background loop must not die
                delay = min(delay * 2, RECONNECT_CEILING)
                _LOGGER.exception("unexpected failure in the Data Hub stream")
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
        if self._on_signed_out is not None:
            self._on_signed_out()
        # The link itself has gone, which is the repair that matters.
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{ISSUE_DATAHUB_STREAM_OFF}_{self.entry.entry_id}"
        )
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

    async def async_read_now(self) -> bool:
        """One status read on demand, for the Refresh button. True if it worked."""
        try:
            await self._async_read_status()
        except DataHubAuthError as err:
            self._signed_out(err)
            return False
        except DataHubError as err:
            _LOGGER.debug("Data Hub status read on demand failed: %s", err)
            return False
        return True

    async def async_vehicle_details(self, vin: str) -> dict[str, Any]:
        """One car's details, as attributes. Raises DataHubError."""
        token = await self._async_access_token()
        return to_attributes(await self.client.async_vehicle(vin, token))

    async def _async_probe_location(self) -> None:
        """Ask for each car's location, and hand on any that is given."""
        now = time.monotonic()
        due = [v for v in self.vehicles if now >= self._location_due.get(v, 0.0)]
        if not due:
            return
        token = await self._async_access_token()
        for vin in due:
            try:
                body = await self.client.async_location(vin, token)
            except DataHubForbidden as err:
                answer = f"refused: {err}"
            else:
                position = to_position(body)
                answer = "permitted" if position else "permitted, no fix"
                if position and self._on_position is not None:
                    self._on_position(vin, position)
            if answer != self.location.get(vin):
                _LOGGER.log(
                    logging.INFO if answer.startswith("permitted") else logging.DEBUG,
                    "Data Hub location for %s: %s",
                    vehicle_label(vin),
                    answer,
                )
            self.location[vin] = answer
            self._location_due[vin] = (
                0.0 if answer.startswith("permitted") else now + LOCATION_INTERVAL
            )
        refused = {v for v, a in self.location.items() if a.startswith("refused")}
        # Not for an owner who has the portal's location instead: they chose
        # that, and the tracker is not missing anything.
        portal = self.entry.data.get(CONF_SSO_COOKIES) or self.entry.data.get(
            CONF_REFRESH_TOKEN
        )
        self._set_issue(
            ISSUE_DATAHUB_LOCATION_REFUSED,
            bool(refused) and not portal,
            cars=self._car_names(refused),
        )

    def _emit(self, vin: str) -> None:
        """Hand the car's current signals on, in the socket's shape.

        Not before the car's first REST snapshot. The stream and that read
        start together, and a streamed signal that won the race was handed on
        as the car's whole status: setup went ahead on one key and the prune
        deleted every entity it did not carry. What the stream sends first is
        kept, and goes out with the snapshot.
        """
        if self._on_status is None or vin not in self._read_in_full:
            return
        reported = self.reports.get(vin)
        values = {name: entry["value"] for name, entry in self.signals[vin].items()}
        self._on_status(
            vin, to_status(values, reported.isoformat() if reported else None)
        )

    async def _async_read_status(self) -> None:
        token = await self._async_access_token()
        answered = False
        for vin in self.vehicles:
            try:
                body = await self.client.async_status(vin, token)
            except DataHubForbidden as err:
                _LOGGER.debug("Data Hub status for %s: %s", vehicle_label(vin), err)
                self._status_refused.add(vin)
                continue
            self._status_refused.discard(vin)
            answered = True
            self._merge(vin, body)
            await self._async_read_ev(vin, token)
            self._read_in_full.add(vin)
            _LOGGER.debug(
                "Data Hub status for %s: reported %s, %d signals",
                vehicle_label(vin),
                self.reports.get(vin),
                len(self.signals.get(vin, {})),
            )
            self._emit(vin)
        if answered:
            self._status_ok_at = time.monotonic()
        self._set_issue(
            ISSUE_DATAHUB_STATUS_REFUSED,
            bool(self._status_refused),
            cars=self._car_names(self._status_refused),
        )
        self._check_certificate()
        self._notify()

    def _set_issue(self, issue: str, active: bool, **placeholders: str) -> None:
        """Raise or clear one of this link's repairs."""
        issue_id = f"{issue}_{self.entry.entry_id}"
        if not active:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=issue,
            translation_placeholders={"title": self.entry.title, **placeholders},
        )

    def _car_names(self, vins: set[str]) -> str:
        """The cars as the owner knows them, for a repair to name."""
        registry = dr.async_get(self.hass)
        names = []
        for vin in vins:
            device = registry.async_get_device(identifiers={(DOMAIN, vin)})
            name = device and (device.name_by_user or device.name)
            names.append(name or vehicle_label(vin))
        return ", ".join(sorted(names))

    def _check_certificate(self) -> None:
        """Say when the client's certificate is close to running out.

        A year after linking JLR stop accepting it, and the link goes with
        it. Nothing on JLR's side warns, so this does, a month ahead.
        """
        certificate = self.entry.data[CONF_DATAHUB].get(DATAHUB_CERTIFICATE)
        try:
            expires = certificate_expiry(certificate)
        except (TypeError, ValueError):
            return
        self._set_issue(
            ISSUE_DATAHUB_CERTIFICATE,
            expires - dt_util.utcnow() < DATAHUB_RENEW_WITHIN,
            date=dt_util.as_local(expires).date().isoformat(),
        )

    def _merge(self, vin: str, body: dict[str, Any]) -> None:
        """A REST answer's signals into what is held, newer values kept."""
        snapshot = _parse_time(body.get("timestamp"))
        signals = self.signals.setdefault(vin, {})
        for name, value in _flatten(body.get("Vehicle"), "Vehicle").items():
            if name == "Vehicle.VehicleIdentification.VIN":
                continue
            seen = signals.get(name, {}).get("time")
            # A value stamped later than this answer is newer.
            if seen and snapshot and seen > snapshot:
                continue
            signals[name] = {"value": value, "time": snapshot}
        if snapshot:
            self._note_report(vin, snapshot)

    async def _async_read_ev(self, vin: str, token: str) -> None:
        """Battery and charging, from the endpoint that carries them.

        The status snapshot has none of it: JLR's catalog marks every traction
        battery signal stream-only, so a plug-in car read from REST alone had
        no charge, range or charging state until the stream delivered, and its
        battery entities were pruned as no longer reported. An owner on a
        Range Rover Sport plug-in hybrid reported exactly that.
        """
        now = time.monotonic()
        if now < self._ev_due.get(vin, 0.0):
            return
        try:
            body = await self.client.async_ev(vin, token)
        except (DataHubAuthError, DataHubRateLimited):
            raise
        except DataHubError as err:
            answer = f"refused: {err}"
            self._ev_due[vin] = now + EV_REFUSED_RETRY
        else:
            self._merge(vin, body)
            answer = "permitted"
            self._ev_due[vin] = 0.0
        if answer != self.ev.get(vin):
            _LOGGER.debug("Data Hub EV data for %s: %s", vehicle_label(vin), answer)
        self.ev[vin] = answer

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
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{ISSUE_DATAHUB_STREAM_OFF}_{self.entry.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_DATAHUB_STREAM_OFF,
                translation_placeholders={"title": self.entry.title},
            )
            self._notify()
            return 0.0
        self.stream_enabled = True
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{ISSUE_DATAHUB_STREAM_OFF}_{self.entry.entry_id}"
        )
        account = stream.account_id or self.account_id
        if not account:
            raise DataHubError("the stream token names no account")
        context: ssl.SSLContext = client_context()
        hosts = (self.broker,) if self.broker else BROKERS
        last_error: MqttError | None = None
        for host in hosts:
            # How this host's attempts ended: "refused" (it answered and
            # said no, so the other host would only refuse too) or
            # "unreachable" (try the other host).
            verdict = "refused"
            for login in self._logins():
                login_refused = False
                for shape, topics in self._topic_sets(account):
                    started = time.monotonic()
                    try:
                        reason = await run_session(
                            host=host,
                            port=login.port,
                            ssl_context=(
                                context
                                if login.port == BROKER_PORT
                                else await self._alpn()
                            ),
                            client_id=login.client_id(self.entry.entry_id, started),
                            username=login.username,
                            password=stream.access_token,
                            topics=topics,
                            on_message=self._on_message,
                            on_subscribed=partial(
                                self._on_subscribed, host, login, shape
                            ),
                            max_seconds=SESSION_SECONDS,
                        )
                    except MqttDropped as err:
                        last_error = err
                        self.stats["last_rejection"] = f"{login.label}, {shape}: {err}"
                        _LOGGER.debug(
                            "Data Hub broker %s, %s, %s: %s",
                            host,
                            login.label,
                            shape,
                            err,
                        )
                        if err.stage == SUBSCRIPTION_STAGE:
                            # The login was accepted; only the topics were
                            # not. Keep this login, try the next layout.
                            self._accepted = login
                            continue
                        login_refused = True
                        break
                    except MqttRejected as err:
                        last_error = err
                        self.stats["last_rejection"] = f"{login.label}: {err}"
                        _LOGGER.debug(
                            "Data Hub broker %s, %s: %s", host, login.label, err
                        )
                        login_refused = True
                        break
                    except MqttError as err:
                        last_error = err
                        if self.broker:
                            raise
                        _LOGGER.debug("Data Hub broker %s: %s", host, err)
                        verdict = "unreachable"
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
                if not login_refused:
                    # Either every layout failed with a login the broker
                    # accepted, so another login could only do worse, or the
                    # host was unreachable.
                    break
            if verdict == "refused":
                # Turned down by a broker that answered: the other host would
                # only be more refusals for JLR to count.
                break
        assert last_error is not None
        raise last_error

    def _topic_sets(self, account: str) -> list[tuple[str, list[str]]]:
        """The topic layouts to subscribe with, best guess first.

        Measured in November 2026: the broker accepted a login and then hung
        up on the docs' own example filter, one ``…/vehicles/{vin}/#`` per car.
        The docs show the topic three ways in three places, and AWS IoT drops
        the whole connection when any filter in a subscription is not allowed,
        so each layout is its own connection, tried once, and the one that
        works is kept.
        """
        if self._shape is not None:
            return [s for s in self._all_topic_sets(account) if s[0] == self._shape]
        return self._all_topic_sets(account)

    def _all_topic_sets(self, account: str) -> list[tuple[str, list[str]]]:
        vins = self.vehicles
        if not vins:
            return [("whole account", [f"account/{account}/#"])]
        # The two that name exact categories go first: wildcards were measured
        # to cost a dropped connection, and every restart would pay for them
        # again. Position comes before status alone because AWS IoT drops the
        # connection over any filter it does not allow, and a car not declared
        # for location may be one, so status alone is the fallback.
        return [
            (
                "each car, status and position",
                [
                    f"account/{account}/vehicles/{v}/{category}"
                    for v in vins
                    for category in ("status", "position")
                ],
            ),
            (
                "each car, status",
                [f"account/{account}/vehicles/{v}/status" for v in vins],
            ),
            (
                "each car, all categories",
                [f"account/{account}/vehicles/{v}/#" for v in vins],
            ),
            (
                "each car, no category",
                [f"account/{account}/vehicles/{v}" for v in vins],
            ),
            ("whole account", [f"account/{account}/#"]),
            (
                "account id in capitals",
                [f"account/{account.upper()}/vehicles/{v}/#" for v in vins],
            ),
            ("first car only", [f"account/{account}/vehicles/{vins[0]}/#"]),
        ]

    def _logins(self) -> list[_Login]:
        """The ways of logging in to try, best guess first.

        The docs give a username and say to keep whatever client id a tool
        generates. The portal shows the username in capitals — but it shows
        that whole panel in capitals, the broker's hostname included — and AWS
        IoT policies commonly tie the client id to the identity. So four
        combinations, then the other port AWS IoT accepts a password on, each
        tried once until one works, after which only that one is used. The
        client id as both username and MQTT client id is the one that worked
        in November 2026, and leads.
        """
        if self._login is not None:
            return [self._login]
        if self._accepted is not None:
            return [self._accepted]
        issued = self.client.client_id
        # The one JLR's broker was measured to accept goes first. Second, it
        # cost a refused connection on every restart, since what worked is
        # only remembered for as long as Home Assistant is running.
        logins = [
            _Login(issued, own_id=True, label="username as issued, as client id"),
            _Login(issued, own_id=False, label="username as issued"),
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

    def _on_subscribed(self, host: str, login: _Login, shape: str) -> None:
        if self.broker != host or self._login != login or self._shape != shape:
            _LOGGER.info(
                "Data Hub stream connected (%s, %s, %s)", host, login.label, shape
            )
        self.broker = host
        self._login = login
        self._shape = shape
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
        moved = False
        for item in items:
            if not isinstance(item, dict) or vin is None:
                continue
            name = item.get("name")
            when = _parse_time(item.get("timestamp"))
            if not isinstance(name, str):
                continue
            held = self.signals.get(vin, {}).get(name, {}).get("time")
            if held and (when is None or when < held):
                # Delivered late: JLR send out of order. An "unlocked" from
                # an hour ago replaced a newer "locked" while the newer time
                # stayed on the car, which is a wrong reading made to look
                # current. A value the same age is taken; it is a redelivery.
                continue
            self.signals.setdefault(vin, {})[name] = {
                "value": item.get("value"),
                "time": when,
                "unit": item.get("unit"),
            }
            if when and self._note_report(vin, when):
                changed = True
            moved = moved or name in POSITION_SIGNALS
            _LOGGER.debug(
                "Data Hub %s %s at %s", vehicle_label(vin), name, item.get("timestamp")
            )
        if changed:
            self._notify()
        if vin is not None and vin in self.signals:
            self._emit(vin)
            if moved:
                self._emit_position(vin)

    def _emit_position(self, vin: str) -> None:
        """Hand on a fix the stream delivered, once both halves agree.

        Latitude and longitude are separate signals, and may come in separate
        messages; a new latitude paired with the last fix's longitude would be
        a place the car never was. They are one reading, so they carry one
        time, and a fix is handed on only when the two times match.
        """
        if self._on_position is None:
            return
        held = self.signals[vin]
        latitude, longitude = held.get(LATITUDE), held.get(LONGITUDE)
        if latitude is None or longitude is None:
            return
        if latitude["time"] != longitude["time"]:
            return
        when = latitude["time"]
        values = {name: entry["value"] for name, entry in held.items()}
        position = signals_to_position(values, when.isoformat() if when else None)
        if position is not None:
            self._on_position(vin, position)

    # -- reporting --------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """What the link has seen, without values or identifiers."""
        return {
            "connected": self.connected,
            "signed_out": self.signed_out,
            "stream_enabled": self.stream_enabled,
            "healthy": self.healthy,
            "location": {vehicle_label(v): a for v, a in self.location.items()},
            "ev": {vehicle_label(v): a for v, a in self.ev.items()},
            "broker": self.broker,
            "login": self._login.label if self._login else None,
            "accepted_login": self._accepted.label if self._accepted else None,
            "topics": self._shape,
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

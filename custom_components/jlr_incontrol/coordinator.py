"""Data coordinator for Jaguar Land Rover InControl.

Everything comes from the JLR Data Hub (datahub_link.py): the cars on the
account, their details, status, location and journeys. Status and location
arrive when the link reads or the stream delivers them; what is left on a
timer here is housekeeping — notice a car being added or removed, and refresh
the vehicle details once a day.

An entry without a link is one set up on the InControl sign-in, which 3.0.0
removed. It loads with nothing to read, keeps its devices and entity
registrations, and asks through a repair for the Data Hub to be linked.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    ATTRIBUTES_RETRY,
    ATTRIBUTES_TTL,
    CONF_ATTRIBUTES,
    CONF_DATAHUB,
    CONF_DECAYED_SEEN,
    CONF_DECAYED_SINCE,
    CONF_LAST_CHANGED,
    CONF_UNSETTLED_SINCE,
    CONF_VOLATILE_SEEN,
    DECAYING_STATUS_KEYS,
    DOMAIN,
    ISSUE_INCONTROL_RETIRED,
    OPT_UNSETTLED_MINUTES,
    POSITION_TRUST_WINDOW,
    RESUBSCRIBE_FLOOR,
    SCAN_INTERVAL_HOUSEKEEPING,
    STALE_AFTER,
    UNSETTLED_MINUTES_DEFAULT,
    UNSETTLED_VEHICLE_STATES,
    VIN_BRANDS,
    VOLATILE_STATUS_KEYS,
    VOLATILE_STATUS_KEYS_1_7,
)
from .datahub import DataHubError
from .datahub_link import DataHubLink
from .redact import vehicle_label

_LOGGER = logging.getLogger(__name__)

# How long to wait at setup for every car's first status read. Generous: it
# covers the token renewal, the status and battery reads, and JLR being slow.
FIRST_SNAPSHOT_TIMEOUT = 60


def brand_from_vin(vin: str) -> dict[str, str]:
    """The one thing a VIN tells us for certain: which marque built it.

    A floor under the Data Hub's details, which can fail on the first read.
    The model is deliberately not guessed from the remaining characters: a
    confidently wrong model is worse than none.
    """
    brand = VIN_BRANDS.get(vin[:3].upper())
    return {"vehicleBrand": brand} if brand else {}


def _restore_seen(stored: Any, *orders: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """Read saved readings back as key-to-value maps, whichever shape they are.

    A dict is the current shape. A list is what earlier releases wrote: values
    in the order of whichever key list was current then, and the length is the
    only way to tell which. Anything else is a shape nothing here wrote, and is
    dropped rather than guessed at; a car with nothing to compare against
    keeps its clock running and starts comparing from the next snapshot.
    """
    restored: dict[str, dict[str, Any]] = {}
    for vin, seen in (stored or {}).items():
        if isinstance(seen, dict):
            restored[vin] = dict(seen)
            continue
        if not isinstance(seen, list):
            continue
        for keys in orders:
            if len(seen) == len(keys):
                restored[vin] = dict(zip(keys, seen, strict=True))
                break
    return restored


class Refresh(StrEnum):
    """What a Refresh press did, for a button that has to say so."""

    DONE = "done"
    TOO_SOON = "too_soon"
    FAILED = "failed"


class JlrCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Owns the Data Hub link and the merged vehicle state."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL_HOUSEKEEPING,
        )
        self.entry = entry
        # None for an entry left over from the InControl sign-in, which waits
        # for the owner to link the Data Hub.
        self.datahub: DataHubLink | None = (
            DataHubLink(
                hass,
                entry,
                on_status=self._handle_status,
                on_position=self._handle_position,
                on_signed_out=self._handle_signed_out,
            )
            if entry.data.get(CONF_DATAHUB)
            else None
        )
        # Change detection for the last-updated signal, kept alongside the
        # Data Hub's own report time: observing when the readings actually
        # change is the freshness signal that survives a report time that
        # moves while nothing else does.
        self._last_status_seen: dict[str, dict[str, Any]] = {}
        # Vehicle details persisted in the config entry: model, year, fuel
        # type, and the nickname and registration an earlier version read from
        # the owner portal, which the Data Hub does not carry.
        stored = entry.data.get(CONF_ATTRIBUTES) or {}
        self._attributes: dict[str, dict[str, Any]] = {
            vin: dict(attrs) for vin, attrs in stored.items()
        }
        self._attributes_attempted: dict[str, Any] = {}
        self._status: dict[str, dict[str, Any]] = {}
        # When the car sent each key of the held status, taken with it.
        self._status_times: dict[str, dict[str, datetime]] = {}
        # Restored, not started empty: rebuilding it from nothing on every
        # reload made the "last updated" sensor jump backwards.
        self._last_changed: dict[str, str] = dict(
            entry.data.get(CONF_LAST_CHANGED) or {}
        )
        # When each car was first seen in the state it is still sitting in,
        # and the volatile readings as they were then. Deliberately not
        # _last_changed: that moves when anything in the document moves, and a
        # parked car's battery voltage drifts on its own.
        self._unsettled_since: dict[str, str] = dict(
            entry.data.get(CONF_UNSETTLED_SINCE) or {}
        )
        # Keyed by status key, so a list that gains a key compares cleanly
        # against one saved before it did. 1.7.x saved bare lists of fourteen,
        # and 1.8.0-beta.1 lists of the current keys; both are read back
        # through the key order that wrote them.
        self._volatile_seen = _restore_seen(
            entry.data.get(CONF_VOLATILE_SEEN),
            VOLATILE_STATUS_KEYS_1_7,
            VOLATILE_STATUS_KEYS,
        )
        # The same pair again, for readings that go stale on their own clock
        # rather than because of what the car is doing.
        self._decayed_since: dict[str, str] = dict(
            entry.data.get(CONF_DECAYED_SINCE) or {}
        )
        self._decayed_seen = _restore_seen(
            entry.data.get(CONF_DECAYED_SEEN), DECAYING_STATUS_KEYS
        )
        self._position: dict[str, dict[str, Any]] = {}
        # When each car was last actually located, for position_trusted.
        self._position_read_at: dict[str, Any] = {}
        self._vehicles: dict[str, dict[str, Any]] = {}
        # Cars not heard from yet. Platform setup reads the status to decide
        # which entities a car gets, so it must not run while one is silent.
        self._awaiting: set[str] = set()
        self._snapshots_ready = asyncio.Event()
        # None for "never". The floor between Refresh presses.
        self._refreshed_at: Any = None

    # ------------------------------------------------------------- lifecycle
    @property
    def dormant(self) -> bool:
        """An entry from the InControl sign-in, waiting for the Data Hub."""
        return self.datahub is None

    async def async_start(self) -> None:
        """Start the link and wait for every car's first status.

        Every car, not just the first: the sensor platform only creates an
        entity when the status key backing it is present, so a car whose
        status lands after setup would get no fuel, odometer or tyre entities
        until the platforms noticed it.

        Setup fails outright if nothing arrives, rather than succeeding into a
        house of unavailable entities.
        """
        if self.datahub is None:
            self._raise_retired_issue()
            return
        ir.async_delete_issue(self.hass, DOMAIN, self.retired_issue_id)
        self._awaiting = set(self._vehicles)
        self._snapshots_ready.clear()
        if not self._vehicles or self._link_refused():
            # A link JLR have stopped accepting: its reauth has said why.
            return
        self.datahub.async_start()
        try:
            async with asyncio.timeout(FIRST_SNAPSHOT_TIMEOUT):
                await self._snapshots_ready.wait()
            if self._link_refused():
                # Not ready-and-retry: each retry would spend another request
                # on a refresh token JLR have already refused, for as long as
                # nobody relinked. Auth failed stops the retries, and its
                # reauth is the way back.
                await self.datahub.async_stop()
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN, translation_key="datahub_signed_out"
                )
        except TimeoutError as err:
            if not self._status:
                waiting = dict(self.datahub.ev_waiting)
                await self.datahub.async_stop()
                if waiting:
                    # Said here, not by the link: each retry starts a new link,
                    # so a link's own count of failures never reaches its
                    # warning on an account whose only car is held back.
                    raise ConfigEntryNotReady(
                        translation_domain=DOMAIN,
                        translation_key="datahub_ev_waiting",
                        translation_placeholders={
                            "cars": ", ".join(
                                sorted(vehicle_label(v) for v in waiting)
                            ),
                            "error": "; ".join(sorted(set(waiting.values()))),
                        },
                    ) from err
                raise ConfigEntryNotReady(
                    translation_domain=DOMAIN,
                    translation_key="no_status",
                    translation_placeholders={"seconds": str(FIRST_SNAPSHOT_TIMEOUT)},
                ) from err
            _LOGGER.warning(
                "no Data Hub status from %s within %ss; their entities will be "
                "created once it arrives",
                ", ".join(sorted(vehicle_label(v) for v in self._awaiting)),
                FIRST_SNAPSHOT_TIMEOUT,
            )

    def _link_refused(self) -> bool:
        """Asked fresh each time: the link signs out while setup awaits it."""
        return self.datahub is not None and self.datahub.signed_out

    def _handle_signed_out(self) -> None:
        """Wake a setup still waiting for status that will now never come."""
        self._snapshots_ready.set()

    @property
    def retired_issue_id(self) -> str:
        return f"{ISSUE_INCONTROL_RETIRED}_{self.entry.entry_id}"

    def _raise_retired_issue(self) -> None:
        """Ask for the Data Hub to be linked, which only the owner can do.

        Not a reauth: there is nothing left to sign in to. Linking needs a
        client the owner creates at data.jlr.com and an approval on their
        phone, and Configure is where it is done.
        """
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self.retired_issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_INCONTROL_RETIRED,
            translation_placeholders={"title": self.entry.title},
        )
        _LOGGER.warning(
            "%s was set up on the InControl sign-in, which this version no "
            "longer has. Link the JLR Data Hub from the integration's Configure "
            "menu; every entity and its history are kept",
            self.entry.title,
        )

    def async_stop_on_hass_shutdown(self) -> None:
        """Save and close the stream when Home Assistant stops.

        Home Assistant does not unload config entries when it stops, so
        unload alone never ran: anything held only in memory was lost, and the
        broker was left holding a session until it timed out on its own.
        """
        self.entry.async_on_unload(
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, self._async_hass_stopping
            )
        )

    async def _async_hass_stopping(self, _event: Event) -> None:
        await self.async_shutdown()

    async def async_shutdown(self) -> None:
        """Save, stop the link, then the coordinator."""
        self._persist()
        if self.datahub is not None:
            await self.datahub.async_stop()
        await super().async_shutdown()

    async def async_refresh_vehicle(self, vin: str) -> Refresh:
        """Read status and location now, for the Refresh button.

        Three outcomes rather than two, because a press that quietly did
        nothing is the failure this button lived down. DONE means this car's
        status was actually read, not just that JLR answered.
        """
        now = dt_util.utcnow()
        if self._refreshed_at is not None and now - self._refreshed_at < (
            RESUBSCRIBE_FLOOR
        ):
            _LOGGER.debug("refreshed %s ago; too soon", now - self._refreshed_at)
            return Refresh.TOO_SOON
        self._refreshed_at = now
        if self.datahub is None or self.datahub.signed_out:
            return Refresh.FAILED
        ok = await self.datahub.async_read_now(vin)
        return Refresh.DONE if ok else Refresh.FAILED

    # ----------------------------------------------------------- housekeeping
    async def _async_update_data(self) -> dict[str, Any]:
        if self.datahub is None or self.datahub.signed_out:
            # Nothing to ask: the repair says why. Keep what is held.
            return self._build()
        # The cars the owner selected on the client at data.jlr.com.
        found: dict[str, dict[str, Any]] = {
            vin: {"vin": vin} for vin in self.datahub.vehicles
        }
        # Everything we hold something about, not just what the last listing
        # said: the caches outlive a restart and the listing does not, so a
        # car removed while Home Assistant was stopped is still found missing.
        removed = self._known_vins() - set(found)
        self._vehicles = found
        if removed:
            self._forget(removed)
        for vin in self._vehicles:
            seed = brand_from_vin(vin)
            current = self._attributes.get(vin, {})
            if seed and not current.get("vehicleBrand"):
                self._attributes[vin] = {**current, **seed}
            await self._async_refresh_details(vin)
        self._persist()
        return self._build()

    def _known_vins(self) -> set[str]:
        """Every VIN this integration is holding anything about."""
        return (
            set(self._vehicles)
            | set(self._attributes)
            | set(self._last_changed)
            | set(self._unsettled_since)
            | set(self._volatile_seen)
            | set(self._decayed_since)
            | set(self._decayed_seen)
        )

    def _forget(self, removed: set[str]) -> None:
        """Drop everything held about cars the account no longer has.

        Every per-VIN cache, because the ones that were missed before are
        exactly the ones nobody thinks about: they kept a sold car alive in
        the entry and in diagnostics.
        """
        for vin in removed:
            self._attributes.pop(vin, None)
            self._attributes_attempted.pop(vin, None)
            self._status.pop(vin, None)
            self._status_times.pop(vin, None)
            self._position.pop(vin, None)
            self._last_status_seen.pop(vin, None)
            self._unsettled_since.pop(vin, None)
            self._volatile_seen.pop(vin, None)
            self._decayed_since.pop(vin, None)
            self._decayed_seen.pop(vin, None)
            self._position_read_at.pop(vin, None)
            self._last_changed.pop(vin, None)
            self._awaiting.discard(vin)
        # A car we were still waiting on has gone; nothing will ever arrive
        # for it, and setup would otherwise sit out the full timeout.
        if not self._awaiting:
            self._snapshots_ready.set()

    async def _async_refresh_details(self, vin: str) -> None:
        """Model, year and fuel type from the Data Hub, daily.

        Merged over what is held, so a nickname read by an earlier version
        survives.
        """
        assert self.datahub is not None
        now = dt_util.utcnow()
        attempted = self._attributes_attempted.get(vin)
        wait = ATTRIBUTES_TTL if vin in self._attributes else ATTRIBUTES_RETRY
        if attempted and now - attempted < wait:
            return
        self._attributes_attempted[vin] = now
        try:
            details = await self.datahub.async_vehicle_details(vin)
        except DataHubError as err:
            _LOGGER.debug("Data Hub details for %s: %s", vehicle_label(vin), err)
            return
        if details:
            self._attributes[vin] = {**self._attributes.get(vin, {}), **details}

    def async_save(self) -> None:
        """Write anything held only in memory back to the config entry."""
        self._persist()

    def _persist(self) -> None:
        """Write anything worth surviving a restart back to the config entry.

        Only when something actually changed: an entry update fires the update
        listeners. Copies, not the live dictionaries: an entry stores what it
        is handed by reference, and sharing one object meant every change
        after the first compared equal and was never written.
        """
        updates: dict[str, Any] = {}
        if (self.entry.data.get(CONF_ATTRIBUTES) or {}) != self._attributes:
            updates[CONF_ATTRIBUTES] = {
                vin: dict(attrs) for vin, attrs in self._attributes.items()
            }
        if (self.entry.data.get(CONF_LAST_CHANGED) or {}) != self._last_changed:
            updates[CONF_LAST_CHANGED] = dict(self._last_changed)
        if (self.entry.data.get(CONF_UNSETTLED_SINCE) or {}) != self._unsettled_since:
            updates[CONF_UNSETTLED_SINCE] = dict(self._unsettled_since)
        stored = {vin: dict(seen) for vin, seen in self._volatile_seen.items()}
        if (self.entry.data.get(CONF_VOLATILE_SEEN) or {}) != stored:
            updates[CONF_VOLATILE_SEEN] = stored
        if (self.entry.data.get(CONF_DECAYED_SINCE) or {}) != self._decayed_since:
            updates[CONF_DECAYED_SINCE] = dict(self._decayed_since)
        decayed = {vin: dict(seen) for vin, seen in self._decayed_seen.items()}
        if (self.entry.data.get(CONF_DECAYED_SEEN) or {}) != decayed:
            updates[CONF_DECAYED_SEEN] = decayed
        if updates:
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, **updates}
            )

    # ------------------------------------------------------- Data Hub inflow
    def _handle_status(
        self,
        vin: str,
        status: dict[str, Any],
        times: dict[str, datetime] | None = None,
    ) -> None:
        """Adopt status from the Data Hub, newest by the car's own clock."""
        held = dt_util.parse_datetime(
            self._status.get(vin, {}).get("LAST_UPDATED_TIME") or ""
        )
        arriving = dt_util.parse_datetime(status.get("LAST_UPDATED_TIME") or "")
        if held and arriving and arriving < held:
            return
        self._status[vin] = status
        self._status_times[vin] = dict(times or {})
        self._note_status_change(vin)
        self._note_unsettled(vin, status)
        self._note_decay(vin, status)
        if vin in self._awaiting:
            # A car whose status arrives after setup needs no reload to get
            # entities: the platforms watch for cars they have not built yet.
            self._awaiting.discard(vin)
            if not self._awaiting:
                self._snapshots_ready.set()
        self._push()

    def status_time(self, vin: str, key: str) -> datetime | None:
        """When the car sent this key of its held status, if that is known."""
        return self._status_times.get(vin, {}).get(key)

    def _handle_position(self, vin: str, position: dict[str, Any]) -> None:
        """Adopt a location from the Data Hub when it is newer than the held one."""
        held = dt_util.parse_datetime(
            str(self._position.get(vin, {}).get("timestamp") or "")
        )
        arriving = dt_util.parse_datetime(str(position.get("timestamp") or ""))
        if held and (arriving is None or arriving < held):
            return
        self._position[vin] = position
        self._position_read_at[vin] = dt_util.utcnow()
        self._push()

    def _note_unsettled(self, vin: str, status: dict[str, Any]) -> None:
        """Track how long this car has been sitting in the state it is in.

        Its own clock, started the first time a car is seen mid-use and
        restarted whenever one of the readings it governs actually moves; not
        _last_changed, which moves when anything in the document moves.
        """
        state = str(status.get("VEHICLE_STATE_TYPE", "")).upper()
        if state not in UNSETTLED_VEHICLE_STATES:
            self._unsettled_since.pop(vin, None)
            self._volatile_seen.pop(vin, None)
            return
        self._note_subset(
            vin,
            status,
            VOLATILE_STATUS_KEYS,
            self._volatile_seen,
            self._unsettled_since,
        )

    def _note_decay(self, vin: str, status: dict[str, Any]) -> None:
        """Track how long the readings that go off on their own have stood.

        No condition about the car attached: coolant temperature is latched
        when the engine stops, so the only question is how long the figure has
        sat unchanged.
        """
        self._note_subset(
            vin,
            status,
            DECAYING_STATUS_KEYS,
            self._decayed_seen,
            self._decayed_since,
        )

    def _note_subset(
        self,
        vin: str,
        status: dict[str, Any],
        keys: tuple[str, ...],
        seen: dict[str, dict[str, Any]],
        since: dict[str, str],
    ) -> None:
        """Record when this group of readings last actually moved.

        Compared key by key, over the keys both sides know: comparing whole
        tuples by position made every stored reading differ from every fresh
        one the moment one more key was watched. A key newly watched has no
        earlier value, so it cannot have moved.
        """
        fingerprint = {key: status.get(key) for key in keys}
        previous = seen.get(vin)
        moved = previous is not None and any(
            previous[key] != fingerprint[key]
            for key in previous.keys() & fingerprint.keys()
        )
        if vin not in since or moved:
            since[vin] = dt_util.utcnow().isoformat()
        seen[vin] = fingerprint

    def _note_status_change(self, vin: str) -> None:
        """Record when the car's reported state last actually changed.

        The first status for a car is not a change: it is the first thing we
        have seen, and when it changed is unknown. Written at once, not at the
        next housekeeping, or a reload in between threw it away.
        """
        status = self._status.get(vin, {})
        if self._last_status_seen.get(vin) not in (None, status):
            self._last_changed[vin] = dt_util.utcnow().isoformat()
            self._persist()
        self._last_status_seen[vin] = status

    def _push(self) -> None:
        """Publish new state to the entities.

        Not async_set_updated_data: that also reschedules the next refresh,
        and readings arriving more often than housekeeping would reset its
        timer forever.
        """
        self.data = self._build()
        self.last_update_success = True
        self.async_update_listeners()

    # ------------------------------------------------------------------ state
    def _build(self) -> dict[str, Any]:
        """Merge the vehicle list, cached details, status and location."""
        data: dict[str, Any] = {
            "vehicles": {},
            "connected": bool(self.datahub and self.datahub.connected),
        }
        for vin, vehicle in self._vehicles.items():
            status = self._status.get(vin, {})
            position = self._position.get(vin, {})
            status_ts = self._newest(
                status.get("LAST_UPDATED_TIME"),
                self._last_changed.get(vin),
            )
            # Whether the fix is old is a question about the fix alone.
            position_ts = position.get("timestamp")
            data["vehicles"][vin] = {
                "role": vehicle.get("role"),
                "attributes": self._attributes.get(vin, {}),
                "status": status,
                "position": position,
                "status_ts": status_ts,
                "position_ts": position_ts,
                "status_stale": self._is_stale(status_ts),
                "readings_provisional": self._readings_provisional(vin),
                "readings_decayed": self._readings_decayed(vin),
                "position_stale": self._is_stale(position_ts),
                "position_trusted": self._position_trusted(vin),
            }
        return data

    @property
    def position_trusted(self) -> bool:
        """Whether any car's last known position can still be asserted."""
        return any(self._position_trusted(vin) for vin in self._vehicles)

    def _position_trusted(self, vin: str) -> bool:
        """Whether this car was located recently enough to vouch for.

        A location we cannot vouch for still resolves to a zone, which is how
        a tracker ends up reporting "home" for a car seven kilometres away.
        """
        last = self._position_read_at.get(vin)
        if last is None:
            return False
        return bool(dt_util.utcnow() - last < POSITION_TRUST_WINDOW)

    @property
    def telemetry_ok(self) -> bool:
        """Whether the readings can still be vouched for."""
        return self.datahub is not None and self.datahub.healthy

    def charge_now_setting(self, vin: str) -> str | None:
        """Return the reported EV charge override, upper-cased."""
        raw = (
            self.data.get("vehicles", {})
            .get(vin, {})
            .get("status", {})
            .get("EV_CHARGE_NOW_SETTING")
        )
        return str(raw).upper() if raw is not None else None

    @staticmethod
    def _newest(*timestamps: str | None) -> str | None:
        """Return the newest parseable timestamp string, or the first non-empty."""
        best: str | None = None
        best_dt = None
        for ts in timestamps:
            if not ts:
                continue
            parsed = dt_util.parse_datetime(ts)
            if parsed is None:
                if best is None:
                    best = ts
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt_util.UTC)
            if best_dt is None or parsed > best_dt:
                best, best_dt = ts, parsed
        return best

    def _readings_decayed(self, vin: str) -> bool:
        """Whether the self-staling readings have stood still too long."""
        return self._stood_too_long(self._decayed_since.get(vin))

    def _readings_provisional(self, vin: str) -> bool:
        """Whether doors, windows and locks for this car are still claims.

        The snapshot was taken while somebody still had the key in the car,
        and the readings it governs have stopped moving for long enough that
        the car is plainly not still reporting. Either alone is ordinary.
        """
        return self._stood_too_long(self._unsettled_since.get(vin))

    def _stood_too_long(self, since: str | None) -> bool:
        """Whether a recorded moment is further back than the option allows."""
        # int() because the options selector hands back strings, and "0" is
        # perfectly truthy — which would leave the off switch quietly on.
        try:
            minutes = int(
                self.entry.options.get(OPT_UNSETTLED_MINUTES, UNSETTLED_MINUTES_DEFAULT)
            )
        except (TypeError, ValueError):
            minutes = UNSETTLED_MINUTES_DEFAULT
        if minutes <= 0:
            return False
        parsed = dt_util.parse_datetime(since) if since else None
        if parsed is None:
            return False
        return bool(dt_util.utcnow() - parsed > timedelta(minutes=minutes))

    @staticmethod
    def _is_stale(timestamp: str | None) -> bool:
        """Return True if the given timestamp is older than STALE_AFTER."""
        if not timestamp:
            return False
        parsed = dt_util.parse_datetime(timestamp)
        if parsed is None:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_util.UTC)
        return bool(dt_util.utcnow() - parsed > STALE_AFTER)

"""Shared entities for JLR InControl."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import JlrCoordinator

_LOGGER = logging.getLogger(__name__)


def async_add_vehicle_entities(
    entry: ConfigEntry,
    platform: Platform,
    coordinator: JlrCoordinator,
    async_add_entities: Callable[[Sequence[Entity]], None],
    build: Callable[[str], Sequence[Entity]],
    protect: Callable[[str], set[str]] | None = None,
) -> None:
    """Create entities per vehicle, now and whenever a new one turns up.

    Platforms used to run exactly once, at setup. A car added to the account
    afterwards was picked up by housekeeping and subscribed to telemetry, and
    its data then arrived for entities that had never been created — so it
    stayed invisible until the user reloaded the integration.

    Sequence rather than list in the callback's return: list is invariant, so
    a platform returning its own entity type would not satisfy list[Entity].

    A vehicle counts as done only once it has actually produced entities. That
    matters because most of them are gated on the status keys a given model
    reports, so a car whose first snapshot has not landed yet builds nothing
    and is simply reconsidered on the next update, rather than being marked
    done and left with no sensors at all.
    """
    built: set[str] = set()

    @callback
    def _add_missing() -> None:
        fresh: list[Entity] = []
        for vin in coordinator.data.get("vehicles", {}):
            if vin in built:
                continue
            entities = build(vin)
            if entities:
                built.add(vin)
                _prune(
                    coordinator,
                    entry,
                    platform,
                    vin,
                    entities,
                    protect(vin) if protect else set(),
                )
                fresh.extend(entities)
        if fresh:
            async_add_entities(fresh)

    _add_missing()
    entry.async_on_unload(coordinator.async_add_listener(_add_missing))


@callback
def _prune(
    coordinator: JlrCoordinator,
    entry: ConfigEntry,
    platform: Platform,
    vin: str,
    entities: Sequence[Entity],
    protected: set[str],
) -> None:
    """Delete registry entries this vehicle no longer has an entity for.

    Most entities are gated on the status keys the car actually reports, and
    that gating has tightened over several versions. Anything a previous
    version created and this one does not is left in the registry, where it
    shows up forever as a greyed-out `restored` entity that will never take a
    value again — a diesel-only AdBlue pair on a petrol Defender, EV sensors on
    an ICE car, rear doors on a two-door.

    Only ever called for a vehicle that has just produced entities, so a car
    whose snapshot has not landed yet is left completely alone rather than
    stripped back to nothing. That is the important precondition: the set kept
    here is derived from the same snapshot that built the entities, so a
    genuinely truncated status document would take the matching registry
    entries — and their history — with it. VHS pushes are whole-vehicle
    documents and the coordinator already replaces its cache wholesale on each
    one, so a partial snapshot is not something these cars send; the removals
    are logged at INFO so that assumption is visible if it ever breaks.

    ``protected`` is the second precondition, and the one that matters for
    electric cars. Deleting because a status key is absent is acting on an
    observation. Deleting because this integration *judged* the car not to be
    electrified is acting on an inference, and while the inference is only ever
    reached through a key the car did send, it falls back to a heuristic
    whenever ``fuelType`` is missing — which is currently the normal case,
    because Approov walls the attributes endpoint. A platform names the ids it
    declined to build on that basis, and they are kept: an EV that dropped
    EV_STATE_OF_CHARGE from one snapshot would otherwise lose its charging
    history permanently, and an unwanted entity on an ICE car is only untidy.
    """
    keep = {entity.unique_id for entity in entities} | protected
    registry = er.async_get(coordinator.hass)
    prefix = f"{vin}_"
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registered.domain != platform or registered.unique_id in keep:
            continue
        if not registered.unique_id.startswith(prefix):
            continue
        _LOGGER.info(
            "Removing %s: this vehicle no longer reports it", registered.entity_id
        )
        registry.async_remove(registered.entity_id)


def is_electric(attributes: dict[str, Any]) -> bool:
    """Return True when the vehicle is a pure BEV."""
    return str(attributes.get("fuelType", "")).lower() == "electric"


def is_electrified(attributes: dict[str, Any], status: dict[str, Any]) -> bool:
    """True for anything with a charge port (BEV or plug-in hybrid).

    ICE cars still report several EV_* status keys with UNKNOWN sentinels
    (seen live on a diesel L460, including EV_CHARGING_STATUS), so key
    presence alone would create phantom EV entities. EV_STATE_OF_CHARGE is
    the one key verified absent on ICE and present on real EVs — use its
    presence as the fallback discriminator for unexpected fuelType strings.
    """
    fuel = str(attributes.get("fuelType", "")).lower()
    if "electric" in fuel or "hybrid" in fuel:
        return True
    return "EV_STATE_OF_CHARGE" in status


class JlrVehicleEntity(CoordinatorEntity[JlrCoordinator]):
    """Base entity bound to a single vehicle (by VIN)."""

    _attr_has_entity_name = True

    # Whether this entity's value comes from the telemetry socket. Most do, and
    # for those a dead socket means the reading is meaningless. Location comes
    # from the owner portal and an action needs no reading at all, so those set
    # this False rather than inheriting a dependency they do not have.
    _requires_telemetry = True

    def __init__(self, coordinator: JlrCoordinator, vin: str) -> None:
        super().__init__(coordinator)
        self._vin = vin

    @property
    def _vehicle(self) -> dict[str, Any]:
        vehicle: dict[str, Any] = self.coordinator.data.get("vehicles", {}).get(
            self._vin, {}
        )
        return vehicle

    @property
    def _attributes(self) -> dict[str, Any]:
        attributes: dict[str, Any] = self._vehicle.get("attributes", {})
        return attributes

    @property
    def is_electric(self) -> bool:
        """Whether this vehicle is a pure BEV."""
        return is_electric(self._attributes)

    @property
    def _position(self) -> dict[str, Any]:
        position: dict[str, Any] = self._vehicle.get("position", {})
        return position

    def _status_value(self, key: str) -> Any:
        """Read a value from the flattened vehicle status dict."""
        return self._vehicle.get("status", {}).get(key)

    @property
    def available(self) -> bool:
        """Available while the vehicle is known and this entity's source is up.

        The coordinator's own success flag only covers the housekeeping poll,
        which keeps succeeding long after the data socket has died — so it is
        not on its own a statement about whether these values mean anything.

        But the socket is not every entity's source. Requiring it for all of
        them meant a telemetry outage hid a location the owner portal had
        fetched successfully minutes earlier, and greyed out the refresh button
        at the moment someone would reach for it. Location survives a dead
        socket by design; its honesty signal is ``position_trusted``, which is
        about the age of the fix and reported separately.
        """
        if not super().available:
            return False
        if self._vin not in self.coordinator.data.get("vehicles", {}):
            return False
        return self.coordinator.telemetry_ok or not self._requires_telemetry

    @property
    def device_info(self) -> DeviceInfo:
        attrs = self._attributes
        # The brand comes from the VIN when the endpoint that knows better is
        # blocked, so it is safe to lean on; the model often is not there.
        brand = attrs.get("vehicleBrand") or "Vehicle"
        model = attrs.get("vehicleType") or attrs.get("model")
        # Two cars on one account would otherwise both be called "Land Rover".
        # The VIN's last four is how the V5C and JLR's own app shorten it.
        name = attrs.get("nickname") or model or f"{brand} {self._vin[-4:]}"
        return DeviceInfo(
            identifiers={(DOMAIN, self._vin)},
            manufacturer=brand,
            model=model,
            name=name,
            serial_number=self._vin,
        )

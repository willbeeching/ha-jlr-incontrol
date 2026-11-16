"""The Jaguar Land Rover InControl integration."""

from __future__ import annotations

import logging
from typing import TypeAlias

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .const import (
    DATAHUB_ISSUES,
    DOMAIN,
    INCONTROL_KEYS,
    ISSUE_DATAHUB_SIGNED_OUT,
    ISSUE_INCONTROL_RETIRED,
    ISSUE_PORTAL_SIGNED_OUT,
    PLATFORMS,
)
from .coordinator import JlrCoordinator

_LOGGER = logging.getLogger(__name__)

# The coordinator lives on the entry rather than in hass.data: typed, tied
# to the entry's lifetime, and gone when it unloads without anyone having to
# remember to pop it.
JlrConfigEntry: TypeAlias = ConfigEntry[JlrCoordinator]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Bring an entry from an earlier version up to this one.

    1.2: the InControl sign-in is gone, and with it every credential it kept,
    the password among them. An entry that also had the Data Hub carries on
    with it; one that did not keeps its title, devices and entities and waits
    for the owner to link it.
    """
    if entry.version > 1:
        # Written by a newer version than this one: leave it alone.
        return False
    if entry.minor_version < 2:
        data = {k: v for k, v in entry.data.items() if k not in INCONTROL_KEYS}
        hass.config_entries.async_update_entry(entry, data=data, minor_version=2)
        # Asked for a portal session this version will never use.
        ir.async_delete_issue(
            hass, DOMAIN, f"{ISSUE_PORTAL_SIGNED_OUT}_{entry.entry_id}"
        )
        _LOGGER.debug("removed the InControl sign-in from %s", entry.title)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: JlrConfigEntry) -> bool:
    """Set up JLR from a config entry."""
    coordinator = JlrCoordinator(hass, entry)
    # Housekeeping first (the cars on the link, their details), then the
    # link itself, which is where the readings come from.
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_start()
    coordinator.async_stop_on_hass_shutdown()

    # Repairs raised before the id was scoped to the entry. Nothing will ever
    # clear these, so they would sit in Repairs forever.
    ir.async_delete_issue(hass, DOMAIN, ISSUE_PORTAL_SIGNED_OUT)
    # Before 3.0.0-beta.2 a revoked link was a repair; it is a reauth now, and
    # nothing raises or clears the repair any more.
    ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_DATAHUB_SIGNED_OUT}_{entry.entry_id}")

    entry.runtime_data = coordinator
    _async_drop_removed_platforms(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # After the platforms, so its entities are listening before it reports.
    # Already running when setup waited for it; starting it again is a no-op.
    if coordinator.datahub:
        coordinator.datahub.async_start()
    return True


# Platforms that existed only to send remote commands. JLR gate those behind app
# attestation now, so the entities cannot work; without this they would sit in
# the registry as permanently unavailable, which reads like a fault.
REMOVED_PLATFORMS = ("lock", "climate")


def _async_drop_removed_platforms(hass: HomeAssistant, entry: JlrConfigEntry) -> None:
    """Delete entities belonging to platforms this version no longer has."""
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.domain in REMOVED_PLATFORMS:
            registry.async_remove(entity.entity_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: JlrConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting the device for a vehicle the account no longer has.

    Only once the account has been read and genuinely does not list the car:
    a failed listing must not be taken as evidence that someone's car is
    gone. Never on an entry waiting for the Data Hub, which lists nothing
    because it cannot read anything yet, and whose devices are what keep the
    entities' history for when it is linked.
    """
    coordinator = entry.runtime_data
    if coordinator.dormant or not coordinator.last_update_success:
        return False
    known = set(coordinator.data.get("vehicles", {}))
    return not any(
        identifier[1] in known
        for identifier in device.identifiers
        if identifier[0] == DOMAIN
    )


async def async_remove_entry(hass: HomeAssistant, entry: JlrConfigEntry) -> None:
    """Take this entry's repairs with it when the entry is deleted.

    A repair outlives the thing it is about otherwise: the coordinator that
    would have cleared it no longer exists.
    """
    for issue in (ISSUE_PORTAL_SIGNED_OUT, ISSUE_INCONTROL_RETIRED, *DATAHUB_ISSUES):
        ir.async_delete_issue(hass, DOMAIN, f"{issue}_{entry.entry_id}")


async def async_unload_entry(hass: HomeAssistant, entry: JlrConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = entry.runtime_data
        # Before a reload builds a new coordinator from what the entry holds:
        # anything gathered since the last poll is only in memory until this.
        coordinator.async_save()
        if coordinator.datahub:
            await coordinator.datahub.async_stop()
    return unloaded

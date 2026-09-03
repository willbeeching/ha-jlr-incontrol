"""The Jaguar Land Rover InControl integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, ISSUE_PORTAL_SIGNED_OUT, PLATFORMS
from .coordinator import JlrCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up JLR InControl from a config entry."""
    coordinator = JlrCoordinator(hass, entry)
    # Housekeeping first (token, device registration, vehicle list), then the
    # telemetry socket, which is where the vehicle data itself comes from.
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_start_telemetry()
    coordinator.async_start_keepalive()

    # Repairs raised before the id was scoped to the entry. Nothing will ever
    # clear these, so they would sit in Repairs forever.
    ir.async_delete_issue(hass, DOMAIN, ISSUE_PORTAL_SIGNED_OUT)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    _async_drop_removed_platforms(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


# Platforms that existed only to send remote commands. JLR gate those behind app
# attestation now, so the entities cannot work; without this they would sit in
# the registry as permanently unavailable, which reads like a fault.
REMOVED_PLATFORMS = ("lock", "climate")


def _async_drop_removed_platforms(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete entities belonging to platforms this version no longer has."""
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.domain in REMOVED_PLATFORMS:
            registry.async_remove(entity.entity_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting the device for a vehicle the account no longer has.

    Sold, traded or removed from InControl, a car leaves its device and its
    entities behind forever otherwise, because nothing else prunes them.
    Deletion is only permitted once the account has been read successfully and
    genuinely does not list the vehicle — a failed listing must not be taken as
    evidence that someone's car is gone.
    """
    coordinator: JlrCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is None or not coordinator.last_update_success:
        return False
    known = set(coordinator.data.get("vehicles", {}))
    return not any(
        identifier[1] in known
        for identifier in device.identifiers
        if identifier[0] == DOMAIN
    )


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Take this entry's repair with it when the entry is deleted.

    A repair outlives the thing it is about otherwise: the coordinator that
    would have cleared it no longer exists, so the warning stays in Repairs
    pointing at an account the user has already removed.
    """
    ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_PORTAL_SIGNED_OUT}_{entry.entry_id}")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: JlrCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.telemetry.async_stop()
        await coordinator.portal.async_close()
    return unloaded

"""The Jaguar Land Rover InControl integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, PLATFORMS
from .coordinator import JlrCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up JLR InControl from a config entry."""
    coordinator = JlrCoordinator(hass, entry)
    # Housekeeping first (token, device registration, vehicle list), then the
    # telemetry socket, which is where the vehicle data itself comes from.
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_start_telemetry()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
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


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: JlrCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.telemetry.async_stop()
        await coordinator.portal.async_close()
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when the *options* change.

    Not on every entry change. JLR rotate the refresh token on each renewal and
    the coordinator persists the new one, so a listener that reloads whenever
    the entry is written turned a five-minute token into a five-minute teardown
    and re-setup of the whole integration — entities dropping out, the device
    re-registering, and the caches wiped, around the clock.
    """
    coordinator: JlrCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None and coordinator.options_snapshot == dict(entry.options):
        return
    await hass.config_entries.async_reload(entry.entry_id)

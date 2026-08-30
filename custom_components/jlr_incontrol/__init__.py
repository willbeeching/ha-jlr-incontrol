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
    coordinator.async_start_keepalive()

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


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: JlrCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.telemetry.async_stop()
        await coordinator.portal.async_close()
    return unloaded

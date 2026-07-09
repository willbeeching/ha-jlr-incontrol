"""Door lock for JLR InControl (RDL / RDU)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import JlrApiError
from .const import CONF_PIN, DOMAIN, SERVICE_LOCK, SERVICE_UNLOCK
from .coordinator import JlrCoordinator
from .entity import JlrVehicleEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up JLR door locks.

    Lock/unlock are PIN-gated remote commands, so the lock entity is only added
    when a PIN is configured. Without a PIN the read-only "all doors locked"
    binary_sensor still reports lock state.
    """
    if not entry.data.get(CONF_PIN):
        return
    coordinator: JlrCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        JlrLock(coordinator, vin) for vin in coordinator.data.get("vehicles", {})
    )


class JlrLock(JlrVehicleEntity, LockEntity):
    """Vehicle door lock. Commands are PIN-gated (needs live-PIN validation)."""

    _attr_translation_key = "doors"

    def __init__(self, coordinator: JlrCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin)
        self._attr_unique_id = f"{vin}_lock"

    @property
    def is_locked(self) -> bool | None:
        value = self._status_value("DOOR_IS_ALL_DOORS_LOCKED")
        if value is None:
            return None
        return str(value).upper() == "TRUE"

    async def async_lock(self, **kwargs: Any) -> None:
        await self._command(SERVICE_LOCK)

    async def async_unlock(self, **kwargs: Any) -> None:
        await self._command(SERVICE_UNLOCK)

    async def _command(self, service_name: str) -> None:
        pin = self.coordinator.entry.data.get(CONF_PIN)
        if not pin:
            raise HomeAssistantError(
                "A vehicle PIN is required to send remote commands. Reconfigure the "
                "integration and provide the PIN."
            )
        try:
            await self.coordinator.client.async_send_command(
                self._vin, service_name, pin
            )
        except JlrApiError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()

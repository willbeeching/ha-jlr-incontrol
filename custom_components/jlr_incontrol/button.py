"""Buttons for JLR InControl (honk & flash, refresh)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import JlrApiError
from .const import CONF_PIN, DOMAIN, SERVICE_HONK_FLASH
from .coordinator import JlrCoordinator
from .entity import JlrVehicleEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up JLR buttons.

    The refresh button (re-poll) needs no PIN and is always added. Honk & flash
    is a PIN-gated remote command, so it is only added when a PIN is configured.
    """
    coordinator: JlrCoordinator = hass.data[DOMAIN][entry.entry_id]
    has_pin = bool(entry.data.get(CONF_PIN))
    entities: list[ButtonEntity] = []
    for vin in coordinator.data.get("vehicles", {}):
        entities.append(JlrRefreshButton(coordinator, vin))
        if has_pin:
            entities.append(JlrHonkFlashButton(coordinator, vin))
    async_add_entities(entities)


class JlrHonkFlashButton(JlrVehicleEntity, ButtonEntity):
    """Honk the horn and flash the lights (HBLF). PIN-gated."""

    _attr_translation_key = "honk_flash"
    _attr_icon = "mdi:bugle"

    def __init__(self, coordinator: JlrCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin)
        self._attr_unique_id = f"{vin}_honk_flash"

    async def async_press(self) -> None:
        pin = self.coordinator.entry.data.get(CONF_PIN)
        if not pin:
            raise HomeAssistantError(
                "A vehicle PIN is required to send remote commands. Reconfigure the "
                "integration and provide the PIN."
            )
        try:
            await self.coordinator.client.async_send_command(
                self._vin, SERVICE_HONK_FLASH, pin
            )
        except JlrApiError as err:
            raise HomeAssistantError(str(err)) from err


class JlrRefreshButton(JlrVehicleEntity, ButtonEntity):
    """Re-poll the vehicle data.

    TODO: a true "refresh from vehicle" (VHS health-status refresh) is a PIN-gated
    remote service. For now this just re-fetches the cached status/position from the
    backend.
    """

    _attr_translation_key = "refresh"
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator: JlrCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin)
        self._attr_unique_id = f"{vin}_refresh"

    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()

"""Buttons for JLR InControl (honk & flash, refresh, update, charge control)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import JlrApiError
from .const import CONF_PIN, DOMAIN, SERVICE_HONK_FLASH
from .coordinator import JlrCoordinator
from .entity import JlrVehicleEntity, is_electrified


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up JLR buttons.

    The refresh button (re-poll) needs no PIN and is always added. Honk & flash
    and the Force charge buttons are PIN-gated remote commands, so they are only
    added when a PIN is configured.
    """
    coordinator: JlrCoordinator = hass.data[DOMAIN][entry.entry_id]
    has_pin = bool(entry.data.get(CONF_PIN))
    entities: list[ButtonEntity] = []
    for vin, vehicle in coordinator.data.get("vehicles", {}).items():
        entities.append(JlrRefreshButton(coordinator, vin))
        entities.append(JlrUpdateFromVehicleButton(coordinator, vin))
        if has_pin:
            entities.append(JlrHonkFlashButton(coordinator, vin))
        status = vehicle.get("status", {})
        if (
            has_pin
            and is_electrified(vehicle.get("attributes", {}), status)
            and "EV_CHARGE_NOW_SETTING" in status
        ):
            entities.append(JlrForceChargeButton(coordinator, vin, enable=True))
            entities.append(JlrForceChargeButton(coordinator, vin, enable=False))
    async_add_entities(entities)

    # The tri-state charge override moved from a binary switch to this
    # sensor + buttons (#6); drop the old switch left in the registry.
    ent_reg = er.async_get(hass)
    for vin in coordinator.data.get("vehicles", {}):
        stale = ent_reg.async_get_entity_id("switch", DOMAIN, f"{vin}_charge_now")
        if stale:
            ent_reg.async_remove(stale)


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
    """Re-poll the cached vehicle data from the JLR backend."""

    _attr_translation_key = "refresh"
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator: JlrCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin)
        self._attr_unique_id = f"{vin}_refresh"

    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()


class JlrUpdateFromVehicleButton(JlrVehicleEntity, ButtonEntity):
    """Request a vehicle health status refresh (VHS), then re-poll."""

    _attr_translation_key = "update_from_vehicle"
    _attr_icon = "mdi:car-connected"

    def __init__(self, coordinator: JlrCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin)
        self._attr_unique_id = f"{vin}_update_from_vehicle"

    async def async_press(self) -> None:
        try:
            await self.coordinator.client.async_send_vhs(self._vin)
        except JlrApiError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()


class JlrForceChargeButton(JlrVehicleEntity, ButtonEntity):
    """Force the CP charge override on (FORCE_ON) or off (FORCE_OFF).

    Two write actions mirror what the API accepts — there is no way to write
    DEFAULT, so the override can only be forced, never explicitly cleared
    (the car returns to DEFAULT on its own). PIN-gated.
    """

    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator: JlrCoordinator, vin: str, *, enable: bool) -> None:
        super().__init__(coordinator, vin)
        self._enable = enable
        suffix = "on" if enable else "off"
        self._attr_translation_key = f"force_charge_{suffix}"
        self._attr_unique_id = f"{vin}_force_charge_{suffix}"

    async def async_press(self) -> None:
        pin = self.coordinator.entry.data.get(CONF_PIN)
        if not pin:
            raise HomeAssistantError(
                "A vehicle PIN is required to send remote commands. Reconfigure the "
                "integration and provide the PIN."
            )
        try:
            await self.coordinator.client.async_send_charge_now(
                self._vin, enable=self._enable, pin=pin
            )
        except JlrApiError as err:
            # Forcing a state the car is already in is a no-op, not a failure.
            if "parameterAlreadyInRequestedState" not in str(err):
                raise HomeAssistantError(str(err)) from err
        self.coordinator.note_charge_now(
            self._vin, "FORCE_ON" if self._enable else "FORCE_OFF"
        )
        await self.coordinator.async_request_refresh()

"""Charge control switch for JLR InControl (BEV/PHEV)."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import JlrApiError
from .const import CONF_PIN, DOMAIN
from .coordinator import JlrCoordinator
from .entity import JlrVehicleEntity

_CHARGING_STATES = {"CHARGING", "INITIALIZATION", "DELAYSTART"}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up JLR charge control switches for electric vehicles."""
    if not entry.data.get(CONF_PIN):
        return
    coordinator: JlrCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[JlrChargeSwitch] = []
    for vin, vehicle in coordinator.data.get("vehicles", {}).items():
        attributes = vehicle.get("attributes", {})
        if str(attributes.get("fuelType", "")).lower() != "electric":
            continue
        if "EV_CHARGING_STATUS" not in vehicle.get("status", {}):
            continue
        entities.append(JlrChargeSwitch(coordinator, vin))
    async_add_entities(entities)


class JlrChargeSwitch(JlrVehicleEntity, SwitchEntity):
    """Force charge on/off via the CP service."""

    _attr_translation_key = "charge_now"
    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator: JlrCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin)
        self._attr_unique_id = f"{vin}_charge_now"

    @property
    def is_on(self) -> bool:
        status = str(self._status_value("EV_CHARGING_STATUS") or "").upper()
        return status in _CHARGING_STATES

    async def async_turn_on(self, **kwargs: object) -> None:
        await self._set_charge(enable=True)

    async def async_turn_off(self, **kwargs: object) -> None:
        await self._set_charge(enable=False)

    async def _set_charge(self, *, enable: bool) -> None:
        pin = self.coordinator.entry.data.get(CONF_PIN)
        if not pin:
            raise HomeAssistantError(
                "A vehicle PIN is required to send remote commands. Reconfigure the "
                "integration and provide the PIN."
            )
        try:
            await self.coordinator.client.async_send_charge_now(
                self._vin, enable=enable, pin=pin
            )
        except JlrApiError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()

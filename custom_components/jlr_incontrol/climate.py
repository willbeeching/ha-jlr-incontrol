"""Climate (remote start / preconditioning) for JLR InControl.

A minimal on/off climate entity: HEAT starts remote climate (REON), OFF stops it
(REOFF). State is read from ``CLIMATE_STATUS_OPERATING_STATUS``. Commands are
PIN-gated and need live-PIN validation.
"""

from __future__ import annotations

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import JlrApiError
from .const import CONF_PIN, DOMAIN, SERVICE_ENGINE_OFF, SERVICE_ENGINE_ON
from .coordinator import JlrCoordinator
from .entity import JlrVehicleEntity

_ON_STATES = {"ON", "RUNNING", "STARTUP", "PRECLIM", "HEATING", "COOLING", "ENGINE_ON"}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up JLR climate entities.

    Remote climate is a PIN-gated command, so the climate entity is only added
    when a PIN is configured.
    """
    if not entry.data.get(CONF_PIN):
        return
    coordinator: JlrCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        JlrClimate(coordinator, vin) for vin in coordinator.data.get("vehicles", {})
    )


class JlrClimate(JlrVehicleEntity, ClimateEntity):
    """Remote climate as a simple on/off climate entity."""

    _attr_translation_key = "climate"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_supported_features = (
        ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: JlrCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin)
        self._attr_unique_id = f"{vin}_climate"

    @property
    def hvac_mode(self) -> HVACMode:
        status = str(
            self._status_value("CLIMATE_STATUS_OPERATING_STATUS") or ""
        ).upper()
        return HVACMode.HEAT if status in _ON_STATES else HVACMode.OFF

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
        else:
            await self.async_turn_on()

    async def async_turn_on(self) -> None:
        await self._command(SERVICE_ENGINE_ON)

    async def async_turn_off(self) -> None:
        await self._command(SERVICE_ENGINE_OFF)

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

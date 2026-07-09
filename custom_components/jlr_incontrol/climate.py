"""Climate (remote start / preconditioning) for JLR InControl.

ICE/PHEV: HEAT starts remote climate (REON), OFF stops it (REOFF).
BEV: uses ECC electric preconditioning with a target temperature.
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
from .const import (
    CONF_PIN,
    DOMAIN,
    ECC_DEFAULT_TEMP,
    ECC_MAX_TEMP,
    ECC_MIN_TEMP,
    SERVICE_ENGINE_OFF,
    SERVICE_ENGINE_ON,
)
from .coordinator import JlrCoordinator
from .entity import JlrVehicleEntity

_ON_STATES = {"ON", "RUNNING", "STARTUP", "PRECLIM", "HEATING", "COOLING", "ENGINE_ON"}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up JLR climate entities.

    ICE/PHEV remote climate is PIN-gated (REON/REOFF). BEV electric preconditioning
    (ECC) authenticates with an empty PIN, so the climate entity is created for
    electric vehicles even when no account PIN is configured.
    """
    coordinator: JlrCoordinator = hass.data[DOMAIN][entry.entry_id]
    has_pin = bool(entry.data.get(CONF_PIN))
    entities: list[JlrClimate] = []
    for vin, vehicle in coordinator.data.get("vehicles", {}).items():
        attributes = vehicle.get("attributes", {})
        is_ev = str(attributes.get("fuelType", "")).lower() == "electric"
        if has_pin or is_ev:
            entities.append(JlrClimate(coordinator, vin))
    async_add_entities(entities)


class JlrClimate(JlrVehicleEntity, ClimateEntity):
    """Remote climate as a simple on/off climate entity."""

    _attr_translation_key = "climate"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_min_temp = ECC_MIN_TEMP
    _attr_max_temp = ECC_MAX_TEMP
    _attr_target_temperature = ECC_DEFAULT_TEMP

    def __init__(self, coordinator: JlrCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin)
        self._attr_unique_id = f"{vin}_climate"
        if self.is_electric:
            self._attr_supported_features = (
                ClimateEntityFeature.TURN_ON
                | ClimateEntityFeature.TURN_OFF
                | ClimateEntityFeature.TARGET_TEMPERATURE
            )
        else:
            self._attr_supported_features = (
                ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
            )

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

    async def async_set_temperature(self, **kwargs: float) -> None:
        if temperature := kwargs.get("temperature"):
            self._attr_target_temperature = temperature
        if self.hvac_mode != HVACMode.OFF:
            await self.async_turn_on()

    async def async_turn_on(self) -> None:
        if self.is_electric:
            await self._ecc_command(start=True)
        else:
            await self._ice_command(SERVICE_ENGINE_ON)

    async def async_turn_off(self) -> None:
        if self.is_electric:
            await self._ecc_command(start=False)
        else:
            await self._ice_command(SERVICE_ENGINE_OFF)

    async def _ice_command(self, service_name: str) -> None:
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

    async def _ecc_command(self, *, start: bool) -> None:
        try:
            await self.coordinator.client.async_send_preconditioning(
                self._vin,
                start=start,
                target_temp_c=self.target_temperature or ECC_DEFAULT_TEMP,
            )
        except JlrApiError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()

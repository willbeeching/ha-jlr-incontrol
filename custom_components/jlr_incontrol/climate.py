"""Climate (remote start / preconditioning) for JLR InControl.

ICE/PHEV: a single HEAT_COOL mode starts remote climate (REON) toward the RCC
target temperature — the car decides whether to heat or cool — and OFF stops
it (REOFF). BEV: uses ECC electric preconditioning with a target temperature.
"""

from __future__ import annotations

import time

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
    CLIMATE_ACTIVE_STATES,
    CLIMATE_ASSUMED_OFF_SECONDS,
    CLIMATE_ASSUMED_ON_SECONDS,
    CONF_PIN,
    DOMAIN,
    ECC_DEFAULT_TEMP,
    ECC_MAX_TEMP,
    ECC_MIN_TEMP,
    ICE_DEFAULT_TEMP,
    ICE_MAX_TEMP,
    ICE_MIN_TEMP,
    SERVICE_ENGINE_OFF,
)
from .coordinator import JlrCoordinator
from .entity import JlrVehicleEntity


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
    """Remote climate: HEAT_COOL on ICE (car picks heat vs cool), ECC on BEV."""

    _attr_translation_key = "climate"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: JlrCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin)
        self._attr_unique_id = f"{vin}_climate"
        # Optimistic running state after a confirmed command; the cached
        # operating status lags by minutes, and without this the entity shows
        # Off while the engine runs, leaving nothing to turn off from the UI.
        self._assumed_active: bool | None = None
        self._assumed_expiry: float = 0.0
        self._attr_supported_features = (
            ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TARGET_TEMPERATURE
        )
        if self.is_electric:
            self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
            self._attr_min_temp = ECC_MIN_TEMP
            self._attr_max_temp = ECC_MAX_TEMP
            self._attr_target_temperature = ECC_DEFAULT_TEMP
        else:
            # A single active mode: REON heats or cools toward the target on
            # its own, so separate Heat/Cool buttons would just send the same
            # command twice (and confuse — see #4). 15.5C is LO, 28.5C is HI.
            self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT_COOL]
            self._attr_min_temp = ICE_MIN_TEMP
            self._attr_max_temp = ICE_MAX_TEMP
            self._attr_target_temperature = ICE_DEFAULT_TEMP

    def _active_mode(self) -> HVACMode:
        return HVACMode.HEAT if self.is_electric else HVACMode.HEAT_COOL

    def _set_assumed(self, active: bool) -> None:
        self._assumed_active = active
        self._assumed_expiry = time.monotonic() + (
            CLIMATE_ASSUMED_ON_SECONDS if active else CLIMATE_ASSUMED_OFF_SECONDS
        )
        self.async_write_ha_state()

    @property
    def hvac_mode(self) -> HVACMode:
        if self._assumed_active is not None and time.monotonic() < self._assumed_expiry:
            active = self._assumed_active
        else:
            status = str(
                self._status_value("CLIMATE_STATUS_OPERATING_STATUS") or ""
            ).upper()
            active = status in CLIMATE_ACTIVE_STATES
        return self._active_mode() if active else HVACMode.OFF

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
        else:
            await self.async_turn_on()

    async def async_set_temperature(self, **kwargs: float) -> None:
        if (temperature := kwargs.get("temperature")) is not None:
            self._attr_target_temperature = temperature
        if self.hvac_mode != HVACMode.OFF:
            await self.async_turn_on()

    async def async_turn_on(self) -> None:
        if self.is_electric:
            await self._ecc_command(start=True)
        else:
            await self._ice_command_start()
        self._set_assumed(True)

    async def async_turn_off(self) -> None:
        if self.is_electric:
            await self._ecc_command(start=False)
        else:
            await self._ice_command(SERVICE_ENGINE_OFF)
        self._set_assumed(False)

    def _ice_target_temp_c(self) -> float:
        if self._attr_target_temperature is not None:
            return float(self._attr_target_temperature)
        return ICE_DEFAULT_TEMP

    def _require_pin(self) -> str:
        pin = self.coordinator.entry.data.get(CONF_PIN)
        if not pin:
            raise HomeAssistantError(
                "A vehicle PIN is required to send remote commands. Reconfigure the "
                "integration and provide the PIN."
            )
        return pin

    async def _ice_command_start(self) -> None:
        pin = self._require_pin()
        try:
            await self.coordinator.client.async_remote_engine_start(
                self._vin, pin, self._ice_target_temp_c()
            )
        except JlrApiError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()

    async def _ice_command(self, service_name: str) -> None:
        pin = self._require_pin()
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

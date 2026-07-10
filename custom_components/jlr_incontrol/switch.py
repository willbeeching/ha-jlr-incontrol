"""Charge control switch for JLR InControl (BEV/PHEV).

The switch reads EV_CHARGE_NOW_SETTING — the direct status readback of the CP
override this switch writes (CHARGE_NOW_SETTING = FORCE_ON/FORCE_OFF). There
is no charge-read endpoint (the app's own code has none; a live charging
I-Pace with no override reports DEFAULT here while EV_CHARGING_STATUS says
CHARGING), so mirroring charging status showed a phantom ON whenever the car
auto-started a charge.
"""

from __future__ import annotations

import time

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import JlrApiError
from .const import CONF_PIN, DOMAIN
from .coordinator import JlrCoordinator
from .entity import JlrVehicleEntity

# How long to trust a confirmed command over the (minutes-stale) server cache.
ASSUMED_STATE_SECONDS = 5 * 60


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
        if "EV_CHARGE_NOW_SETTING" not in vehicle.get("status", {}):
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
        self._assumed_on: bool | None = None
        self._assumed_expiry: float = 0.0

    @property
    def is_on(self) -> bool | None:
        if self._assumed_on is not None and time.monotonic() < self._assumed_expiry:
            return self._assumed_on
        setting = str(self._status_value("EV_CHARGE_NOW_SETTING") or "").upper()
        if setting == "FORCE_ON":
            return True
        if setting in ("FORCE_OFF", "DEFAULT"):
            return False
        return None

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
            # The switch mirrors EV_CHARGING_STATUS, so it reads ON when the
            # car auto-started charging on plug-in with no CP override active.
            # Asking the car to match a state it is already in is a no-op,
            # not a failure (#1).
            if "parameterAlreadyInRequestedState" not in str(err):
                raise HomeAssistantError(str(err)) from err
        # Trust the confirmed command until the cached status catches up.
        self._assumed_on = enable
        self._assumed_expiry = time.monotonic() + ASSUMED_STATE_SECONDS
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

"""Entities from earlier versions that must not be left behind.

Every one of these was created by a version of this integration that has since
learned better. Left in the registry they sit permanently unavailable, which
reads as a fault rather than as something that was removed on purpose.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT, Doubles  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol.const import DOMAIN  # noqa: E402


def plant(hass: HomeAssistant, entry: MockConfigEntry, domain: str, key: str) -> str:
    """Create an entity the way an older version would have."""
    return (
        er.async_get(hass)
        .async_get_or_create(domain, DOMAIN, f"{KEPT}_{key}", config_entry=entry)
        .entity_id
    )


def survives(hass: HomeAssistant, entity_id: str) -> bool:
    return er.async_get(hass).async_get(entity_id) is not None


class TestCommandsThatCanNoLongerRun:
    @pytest.mark.parametrize(
        "key",
        ["honk_flash", "update_from_vehicle", "force_charge_on", "force_charge_off"],
    )
    async def test_a_removed_button_is_deleted(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        doubles: Doubles,
        key: str,
    ) -> None:
        stale = plant(hass, entry, "button", key)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert not survives(hass, stale)

    async def test_the_old_charge_now_switch_is_deleted(
        self, hass: HomeAssistant, entry: MockConfigEntry, doubles: Doubles
    ) -> None:
        # It became a tri-state sensor: a switch could not tell "no override"
        # from "charging actively suppressed" (#6).
        stale = plant(hass, entry, "switch", "charge_now")
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert not survives(hass, stale)

    async def test_the_surviving_button_is_left_alone(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        assert er.async_get(hass).async_get_entity_id(
            "button", DOMAIN, f"{KEPT}_refresh"
        )


class TestEntitiesForHardwareTheCarDoesNotHave:
    async def test_rear_door_sensors_go_on_a_two_door(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        doubles: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from doubles import FakeClient

        async def attributes(self: FakeClient, vin: str) -> dict:
            return {
                "vehicleBrand": "Jaguar",
                "fuelType": "Petrol",
                "nickname": "Test Car",
                "numberOfDoors": "2",
            }

        monkeypatch.setattr(FakeClient, "async_get_attributes", attributes)
        stale = plant(hass, entry, "binary_sensor", "door_rear_left")
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert not survives(hass, stale)

    @pytest.mark.parametrize("key", ["ev_charging", "ev_plugged_in"])
    async def test_charging_sensors_go_on_a_car_with_no_plug(
        self, hass: HomeAssistant, entry: MockConfigEntry, doubles: Doubles, key: str
    ) -> None:
        # ICE cars report EV_* keys with UNKNOWN sentinels, which is how these
        # appeared in the first place.
        stale = plant(hass, entry, "binary_sensor", key)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert not survives(hass, stale)

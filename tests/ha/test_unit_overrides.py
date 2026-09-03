"""Forcing units, and putting them back.

Home Assistant already converts by unit system; these options exist for people
whose car and whose household disagree. The override is written into the entity
registry rather than onto the entity, so clearing it has to remove the entry
again — leaving it behind would pin the unit forever with nothing in the UI
saying why.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT, Doubles  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol.const import DOMAIN  # noqa: E402

DATA = {
    "username": "someone@example.com",
    "device_id": "a-device",
    "user_id": "a-user",
    "refresh_token": "a-refresh-token",
    "sso_cookies": {"iPlanetDirectoryPro": "a-cookie"},
    "portal_cookies": {"JSESSIONID": "a-session"},
    "portal_base": "https://incontrol.jaguar.com/jaguar-portal-owner-web",
}


async def set_up(hass: HomeAssistant, options: dict[str, str]) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data=DATA, options=options, unique_id="a-user"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def unit_for(hass: HomeAssistant, key: str) -> Any:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{KEPT}_{key}"
    )
    assert entity_id, f"no entity for {key}"
    options = er.async_get(hass).async_get(entity_id).options
    return options.get("sensor", {}).get("unit_of_measurement")


class TestForcingAUnit:
    async def test_distance_is_pinned(
        self, hass: HomeAssistant, doubles: Doubles
    ) -> None:
        entry = await set_up(hass, {"distance_unit": "km"})
        assert unit_for(hass, "odometer") == "km"
        assert entry.options["distance_unit"] == "km"

    async def test_pressure_is_pinned(
        self, hass: HomeAssistant, doubles: Doubles
    ) -> None:
        await set_up(hass, {"pressure_unit": "psi"})
        assert unit_for(hass, "tyre_pressure_fl") == "psi"

    async def test_both_at_once(self, hass: HomeAssistant, doubles: Doubles) -> None:
        await set_up(hass, {"distance_unit": "miles", "pressure_unit": "bar"})
        assert unit_for(hass, "odometer") == "mi"  # UnitOfLength.MILES
        assert unit_for(hass, "tyre_pressure_fl") == "bar"

    async def test_a_pressure_choice_leaves_distance_alone(
        self, hass: HomeAssistant, doubles: Doubles
    ) -> None:
        await set_up(hass, {"pressure_unit": "psi"})
        assert unit_for(hass, "odometer") is None


class TestPuttingItBack:
    async def test_choosing_the_default_again_removes_the_override(
        self, hass: HomeAssistant, doubles: Doubles
    ) -> None:
        # The failure this guards: the registry keeps the old unit, the dialog
        # says "Use Home Assistant default", and the two disagree forever.
        entry = await set_up(hass, {"distance_unit": "km"})
        assert unit_for(hass, "odometer") == "km"

        hass.config_entries.async_update_entry(
            entry, options={"distance_unit": "default"}
        )
        # The options flow reloads for you; changing the entry directly does
        # not, and it is setup that applies the override.
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

        assert unit_for(hass, "odometer") is None

    async def test_no_options_at_all_writes_nothing(
        self, hass: HomeAssistant, doubles: Doubles
    ) -> None:
        await set_up(hass, {})
        assert unit_for(hass, "odometer") is None

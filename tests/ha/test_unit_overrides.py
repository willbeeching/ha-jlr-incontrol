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

from custom_components.jlr_incontrol.const import (  # noqa: E402
    CONF_APPLIED_UNITS,
    DOMAIN,
)

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


def entity_id_for(hass: HomeAssistant, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{KEPT}_{key}"
    )
    assert entity_id, f"no entity for {key}"
    return entity_id


def pick_by_hand(hass: HomeAssistant, key: str, unit: str) -> None:
    """What the entity's own settings dialog does with a unit."""
    er.async_get(hass).async_update_entity_options(
        entity_id_for(hass, key), "sensor", {"unit_of_measurement": unit}
    )


async def restart(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()


class TestAUnitPickedByHand:
    """#40: a unit chosen in the entity's own settings went back on restart.

    With the option on the default, every setup removed whatever unit each
    distance and pressure sensor had been given, unable to tell one this
    integration forced from one the user chose. Every restart and update put
    the odometer back to miles.
    """

    async def test_it_survives_a_restart(
        self, hass: HomeAssistant, doubles: Doubles
    ) -> None:
        entry = await set_up(hass, {})
        pick_by_hand(hass, "odometer", "km")

        await restart(hass, entry)

        assert unit_for(hass, "odometer") == "km"

    async def test_it_survives_with_a_unit_forced_for_the_other_kind(
        self, hass: HomeAssistant, doubles: Doubles
    ) -> None:
        entry = await set_up(hass, {"pressure_unit": "psi"})
        pick_by_hand(hass, "odometer", "km")

        await restart(hass, entry)

        assert unit_for(hass, "odometer") == "km"
        assert unit_for(hass, "tyre_pressure_fl") == "psi"

    async def test_a_forced_unit_does_not_overwrite_it_again_on_restart(
        self, hass: HomeAssistant, doubles: Doubles
    ) -> None:
        # Forcing applies when the option is chosen. After that a sensor the
        # user has set differently is theirs, restart or not.
        entry = await set_up(hass, {"distance_unit": "km"})
        pick_by_hand(hass, "odometer", "mi")

        await restart(hass, entry)

        assert unit_for(hass, "odometer") == "mi"

    async def test_going_back_to_the_default_leaves_it_alone(
        self, hass: HomeAssistant, doubles: Doubles
    ) -> None:
        # Only the unit this integration wrote is taken back out.
        entry = await set_up(hass, {"distance_unit": "km"})
        pick_by_hand(hass, "odometer", "mi")

        hass.config_entries.async_update_entry(
            entry, options={"distance_unit": "default"}
        )
        await restart(hass, entry)

        assert unit_for(hass, "odometer") == "mi"


class TestWhatItRemembers:
    async def test_the_forced_unit_is_recorded(
        self, hass: HomeAssistant, doubles: Doubles
    ) -> None:
        entry = await set_up(hass, {"distance_unit": "km", "pressure_unit": "psi"})
        assert entry.data[CONF_APPLIED_UNITS] == {"distance": "km", "pressure": "psi"}

    async def test_the_default_records_nothing(
        self, hass: HomeAssistant, doubles: Doubles
    ) -> None:
        entry = await set_up(hass, {"distance_unit": "km"})
        hass.config_entries.async_update_entry(
            entry, options={"distance_unit": "default"}
        )
        await restart(hass, entry)
        assert entry.data[CONF_APPLIED_UNITS] == {}


class TestTheDefaultMeansHomeAssistants:
    """Every distance sensor used to suggest miles, whatever the unit system.

    So "Use Home Assistant default" meant miles on a metric install. It now
    means what it says for sensors created from here on; ones that already
    exist keep the unit they were created with.
    """

    def unit_shown(self, hass: HomeAssistant, key: str) -> Any:
        state = hass.states.get(entity_id_for(hass, key))
        return state.attributes.get("unit_of_measurement")

    async def test_a_metric_install_gets_kilometres(
        self, hass: HomeAssistant, doubles: Doubles
    ) -> None:
        from homeassistant.util.unit_system import METRIC_SYSTEM

        hass.config.units = METRIC_SYSTEM
        await set_up(hass, {})
        assert self.unit_shown(hass, "odometer") == "km"

    async def test_a_us_customary_install_gets_miles(
        self, hass: HomeAssistant, doubles: Doubles
    ) -> None:
        from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

        hass.config.units = US_CUSTOMARY_SYSTEM
        await set_up(hass, {})
        assert self.unit_shown(hass, "odometer") == "mi"

    async def test_an_existing_sensor_keeps_its_miles(
        self, hass: HomeAssistant, doubles: Doubles
    ) -> None:
        # Created by an earlier release, which stored miles as the suggested
        # unit. Core keeps that, so nobody's odometer changes on upgrade.
        from homeassistant.util.unit_system import METRIC_SYSTEM

        hass.config.units = METRIC_SYSTEM
        entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="a-user")
        entry.add_to_hass(hass)
        registry = er.async_get(hass)
        created = registry.async_get_or_create(
            "sensor", DOMAIN, f"{KEPT}_odometer", config_entry=entry
        )
        registry.async_update_entity_options(
            created.entity_id,
            "sensor.private",
            {"suggested_unit_of_measurement": "mi"},
        )

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert self.unit_shown(hass, "odometer") == "mi"

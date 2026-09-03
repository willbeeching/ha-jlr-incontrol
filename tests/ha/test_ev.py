"""An electric car, which produces a different set of entities entirely.

Nothing in the EV half of this integration was exercised, because the vehicle
in the other tests is a diesel. That matters more than the line count suggests:
telling a BEV from a plug-in hybrid from an ICE car is done by heuristic, since
ICE cars report several EV_* keys with UNKNOWN sentinels and would otherwise
grow phantom charging sensors.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

import doubles  # noqa: E402
from doubles import KEPT, Doubles, FakeClient  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol.entity import (  # noqa: E402
    is_electric,
    is_electrified,
)

EV_STATUS = {
    "EV_STATE_OF_CHARGE": "80",
    "EV_RANGE_ON_BATTERY_MILES": "210",
    "EV_MINUTES_TO_FULLY_CHARGED": "45",
    "EV_CHARGING_STATUS": "CHARGING",
    "EV_CHARGING_METHOD": "WIRED",
    "EV_CHARGE_NOW_SETTING": "FORCE_ON",
    "EV_PRECONDITION_REMAINING_RUNTIME_MINUTES": "12",
    "ODOMETER": "123456",
    "LAST_UPDATED_TIME": "2026-08-26T08:00:00.000Z",
}


@pytest.fixture
def electric(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the account's car an I-PACE rather than a diesel.

    The status has to be in place before setup, not pushed afterwards: a
    vehicle's entities are built from its first snapshot and it is not
    reconsidered once it has produced any, so a key that turns up later gains
    no entity until the integration is reloaded.
    """

    async def attributes(self: FakeClient, vin: str) -> dict[str, Any]:
        return {
            "vehicleBrand": "Jaguar",
            "fuelType": "Electric",
            "nickname": "Test Car",
        }

    monkeypatch.setattr(FakeClient, "async_get_attributes", attributes)
    monkeypatch.setattr(doubles, "STATUS", EV_STATUS)


@pytest.fixture
async def charging(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    electric: None,
    doubles: Doubles,
) -> Doubles:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return doubles


def states(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, str]:
    return {
        item.entity_id: hass.states.get(item.entity_id).state
        for item in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        if item.disabled_by is None and hass.states.get(item.entity_id)
    }


class TestTellingTheDrivetrainsApart:
    def test_a_bev_is_electric(self) -> None:
        assert is_electric({"fuelType": "Electric"})

    def test_a_hybrid_is_not_a_bev(self) -> None:
        assert not is_electric({"fuelType": "Hybrid Electric"})

    def test_a_hybrid_still_has_a_charge_port(self) -> None:
        assert is_electrified({"fuelType": "Hybrid Electric"}, {})

    def test_a_diesel_has_none(self) -> None:
        # Even though it reports EV_* keys with UNKNOWN sentinels, which is
        # exactly how phantom charging entities used to appear.
        assert not is_electrified(
            {"fuelType": "Diesel"}, {"EV_CHARGING_STATUS": "UNKNOWN"}
        )

    def test_an_unfamiliar_fuel_type_falls_back_to_the_battery_key(self) -> None:
        # EV_STATE_OF_CHARGE is the one key verified absent on ICE cars.
        assert is_electrified({"fuelType": "Something New"}, EV_STATUS)
        assert not is_electrified({"fuelType": "Something New"}, {"ODOMETER": "1"})


class TestAnElectricCarsEntities:
    async def test_the_battery_and_range_appear(
        self, hass: HomeAssistant, entry: MockConfigEntry, charging: Doubles
    ) -> None:
        live = states(hass, entry)
        assert any("battery" in name for name in live)
        assert any("range" in name for name in live)

    async def test_the_charging_status_is_a_translated_option(
        self, hass: HomeAssistant, entry: MockConfigEntry, charging: Doubles
    ) -> None:
        # Not the raw API word: the state is the slug, and strings.json does
        # the wording.
        live = states(hass, entry)
        charging_states = [
            value for name, value in live.items() if "charging_status" in name
        ]
        assert charging_states == ["charging"]

    async def test_a_diesel_grows_none_of_them(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        live = states(hass, entry)
        assert not any("charging_status" in name for name in live)


class TestWhatTheDocsPromiseAboutEvEntities:
    """Two are documented as off by default. Checked through the registry,
    because Home Assistant turns _attr_ class attributes into properties and
    reading them off the class tells you nothing."""

    def disabled(self, hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
        return {
            item.unique_id
            for item in er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
            if item.disabled_by is not None
        }

    async def test_the_evcc_connector_letter_is_off(
        self, hass: HomeAssistant, entry: MockConfigEntry, charging: Doubles
    ) -> None:
        # For wallbox controllers to switch on, not for a dashboard.
        assert f"{KEPT}_evcc_status" in self.disabled(hass, entry)

    async def test_the_charge_now_override_is_off(
        self, hass: HomeAssistant, entry: MockConfigEntry, charging: Doubles
    ) -> None:
        assert f"{KEPT}_charge_now_setting" in self.disabled(hass, entry)

    async def test_the_battery_percentage_is_not(
        self, hass: HomeAssistant, entry: MockConfigEntry, charging: Doubles
    ) -> None:
        # It is the whole point of an EV showing up in Home Assistant.
        assert f"{KEPT}_ev_battery" not in self.disabled(hass, entry)

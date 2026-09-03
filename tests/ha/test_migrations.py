"""Entities from earlier versions that must not be left behind.

Every one of these was created by a version of this integration that has since
learned better. Left in the registry they sit permanently unavailable, which
reads as a fault rather than as something that was removed on purpose.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT, SOLD, Doubles  # noqa: E402
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


class TestEntitiesTheCarStoppedReporting:
    """The general case the hand-maintained removal lists could never cover.

    Every one of these entities is gated on a status key. A car that does not
    report the key — a petrol Defender and its diesel-only AdBlue pair, seen
    live — kept whatever an earlier, less careful version had already created.
    """

    async def test_a_sensor_for_a_key_the_car_never_sends_goes(
        self, hass: HomeAssistant, entry: MockConfigEntry, doubles: Doubles
    ) -> None:
        stale = plant(hass, entry, "sensor", "adblue_range")
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert not survives(hass, stale)

    async def test_a_binary_sensor_for_a_key_the_car_never_sends_goes(
        self, hass: HomeAssistant, entry: MockConfigEntry, doubles: Doubles
    ) -> None:
        stale = plant(hass, entry, "binary_sensor", "adblue_warning")
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert not survives(hass, stale)

    async def test_a_sensor_the_car_does_send_survives(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        assert er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"{KEPT}_odometer"
        )

    async def test_another_domain_is_not_swept_up(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # Nothing on the sensor platform is called "doors", so a prune that
        # forgot to filter by domain would take the binary sensor with it.
        assert er.async_get(hass).async_get_entity_id(
            "binary_sensor", DOMAIN, f"{KEPT}_doors_locked"
        )

    async def test_a_disabled_entity_is_not_mistaken_for_an_orphan(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # All info is created disabled, so it has a registry entry and no
        # state. Judging "did we build this?" by the state machine would
        # delete it on every restart.
        registered = er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"{KEPT}_all_info"
        )
        assert registered
        assert er.async_get(hass).async_get(registered).disabled

    async def test_a_vehicle_that_is_not_on_the_account_is_left_alone(
        self, hass: HomeAssistant, entry: MockConfigEntry, doubles: Doubles
    ) -> None:
        # Deciding a car has no AdBlue sensor requires having read its status.
        # A VIN the account no longer lists is a car nothing is known about,
        # and its history is not this code's to delete.
        stale = (
            er.async_get(hass)
            .async_get_or_create(
                "sensor", DOMAIN, f"{SOLD}_odometer", config_entry=entry
            )
            .entity_id
        )
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert survives(hass, stale)

    async def test_a_car_that_has_not_reported_yet_keeps_everything(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        doubles: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A silent car reports no status keys, which is not the same thing.

        This is the case that makes the whole approach safe or not: a flat 12V
        or no signal must read as "not known yet" rather than "has none of
        them", or one bad night deletes a car's history.
        """
        from doubles import FakeClient, FakeTelemetry

        async def vehicles(self: FakeClient) -> list[dict[str, Any]]:
            return [{"vin": KEPT, "role": "Owner"}, {"vin": SOLD, "role": "Owner"}]

        async def only_one_answers(self: FakeTelemetry) -> None:
            self.connected = True
            self.on_connected(True)
            self.push(KEPT)

        monkeypatch.setattr(FakeClient, "async_get_vehicles", vehicles)
        monkeypatch.setattr(FakeTelemetry, "async_start", only_one_answers)
        # Setup genuinely waits this out, in real seconds.
        monkeypatch.setattr(
            "custom_components.jlr_incontrol.coordinator.FIRST_SNAPSHOT_TIMEOUT", 0.05
        )

        silent = (
            er.async_get(hass)
            .async_get_or_create(
                "sensor", DOMAIN, f"{SOLD}_adblue_range", config_entry=entry
            )
            .entity_id
        )
        talkative = plant(hass, entry, "sensor", "adblue_range")

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert survives(hass, silent)
        assert not survives(hass, talkative)

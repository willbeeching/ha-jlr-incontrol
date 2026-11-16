"""Entities appear when their vehicle does, and go unavailable honestly.

Two bugs live here. Platforms used to run exactly once at setup, so a car
added to the account afterwards had its data arrive for entities that had
never been created. And availability used to be one flag for everything, so a
failing status read hid a location fetched successfully minutes earlier.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT, SOLD, Doubles, device_for  # noqa: E402
from homeassistant.const import STATE_UNAVAILABLE  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol import (  # noqa: E402
    async_remove_config_entry_device,
)


def entity_ids(hass: HomeAssistant, entry: MockConfigEntry, domain: str) -> list[str]:
    """Registered entities of one domain that are actually switched on.

    Disabled-by-default entities are in the registry but have no state, so a
    test that read straight from the registry would ask the state machine for
    something that was never going to be there.
    """
    return sorted(
        item.entity_id
        for item in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        if item.domain == domain and item.disabled_by is None
    )


class TestVehiclesArrivingLater:
    async def test_a_car_added_after_setup_gets_entities(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        before = entity_ids(hass, entry, "sensor")

        # Added to the client at data.jlr.com: the link's next renewal lists
        # it, and no reload follows.
        loaded.link.vehicles.append(SOLD)
        await entry.runtime_data.async_refresh()
        loaded.link.push(SOLD)
        await hass.async_block_till_done()

        after = entity_ids(hass, entry, "sensor")
        assert len(after) > len(before), "the new car created no entities"
        assert device_for(hass, entry, SOLD)


class TestAvailability:
    async def test_a_stream_that_drops_leaves_the_readings_alone(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # The broker ends every session at an hour and the stream reconnects.
        # While status reads are recent the readings stand.
        loaded.link.connected = False
        entry.runtime_data.async_update_listeners()
        await hass.async_block_till_done()

        for entity_id in entity_ids(hass, entry, "sensor"):
            assert hass.states.get(entity_id).state != STATE_UNAVAILABLE

    async def test_a_link_that_stops_vouching_makes_status_unavailable(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # The housekeeping keeps succeeding while status reads fail, so the
        # coordinator's own success flag says nothing useful here.
        loaded.link.drop()
        entry.runtime_data.async_update_listeners()
        await hass.async_block_till_done()

        for entity_id in entity_ids(hass, entry, "sensor"):
            if entity_id.endswith("_last_report_data_hub"):
                # About the link, not a reading: it says when the car last
                # reported, which is exactly what someone wants to see now.
                continue
            assert hass.states.get(entity_id).state == STATE_UNAVAILABLE, entity_id

    async def test_but_not_the_location(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # Its honesty signal is the age of the fix, reported separately.
        loaded.link.drop()
        entry.runtime_data.async_update_listeners()
        await hass.async_block_till_done()

        trackers = entity_ids(hass, entry, "device_tracker")
        assert trackers
        for entity_id in trackers:
            assert hass.states.get(entity_id).state != STATE_UNAVAILABLE

    async def test_a_healthy_link_leaves_everything_available(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        for entity_id in entity_ids(hass, entry, "sensor"):
            assert hass.states.get(entity_id).state != STATE_UNAVAILABLE


class TestDeletingASoldVehicle:
    async def test_a_vehicle_still_on_the_account_cannot_be_deleted(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        device = device_for(hass, entry, KEPT)
        assert not await async_remove_config_entry_device(hass, entry, device)

    async def test_a_vehicle_the_account_has_lost_can_be_deleted(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        device = device_for(hass, entry, KEPT)
        loaded.link.vehicles.clear()
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

        assert await async_remove_config_entry_device(hass, entry, device)

    async def test_a_link_jlr_refuse_is_not_evidence_a_car_is_gone(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        device = device_for(hass, entry, KEPT)
        loaded.link.signed_out = True
        loaded.link.vehicles.clear()
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

        assert not await async_remove_config_entry_device(hass, entry, device)


class TestTheTrackerWithholdsWhatItCannotVouchFor:
    """No coordinates at all, rather than old ones.

    Home Assistant derives the zone from the coordinates, so handing back a
    fix nobody can vouch for does not produce a cautious answer — it produces
    a confident wrong one, and a car miles away reads as "home".
    """

    def tracker(self, **vehicle: Any) -> Any:
        from custom_components.jlr_incontrol.device_tracker import (
            JlrDeviceTracker,
        )

        made = JlrDeviceTracker.__new__(JlrDeviceTracker)
        made._vin = KEPT
        made.coordinator = type("C", (), {"data": {"vehicles": {KEPT: vehicle}}})()
        return made

    def test_an_untrusted_fix_gives_neither_coordinate(self) -> None:
        made = self.tracker(
            position={"latitude": 51.5, "longitude": -0.1},
            position_trusted=False,
        )
        assert made.latitude is None
        assert made.longitude is None

    def test_a_trusted_one_gives_both(self) -> None:
        made = self.tracker(
            position={"latitude": 51.5, "longitude": -0.1},
            position_trusted=True,
        )
        assert made.latitude == 51.5
        assert made.longitude == -0.1

    def test_a_coordinate_that_is_not_a_number_is_not_reported(self) -> None:
        # A location that answers with something unexpected must not put the
        # car at zero degrees north.
        made = self.tracker(
            position={"latitude": "somewhere", "longitude": None},
            position_trusted=True,
        )
        assert made.latitude is None
        assert made.longitude is None

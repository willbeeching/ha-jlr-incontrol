"""Entities appear when their vehicle does, and go unavailable honestly.

Two bugs live here. Platforms used to run exactly once at setup, so a car
added to the account afterwards was subscribed to telemetry and its data
arrived for entities that had never been created. And availability used to be
one flag for everything, so a dead socket hid a location the owner portal had
fetched successfully minutes earlier.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT, SOLD, Doubles  # noqa: E402
from homeassistant.const import STATE_UNAVAILABLE  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import device_registry as dr  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol import (  # noqa: E402
    async_remove_config_entry_device,
)
from custom_components.jlr_incontrol.const import (  # noqa: E402
    DOMAIN,
    TELEMETRY_GRACE,
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

        loaded.client.vehicles.append({"vin": SOLD, "role": "Owner"})
        await entry.runtime_data.async_refresh()
        loaded.telemetry.push(SOLD)
        await hass.async_block_till_done()

        after = entity_ids(hass, entry, "sensor")
        assert len(after) > len(before), "the new car created no entities"
        assert dr.async_get(hass).async_get_device(identifiers={(DOMAIN, SOLD)})

    async def test_it_takes_no_reload_to_see_them(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        loaded.client.vehicles.append({"vin": SOLD, "role": "Owner"})
        await entry.runtime_data.async_refresh()
        loaded.telemetry.push(SOLD)
        await hass.async_block_till_done()

        assert loaded.telemetry.vins == sorted([KEPT, SOLD])


class TestAvailability:
    async def test_a_momentary_drop_does_not_flap_everything(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # The STOMP session is bound to a short-lived token, so it reconnects
        # every few minutes as a matter of course. Marking the house
        # unavailable each time would be noise, not honesty.
        loaded.telemetry.drop()
        await hass.async_block_till_done()

        for entity_id in entity_ids(hass, entry, "sensor"):
            assert hass.states.get(entity_id).state != STATE_UNAVAILABLE

    async def test_a_drop_that_outlasts_the_grace_makes_status_unavailable(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles, freezer
    ) -> None:
        # The housekeeping poll keeps succeeding long after the socket dies,
        # so the coordinator's own success flag says nothing useful here.
        loaded.telemetry.drop()
        freezer.tick(TELEMETRY_GRACE + timedelta(minutes=1))
        # Something has to make the entities look again; nothing is scheduled
        # to re-evaluate availability on its own.
        entry.runtime_data.async_update_listeners()
        await hass.async_block_till_done()

        for entity_id in entity_ids(hass, entry, "sensor"):
            assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    async def test_a_dead_socket_does_not_hide_the_location(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles, freezer
    ) -> None:
        # Location comes from the owner portal, which is still working. Its
        # honesty signal is the age of the fix, reported separately.
        loaded.telemetry.drop()
        freezer.tick(TELEMETRY_GRACE + timedelta(minutes=1))
        entry.runtime_data.async_update_listeners()
        await hass.async_block_till_done()

        trackers = entity_ids(hass, entry, "device_tracker")
        assert trackers
        for entity_id in trackers:
            assert hass.states.get(entity_id).state != STATE_UNAVAILABLE

    async def test_a_live_socket_leaves_everything_available(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        for entity_id in entity_ids(hass, entry, "sensor"):
            assert hass.states.get(entity_id).state != STATE_UNAVAILABLE


class TestDeletingASoldVehicle:
    async def test_a_vehicle_still_on_the_account_cannot_be_deleted(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, KEPT)})
        assert not await async_remove_config_entry_device(hass, entry, device)

    async def test_a_vehicle_the_account_has_lost_can_be_deleted(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, KEPT)})
        loaded.client.vehicles.clear()
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

        assert await async_remove_config_entry_device(hass, entry, device)

    async def test_a_failed_listing_is_not_evidence_a_car_is_gone(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        from custom_components.jlr_incontrol.api import JlrApiError

        device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, KEPT)})
        loaded.client.connect_error = JlrApiError("Jaguar Land Rover returned 503")
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

        assert not await async_remove_config_entry_device(hass, entry, device)

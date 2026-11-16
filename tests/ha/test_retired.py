"""Entries from the InControl sign-in, after 3.0.0 removed it.

Two things have to hold. Every credential that sign-in kept, the password
among them, is removed from the entry on upgrade. And an entry that had only
that sign-in keeps its devices, entities and history while it waits for the
owner to link the Data Hub, then picks them up again once it is linked.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from conftest import DATAHUB  # noqa: E402
from doubles import KEPT, Doubles, device_for  # noqa: E402
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from homeassistant.helpers import device_registry as dr  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.helpers import issue_registry as ir  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol import (  # noqa: E402
    async_remove_config_entry_device,
)
from custom_components.jlr_incontrol.const import DOMAIN  # noqa: E402
from custom_components.jlr_incontrol.diagnostics import (  # noqa: E402
    async_get_config_entry_diagnostics,
)

# What a 2.x entry on the InControl sign-in held. Made-up values.
SIGNED_IN = {
    "username": "someone@example.com",
    "password": "hunter2-and-then-some",
    "device_id": "3f2c9a71-5d84-4c1e-9a30-6b7d8e5f4a21",
    "user_id": "user-01H8XK4Q2N",
    "refresh_token": "a-refresh-token",
    "sso_cookies": {"iPlanetDirectoryPro": "a-cookie"},
    "portal_cookies": {"JSESSIONID": "a-session"},
    "portal_base": "https://incontrol.jaguar.com/jaguar-portal-owner-web",
    "portal_minted": "2026-11-01T00:00:00+00:00",
    "attributes": {KEPT: {"nickname": "Test Car", "vehicleBrand": "Jaguar"}},
}
INCONTROL_KEYS = set(SIGNED_IN) - {"username", "attributes"}


def old_entry(hass: HomeAssistant, **data: object) -> MockConfigEntry:
    created = MockConfigEntry(
        domain=DOMAIN,
        title="someone@example.com",
        data={**SIGNED_IN, **data},
        unique_id="user-01H8XK4Q2N",
        version=1,
        minor_version=1,
    )
    created.add_to_hass(hass)
    return created


def with_a_car(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """The device and one entity a 2.x install had registered for its car."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, KEPT)},
        manufacturer="Jaguar",
        name="Test Car",
    )
    return (
        er.async_get(hass)
        .async_get_or_create(
            "sensor",
            DOMAIN,
            f"{KEPT}_fuel_level",
            config_entry=entry,
            device_id=device.id,
        )
        .entity_id
    )


class TestTheMigration:
    async def test_a_linked_entry_loses_only_the_sign_in(
        self, hass: HomeAssistant, doubles: Doubles
    ) -> None:
        entry = old_entry(hass, datahub=dict(DATAHUB))
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.minor_version == 2
        assert not INCONTROL_KEYS & set(entry.data)
        assert entry.data["datahub"]["client_id"] == DATAHUB["client_id"]
        assert entry.data["username"] == "someone@example.com"
        # The nickname read from the owner portal is kept with the details.
        attributes = entry.runtime_data.data["vehicles"][KEPT]["attributes"]
        assert attributes["nickname"] == "Test Car"
        assert entry.state is ConfigEntryState.LOADED

    async def test_an_entry_on_the_sign_in_alone_loses_it_too(
        self, hass: HomeAssistant, doubles: Doubles
    ) -> None:
        entry = old_entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert not INCONTROL_KEYS & set(entry.data)
        assert "hunter2-and-then-some" not in repr(entry.data)

    async def test_the_old_portal_prompt_goes_with_it(
        self, hass: HomeAssistant, doubles: Doubles
    ) -> None:
        entry = old_entry(hass, datahub=dict(DATAHUB))
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"portal_signed_out_{entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="datahub_signed_out",
        )
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        issues = ir.async_get(hass)
        assert (
            issues.async_get_issue(DOMAIN, f"portal_signed_out_{entry.entry_id}")
            is None
        )

    async def test_an_entry_from_a_newer_version_is_left_alone(
        self, hass: HomeAssistant, doubles: Doubles
    ) -> None:
        entry = MockConfigEntry(
            domain=DOMAIN, data={"datahub": dict(DATAHUB)}, version=2
        )
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.MIGRATION_ERROR


class TestWaitingForTheDataHub:
    @pytest.fixture
    async def waiting(
        self, hass: HomeAssistant, doubles: Doubles
    ) -> tuple[MockConfigEntry, str]:
        entry = old_entry(hass)
        entity_id = with_a_car(hass, entry)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry, entity_id

    async def test_it_loads_reading_nothing(
        self, hass: HomeAssistant, waiting: tuple[MockConfigEntry, str]
    ) -> None:
        entry, _ = waiting
        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data.dormant
        assert entry.runtime_data.data["vehicles"] == {}

    async def test_it_asks_for_the_data_hub(
        self, hass: HomeAssistant, waiting: tuple[MockConfigEntry, str]
    ) -> None:
        entry, _ = waiting
        issue = ir.async_get(hass).async_get_issue(
            DOMAIN, f"incontrol_retired_{entry.entry_id}"
        )
        assert issue is not None
        assert issue.severity is ir.IssueSeverity.ERROR
        assert issue.translation_placeholders == {"title": entry.title}

    async def test_its_entities_and_device_are_kept(
        self, hass: HomeAssistant, waiting: tuple[MockConfigEntry, str]
    ) -> None:
        entry, entity_id = waiting
        assert er.async_get(hass).async_get(entity_id) is not None
        device = device_for(hass, entry, KEPT)
        assert device is not None
        # And cannot be deleted from under it: it lists no cars because it
        # reads nothing yet, not because the car has gone.
        assert not await async_remove_config_entry_device(hass, entry, device)

    async def test_configure_offers_the_link(
        self, hass: HomeAssistant, waiting: tuple[MockConfigEntry, str]
    ) -> None:
        entry, _ = waiting
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.MENU
        assert result["menu_options"] == ["settings", "datahub"]

    async def test_diagnostics_say_what_it_is_waiting_for(
        self, hass: HomeAssistant, waiting: tuple[MockConfigEntry, str]
    ) -> None:
        entry, _ = waiting
        report = await async_get_config_entry_diagnostics(hass, entry)
        assert report["source"]["waiting_for_datahub"] is True
        assert report["datahub"] is None

    async def test_linking_brings_everything_back(
        self, hass: HomeAssistant, waiting: tuple[MockConfigEntry, str]
    ) -> None:
        # What the options' linking steps write, then the reload they ask for.
        entry, entity_id = waiting
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "datahub": dict(DATAHUB)}
        )
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert not entry.runtime_data.dormant
        # The same entity, so its history carries on.
        assert hass.states.get(entity_id) is not None
        assert float(hass.states.get(entity_id).state) == 42
        assert (
            ir.async_get(hass).async_get_issue(
                DOMAIN, f"incontrol_retired_{entry.entry_id}"
            )
            is None
        )

    async def test_removing_the_entry_takes_the_repair_with_it(
        self, hass: HomeAssistant, waiting: tuple[MockConfigEntry, str]
    ) -> None:
        entry, _ = waiting
        assert await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
        assert (
            ir.async_get(hass).async_get_issue(
                DOMAIN, f"incontrol_retired_{entry.entry_id}"
            )
            is None
        )

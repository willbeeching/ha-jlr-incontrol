"""The unit options, and the reload they trigger.

Small surface, two traps. The dialog reloads the whole integration when it
closes, and this integration cannot afford a gratuitous reload: the refresh
token rotates every few minutes and an entry write used to take the whole
thing down and back up each time. And the stored values are what existing
entries already hold, so they cannot be renamed to make the dialog tidier.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import Doubles  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)


async def open_options(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    """Open the dialog and go to the settings, past the menu it starts on."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    await hass.async_block_till_done()
    return result


class TestTheDialog:
    async def test_it_opens(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.MENU
        assert result["step_id"] == "init"
        # Linking again is offered only when the link has stopped or its
        # certificate is running out; this one is working.
        assert result["menu_options"] == ["settings"]

    async def test_the_settings_are_one_step_in(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        result = await open_options(hass, entry)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "settings"

    async def test_a_choice_is_stored(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        result = await open_options(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"distance_unit": "km", "pressure_unit": "bar"}
        )
        await hass.async_block_till_done()
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options["distance_unit"] == "km"
        assert entry.options["pressure_unit"] == "bar"

    async def test_the_stored_values_are_the_slugs_not_the_labels(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # Existing entries hold these. Renaming them to something prettier
        # would silently reset everyone's units to the default.
        result = await open_options(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"distance_unit": "miles", "pressure_unit": "psi"}
        )
        await hass.async_block_till_done()
        # Exact, not a subset: this is the test that would catch a key being
        # renamed. unsettled_minutes arrives at its default because the form
        # carries it whether or not the submission mentioned it, and it is
        # stored as the string the selector validates.
        assert entry.options == {
            "distance_unit": "miles",
            "pressure_unit": "psi",
            "unsettled_minutes": "30",
        }

    async def test_it_reopens_showing_what_was_chosen(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        result = await open_options(hass, entry)
        await hass.config_entries.options.async_configure(
            result["flow_id"], {"distance_unit": "km", "pressure_unit": "kpa"}
        )
        await hass.async_block_till_done()

        again = await open_options(hass, entry)
        assert again["type"] is FlowResultType.FORM

    async def test_the_integration_survives_the_reload_it_causes(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # OptionsFlowWithReload reloads the entry when the dialog closes. An
        # update listener of our own on top would reload twice — and a
        # listener that fires on any entry write turns the rotating refresh
        # token into a teardown every few minutes. That happened once.
        result = await open_options(hass, entry)
        await hass.config_entries.options.async_configure(
            result["flow_id"], {"distance_unit": "km", "pressure_unit": "bar"}
        )
        await hass.async_block_till_done()
        assert entry.runtime_data.data["vehicles"]

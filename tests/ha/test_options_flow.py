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

from custom_components.jlr_incontrol.config_flow import (  # noqa: E402
    _login_error_code,
)


async def open_options(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.async_block_till_done()
    return result


class TestTheDialog:
    async def test_it_opens(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        result = await open_options(hass, entry)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "init"

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
        assert entry.options == {"distance_unit": "miles", "pressure_unit": "psi"}

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


class TestWordingASignInFailure:
    """Which of three explanations the user gets, from JLR's error text."""

    @pytest.mark.parametrize(
        "text",
        [
            "the password was rejected",
            "Authentication Failed: bad credential",
            "REJECTED by the identity provider",
        ],
    )
    def test_a_credentials_problem_says_so(self, text: str) -> None:
        from custom_components.jlr_incontrol.auth import JlrLoginError

        assert _login_error_code(JlrLoginError(text)) == "invalid_auth"

    @pytest.mark.parametrize(
        "text",
        [
            "this account does not support the journey",
            "the journey has changed shape",
        ],
    )
    def test_a_changed_journey_says_so(self, text: str) -> None:
        # Distinct on purpose: nothing the user types will fix it, and it is
        # the signal that this integration needs updating.
        from custom_components.jlr_incontrol.auth import JlrLoginError

        assert _login_error_code(JlrLoginError(text)) == "unsupported_journey"

    def test_anything_else_is_treated_as_reachability(self) -> None:
        from custom_components.jlr_incontrol.auth import JlrLoginError

        assert _login_error_code(JlrLoginError("connection reset")) == "cannot_connect"

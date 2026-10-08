"""Every form the integration shows can be drawn by the core it runs on.

The flow tests drive the steps from Python and never turn a schema into what
the frontend receives, which is the step that changed under them: core 2026.10
moved from voluptuous to probatio and serialises forms with probatio's own
converter. A schema that converter could not read would pass every other test
and leave the sign-in dialog blank in the browser.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import Doubles  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import config_validation as cv  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol import config_flow  # noqa: E402
from custom_components.jlr_incontrol.const import DOMAIN  # noqa: E402

try:  # Home Assistant 2026.10 onwards
    from probatio import to_field_list

    def render(schema: Any) -> list[dict[str, Any]]:
        return to_field_list(schema, custom_serializer=cv.custom_serializer)

except ImportError:  # the cores before it
    import voluptuous_serialize

    def render(schema: Any) -> list[dict[str, Any]]:
        return voluptuous_serialize.convert(
            schema, custom_serializer=cv.custom_serializer
        )


@pytest.mark.parametrize(
    "schema",
    ["STEP_USER_SCHEMA", "STEP_REAUTH_SCHEMA", "STEP_CODE_SCHEMA", "OPTIONS_SCHEMA"],
)
def test_each_schema_converts(schema: str) -> None:
    fields = render(getattr(config_flow, schema))
    assert fields, f"{schema} rendered as an empty form"


async def test_the_sign_in_form_as_the_frontend_gets_it(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    names = {field["name"] for field in render(result["data_schema"])}
    assert names == {"username", "password"}


async def test_the_options_form_with_its_current_values(
    hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
) -> None:
    # Goes through add_suggested_values_to_schema, which on 2026.10 is core's
    # probatio code copying a schema this integration built.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fields = render(result["data_schema"])
    assert len(fields) == len(config_flow.OPTIONS_SCHEMA.schema)
    assert all("suggested_value" in field.get("description", {}) for field in fields)

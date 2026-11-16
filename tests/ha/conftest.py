"""Fixtures that set the integration up against a real Home Assistant.

What is real here is Home Assistant: the config entry, the platforms, both
registries and the issue registry. That is the half where the bugs have been.
The Data Hub link, which talks to JLR, is replaced with the stand-in in
doubles.py; the link itself is tested against a fake JLR client elsewhere.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT, Doubles, FakeLink  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol.const import DOMAIN  # noqa: E402

COORDINATOR = "custom_components.jlr_incontrol.coordinator"


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations: Any) -> None:
    """Let Home Assistant find this repo's custom_components directory."""


# A link as setup leaves it in the entry. Made-up values of the same shape.
DATAHUB = {
    "client_id": "dsas-0123456789ab",
    "private_key": "-----BEGIN PRIVATE KEY-----\nnot used by the fake\n",
    "certificate": "",
    "refresh_token": "offline-1",
    "vehicles": [KEPT],
    "account_id": "acct-1",
}


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """An entry on the Data Hub, as setup has made since 2.0.0."""
    created = MockConfigEntry(
        domain=DOMAIN,
        title="JLR Data Hub",
        data={"datahub": dict(DATAHUB)},
        unique_id="acct-1",
        version=1,
        minor_version=2,
    )
    created.add_to_hass(hass)
    return created


@pytest.fixture
def doubles(monkeypatch: pytest.MonkeyPatch) -> Doubles:
    """Swap the Data Hub link for a fake, and hand it back."""
    handles = Doubles()
    monkeypatch.setattr(f"{COORDINATOR}.DataHubLink", handles.builder("link", FakeLink))
    return handles


@pytest.fixture
async def loaded(
    hass: HomeAssistant, entry: MockConfigEntry, doubles: Doubles
) -> Doubles:
    """A fully set-up integration with one vehicle."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return doubles

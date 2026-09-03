"""Fixtures that set the integration up against a real Home Assistant.

What is real here is Home Assistant: the config entry, the platforms, both
registries and the issue registry. That is the half where the bugs have been.
The three collaborators that talk to JLR over the network are replaced with
the stand-ins in doubles.py.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import (  # noqa: E402
    Doubles,
    FakeClient,
    FakePortal,
    FakeTelemetry,
)
from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol.const import DOMAIN  # noqa: E402

COORDINATOR = "custom_components.jlr_incontrol.coordinator"


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations: Any) -> None:
    """Let Home Assistant find this repo's custom_components directory."""


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    created = MockConfigEntry(
        domain=DOMAIN,
        title="Jaguar Land Rover InControl",
        data={
            "username": "someone@example.com",
            "password": "hunter2-and-then-some",
            "device_id": "3f2c9a71-5d84-4c1e-9a30-6b7d8e5f4a21",
            "user_id": "user-01H8XK4Q2N",
            "refresh_token": "a-refresh-token",
            "sso_cookies": {"iPlanetDirectoryPro": "a-cookie"},
            "portal_cookies": {"JSESSIONID": "a-session"},
            "portal_base": "https://incontrol.jaguar.com/jaguar-portal-owner-web",
        },
        unique_id="someone@example.com",
    )
    created.add_to_hass(hass)
    return created


@pytest.fixture
def doubles(monkeypatch: pytest.MonkeyPatch) -> Doubles:
    """Swap the three network collaborators for fakes, and hand them back."""
    handles = Doubles()
    for attribute, name, cls in (
        ("client", "JlrClient", FakeClient),
        ("telemetry", "JlrTelemetry", FakeTelemetry),
        ("portal", "JlrPortal", FakePortal),
    ):
        monkeypatch.setattr(f"{COORDINATOR}.{name}", handles.builder(attribute, cls))
    return handles


@pytest.fixture
async def loaded(
    hass: HomeAssistant, entry: MockConfigEntry, doubles: Doubles
) -> Doubles:
    """A fully set-up integration with one vehicle."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return doubles

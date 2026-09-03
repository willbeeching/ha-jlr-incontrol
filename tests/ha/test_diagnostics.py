"""A diagnostics download gets pasted into a public GitHub issue.

One already has, carrying a reporter's VIN. Everything leaving here is
scrubbed as a whole structure rather than by a list of field names — the list
was tried, it named ``imei`` and ``serialNumber`` while the payload calls them
``TU_STATUS_IMEI`` and ``TU_STATUS_SERIAL_NUMBER``, and a permanent hardware
identifier shipped in clear in every download for as long as that lasted.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT, REGISTRATION, Doubles  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol.diagnostics import (  # noqa: E402
    async_get_config_entry_diagnostics,
)


async def dump(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """The whole download as text, which is how it reaches an issue."""
    return json.dumps(await async_get_config_entry_diagnostics(hass, entry))


class TestNothingIdentifyingGetsOut:
    @pytest.mark.parametrize(
        "secret",
        [
            KEPT,
            "356938035643809",
            "TU-000-111-222",
            REGISTRATION,
        ],
        ids=["vin", "imei", "tu-serial", "registration"],
    )
    async def test_it_is_not_in_the_download(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        loaded: Doubles,
        secret: str,
    ) -> None:
        assert secret not in await dump(hass, entry)

    async def test_the_vin_is_not_a_key_either(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # The first leak was in key position: last_push was keyed by VIN, so
        # the redaction applied to everything under it never saw them.
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        assert KEPT not in diagnostics["vehicles"]
        assert KEPT not in diagnostics["telemetry"]["last_push"]

    async def test_one_car_is_still_followable_through_the_dump(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # A stable label, not a random one: the point of the download is being
        # able to follow a single vehicle through it.
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        (label,) = diagnostics["vehicles"]
        assert label.startswith("vehicle_")
        assert label in diagnostics["telemetry"]["last_push"]


class TestItIsStillWorthDownloading:
    async def test_it_says_where_the_data_came_from(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # Without this a dump of empty vehicles looks the same whether the
        # socket is down or the car is.
        telemetry = (await async_get_config_entry_diagnostics(hass, entry))["telemetry"]
        assert telemetry["connected"] is True
        assert telemetry["trusted"] is True
        assert telemetry["vehicles_subscribed"] == 1

    async def test_the_status_that_is_not_identifying_survives(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        (vehicle,) = diagnostics["vehicles"].values()
        assert vehicle["status"]["ODOMETER"] == "123456"
        assert vehicle["attributes"]["vehicleBrand"] == "Jaguar"

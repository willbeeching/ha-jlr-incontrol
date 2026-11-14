"""Last-journey sensors, from the Data Hub's journeys, in a running core.

Set up on the Data Hub with the stream parked, as in test_datahub_source.
JLR refused journeys to owners until November 2026, so a car whose journeys
are refused must gain no sensors, and a car whose journeys arrive after the
rest of its sensors must not lose them to the prune on the next restart.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT, Doubles, FakeDataHub  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol import datahub_link  # noqa: E402
from custom_components.jlr_incontrol.const import DOMAIN  # noqa: E402

SETTINGS = {
    "client_id": "dsas-0123456789ab",
    "private_key": "not used by the fake",
    "certificate": "",
    "refresh_token": "offline-1",
    "vehicles": [KEPT],
    "account_id": "acct-1",
}

# Made-up values in the shape JLR send.
ANSWER = {
    "items": [
        {
            "journeyId": "10000001",
            "startTime": "2026-11-08T18:00:00Z",
            "endTime": "2026-11-08T18:30:00Z",
            "duration": 1800,
            "distance": {"value": 32.19, "unit": "km"},
            "averageSpeed": {"value": 64.37, "unit": "km/h"},
            "startLocation": {"latitude": "51.5000", "longitude": "-0.1200"},
            "endLocation": {"latitude": "51.6000", "longitude": "-0.1300"},
        }
    ],
    "page": {"totalElements": 1, "totalPages": 1, "currentPage": 0, "pageSize": 20},
}

SENSORS = (
    "sensor.test_car_last_journey_distance",
    "sensor.test_car_last_journey_duration",
    "sensor.test_car_last_journey_average_speed",
    "sensor.test_car_last_journey_ended",
)


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch, doubles: Doubles) -> Doubles:
    monkeypatch.setattr(
        datahub_link, "DataHubClient", doubles.builder("datahub", FakeDataHub)
    )

    async def parked(**kwargs: Any) -> str:
        await asyncio.Event().wait()
        return "unreachable"

    monkeypatch.setattr(datahub_link, "run_session", parked)
    return doubles


async def set_up(hass: HomeAssistant, entry: MockConfigEntry, **options: Any) -> None:
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "datahub": dict(SETTINGS)}, options=options
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # The journeys read follows the status and location reads in the link's
    # own task, which core does not wait for.
    for _ in range(20):
        if entry.runtime_data.datahub.journey_access:
            break
        await asyncio.sleep(0)
    await hass.async_block_till_done()


class TestLastJourney:
    async def test_a_car_with_journeys_shows_its_last(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        hub: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(FakeDataHub, "journeys", ANSWER)
        await set_up(hass, entry)
        distance, duration, speed, ended = (hass.states.get(e) for e in SENSORS)
        assert float(distance.state) == pytest.approx(32.19)
        assert distance.attributes["unit_of_measurement"] == "km"
        assert float(duration.state) == 30
        assert duration.attributes["unit_of_measurement"] == "min"
        assert float(speed.state) == pytest.approx(64.37)
        assert ended.state == "2026-11-08T18:30:00+00:00"
        assert ended.attributes["started"] == "2026-11-08T18:00:00+00:00"
        # Where it went is in JLR's answer and nowhere here.
        assert "51.6000" not in repr(hass.states.async_all())
        await hass.config_entries.async_unload(entry.entry_id)

    async def test_distance_and_speed_follow_the_distance_option(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        hub: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(FakeDataHub, "journeys", ANSWER)
        await set_up(hass, entry, distance_unit="miles")
        distance = hass.states.get(SENSORS[0])
        speed = hass.states.get(SENSORS[2])
        assert distance.attributes["unit_of_measurement"] == "mi"
        assert float(distance.state) == pytest.approx(20.0, abs=0.01)
        assert speed.attributes["unit_of_measurement"] == "mph"
        assert float(speed.state) == pytest.approx(40.0, abs=0.01)
        await hass.config_entries.async_unload(entry.entry_id)

    async def test_a_car_refused_journeys_gains_no_sensors(
        self, hass: HomeAssistant, entry: MockConfigEntry, hub: Doubles
    ) -> None:
        await set_up(hass, entry)
        assert entry.runtime_data.datahub.journey_access[KEPT].startswith("refused")
        assert all(hass.states.get(e) is None for e in SENSORS)
        # The rest of the car is there as ever.
        assert hass.states.get("sensor.test_car_fuel_level") is not None
        await hass.config_entries.async_unload(entry.entry_id)

    async def test_the_sensors_outlive_a_restart_that_has_not_read_them_yet(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        hub: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(FakeDataHub, "journeys", ANSWER)
        await set_up(hass, entry)
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        # Next time round JLR fail to answer: the status builds the car and
        # prunes before any journey is read.
        monkeypatch.setattr(FakeDataHub, "journeys", datahub_link.DataHubError("500"))
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        for key in (
            "last_journey_distance",
            "last_journey_duration",
            "last_journey_average_speed",
            "last_journey_end",
        ):
            assert registry.async_get_entity_id("sensor", DOMAIN, f"{KEPT}_{key}")
        await hass.config_entries.async_unload(entry.entry_id)

    async def test_a_journey_read_later_adds_the_sensors_then(
        self, hass: HomeAssistant, entry: MockConfigEntry, hub: Doubles
    ) -> None:
        await set_up(hass, entry)
        link = entry.runtime_data.datahub
        assert hass.states.get(SENSORS[0]) is None
        before = len(hass.states.async_entity_ids("sensor"))
        link.client.journeys_result = ANSWER
        await link._async_read_journeys({KEPT})
        await hass.async_block_till_done()
        assert float(hass.states.get(SENSORS[0]).state) == pytest.approx(32.19)
        # A newer journey updates the sensors already there, adding none.
        newer = {**ANSWER["items"][0], "endTime": "2026-11-09T08:00:00Z"}
        newer["distance"] = {"value": 5.0, "unit": "km"}
        link.client.journeys_result = {**ANSWER, "items": [newer]}
        await link._async_read_journeys({KEPT})
        await hass.async_block_till_done()
        assert float(hass.states.get(SENSORS[0]).state) == 5.0
        assert len(hass.states.async_entity_ids("sensor")) == before + 4
        await hass.config_entries.async_unload(entry.entry_id)

    async def test_changing_the_option_later_reaches_them(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        hub: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(FakeDataHub, "journeys", ANSWER)
        await set_up(hass, entry, distance_unit="km")
        assert hass.states.get(SENSORS[0]).attributes["unit_of_measurement"] == "km"
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        await set_up(hass, entry, distance_unit="miles")
        assert hass.states.get(SENSORS[0]).attributes["unit_of_measurement"] == "mi"
        assert hass.states.get(SENSORS[2]).attributes["unit_of_measurement"] == "mph"
        await hass.config_entries.async_unload(entry.entry_id)

"""The Data Hub as the source of vehicle status, in place of the socket.

Driven through a real Home Assistant with the link set up on the entry: the
same entities as ever, fed from the Data Hub's REST status (the stream is held
parked here), with the socket left closed — until the link stops working, when
it is opened as a fallback.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT, Doubles, FakeDataHub  # noqa: E402
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol import datahub_link  # noqa: E402
from custom_components.jlr_incontrol.coordinator import (  # noqa: E402
    FIRST_SNAPSHOT_TIMEOUT,
    Resubscription,
)
from custom_components.jlr_incontrol.datahub import (  # noqa: E402
    DataHubAuthError,
    DataHubError,
    DataHubForbidden,
    DataHubRateLimited,
)
from custom_components.jlr_incontrol.datahub_link import DataHubLink  # noqa: E402

SETTINGS = {
    "client_id": "dsas-0123456789ab",
    "private_key": "not used by the fake",
    "certificate": "",
    "refresh_token": "offline-1",
    "vehicles": [KEPT],
    "account_id": "acct-1",
}


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


def link_entry(entry: MockConfigEntry, hass: HomeAssistant, **changes: Any) -> None:
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "datahub": {**SETTINGS, **changes}}
    )


@pytest.fixture
async def on_datahub(
    hass: HomeAssistant, entry: MockConfigEntry, hub: Doubles
) -> Doubles:
    link_entry(entry, hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield hub
    await hass.config_entries.async_unload(entry.entry_id)


class TestStatusFromTheDataHub:
    async def test_the_entities_read_the_data_hub_and_the_socket_stays_shut(
        self, hass: HomeAssistant, entry: MockConfigEntry, on_datahub: Doubles
    ) -> None:
        coordinator = entry.runtime_data
        assert coordinator.status_source == "datahub"
        assert not on_datahub.telemetry.connected
        # Values from the Data Hub's snapshot, under the same entities as ever.
        assert float(hass.states.get("sensor.test_car_fuel_level").state) == 42
        assert hass.states.get("sensor.test_car_odometer") is not None
        window = hass.states.get("binary_sensor.test_car_front_left_window")
        assert window is not None and window.state == "off"
        status = coordinator.data["vehicles"][KEPT]["status"]
        assert status["LAST_UPDATED_TIME"] == "2026-11-07T09:00:00+00:00"
        assert status["DOOR_BOOT_LOCK_STATUS"] == "LOCKED"

    async def test_readings_count_as_live_while_the_link_is_healthy(
        self, hass: HomeAssistant, entry: MockConfigEntry, on_datahub: Doubles
    ) -> None:
        coordinator = entry.runtime_data
        assert coordinator.telemetry_ok
        link = coordinator.datahub
        link._status_ok_at = None
        assert not coordinator.telemetry_ok
        link.connected = True
        assert coordinator.telemetry_ok
        link.signed_out = True
        assert not coordinator.telemetry_ok

    async def test_a_snapshot_older_than_the_one_held_is_ignored(
        self, hass: HomeAssistant, entry: MockConfigEntry, on_datahub: Doubles
    ) -> None:
        coordinator = entry.runtime_data
        coordinator._handle_datahub_status(
            KEPT,
            {"LAST_UPDATED_TIME": "2026-11-01T00:00:00Z", "FUEL_LEVEL_PERC": "1"},
        )
        assert coordinator.data["vehicles"][KEPT]["status"]["FUEL_LEVEL_PERC"] == "42"
        coordinator._handle_datahub_status(
            KEPT,
            {"LAST_UPDATED_TIME": "2026-11-08T00:00:00Z", "FUEL_LEVEL_PERC": "41"},
        )
        assert coordinator.data["vehicles"][KEPT]["status"]["FUEL_LEVEL_PERC"] == "41"

    async def test_refresh_reads_the_data_hub_rather_than_the_socket(
        self, hass: HomeAssistant, entry: MockConfigEntry, on_datahub: Doubles
    ) -> None:
        coordinator = entry.runtime_data
        before = on_datahub.datahub.calls.count("status")
        assert await coordinator.async_resubscribe_telemetry(KEPT) is (
            Resubscription.DONE
        )
        assert on_datahub.datahub.calls.count("status") == before + 1
        assert not on_datahub.telemetry.connected
        # The floor still applies: a second press straight after is refused.
        assert await coordinator.async_resubscribe_telemetry(KEPT) is (
            Resubscription.TOO_SOON
        )

    async def test_a_refresh_that_fails_says_so(
        self, hass: HomeAssistant, entry: MockConfigEntry, on_datahub: Doubles
    ) -> None:
        coordinator = entry.runtime_data
        on_datahub.datahub.status_result = DataHubError("down")
        on_datahub.datahub._tokens = None
        assert await coordinator.async_resubscribe_telemetry(KEPT) is (
            Resubscription.FAILED
        )

    async def test_a_revoked_link_falls_back_to_the_socket(
        self, hass: HomeAssistant, entry: MockConfigEntry, on_datahub: Doubles
    ) -> None:
        coordinator = entry.runtime_data
        coordinator.datahub._signed_out(DataHubAuthError("invalid_grant"))
        await hass.async_block_till_done()
        assert coordinator.status_source == "socket"
        assert on_datahub.telemetry.connected
        # And Data Hub statuses no longer land once the socket has taken over.
        coordinator._handle_datahub_status(
            KEPT,
            {"LAST_UPDATED_TIME": "2026-12-01T00:00:00Z", "FUEL_LEVEL_PERC": "1"},
        )
        assert coordinator.data["vehicles"][KEPT]["status"]["FUEL_LEVEL_PERC"] != "1"
        # A second notice is harmless.
        coordinator._datahub_signed_out()

    async def test_a_location_the_data_hub_gives_is_used_when_newer(
        self, hass: HomeAssistant, entry: MockConfigEntry, on_datahub: Doubles
    ) -> None:
        coordinator = entry.runtime_data
        coordinator._position[KEPT] = {
            "latitude": 1.0,
            "longitude": 2.0,
            "timestamp": "2026-11-07T09:00:00Z",
        }
        coordinator._handle_datahub_position(
            KEPT, {"latitude": 9.0, "longitude": 9.0, "timestamp": "2026-11-06T00:00Z"}
        )
        assert coordinator._position[KEPT]["latitude"] == 1.0
        coordinator._handle_datahub_position(
            KEPT, {"latitude": 3.0, "longitude": 4.0, "timestamp": "2026-11-07T10:00Z"}
        )
        assert coordinator.data["vehicles"][KEPT]["position"]["latitude"] == 3.0
        assert coordinator.data["vehicles"][KEPT]["position_trusted"]


class TestWhenTheDataHubIsNotTheSource:
    async def test_a_link_that_misses_a_car_leaves_status_on_the_socket(
        self, hass: HomeAssistant, entry: MockConfigEntry, hub: Doubles
    ) -> None:
        link_entry(entry, hass, vehicles=["SALZZ0000000000ZZ"])
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data.status_source == "socket"
        assert hub.telemetry.connected
        assert await hass.config_entries.async_unload(entry.entry_id)

    async def test_no_status_from_the_data_hub_retries_setup(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        hub: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(f"{Resubscription.__module__}.FIRST_SNAPSHOT_TIMEOUT", 0.05)
        assert FIRST_SNAPSHOT_TIMEOUT > 0.05
        link_entry(entry, hass)
        build = datahub_link.DataHubClient

        def silent(*args: Any) -> Any:
            fake = build(*args)
            fake.status_result = DataHubForbidden("status")
            return fake

        monkeypatch.setattr(datahub_link, "DataHubClient", silent)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_RETRY


class Steps:
    def __init__(self, limit: int) -> None:
        self.waits: list[float] = []
        self.limit = limit

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        if len(self.waits) >= self.limit:
            raise asyncio.CancelledError


def bare(
    hass: HomeAssistant, entry: MockConfigEntry, steps: int, **callbacks: Any
) -> tuple[DataHubLink, Steps]:
    link_entry(entry, hass)
    link = DataHubLink(hass, entry, **callbacks)
    pause = Steps(steps)
    link._pause = pause  # type: ignore[method-assign]
    return link, pause


async def status_loop(link: DataHubLink) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        await link._async_status_loop()


class TestTheStatusTimer:
    async def test_every_fifteen_minutes_until_the_stream_delivers(
        self, hass: HomeAssistant, entry: MockConfigEntry, hub: Doubles
    ) -> None:
        link, pause = bare(hass, entry, 2)
        await status_loop(link)
        assert pause.waits[0] == datahub_link.STATUS_INTERVAL_UNPROVEN
        link.stats["messages"] = 1
        pause.waits.clear()
        await status_loop(link)
        assert pause.waits[0] == datahub_link.STATUS_INTERVAL

    async def test_location_is_asked_for_hourly_not_every_read(
        self, hass: HomeAssistant, entry: MockConfigEntry, hub: Doubles
    ) -> None:
        link, _ = bare(hass, entry, 3)
        await status_loop(link)
        assert hub.datahub.calls.count("status") == 3
        assert hub.datahub.calls.count("location") == 1
        (answer,) = link.location.values()
        assert answer.startswith("refused")
        assert link.diagnostics()["location"]

    async def test_a_location_when_jlr_allow_it_is_handed_on(
        self, hass: HomeAssistant, entry: MockConfigEntry, hub: Doubles
    ) -> None:
        positions: list[tuple[str, dict[str, Any]]] = []
        link, _ = bare(
            hass, entry, 1, on_position=lambda v, p: positions.append((v, p))
        )
        hub.datahub.location_result = {
            "Vehicle": {"CurrentLocation": {"Latitude": 1.5, "Longitude": 2.5}},
            "timestamp": "2026-11-07T09:00:00Z",
        }
        await status_loop(link)
        assert positions == [
            (
                KEPT,
                {
                    "latitude": 1.5,
                    "longitude": 2.5,
                    "heading": None,
                    "speed": None,
                    "timestamp": "2026-11-07T09:00:00Z",
                },
            )
        ]
        assert link.location[KEPT] == "permitted"

    async def test_a_location_answer_without_a_fix_is_noted(
        self, hass: HomeAssistant, entry: MockConfigEntry, hub: Doubles
    ) -> None:
        link, _ = bare(hass, entry, 1)
        hub.datahub.location_result = {"Vehicle": {}}
        await status_loop(link)
        assert link.location[KEPT] == "permitted, no fix"

    async def test_a_rate_limit_waits_as_long_as_asked(
        self, hass: HomeAssistant, entry: MockConfigEntry, hub: Doubles
    ) -> None:
        link, pause = bare(hass, entry, 1)
        hub.datahub.status_result = DataHubRateLimited(3600)
        await status_loop(link)
        assert pause.waits == [3600]

    async def test_a_failed_read_tries_again_on_the_timer(
        self, hass: HomeAssistant, entry: MockConfigEntry, hub: Doubles
    ) -> None:
        link, pause = bare(hass, entry, 2)
        hub.datahub.status_result = DataHubError("down")
        await status_loop(link)
        assert len(pause.waits) == 2
        assert link._status_ok_at is None

    async def test_an_unexpected_failure_does_not_end_the_loop(
        self, hass: HomeAssistant, entry: MockConfigEntry, hub: Doubles
    ) -> None:
        link, pause = bare(hass, entry, 2)
        hub.datahub.status_result = RuntimeError("bug")
        await status_loop(link)
        assert len(pause.waits) == 2

    async def test_a_read_on_demand_when_the_link_is_revoked(
        self, hass: HomeAssistant, entry: MockConfigEntry, hub: Doubles
    ) -> None:
        signed_out: list[bool] = []
        link, _ = bare(hass, entry, 1, on_signed_out=lambda: signed_out.append(True))
        hub.datahub.refresh_result = DataHubAuthError("invalid_grant")
        assert await link.async_read_now() is False
        assert link.signed_out and signed_out == [True]


class TestTheStreamLoopSurvives:
    async def test_an_unexpected_failure_backs_off_and_carries_on(
        self, hass: HomeAssistant, entry: MockConfigEntry, hub: Doubles
    ) -> None:
        link, pause = bare(hass, entry, 2)
        hub.datahub.stream_result = RuntimeError("bug")
        with contextlib.suppress(asyncio.CancelledError):
            await link._async_run()
        assert len(pause.waits) == 2
        assert pause.waits[1] > pause.waits[0]

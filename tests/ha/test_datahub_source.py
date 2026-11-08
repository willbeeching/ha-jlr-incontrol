"""The Data Hub as the source of vehicle status, in place of the socket.

Driven through a real Home Assistant with the link set up on the entry: the
same entities as ever, fed from the Data Hub's REST status (the stream is held
parked here), with the socket left closed — until the link stops working, when
it is opened as a fallback.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT, Doubles, FakeDataHub  # noqa: E402
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
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
        # Nor the old REST API: the cars and their details come from the hub.
        assert on_datahub.client.calls == []
        attributes = coordinator.data["vehicles"][KEPT]["attributes"]
        assert attributes["fuelType"] == "diesel"
        assert attributes["modelYear"] == 2023
        assert attributes["numberOfDoors"] == "5"
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


class TestTheOldApiIsLeftAlone:
    async def test_the_cars_are_the_ones_on_the_client_not_the_old_list(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        hub: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The old vehicle list is not consulted once the Data Hub is linked:
        # the cars are the ones the owner selected at data.jlr.com.
        other = "SALZZ0000000000ZZ"
        link_entry(entry, hass, vehicles=[other])
        build = datahub_link.DataHubClient

        def other_car(*args: Any) -> Any:
            fake = build(*args)
            fake.tokens.vehicles = [other]
            return fake

        monkeypatch.setattr(datahub_link, "DataHubClient", other_car)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert set(entry.runtime_data.data["vehicles"]) == {other}
        assert hub.client.calls == []
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

    async def test_a_refused_car_is_asked_hourly_not_every_read(
        self, hass: HomeAssistant, entry: MockConfigEntry, hub: Doubles
    ) -> None:
        link, _ = bare(hass, entry, 3)
        await status_loop(link)
        assert hub.datahub.calls.count("status") == 3
        assert hub.datahub.calls.count("location") == 1
        (answer,) = link.location.values()
        assert answer.startswith("refused")
        assert link.diagnostics()["location"]

    async def test_a_permitted_car_is_located_with_every_status_read(
        self, hass: HomeAssistant, entry: MockConfigEntry, hub: Doubles
    ) -> None:
        # Once the car is declared at data.jlr.com, location rides on the
        # status cadence, as the portal's position did.
        link, _ = bare(hass, entry, 3)
        hub.datahub.location_result = {
            "Vehicle": {"CurrentLocation": {"Latitude": 1.5, "Longitude": 2.5}},
            "timestamp": "2026-11-07T09:00:00Z",
        }
        await status_loop(link)
        assert hub.datahub.calls.count("status") == 3
        assert hub.datahub.calls.count("location") == 3

    async def test_a_declaration_made_later_is_noticed_within_the_hour(
        self, hass: HomeAssistant, entry: MockConfigEntry, hub: Doubles
    ) -> None:
        link, _ = bare(hass, entry, 1)
        await status_loop(link)
        assert link.location[KEPT].startswith("refused")
        hub.datahub.location_result = {"Vehicle": {}}
        link._location_due[KEPT] = 0.0  # the hour has passed
        link._pause = Steps(1)  # type: ignore[method-assign]
        await status_loop(link)
        assert link.location[KEPT] == "permitted, no fix"

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


@pytest.fixture
def hub_only(hass: HomeAssistant) -> MockConfigEntry:
    """An entry set up on the Data Hub alone: no InControl sign-in at all."""
    created = MockConfigEntry(
        domain="jlr_incontrol",
        title="JLR Data Hub",
        data={"datahub": dict(SETTINGS)},
        unique_id="acct-1",
    )
    created.add_to_hass(hass)
    return created


class TestAnEntryOnTheDataHubAlone:
    async def test_it_runs_without_ever_touching_the_old_api(
        self, hass: HomeAssistant, hub_only: MockConfigEntry, hub: Doubles
    ) -> None:
        assert await hass.config_entries.async_setup(hub_only.entry_id)
        await hass.async_block_till_done()
        coordinator = hub_only.runtime_data
        assert not coordinator.has_incontrol
        assert coordinator.status_source == "datahub"
        assert hub.client.calls == []
        assert not hub.telemetry.connected
        assert float(hass.states.get("sensor.test_car_fuel_level").state) == 42
        assert await hass.config_entries.async_unload(hub_only.entry_id)

    async def test_a_revoked_link_has_no_socket_to_fall_back_on(
        self, hass: HomeAssistant, hub_only: MockConfigEntry, hub: Doubles
    ) -> None:
        assert await hass.config_entries.async_setup(hub_only.entry_id)
        await hass.async_block_till_done()
        coordinator = hub_only.runtime_data
        coordinator.datahub._signed_out(DataHubAuthError("invalid_grant"))
        await hass.async_block_till_done()
        assert coordinator.status_source == "datahub"
        assert not hub.telemetry.connected
        assert not coordinator.telemetry_ok
        # Housekeeping keeps what it holds rather than asking anyone.
        held = coordinator.data["vehicles"][KEPT]["status"]
        data = await coordinator._async_housekeeping()
        assert data["vehicles"][KEPT]["status"] == held
        assert hub.client.calls == []
        assert await hass.config_entries.async_unload(hub_only.entry_id)

    async def test_details_that_cannot_be_read_keep_what_is_held(
        self, hass: HomeAssistant, hub_only: MockConfigEntry, hub: Doubles
    ) -> None:
        assert await hass.config_entries.async_setup(hub_only.entry_id)
        await hass.async_block_till_done()
        coordinator = hub_only.runtime_data
        held = dict(coordinator._attributes[KEPT])
        # Within the day: not asked again.
        before = hub.datahub.calls.count("details")
        await coordinator._async_refresh_datahub_details(KEPT)
        assert hub.datahub.calls.count("details") == before
        # Due, and refused: what was held stays.
        coordinator._attributes_attempted.clear()
        hub.datahub.vehicle_result = DataHubError("down")
        await coordinator._async_refresh_datahub_details(KEPT)
        assert coordinator._attributes[KEPT] == held
        assert await hass.config_entries.async_unload(hub_only.entry_id)

    async def test_with_nothing_listed_yet_it_waits_rather_than_failing(
        self,
        hass: HomeAssistant,
        hub_only: MockConfigEntry,
        hub: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        hass.config_entries.async_update_entry(
            hub_only, data={"datahub": {**SETTINGS, "vehicles": []}}
        )
        build = datahub_link.DataHubClient

        def no_cars(*args: Any) -> Any:
            fake = build(*args)
            fake.tokens.vehicles = []
            return fake

        monkeypatch.setattr(datahub_link, "DataHubClient", no_cars)
        assert await hass.config_entries.async_setup(hub_only.entry_id)
        await hass.async_block_till_done()
        assert hub_only.runtime_data.data["vehicles"] == {}
        assert await hass.config_entries.async_unload(hub_only.entry_id)


@pytest.fixture
def sign_in(monkeypatch: pytest.MonkeyPatch) -> type:
    from doubles import FakeSignIn

    from custom_components.jlr_incontrol import config_flow

    monkeypatch.setattr(FakeSignIn, "begin_error", None)
    monkeypatch.setattr(FakeSignIn, "complete_error", None)
    monkeypatch.setattr(config_flow, "JlrLogin", FakeSignIn)
    return FakeSignIn


async def open_location(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    assert menu["menu_options"] == ["settings", "location"]
    return await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "location"}
    )


class TestLocationFromAnInControlSignIn:
    async def test_it_keeps_the_portal_session_and_nothing_else(
        self,
        hass: HomeAssistant,
        hub_only: MockConfigEntry,
        hub: Doubles,
        sign_in: type,
    ) -> None:
        assert await hass.config_entries.async_setup(hub_only.entry_id)
        await hass.async_block_till_done()
        result = await open_location(hass, hub_only)
        assert result["step_id"] == "location"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"username": "someone@example.com", "password": "pw"}
        )
        assert result["step_id"] == "location_code"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"code": " 123456 "}
        )
        await hass.async_block_till_done()
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert hub_only.data["sso_cookies"] == {
            "iPlanetDirectoryPro": "a-forgerock-session"
        }
        assert hub_only.data["username"] == "someone@example.com"
        # Not the password, and not the old API's token: status stays on the
        # Data Hub and nothing reaches the old API.
        assert "password" not in hub_only.data
        assert "refresh_token" not in hub_only.data
        coordinator = hub_only.runtime_data
        assert not coordinator.has_incontrol
        assert coordinator.status_source == "datahub"
        assert coordinator.portal.configured
        assert hub.client.calls == []
        assert await hass.config_entries.async_unload(hub_only.entry_id)

    async def test_a_refused_password_is_said_on_the_form(
        self,
        hass: HomeAssistant,
        hub_only: MockConfigEntry,
        hub: Doubles,
        sign_in: type,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from custom_components.jlr_incontrol.auth import (
            JlrInvalidCode,
            JlrLoginError,
        )

        assert await hass.config_entries.async_setup(hub_only.entry_id)
        await hass.async_block_till_done()
        for error, code in (
            (JlrInvalidCode("no"), "invalid_auth"),
            (JlrLoginError("password rejected"), "invalid_auth"),
            (RuntimeError("bug"), "cannot_connect"),
        ):
            monkeypatch.setattr(sign_in, "begin_error", error)
            result = await open_location(hass, hub_only)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"username": "a@b.c", "password": "x"}
            )
            assert result["errors"] == {"base": code}
        assert await hass.config_entries.async_unload(hub_only.entry_id)

    async def test_a_wrong_code_can_be_typed_again_and_a_dead_journey_ends(
        self,
        hass: HomeAssistant,
        hub_only: MockConfigEntry,
        hub: Doubles,
        sign_in: type,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from custom_components.jlr_incontrol.auth import (
            JlrInvalidCode,
            JlrSessionExpired,
        )

        assert await hass.config_entries.async_setup(hub_only.entry_id)
        await hass.async_block_till_done()
        result = await open_location(hass, hub_only)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"username": "a@b.c", "password": "x"}
        )
        monkeypatch.setattr(sign_in, "complete_error", JlrInvalidCode("no"))
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"code": "1"}
        )
        assert result["errors"] == {"base": "invalid_code"}
        monkeypatch.setattr(sign_in, "complete_error", JlrSessionExpired("gone"))
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"code": "2"}
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "location_failed"
        assert "sso_cookies" not in hub_only.data
        assert await hass.config_entries.async_unload(hub_only.entry_id)

    async def test_signing_in_again_from_reconfigure_keeps_it_location_only(
        self,
        hass: HomeAssistant,
        hub_only: MockConfigEntry,
        hub: Doubles,
        sign_in: type,
    ) -> None:
        # The portal's "sign in again" repair points at Reconfigure.
        hass.config_entries.async_update_entry(
            hub_only,
            data={
                **hub_only.data,
                "username": "someone@example.com",
                "portal_cookies": {"JSESSIONID": "stale"},
            },
        )
        result = await hub_only.start_reconfigure_flow(hass)
        assert result["step_id"] == "reconfigure"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "pw"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "123456"}
        )
        await hass.async_block_till_done()
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        assert hub_only.data["sso_cookies"]
        assert "portal_cookies" not in hub_only.data
        assert "refresh_token" not in hub_only.data
        assert hub.client.calls == []


class TestStartingUpWithAStreamThatSpeaksFirst:
    async def test_an_early_streamed_reading_does_not_cost_entities(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        hub: Doubles,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A restart where the stream answers before the first REST read.

        The one streamed signal was taken for the car's whole status: setup
        went ahead on it and the prune deleted every entity whose key it did
        not carry, the fuel level among them, for good.
        """
        from homeassistant.helpers import entity_registry as er

        link_entry(entry, hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        fuel = registry.async_get_entity_id(
            "sensor", "jlr_incontrol", f"{KEPT}_fuel_level"
        )
        assert fuel is not None
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        released = asyncio.Event()

        class SlowHub(FakeDataHub):
            async def async_status(self, vin: str, access_token: str) -> Any:
                await released.wait()
                return await super().async_status(vin, access_token)

        monkeypatch.setattr(
            datahub_link, "DataHubClient", hub.builder("datahub", SlowHub)
        )

        async def speaks_first(**kwargs: Any) -> str:
            kwargs["on_subscribed"]()
            kwargs["on_message"](
                f"account/acct-1/vehicles/{KEPT}/status",
                b'{"name": "Vehicle.TraveledDistance",'
                b' "timestamp": "2026-11-07T10:00:00Z", "value": 12346000}',
            )
            released.set()
            await asyncio.Event().wait()
            return "unreachable"

        monkeypatch.setattr(datahub_link, "run_session", speaks_first)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert registry.async_get(fuel) is not None
        state = hass.states.get(fuel)
        assert state is not None and float(state.state) == 42
        await hass.config_entries.async_unload(entry.entry_id)


EV_ANSWER = {
    "timestamp": "2026-11-07T09:00:00.000Z",
    "Vehicle": {
        "Powertrain": {
            "TractionBattery": {
                "StateOfCharge": {"Current": 64},
                "Range": 40,
                "Charging": {"IsCharging": True, "IsPlugConnected": True},
            }
        }
    },
}


class TestBatteryFromTheEvEndpoint:
    async def test_a_plug_in_car_is_read_with_every_status_read(
        self, hass: HomeAssistant, entry: MockConfigEntry, hub: Doubles
    ) -> None:
        seen: list[dict[str, str]] = []
        link, _ = bare(hass, entry, 3, on_status=lambda _, s: seen.append(s))
        hub.datahub.ev_result = EV_ANSWER
        await status_loop(link)
        assert hub.datahub.calls.count("ev") == 3
        # Out with the car's first status, not after it: entities are built
        # from that one, and a battery missing from it was a battery gone.
        assert seen[0]["EV_STATE_OF_CHARGE"] == "64"
        assert seen[0]["EV_CHARGING_STATUS"] == "CHARGING"
        assert seen[0]["FUEL_LEVEL_PERC"] == "42"
        assert list(link.diagnostics()["ev"].values()) == ["permitted"]

    async def test_a_car_without_a_charge_port_is_asked_daily_not_every_read(
        self, hass: HomeAssistant, entry: MockConfigEntry, hub: Doubles
    ) -> None:
        link, _ = bare(hass, entry, 3)
        await status_loop(link)
        assert hub.datahub.calls.count("status") == 3
        assert hub.datahub.calls.count("ev") == 1
        (answer,) = link.ev.values()
        assert answer.startswith("refused")
        assert link._ev_due[KEPT] - time.monotonic() > 23 * 3600

    async def test_a_failure_on_the_ev_endpoint_does_not_cost_the_status(
        self, hass: HomeAssistant, entry: MockConfigEntry, hub: Doubles
    ) -> None:
        seen: list[dict[str, str]] = []
        link, _ = bare(hass, entry, 1, on_status=lambda _, s: seen.append(s))
        hub.datahub.ev_result = DataHubError("GET ev answered 500")
        await status_loop(link)
        assert seen and seen[0]["FUEL_LEVEL_PERC"] == "42"

    async def test_a_sign_out_on_the_ev_endpoint_is_a_sign_out(
        self, hass: HomeAssistant, entry: MockConfigEntry, hub: Doubles
    ) -> None:
        link, _ = bare(hass, entry, 1)
        hub.datahub.ev_result = DataHubAuthError("revoked")
        await status_loop(link)
        assert link.signed_out

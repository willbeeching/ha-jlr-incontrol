"""The Data Hub link, running inside Home Assistant.

What is real: Home Assistant, the config entry, the entities and the issue
registry. What is not: JLR (the client is FakeDataHub) and the broker (each
test scripts what a session delivers). The loop's waits go through one method,
replaced here so a test can step through backoff without sitting through it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT, Doubles, FakeDataHub  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.helpers import issue_registry as ir  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol import datahub_link  # noqa: E402
from custom_components.jlr_incontrol.const import DOMAIN  # noqa: E402
from custom_components.jlr_incontrol.datahub import (  # noqa: E402
    DataHubAuthError,
    DataHubForbidden,
    DataHubRateLimited,
)
from custom_components.jlr_incontrol.datahub_link import DataHubLink  # noqa: E402
from custom_components.jlr_incontrol.diagnostics import (  # noqa: E402
    async_get_config_entry_diagnostics,
)
from custom_components.jlr_incontrol.stream import (  # noqa: E402
    MqttDropped,
    MqttError,
    MqttRefused,
)

SETTINGS = {
    "client_id": "dsas-0123456789ab",
    "private_key": "-----BEGIN PRIVATE KEY-----\nnot used by the fake\n",
    "certificate": "-----BEGIN CERTIFICATE-----\n",
    "refresh_token": "offline-1",
    "vehicles": [KEPT],
    "account_id": "acct-1",
}


def message(name: str, when: str, value: Any = 1) -> bytes:
    return json.dumps(
        {"name": name, "timestamp": when, "unit": None, "value": value}
    ).encode()


TOPIC = f"account/acct-1/vehicles/{KEPT}/status"


class Broker:
    """Stands in for run_session. Each call takes the next scripted session.

    A session is a list of (topic, payload) to deliver and then how it ends:
    a reason string to return, or an exception to raise. ``hold`` keeps the
    first session open until the test releases it.
    """

    def __init__(self) -> None:
        self.sessions: list[tuple[list[tuple[str, bytes]], Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.hold: asyncio.Event | None = None
        self.opened = asyncio.Event()

    async def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if not self.sessions:
            await asyncio.Event().wait()  # park until cancelled
        deliveries, end = self.sessions.pop(0)
        if isinstance(end, BaseException) and not deliveries:
            raise end
        kwargs["on_subscribed"]()
        for topic, payload in deliveries:
            kwargs["on_message"](topic, payload)
        self.opened.set()
        if self.hold is not None:
            await self.hold.wait()
        if isinstance(end, BaseException):
            raise end
        return str(end)


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> Broker:
    fake = Broker()
    monkeypatch.setattr(datahub_link, "run_session", fake)
    return fake


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch, doubles: Doubles) -> Doubles:
    monkeypatch.setattr(
        datahub_link, "DataHubClient", doubles.builder("datahub", FakeDataHub)
    )
    return doubles


@pytest.fixture
def linked(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "datahub": dict(SETTINGS)}
    )
    return entry


class Steps:
    """Replaces the loop's waits: records them, and stops after ``limit``."""

    def __init__(self, limit: int) -> None:
        self.waits: list[float] = []
        self.limit = limit

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        if len(self.waits) >= self.limit:
            raise asyncio.CancelledError


def bare_link(
    hass: HomeAssistant, entry: MockConfigEntry, steps: int
) -> tuple[DataHubLink, Steps]:
    link = DataHubLink(hass, entry)
    pause = Steps(steps)
    link._pause = pause
    return link, pause


async def run(link: DataHubLink) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        await link._async_run()


class TestSetUp:
    async def test_without_a_link_nothing_changes(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        assert entry.runtime_data.datahub is None
        registry = er.async_get(hass)
        ids = {
            e.unique_id
            for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        }
        assert f"{KEPT}_datahub_last_report" not in ids

    async def test_a_link_adds_its_two_entities_and_reads_status(
        self,
        hass: HomeAssistant,
        linked: MockConfigEntry,
        hub: Doubles,
        broker: Broker,
    ) -> None:
        broker.sessions.append(
            (
                [(TOPIC, message("Vehicle.TraveledDistance", "2026-11-07T15:00:00Z"))],
                "x",
            )
        )
        broker.hold = asyncio.Event()
        assert await hass.config_entries.async_setup(linked.entry_id)
        await asyncio.wait_for(broker.opened.wait(), 5)
        await hass.async_block_till_done()

        report = hass.states.get("sensor.test_car_last_report_data_hub")
        stream = hass.states.get("binary_sensor.test_car_data_hub_stream")
        assert report is not None and stream is not None
        # The streamed signal is newer than the REST snapshot, and wins.
        assert report.state == "2026-11-07T15:00:00+00:00"
        assert stream.state == "on"
        assert hub.datahub.calls[:3] == ["refresh offline-1", "status", "stream token"]
        call = broker.calls[0]
        # As issued; the portal's capitals are only how it displays it.
        assert call["username"] == "dsas-0123456789ab"
        assert call["password"] == "stream"
        assert call["topics"] == [f"account/acct-1/vehicles/{KEPT}/#"]

        broker.hold.set()
        assert await hass.config_entries.async_unload(linked.entry_id)
        await hass.async_block_till_done()

    async def test_the_rotated_refresh_token_is_kept(
        self,
        hass: HomeAssistant,
        linked: MockConfigEntry,
        hub: Doubles,
        broker: Broker,
    ) -> None:
        assert await hass.config_entries.async_setup(linked.entry_id)
        await hass.async_block_till_done()
        assert linked.data["datahub"]["refresh_token"] == "offline-2"
        # Everything else the link needs survives the write.
        assert linked.data["datahub"]["private_key"] == SETTINGS["private_key"]
        assert linked.data["refresh_token"] == "a-refresh-token"
        assert await hass.config_entries.async_unload(linked.entry_id)

    async def test_unloading_stops_the_stream(
        self,
        hass: HomeAssistant,
        linked: MockConfigEntry,
        hub: Doubles,
        broker: Broker,
    ) -> None:
        assert await hass.config_entries.async_setup(linked.entry_id)
        await hass.async_block_till_done()
        link = linked.runtime_data.datahub
        assert link is not None and link._task is not None
        assert await hass.config_entries.async_unload(linked.entry_id)
        assert link._task is None
        await link.async_stop()  # a second stop is harmless

    async def test_the_diagnostics_show_signal_names_and_times_not_values(
        self,
        hass: HomeAssistant,
        linked: MockConfigEntry,
        hub: Doubles,
        broker: Broker,
    ) -> None:
        broker.sessions.append(
            (
                [
                    (
                        TOPIC,
                        message(
                            "Vehicle.CurrentLocation.Latitude",
                            "2026-11-07T15:00:00Z",
                            51.5,
                        ),
                    )
                ],
                "x",
            )
        )
        broker.hold = asyncio.Event()
        assert await hass.config_entries.async_setup(linked.entry_id)
        await asyncio.wait_for(broker.opened.wait(), 5)
        result = await async_get_config_entry_diagnostics(hass, linked)
        text = json.dumps(result, default=str)
        assert KEPT not in text
        assert "51.5" not in text
        assert "acct-1" not in text
        section = result["datahub"]
        assert section["connected"] is True
        (vehicle,) = section["vehicles"].values()
        assert "Vehicle.TraveledDistance" in vehicle["signals"]
        broker.hold.set()
        assert await hass.config_entries.async_unload(linked.entry_id)


class TestTheLoop:
    async def test_a_healthy_session_reconnects_after_the_floor(
        self,
        hass: HomeAssistant,
        linked: MockConfigEntry,
        hub: Doubles,
        broker: Broker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Every session counts as healthy, however short the fake one was.
        monkeypatch.setattr(datahub_link, "HEALTHY_SESSION", 0.0)
        link, pause = bare_link(hass, linked, 2)
        broker.sessions += [([], "time limit"), ([], "time limit")]
        await run(link)
        assert all(
            datahub_link.RECONNECT_FLOOR <= w <= datahub_link.RECONNECT_FLOOR * 1.2
            for w in pause.waits
        )
        assert link.stats["sessions"] == 2

    async def test_a_session_that_dies_at_once_backs_off(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, broker: Broker
    ) -> None:
        link, pause = bare_link(hass, linked, 3)
        broker.sessions += [([], "closed by broker")] * 3
        await run(link)
        assert pause.waits[0] < pause.waits[1] < pause.waits[2]
        assert pause.waits[0] >= datahub_link.RECONNECT_FLOOR * 2

    async def test_faults_back_off_and_are_recorded(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, broker: Broker
    ) -> None:
        link, pause = bare_link(hass, linked, 2)
        link.broker = datahub_link.BROKERS[0]
        broker.sessions += [([], MqttError("reset")), ([], MqttError("reset"))]
        await run(link)
        assert link.stats["last_error"] == "reset"
        assert pause.waits[1] > pause.waits[0]

    async def test_a_rate_limit_waits_as_long_as_asked(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, broker: Broker
    ) -> None:
        link, pause = bare_link(hass, linked, 1)
        hub.datahub.stream_result = DataHubRateLimited(600)
        await run(link)
        assert 600 <= pause.waits[0] <= 720

    async def test_a_revoked_link_stops_and_raises_a_repair(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, broker: Broker
    ) -> None:
        link, pause = bare_link(hass, linked, 5)
        updates: list[bool] = []
        link.async_add_listener(lambda: updates.append(True))
        hub.datahub.refresh_result = DataHubAuthError("invalid_grant")
        await run(link)
        assert link.signed_out
        assert pause.waits == []  # no retrying an answer that cannot change
        assert updates
        issue = ir.async_get(hass).async_get_issue(
            DOMAIN, f"datahub_signed_out_{linked.entry_id}"
        )
        assert issue is not None

    async def test_a_working_renewal_clears_that_repair(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, broker: Broker
    ) -> None:
        link, _ = bare_link(hass, linked, 1)
        hub.datahub.refresh_result = DataHubAuthError("invalid_grant")
        await run(link)
        hub.datahub.refresh_result = hub.datahub.tokens
        link._tokens = None
        await link._async_access_token()
        issue = ir.async_get(hass).async_get_issue(
            DOMAIN, f"datahub_signed_out_{linked.entry_id}"
        )
        assert issue is None

    async def test_a_stream_switched_off_is_asked_about_hourly(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, broker: Broker
    ) -> None:
        link, pause = bare_link(hass, linked, 2)
        hub.datahub.stream_result = DataHubForbidden("not enabled")
        await run(link)
        assert link.stream_enabled is False
        assert datahub_link.STATUS_INTERVAL in pause.waits
        assert broker.calls == []

    async def test_a_rejected_login_tries_each_way_once_on_one_broker(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, broker: Broker
    ) -> None:
        # Bad credentials are bad on both brokers; trying the second host would
        # only add to the refusals JLR see.
        link, _ = bare_link(hass, linked, 1)
        broker.sessions += [([], MqttRefused(5))] + [([], MqttDropped("login"))] * 4
        await run(link)
        seen = [
            (c["host"], c["port"], c["username"], c["client_id"]) for c in broker.calls
        ]
        assert {host for host, _, _, _ in seen} == {datahub_link.BROKERS[0]}
        assert [(port, user, cid == user) for _, port, user, cid in seen] == [
            (8883, "dsas-0123456789ab", False),
            (8883, "dsas-0123456789ab", True),
            (8883, "DSAS-0123456789AB", False),
            (8883, "DSAS-0123456789AB", True),
            (443, "dsas-0123456789ab", False),
        ]
        assert "443" in link.stats["last_rejection"]
        # The 443 attempt asks for "mqtt" in the handshake on a context of its
        # own; the shared one every other connection uses is left alone.
        assert broker.calls[-1]["ssl_context"] is not broker.calls[0]["ssl_context"]

    async def test_the_login_that_worked_is_the_one_kept(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, broker: Broker
    ) -> None:
        link, _ = bare_link(hass, linked, 2)
        broker.sessions += [
            ([], MqttDropped("login")),
            ([], "time limit"),
            ([], "time limit"),
        ]
        await run(link)
        assert len(broker.calls) == 3
        last = broker.calls[-1]
        assert last["client_id"] == last["username"] == "dsas-0123456789ab"
        diagnostics = link.diagnostics()
        assert diagnostics["login"] == "username as issued, as client id"

    async def test_an_unreachable_broker_falls_back_to_the_other(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, broker: Broker
    ) -> None:
        link, _ = bare_link(hass, linked, 1)
        broker.sessions += [([], MqttError("no such host")), ([], "time limit")]
        await run(link)
        assert [c["host"] for c in broker.calls] == list(datahub_link.BROKERS)
        assert link.broker == datahub_link.BROKERS[1]

    async def test_once_a_broker_has_worked_it_is_the_only_one_tried(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, broker: Broker
    ) -> None:
        link, _ = bare_link(hass, linked, 1)
        link.broker = datahub_link.BROKERS[1]
        broker.sessions.append(([], MqttError("reset")))
        await run(link)
        assert [c["host"] for c in broker.calls] == [datahub_link.BROKERS[1]]

    async def test_no_broker_reachable_is_one_fault(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, broker: Broker
    ) -> None:
        link, _ = bare_link(hass, linked, 1)
        broker.sessions += [([], MqttError("a")), ([], MqttError("b"))]
        await run(link)
        assert link.stats["last_error"] == "b"

    async def test_a_stream_token_naming_no_account_is_a_fault(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, broker: Broker
    ) -> None:
        link, _ = bare_link(hass, linked, 1)
        link.account_id = None
        hub.datahub.refresh_result.account_id = None
        hub.datahub.stream_result.account_id = None
        await run(link)
        assert "no account" in link.stats["last_error"]


class TestWhatItReads:
    async def test_a_vehicle_that_refuses_status_is_skipped(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        hub.datahub.status_result = DataHubForbidden("status")
        await link._async_read_status()
        assert KEPT not in link.reports

    async def test_the_snapshot_does_not_overwrite_a_newer_streamed_value(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        link._on_message(
            TOPIC, message("Vehicle.TraveledDistance", "2026-11-08T00:00:00Z", 9)
        )
        await link._async_read_status()
        assert link.signals[KEPT]["Vehicle.TraveledDistance"]["value"] == 9
        assert link.signals[KEPT]["Vehicle.Body.Trunk.Rear.IsOpen"]["value"] == "CLOSED"
        assert "Vehicle.VehicleIdentification.VIN" not in link.signals[KEPT]

    async def test_the_token_is_reused_until_it_is_nearly_spent(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        await link._async_access_token()
        await link._async_access_token()
        assert hub.datahub.calls.count("refresh offline-1") == 1

    async def test_a_snapshot_without_a_time_reports_nothing(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        hub.datahub.status_result = {"Vehicle": {"TraveledDistance": 1}}
        await link._async_read_status()
        assert KEPT not in link.reports

    @pytest.mark.parametrize(
        ("topic", "payload"),
        [
            (TOPIC, b"not json"),
            ("account/acct-1/other", message("Vehicle.X", "2026-11-07T15:00:00Z")),
            (TOPIC, json.dumps([{"timestamp": "2026-11-07T15:00:00Z"}, 3]).encode()),
        ],
    )
    async def test_messages_it_cannot_place_are_dropped(
        self,
        hass: HomeAssistant,
        linked: MockConfigEntry,
        hub: Doubles,
        topic: str,
        payload: bytes,
    ) -> None:
        link = DataHubLink(hass, linked)
        link._on_message(topic, payload)
        assert link.reports == {}
        assert link.stats["messages"] == 1

    async def test_a_batch_of_signals_is_one_update(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        updates: list[bool] = []
        remove = link.async_add_listener(lambda: updates.append(True))
        batch = [
            {"name": "Vehicle.A", "timestamp": "2026-11-07T15:00:00Z", "value": 1},
            {"name": "Vehicle.B", "timestamp": "2026-11-07T15:00:01Z", "value": 2},
        ]
        link._on_message(TOPIC, json.dumps(batch).encode())
        assert len(updates) == 1
        # An older signal after a newer one does not move the report back.
        link._on_message(TOPIC, message("Vehicle.C", "2026-11-07T14:00:00Z"))
        assert len(updates) == 1
        assert link.reports[KEPT].isoformat() == "2026-11-07T15:00:01+00:00"
        remove()
        link._on_message(TOPIC, message("Vehicle.D", "2026-11-07T16:00:00Z"))
        assert len(updates) == 1

    async def test_a_timestamp_that_is_not_text_is_ignored(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        link._on_message(
            TOPIC, json.dumps({"name": "Vehicle.A", "timestamp": 5}).encode()
        )
        assert link.signals[KEPT]["Vehicle.A"]["time"] is None
        assert link.reports == {}

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
import logging
import time
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT, SOLD, Doubles, FakeDataHub  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import issue_registry as ir  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol import datahub_link  # noqa: E402
from custom_components.jlr_incontrol.const import DOMAIN  # noqa: E402
from custom_components.jlr_incontrol.datahub import (  # noqa: E402
    DataHubAuthError,
    DataHubError,
    DataHubForbidden,
    DataHubRateLimited,
)
from custom_components.jlr_incontrol.datahub_link import DataHubLink  # noqa: E402
from custom_components.jlr_incontrol.diagnostics import (  # noqa: E402
    async_get_config_entry_diagnostics,
)
from custom_components.jlr_incontrol.redact import scrub  # noqa: E402
from custom_components.jlr_incontrol.stream import (  # noqa: E402
    LOGIN_STAGE,
    SUBSCRIPTION_STAGE,
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
    # The real link, reading the fake JLR client below.
    monkeypatch.setattr(
        "custom_components.jlr_incontrol.coordinator.DataHubLink",
        datahub_link.DataHubLink,
    )
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


def reauths(hass: HomeAssistant, entry: MockConfigEntry) -> list[Any]:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"].get("source") == "reauth"
        and flow["context"].get("entry_id") == entry.entry_id
    ]


async def run_status(link: DataHubLink) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        await link._async_status_loop()


async def run(link: DataHubLink) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        await link._async_run()


class TestAnOutageInTheLog:
    """Said once on the way down and once on the way back, at info.

    Every failed read and reconnect used to file its own warning: up to four
    an hour each, for as long as JLR were down, while the entities stayed
    available for three hours on what had already been read.
    """

    @staticmethod
    def lines(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
        return [
            (record.levelno, record.getMessage())
            for record in caplog.records
            if record.name == datahub_link.__name__ and record.levelno >= logging.INFO
        ]

    async def test_failing_reads_are_said_once_and_their_recovery_once(
        self,
        hass: HomeAssistant,
        linked: MockConfigEntry,
        hub: Doubles,
        broker: Broker,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        link, _ = bare_link(hass, linked, 3)
        good = hub.datahub.status_result
        hub.datahub.status_result = [DataHubError("502"), DataHubError("502"), good]
        with caplog.at_level(logging.DEBUG, logger=datahub_link.__name__):
            await run_status(link)
        said = self.lines(caplog)
        assert [level for level, _ in said] == [logging.INFO, logging.INFO]
        assert "reads failing: 502" in said[0][1]
        assert "reads working again" in said[1][1]

    async def test_a_rate_limit_is_an_outage_like_any_other(
        self,
        hass: HomeAssistant,
        linked: MockConfigEntry,
        hub: Doubles,
        broker: Broker,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        link, _ = bare_link(hass, linked, 2)
        hub.datahub.status_result = [DataHubRateLimited(900), DataHubRateLimited(900)]
        with caplog.at_level(logging.INFO, logger=datahub_link.__name__):
            await run_status(link)
        said = self.lines(caplog)
        assert len(said) == 1
        assert "asked to slow down for 900s" in said[0][1]

    async def test_a_failing_stream_is_said_once_and_its_recovery_once(
        self,
        hass: HomeAssistant,
        linked: MockConfigEntry,
        hub: Doubles,
        broker: Broker,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        link, _ = bare_link(hass, linked, 3)
        hub.datahub.stream_result = [
            DataHubError("503"),
            DataHubError("503"),
            hub.datahub.stream_result,
        ]
        broker.sessions.append(([], "time limit"))
        with caplog.at_level(logging.DEBUG, logger=datahub_link.__name__):
            await run(link)
        said = [message for _, message in self.lines(caplog)]
        failing = [message for message in said if "stream failing" in message]
        assert len(failing) == 1
        assert "503" in failing[0]
        assert any("stream working again" in message for message in said)
        assert not [
            record
            for record in caplog.records
            if record.name == datahub_link.__name__
            and record.levelno >= logging.WARNING
        ]

    async def test_reads_and_stream_are_told_apart(
        self,
        hass: HomeAssistant,
        linked: MockConfigEntry,
        hub: Doubles,
        broker: Broker,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # One failing while the other works is half an outage, and the line
        # should say which half.
        link, _ = bare_link(hass, linked, 1)
        with caplog.at_level(logging.INFO, logger=datahub_link.__name__):
            link._went_down("stream", "503")
            link._came_back("reads")
            link._went_down("reads", "502")
        said = [message for _, message in self.lines(caplog)]
        assert len(said) == 2
        assert "stream failing" in said[0]
        assert "reads failing" in said[1]


class TestSetUp:
    async def test_a_link_adds_its_two_entities_and_reads_status(
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
                            "Vehicle.TraveledDistance",
                            "2026-11-07T15:00:00Z",
                            12350000,
                        ),
                    )
                ],
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
        # Status first, then the location probe, then the stream; the two
        # loops run side by side, so only membership is fixed beyond the first.
        # The car's details during housekeeping, then status, the location
        # probe and the stream, the last three side by side.
        assert hub.datahub.calls[:2] == ["refresh offline-1", "details"]
        assert {"status", "location", "stream token"} <= set(hub.datahub.calls)
        call = broker.calls[0]
        # As issued; the portal's capitals are only how it displays it.
        assert call["username"] == "dsas-0123456789ab"
        assert call["password"] == "stream"
        assert call["topics"] == [
            f"account/acct-1/vehicles/{KEPT}/status",
            f"account/acct-1/vehicles/{KEPT}/position",
        ]

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
        assert linked.data["datahub"]["client_id"] == SETTINGS["client_id"]
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

    async def test_a_revoked_link_stops_and_asks_to_be_linked_again(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, broker: Broker
    ) -> None:
        link, pause = bare_link(hass, linked, 5)
        updates: list[bool] = []
        link.async_add_listener(lambda: updates.append(True))
        hub.datahub.refresh_result = DataHubAuthError("invalid_grant")
        await run_status(link)
        await hass.async_block_till_done()
        assert link.signed_out
        assert pause.waits == []  # no retrying an answer that cannot change
        assert updates
        assert reauths(hass, linked)
        # A reauth, not the repair that used to say the same thing.
        assert (
            ir.async_get(hass).async_get_issue(
                DOMAIN, f"datahub_signed_out_{linked.entry_id}"
            )
            is None
        )

    async def test_a_stream_switched_off_is_asked_about_hourly(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, broker: Broker
    ) -> None:
        link, pause = bare_link(hass, linked, 2)
        hub.datahub.stream_result = DataHubForbidden("not enabled")
        await run(link)
        assert link.stream_enabled is False
        assert datahub_link.STATUS_INTERVAL in pause.waits
        assert broker.calls == []

    async def test_a_stream_switched_off_is_said_in_repairs_until_it_is_on(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, broker: Broker
    ) -> None:
        # Nothing else tells an owner the toggle at data.jlr.com is off: the
        # readings carry on, just a REST read at a time.
        issues = ir.async_get(hass)
        issue_id = f"datahub_stream_off_{linked.entry_id}"
        link, _ = bare_link(hass, linked, 1)
        hub.datahub.stream_result = DataHubForbidden("not enabled")
        await link._async_stream_once()
        assert issues.async_get_issue(DOMAIN, issue_id) is not None
        hub.datahub.stream_result = hub.datahub.tokens
        broker.sessions.append(([], "time limit"))
        await link._async_stream_once()
        assert link.stream_enabled is True
        assert issues.async_get_issue(DOMAIN, issue_id) is None

    async def test_a_link_that_has_gone_takes_the_stream_notice_with_it(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, broker: Broker
    ) -> None:
        issues = ir.async_get(hass)
        link, _ = bare_link(hass, linked, 1)
        hub.datahub.stream_result = DataHubForbidden("not enabled")
        await link._async_stream_once()
        link._signed_out(DataHubAuthError("revoked"))
        await hass.async_block_till_done()
        assert (
            issues.async_get_issue(DOMAIN, f"datahub_stream_off_{linked.entry_id}")
            is None
        )
        assert reauths(hass, linked)

    async def test_a_rejected_login_tries_each_way_once_on_one_broker(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, broker: Broker
    ) -> None:
        # Bad credentials are bad on both brokers; trying the second host would
        # only add to the refusals JLR see.
        link, pause = bare_link(hass, linked, 1)
        broker.sessions += [([], MqttRefused(5))] + [([], MqttDropped(LOGIN_STAGE))] * 4
        await run(link)
        seen = [
            (c["host"], c["port"], c["username"], c["client_id"]) for c in broker.calls
        ]
        assert {host for host, _, _, _ in seen} == {datahub_link.BROKERS[0]}
        assert [(port, user, cid == user) for _, port, user, cid in seen] == [
            (8883, "dsas-0123456789ab", True),
            (8883, "dsas-0123456789ab", False),
            (8883, "DSAS-0123456789AB", False),
            (8883, "DSAS-0123456789AB", True),
            (443, "dsas-0123456789ab", False),
        ]
        assert "443" in link.stats["last_rejection"]
        # Then the stream is left for hours, not minutes: the next wait is
        # the hourly status, with the stream not due again until much later.
        assert pause.waits == [datahub_link.STATUS_INTERVAL]
        assert link._stream_after - time.monotonic() > 5 * 3600
        # The 443 attempt asks for "mqtt" in the handshake on a context of its
        # own; the shared one every other connection uses is left alone.
        assert broker.calls[-1]["ssl_context"] is not broker.calls[0]["ssl_context"]

    async def test_the_login_that_worked_is_the_one_kept(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, broker: Broker
    ) -> None:
        link, _ = bare_link(hass, linked, 2)
        broker.sessions += [
            ([], MqttDropped(LOGIN_STAGE)),
            ([], "time limit"),
            ([], "time limit"),
        ]
        await run(link)
        assert len(broker.calls) == 3
        # The first way refused, the second worked, and the second is the
        # only one asked again.
        assert broker.calls[0]["client_id"] == broker.calls[0]["username"]
        assert broker.calls[1]["client_id"] != broker.calls[1]["username"]
        assert broker.calls[2]["client_id"] != broker.calls[2]["username"]
        diagnostics = link.diagnostics()
        assert diagnostics["login"] == "username as issued"

    async def test_the_login_jlr_accept_is_tried_first(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, broker: Broker
    ) -> None:
        # Measured in November 2026: only the client id as both username and
        # MQTT client id gets in. Tried second, it cost a refused connection
        # on every restart of Home Assistant.
        link, _ = bare_link(hass, linked, 1)
        broker.sessions.append(([], "time limit"))
        await run(link)
        assert len(broker.calls) == 1
        first = broker.calls[0]
        assert first["client_id"] == first["username"] == "dsas-0123456789ab"

    async def test_a_login_accepted_then_a_topic_refused_tries_other_layouts(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, broker: Broker
    ) -> None:
        # What JLR's broker did in November 2026: the client id as both the
        # username and the MQTT client id got past the login, and the docs'
        # own filter was then refused.
        link, _ = bare_link(hass, linked, 1)
        broker.sessions += [
            ([], MqttDropped(SUBSCRIPTION_STAGE)),
            ([], "time limit"),
        ]
        await run(link)
        # Position with status first; a car not declared for location may
        # make the broker drop that, so status alone comes next.
        assert [c["topics"] for c in broker.calls] == [
            [
                f"account/acct-1/vehicles/{KEPT}/status",
                f"account/acct-1/vehicles/{KEPT}/position",
            ],
            [f"account/acct-1/vehicles/{KEPT}/status"],
        ]
        assert broker.calls[1]["username"] == broker.calls[1]["client_id"]
        diagnostics = link.diagnostics()
        assert diagnostics["topics"] == "each car, status"
        assert diagnostics["login"] == "username as issued, as client id"

    async def test_every_layout_refused_keeps_the_login_and_waits_hours(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, broker: Broker
    ) -> None:
        link, pause = bare_link(hass, linked, 1)
        layouts = len(link._all_topic_sets("acct-1"))
        broker.sessions += [([], MqttDropped(SUBSCRIPTION_STAGE))] * layouts
        await run(link)
        # Every layout once with the login that was accepted, and no other
        # login tried after it.
        assert len(broker.calls) == layouts
        assert len({(c["username"], c["client_id"]) for c in broker.calls}) == 1
        assert pause.waits == [datahub_link.STATUS_INTERVAL]
        assert (
            link.diagnostics()["accepted_login"] == "username as issued, as client id"
        )
        # The next round starts from the login that was accepted.
        assert [login.label for login in link._logins()] == [
            "username as issued, as client id"
        ]

    async def test_without_a_vehicle_list_it_asks_for_the_whole_account(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        link.vehicles = []
        assert link._topic_sets("acct-1") == [("whole account", ["account/acct-1/#"])]

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

    async def test_a_persistent_ev_failure_is_warned_about_once(
        self,
        hass: HomeAssistant,
        linked: MockConfigEntry,
        hub: Doubles,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        link = DataHubLink(hass, linked)
        hub.datahub.ev_result = DataHubError("500")
        with caplog.at_level(
            logging.WARNING, logger="custom_components.jlr_incontrol.datahub_link"
        ):
            for _ in range(datahub_link.EV_FAILURES_BEFORE_WARNING + 1):
                await link._async_read_status()
        assert caplog.text.count("held back") == 1

    async def test_the_snapshot_does_not_overwrite_a_newer_streamed_value(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        link._on_message(
            TOPIC,
            message(
                "Vehicle.Powertrain.FuelSystem.RelativeLevel", "2026-11-08T00:00:00Z", 9
            ),
        )
        await link._async_read_status()
        assert (
            link.signals[KEPT]["Vehicle.Powertrain.FuelSystem.RelativeLevel"]["value"]
            == 9
        )
        assert link.signals[KEPT]["Vehicle.Body.Trunk.Rear.IsOpen"]["value"] == "CLOSED"
        assert "Vehicle.VehicleIdentification.VIN" not in link.signals[KEPT]

    async def test_the_token_is_reused_until_it_is_nearly_spent(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        await link._async_access_token()
        await link._async_access_token()
        assert hub.datahub.calls.count("refresh offline-1") == 1

    async def test_reads_that_find_the_token_spent_together_renew_it_once(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        # JLR rotate the refresh token on use: a second renewal with the one
        # just spent is refused, and the link signs itself out.
        link = DataHubLink(hass, linked)
        release = asyncio.Event()
        spent: list[str] = []

        async def renew(refresh_token: str) -> Any:
            if refresh_token in spent:
                raise DataHubAuthError("invalid_grant")
            spent.append(refresh_token)
            await release.wait()
            return hub.datahub.tokens

        link.client.async_refresh = renew
        first = asyncio.create_task(link._async_access_token())
        second = asyncio.create_task(link._async_access_token())
        await asyncio.sleep(0)
        release.set()
        assert await first == await second == "access"
        assert spent == ["offline-1"]
        assert not link.signed_out

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
        assert link.stats["unread"] == 1
        assert link.stats["fresh"] == 0

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


class TestRefresh:
    """The button's on-demand read: what it touches, and what counts as done."""

    async def test_a_press_reads_location_as_well_as_status(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        assert await link.async_read_now() is True
        assert hub.datahub.calls.count("status") == 1
        assert hub.datahub.calls.count("location") == 1
        assert link.location[KEPT].startswith("refused")

    async def test_a_press_leaves_journeys_to_their_own_clock(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        assert await link.async_read_now() is True
        assert "journeys 0" not in hub.datahub.calls

    async def test_a_location_failure_does_not_fail_the_press(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        hub.datahub.location_result = DataHubError("500")
        assert await link.async_read_now() is True
        assert KEPT in link._read_in_full

    async def test_a_refused_status_is_not_a_successful_refresh(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        hub.datahub.status_result = DataHubForbidden("status")
        assert await link.async_read_now() is False

    async def test_a_press_is_for_its_own_car(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        # Two cars, one refused status. The other answering is not a refresh
        # of the refused one, though it is of itself.
        link = DataHubLink(hass, linked)
        # The cars come from what the token covers.
        hub.datahub.tokens.vehicles = [KEPT, SOLD]
        answer = hub.datahub.status_result
        hub.datahub.status_result = [DataHubForbidden("status"), answer]
        assert await link.async_read_now(KEPT) is False
        hub.datahub.status_result = [DataHubForbidden("status"), answer]
        assert await link.async_read_now(SOLD) is True
        hub.datahub.status_result = [DataHubForbidden("status"), answer]
        assert await link.async_read_now() is True

    async def test_a_status_held_back_by_ev_is_not_a_successful_refresh(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        hub.datahub.ev_result = DataHubError("500")
        assert await link.async_read_now() is False
        assert KEPT not in link._read_in_full


def in_unit(name: str, when: str, value: Any, unit: str | None) -> bytes:
    """A streamed signal in the broker's own shape, numbers as text."""
    return json.dumps(
        [
            {
                "dataType": "double",
                "signalName": name.removeprefix("Vehicle."),
                "vssIdentifier": name,
                "signalTimestamp": when,
                "signalValue": str(value),
                "unit": unit,
            }
        ]
    ).encode()


class TestStreamedDistances:
    """The stream says its own unit, and it is not always REST's.

    A Defender's odometer came down the stream as 19156 while REST held it in
    metres, some 30.8 million, and read 11 miles until the next REST read.
    """

    @pytest.fixture
    async def link(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> DataHubLink:
        return DataHubLink(hass, linked)

    async def statuses(self, hass: HomeAssistant, link: DataHubLink) -> Any:
        seen: list[dict[str, str]] = []
        link._on_status = lambda _, status: seen.append(status)
        await link._async_read_status()
        return seen

    async def test_an_odometer_in_miles_is_read_as_miles(
        self, hass: HomeAssistant, link: DataHubLink, hub: Doubles
    ) -> None:
        seen = await self.statuses(hass, link)
        assert seen[-1]["ODOMETER_METER"] == "12345000"
        link._on_message(
            TOPIC,
            in_unit("Vehicle.TraveledDistance", "1794830000000", 7671, "mi"),
        )
        assert seen[-1]["ODOMETER_METER"] == "12345278"
        assert seen[-1]["ODOMETER_MILES"] == "7671"

    @pytest.mark.parametrize("miles", [7672, 7750, 7875, 7999])
    async def test_a_whole_mileage_reads_back_as_itself(
        self, hass: HomeAssistant, link: DataHubLink, hub: Doubles, miles: int
    ) -> None:
        # Rounded to the nearest metre, half of these came back a mile short,
        # and every multiple of 125 miles read one less.
        seen = await self.statuses(hass, link)
        link._on_message(
            TOPIC,
            in_unit("Vehicle.TraveledDistance", "1794830000000", miles, "mi"),
        )
        assert seen[-1]["ODOMETER_MILES"] == str(miles)
        assert link.diagnostics()["stats"]["units"] == {
            "Vehicle.TraveledDistance": "mi"
        }

    @pytest.mark.parametrize(
        ("value", "unit", "expected"),
        [
            (12346, "km", "12346000"),
            (12346000, "m", "12346000"),
            (12346000, None, "12346000"),
        ],
    )
    async def test_an_odometer_in_km_or_metres_is_read_as_such(
        self,
        hass: HomeAssistant,
        link: DataHubLink,
        hub: Doubles,
        value: int,
        unit: str | None,
        expected: str,
    ) -> None:
        seen = await self.statuses(hass, link)
        link._on_message(
            TOPIC, in_unit("Vehicle.TraveledDistance", "1794830000000", value, unit)
        )
        assert seen[-1]["ODOMETER_METER"] == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            # Metres, labelled km: a Defender in #62, which 2.1.4 showed a
            # thousand times too high.
            (12350000, "12350000"),
            # Miles, labelled km: a Defender here, which 3.0.0-beta.2 read as
            # metres and showed as 11 miles.
            (7672, "12346888"),
        ],
    )
    async def test_an_odometer_labelled_km_is_read_by_where_it_fits(
        self,
        hass: HomeAssistant,
        link: DataHubLink,
        hub: Doubles,
        value: int,
        expected: str,
    ) -> None:
        seen = await self.statuses(hass, link)
        link._on_message(
            TOPIC, in_unit("Vehicle.TraveledDistance", "1794830000000", value, "km")
        )
        assert seen[-1]["ODOMETER_METER"] == expected

    async def test_an_odometer_that_fits_no_scale_is_left_for_rest(
        self, hass: HomeAssistant, link: DataHubLink, hub: Doubles
    ) -> None:
        # 2,000 km ahead as metres, miles or kilometres: nobody drives that
        # between two REST reads.
        seen = await self.statuses(hass, link)
        link._on_message(
            TOPIC,
            in_unit("Vehicle.TraveledDistance", "1794830000000", 14345000, "km"),
        )
        assert seen[-1]["ODOMETER_METER"] == "12345000"
        assert link.stats["unconverted"] == 1

    async def test_an_odometer_before_any_rest_read_waits_for_it(
        self, hass: HomeAssistant, link: DataHubLink, hub: Doubles
    ) -> None:
        # Nothing to place it against, so nothing to say which scale it is in.
        link._on_message(
            TOPIC,
            in_unit("Vehicle.TraveledDistance", "1794830000000", 12345000, "km"),
        )
        assert "Vehicle.TraveledDistance" not in link.signals.get(KEPT, {})
        assert link.stats["unconverted"] == 1

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(622, 1001012), (1001, 1001000)],
    )
    async def test_a_new_cars_odometer_takes_the_nearest_fit(
        self,
        hass: HomeAssistant,
        link: DataHubLink,
        hub: Doubles,
        value: int,
        expected: int,
    ) -> None:
        # Under about 1,640 km, miles and kilometres can both land ahead of the
        # held figure; the one nearer it is the reading.
        hub.datahub.status_result = {
            **hub.datahub.status_result,
            "Vehicle": {
                **hub.datahub.status_result["Vehicle"],
                "TraveledDistance": 1000000,
            },
        }
        await self.statuses(hass, link)
        link._on_message(
            TOPIC, in_unit("Vehicle.TraveledDistance", "1794830000000", value, "km")
        )
        held = link.signals[KEPT]["Vehicle.TraveledDistance"]["value"]
        assert held == expected

    async def held_at(
        self, hass: HomeAssistant, link: DataHubLink, hub: Doubles, metres: int
    ) -> Any:
        hub.datahub.status_result = {
            **hub.datahub.status_result,
            "Vehicle": {
                **hub.datahub.status_result["Vehicle"],
                "TraveledDistance": metres,
            },
        }
        return await self.statuses(hass, link)

    @pytest.mark.parametrize(
        ("held", "value"),
        [
            # REST at 1,000.8 km and the stream's whole kilometres: the car
            # standing still. Taken as miles, it was 608 km ahead.
            (1000800, 1000),
            # REST at 7,670.86 miles and the stream's whole miles.
            (12345000, 7670),
        ],
    )
    async def test_a_rounded_reading_keeps_the_held_odometer(
        self,
        hass: HomeAssistant,
        link: DataHubLink,
        hub: Doubles,
        held: int,
        value: int,
    ) -> None:
        seen = await self.held_at(hass, link, hub, held)
        link._on_message(
            TOPIC, in_unit("Vehicle.TraveledDistance", "1794830000000", value, "km")
        )
        assert seen[-1]["ODOMETER_METER"] == str(held)
        assert link.stats["unconverted"] == 0

    @pytest.mark.parametrize(
        ("held", "value", "when", "expected"),
        [
            # REST at 1,001 km, the stream's 1000 eighty seconds later: just
            # outside the rounding tolerance, and as miles 608 km on.
            (1001000, 1000, "2026-11-07T09:01:20Z", 1001000),
            (1001100, 1000, "2026-11-07T09:01:20Z", 1001100),
            # A kilometre in ten minutes is a drive.
            (12345000, 12346, "2026-11-07T09:10:00Z", 12346000),
            # Fifty-five in ten minutes is not.
            (12345000, 12400, "2026-11-07T09:10:00Z", 12345000),
        ],
    )
    async def test_an_odometer_moves_no_further_than_the_car_could_drive(
        self,
        hass: HomeAssistant,
        link: DataHubLink,
        hub: Doubles,
        held: int,
        value: int,
        when: str,
        expected: int,
    ) -> None:
        # REST's snapshot is stamped 09:00 by the car.
        seen = await self.held_at(hass, link, hub, held)
        link._on_message(TOPIC, in_unit("Vehicle.TraveledDistance", when, value, "km"))
        assert seen[-1]["ODOMETER_METER"] == str(expected)

    @staticmethod
    def precise(when: str, metres: int) -> bytes:
        return json.dumps(
            {"name": "Vehicle.TraveledDistance", "timestamp": when, "value": metres}
        ).encode()

    async def test_a_whole_mile_confirmation_allows_a_mile_on(
        self, hass: HomeAssistant, link: DataHubLink, hub: Doubles
    ) -> None:
        # REST at 09:00; a whole mile at 09:05 confirming it; then a precise
        # 1 km on, two seconds later. The whole mile only said the car was
        # within a mile, and the kilometre was dropped as 1 km in two seconds.
        seen = await self.held_at(hass, link, hub, 12345000)
        link._on_message(
            TOPIC,
            in_unit("Vehicle.TraveledDistance", "2026-11-07T09:05:00Z", 7670, "km"),
        )
        link._on_message(TOPIC, self.precise("2026-11-07T09:05:02Z", 12346000))
        assert seen[-1]["ODOMETER_METER"] == "12346000"

    async def test_a_precise_reading_may_be_a_mile_past_a_whole_mile(
        self, hass: HomeAssistant, link: DataHubLink, hub: Doubles
    ) -> None:
        # A whole mile that ticked the odometer over is up to a mile behind
        # the car; the precise reading a moment later can be that far ahead.
        seen = await self.held_at(hass, link, hub, 12345000)
        link._on_message(
            TOPIC,
            in_unit("Vehicle.TraveledDistance", "2026-11-07T09:01:00Z", 7671, "km"),
        )
        assert seen[-1]["ODOMETER_METER"] == "12345278"
        link._on_message(TOPIC, self.precise("2026-11-07T09:01:02Z", 12346500))
        assert seen[-1]["ODOMETER_METER"] == "12346500"

    async def test_a_confirmation_to_the_metre_restarts_the_clock(
        self, hass: HomeAssistant, link: DataHubLink, hub: Doubles
    ) -> None:
        # REST at 09:00, the stream to the metre at 12:00 saying the same: the
        # car was there at 12:00. Timed from 09:00 instead, 1000 two seconds
        # later passed as 1000 miles, 608 km on.
        seen = await self.held_at(hass, link, hub, 1001000)
        link._on_message(TOPIC, self.precise("2026-11-07T12:00:00Z", 1001000))
        link._on_message(
            TOPIC,
            in_unit("Vehicle.TraveledDistance", "2026-11-07T12:00:02Z", 1000, "km"),
        )
        assert seen[-1]["ODOMETER_METER"] == "1001000"

    async def test_whole_km_confirmations_keep_the_clock_current(
        self, hass: HomeAssistant, link: DataHubLink, hub: Doubles
    ) -> None:
        # A new car parked all day, confirmed in whole kilometres: timed from
        # REST's 09:00, the day allowed the full 1,000 km and 1000 two
        # seconds after the last confirmation passed as 1000 miles.
        seen = await self.held_at(hass, link, hub, 1001000)
        for hour in ("12", "15", "18", "21"):
            link._on_message(
                TOPIC,
                in_unit(
                    "Vehicle.TraveledDistance", f"2026-11-07T{hour}:00:00Z", 1001, "km"
                ),
            )
        link._on_message(
            TOPIC,
            in_unit("Vehicle.TraveledDistance", "2026-11-07T21:00:02Z", 1000, "km"),
        )
        assert seen[-1]["ODOMETER_METER"] == "1001000"

    async def test_neither_lets_the_wrong_scale_back_in(
        self, hass: HomeAssistant, link: DataHubLink, hub: Doubles
    ) -> None:
        # A whole kilometre confirming REST, then 1000 two seconds on: as
        # miles it is still 608 km away.
        seen = await self.held_at(hass, link, hub, 1001000)
        link._on_message(
            TOPIC,
            in_unit("Vehicle.TraveledDistance", "2026-11-07T09:05:00Z", 1001, "km"),
        )
        link._on_message(
            TOPIC,
            in_unit("Vehicle.TraveledDistance", "2026-11-07T09:05:02Z", 1000, "km"),
        )
        assert seen[-1]["ODOMETER_METER"] == "1001000"

    @pytest.mark.parametrize(
        ("value", "expected"), [(12346, 12346000), (12348, 12345000)]
    )
    async def test_with_no_time_held_only_one_unit_ahead_is_believed(
        self,
        hass: HomeAssistant,
        link: DataHubLink,
        hub: Doubles,
        value: int,
        expected: int,
    ) -> None:
        # No time to say how far the car could have gone: a kilometre ticking
        # over is believed, anything more waits for REST.
        snapshot = dict(hub.datahub.status_result)
        snapshot.pop("timestamp")
        hub.datahub.status_result = snapshot
        await self.statuses(hass, link)
        link._on_message(
            TOPIC,
            in_unit("Vehicle.TraveledDistance", "2026-11-07T10:00:00Z", value, "km"),
        )
        assert link.signals[KEPT]["Vehicle.TraveledDistance"]["value"] == expected

    async def test_a_range_infinite_once_converted_is_dropped_alone(
        self, hass: HomeAssistant, link: DataHubLink, hub: Doubles
    ) -> None:
        # 1e308 miles is finite; as kilometres it is not, and writing the
        # status out raised, losing the rest of the message.
        seen = await self.statuses(hass, link)
        before = seen[-1].get("EV_RANGE_ON_BATTERY_KM")
        signal = {"dataType": "double", "signalTimestamp": "1794830000000"}
        message_ = [
            {
                **signal,
                "vssIdentifier": "Vehicle.Powertrain.TractionBattery.Range",
                "signalValue": "1e308",
                "unit": "mi",
            },
            {
                **signal,
                "vssIdentifier": "Vehicle.Powertrain.FuelSystem.RelativeLevel",
                "signalValue": "55",
                "unit": "percent",
            },
        ]
        link._on_message(TOPIC, json.dumps(message_).encode())
        assert seen[-1].get("EV_RANGE_ON_BATTERY_KM") == before
        assert seen[-1]["FUEL_LEVEL_PERC"] == "55"
        assert link.stats["unconverted"] == 1

    @pytest.mark.parametrize(
        "name",
        ["Vehicle.TraveledDistance", "Vehicle.Powertrain.FuelSystem.Range"],
    )
    @pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity", "1e400"])
    async def test_a_number_that_is_not_finite_is_dropped_alone(
        self,
        hass: HomeAssistant,
        link: DataHubLink,
        hub: Doubles,
        name: str,
        text: str,
    ) -> None:
        # It raised on rounding, which lost the rest of the message and closed
        # the stream session.
        seen = await self.statuses(hass, link)
        before = dict(seen[-1])
        signal = {"dataType": "double", "signalTimestamp": "1794830000000"}
        message_ = [
            {**signal, "vssIdentifier": name, "signalValue": text, "unit": "km"},
            {
                **signal,
                "vssIdentifier": "Vehicle.Powertrain.FuelSystem.RelativeLevel",
                "signalValue": "55",
                "unit": "percent",
            },
        ]
        link._on_message(TOPIC, json.dumps(message_).encode())
        assert seen[-1]["ODOMETER_METER"] == before["ODOMETER_METER"]
        assert seen[-1].get("DISTANCE_TO_EMPTY_FUEL") == before.get(
            "DISTANCE_TO_EMPTY_FUEL"
        )
        assert seen[-1]["FUEL_LEVEL_PERC"] == "55"
        assert link.stats["unconverted"] == 1

    @pytest.mark.parametrize(
        "name",
        ["Vehicle.TraveledDistance", "Vehicle.Powertrain.FuelSystem.Range"],
    )
    async def test_a_number_too_big_for_a_float_is_dropped(
        self, hass: HomeAssistant, link: DataHubLink, hub: Doubles, name: str
    ) -> None:
        seen = await self.statuses(hass, link)
        before = dict(seen[-1])
        # JSON allows an integer of any length; a float does not.
        huge = (
            f'{{"name": "{name}", "timestamp": "2026-11-07T10:00:00Z",'
            f' "unit": "km", "value": 1{"0" * 400}}}'
        )
        link._on_message(TOPIC, huge.encode())
        assert seen[-1]["ODOMETER_METER"] == before["ODOMETER_METER"]
        assert link.stats["unconverted"] == 1

    async def test_a_lower_odometer_is_not_taken(
        self, hass: HomeAssistant, link: DataHubLink, hub: Doubles
    ) -> None:
        # Whatever the reason, unit or otherwise: taken, Home Assistant counts
        # a total_increasing sensor going down as the meter being reset, and
        # adds the whole mileage again when the right figure returns.
        seen = await self.statuses(hass, link)
        link._on_message(
            TOPIC, in_unit("Vehicle.TraveledDistance", "1794830000000", 19156, None)
        )
        assert seen[-1]["ODOMETER_METER"] == "12345000"
        assert link.stats["unconverted"] == 1

    async def test_a_range_in_miles_is_read_in_km(
        self, hass: HomeAssistant, link: DataHubLink, hub: Doubles
    ) -> None:
        seen = await self.statuses(hass, link)
        link._on_message(
            TOPIC,
            in_unit(
                "Vehicle.Powertrain.FuelSystem.Range", "1794830000000", 125, "miles"
            ),
        )
        assert seen[-1]["DISTANCE_TO_EMPTY_FUEL"] == "201.2"

    async def test_a_unit_not_known_here_is_left_for_rest(
        self, hass: HomeAssistant, link: DataHubLink, hub: Doubles
    ) -> None:
        seen = await self.statuses(hass, link)
        before = seen[-1].get("DISTANCE_TO_EMPTY_FUEL")
        link._on_message(
            TOPIC,
            in_unit(
                "Vehicle.Powertrain.FuelSystem.Range", "1794830000000", 9, "furlong"
            ),
        )
        assert seen[-1].get("DISTANCE_TO_EMPTY_FUEL") == before
        assert link.stats["unconverted"] == 1
        assert link.stats["units"]["Vehicle.Powertrain.FuelSystem.Range"] == "furlong"

    async def test_a_reading_that_is_not_a_distance_keeps_its_unit(
        self, hass: HomeAssistant, link: DataHubLink, hub: Doubles
    ) -> None:
        seen = await self.statuses(hass, link)
        link._on_message(
            TOPIC,
            in_unit(
                "Vehicle.Powertrain.FuelSystem.RelativeLevel",
                "1794830000000",
                55,
                "percent",
            ),
        )
        assert seen[-1]["FUEL_LEVEL_PERC"] == "55"


class TestWhatTheStreamProves:
    """The first drive on the stream brought thirteen messages and not one
    reading newer than REST had, and the link went hourly on the count."""

    async def test_a_message_shaped_like_the_rest_snapshot_is_read(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        statuses: list[dict[str, str]] = []
        link = DataHubLink(hass, linked, on_status=lambda _, s: statuses.append(s))
        await link._async_read_status()
        snapshot = {
            "timestamp": "2026-11-07T10:00:00.000Z",
            "Vehicle": {
                "VehicleIdentification": {"VIN": KEPT},
                "TraveledDistance": 12346000,
            },
        }
        link._on_message(TOPIC, json.dumps(snapshot).encode())
        held = link.signals[KEPT]["Vehicle.TraveledDistance"]
        assert held["value"] == 12346000
        assert "Vehicle.VehicleIdentification.VIN" not in link.signals[KEPT]
        assert link.reports[KEPT].isoformat() == "2026-11-07T10:00:00+00:00"
        assert statuses[-1]["ODOMETER_METER"] == "12346000"
        assert link.stats["fresh"] == 1
        assert link.stats["unread"] == 0

    async def test_a_snapshot_shaped_fix_is_handed_on(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        positions: list[dict[str, Any]] = []
        link = DataHubLink(hass, linked, on_position=lambda _, p: positions.append(p))
        snapshot = {
            "timestamp": "2026-11-07T10:00:00Z",
            "Vehicle": {"CurrentLocation": {"Latitude": 51.5, "Longitude": -0.12}},
        }
        link._on_message(POSITION_TOPIC, json.dumps(snapshot).encode())
        assert positions[-1]["latitude"] == 51.5

    async def test_a_redelivery_of_what_rest_brought_proves_nothing(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        await link._async_read_status()
        link._on_message(
            TOPIC,
            message(
                "Vehicle.Powertrain.FuelSystem.RelativeLevel", "2026-11-07T09:00:00Z", 5
            ),
        )
        link._on_message(
            TOPIC,
            message(
                "Vehicle.Powertrain.FuelSystem.RelativeLevel", "2026-11-07T08:00:00Z", 4
            ),
        )
        assert link.stats["messages"] == 2
        assert link.stats["fresh"] == 0
        assert link.stats["unread"] == 0
        assert link.diagnostics()["stats"]["last_message"]

    async def test_a_message_before_the_first_snapshot_proves_nothing(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        # What the broker holds and sends on subscribing is old, and REST
        # has not said how old yet.
        link = DataHubLink(hass, linked)
        link._on_message(
            TOPIC,
            message(
                "Vehicle.Powertrain.FuelSystem.RelativeLevel", "2026-11-07T10:00:00Z"
            ),
        )
        assert link.stats["fresh"] == 0
        await link._async_read_status()
        link._on_message(
            TOPIC,
            message(
                "Vehicle.Powertrain.FuelSystem.RelativeLevel", "2026-11-07T11:00:00Z"
            ),
        )
        assert link.stats["fresh"] == 1

    async def test_an_unread_message_is_described_by_its_keys_alone(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        odd = {"signal": "Vehicle.Speed", "ts": 1, "v": 51.5}
        link._on_message(TOPIC, json.dumps(odd).encode())
        link._on_message(POSITION_TOPIC, json.dumps([odd, odd]).encode())
        link._on_message(TOPIC, b"[]")
        stats = link.diagnostics()["stats"]
        assert stats["unread"] == 3
        assert stats["unread_shape"] == "[0]"
        link._on_message(POSITION_TOPIC, json.dumps([odd]).encode())
        stats = link.diagnostics()["stats"]
        assert stats["unread_shape"] == "[1] {signal, ts, v}"
        assert "51.5" not in json.dumps(link.diagnostics(), default=str)
        link._on_message(TOPIC, b"7")
        assert link.stats["unread_shape"] == "int"


def sent_by_broker(
    vss: str | None,
    value: Any,
    when: Any,
    data_type: str = "string",
    name: str | None = "signal",
) -> dict[str, Any]:
    """One signal as the broker really sends it (measured, not documented)."""
    return {
        "dataType": data_type,
        "description": "made up",
        "signalName": name,
        "signalTimestamp": when,
        "signalValue": value,
        "unit": None,
        "vssIdentifier": vss,
    }


class TestWhatTheBrokerReallySends:
    """Five messages on a drive in Australia, every one unread by 2.0.2."""

    async def test_a_batch_in_the_brokers_shape_is_read(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        statuses: list[dict[str, str]] = []
        link = DataHubLink(hass, linked, on_status=lambda _, s: statuses.append(s))
        await link._async_read_status()
        batch = [
            sent_by_broker(
                "Vehicle.TraveledDistance", "12346000", "2026-11-07T10:00:00Z", "double"
            ),
            sent_by_broker(
                "Vehicle.Body.Trunk.Rear.IsOpen", "OPEN", "2026-11-07T10:00:00Z"
            ),
        ]
        link._on_message(TOPIC, json.dumps(batch).encode())
        held = link.signals[KEPT]
        assert held["Vehicle.TraveledDistance"]["value"] == 12346000
        assert held["Vehicle.Body.Trunk.Rear.IsOpen"]["value"] == "OPEN"
        assert statuses[-1]["ODOMETER_METER"] == "12346000"
        assert link.stats["fresh"] == 1
        assert link.stats["unread"] == 0

    async def test_an_epoch_time_and_a_fractional_number_are_read(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        # 2026-11-07T10:00:00Z in milliseconds.
        sent = sent_by_broker("Vehicle.Speed", "51.5", 1794045600000, "float")
        link._on_message(TOPIC, json.dumps([sent]).encode())
        held = link.signals[KEPT]["Vehicle.Speed"]
        assert held["value"] == 51.5
        assert held["time"].isoformat() == "2026-11-07T10:00:00+00:00"

    async def test_the_signal_name_stands_in_for_a_missing_vss_path(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        sent = sent_by_broker(None, "x", "2026-11-07T10:00:00Z", name="Vehicle.A")
        link._on_message(TOPIC, json.dumps([sent]).encode())
        assert "Vehicle.A" in link.signals[KEPT]
        nameless = sent_by_broker(None, "x", "2026-11-07T10:00:00Z", name=None)
        link._on_message(TOPIC, json.dumps([nameless]).encode())
        assert link.stats["unread"] == 1

    async def test_text_in_a_number_field_is_kept_as_sent(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        sent = sent_by_broker("Vehicle.A", "UNKNOWN", "2026-11-07T10:00:00Z", "double")
        link._on_message(TOPIC, json.dumps([sent]).encode())
        assert link.signals[KEPT]["Vehicle.A"]["value"] == "UNKNOWN"

    async def test_a_fix_in_the_brokers_shape_is_handed_on(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        positions: list[dict[str, Any]] = []
        link = DataHubLink(hass, linked, on_position=lambda _, p: positions.append(p))
        when = "2026-11-07T10:00:00Z"
        batch = [
            sent_by_broker("Vehicle.CurrentLocation.Latitude", "51.5", when, "double"),
            sent_by_broker(
                "Vehicle.CurrentLocation.Longitude", "-0.12", when, "double"
            ),
        ]
        link._on_message(POSITION_TOPIC, json.dumps(batch).encode())
        assert positions[-1]["latitude"] == 51.5
        assert positions[-1]["longitude"] == -0.12


class TestTheTimesTheStreamSends:
    """An I-Pace's streamed fixes went unread for want of a time."""

    async def test_a_fix_timed_in_epoch_text_is_handed_on(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        positions: list[dict[str, Any]] = []
        link = DataHubLink(hass, linked, on_position=lambda _, p: positions.append(p))
        # 2026-11-07T10:00:00Z in milliseconds, sent as text.
        when = "1794045600000"
        batch = [
            sent_by_broker("Vehicle.CurrentLocation.Latitude", "51.5", when, "double"),
            sent_by_broker(
                "Vehicle.CurrentLocation.Longitude", "-0.12", when, "double"
            ),
        ]
        link._on_message(POSITION_TOPIC, json.dumps(batch).encode())
        assert positions[-1]["timestamp"] == "2026-11-07T10:00:00+00:00"
        assert link.stats["untimed"] == 0

    @pytest.mark.parametrize(
        "when",
        [
            "2026-11-07T10:00:00.000Z",
            "1794045600000",
            "2026-11-07 10:00:00 +0000 UTC",
            "2026-11-07T10:00:00Z[UTC]",
            "Sat Nov 07 10:00:00 UTC 2026",
            "2026-11-07 10:00:00",
        ],
    )
    async def test_a_newer_reading_in_any_common_time_form_is_news(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, when: str
    ) -> None:
        # The snapshot double reports 09:00; each of these is 10:00 UTC.
        link = DataHubLink(hass, linked)
        await link._async_read_status()
        sent = sent_by_broker("Vehicle.TraveledDistance", "12346000", when, "double")
        link._on_message(TOPIC, json.dumps([sent]).encode())
        assert link.stats["fresh"] == 1
        assert link.reports[KEPT].isoformat() == "2026-11-07T10:00:00+00:00"

    async def test_a_time_it_cannot_read_is_counted_and_shaped(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        await link._async_read_status()
        sent = sent_by_broker("Vehicle.A", "x", "8 Nov 2026 19:58 UTC")
        link._on_message(TOPIC, json.dumps([sent]).encode())
        assert link.stats["untimed"] == 1
        assert link.stats["untimed_format"] == "9 Nov 9999 99:99 UTC"
        assert link.stats["fresh"] == 0
        link._on_message(
            TOPIC, json.dumps([{**sent, "signalTimestamp": None}]).encode()
        )
        assert link.stats["untimed_format"] == "NoneType"

    async def test_the_fix_time_survives_the_redaction(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        when = "2026-11-07T10:00:00Z"
        batch = [
            sent_by_broker("Vehicle.CurrentLocation.Latitude", "51.5", when, "double"),
            sent_by_broker(
                "Vehicle.CurrentLocation.Longitude", "-0.12", when, "double"
            ),
        ]
        link._on_message(POSITION_TOPIC, json.dumps(batch).encode())
        (car,) = scrub(link.diagnostics())["vehicles"].values()
        assert car["fix_time"].isoformat() == "2026-11-07T10:00:00+00:00"
        assert "51.5" not in json.dumps(scrub(link.diagnostics()), default=str)


POSITION_TOPIC = f"account/acct-1/vehicles/{KEPT}/position"


def fix(when: str, latitude: Any = 51.5, longitude: Any = -0.12) -> list[dict]:
    return [
        {
            "name": "Vehicle.CurrentLocation.Latitude",
            "timestamp": when,
            "value": latitude,
        },
        {
            "name": "Vehicle.CurrentLocation.Longitude",
            "timestamp": when,
            "value": longitude,
        },
    ]


class TestPositionOnTheStream:
    async def test_a_fix_on_the_stream_is_handed_on(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        positions: list[tuple[str, dict[str, Any]]] = []
        link = DataHubLink(
            hass, linked, on_position=lambda v, p: positions.append((v, p))
        )
        batch = [
            *fix("2026-11-07T15:00:00Z"),
            {
                "name": "Vehicle.CurrentLocation.Heading",
                "timestamp": "2026-11-07T15:00:00Z",
                "value": 90,
            },
        ]
        link._on_message(POSITION_TOPIC, json.dumps(batch).encode())
        assert positions == [
            (
                KEPT,
                {
                    "latitude": 51.5,
                    "longitude": -0.12,
                    "heading": 90.0,
                    "speed": None,
                    "timestamp": "2026-11-07T15:00:00+00:00",
                },
            )
        ]

    async def test_half_a_fix_waits_for_the_other_half(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        # A new latitude with the last fix's longitude is a place the car
        # never was.
        positions: list[dict[str, Any]] = []
        link = DataHubLink(hass, linked, on_position=lambda _, p: positions.append(p))
        link._on_message(
            POSITION_TOPIC, json.dumps(fix("2026-11-07T15:00:00Z")).encode()
        )
        latitude, longitude = fix("2026-11-07T16:00:00Z", 52.0, -1.0)
        link._on_message(POSITION_TOPIC, json.dumps(latitude).encode())
        assert len(positions) == 1
        link._on_message(POSITION_TOPIC, json.dumps(longitude).encode())
        assert len(positions) == 2
        assert positions[-1]["latitude"] == 52.0
        assert positions[-1]["longitude"] == -1.0

    async def test_a_status_signal_is_not_a_fix(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        positions: list[dict[str, Any]] = []
        link = DataHubLink(hass, linked, on_position=lambda _, p: positions.append(p))
        link._on_message(
            POSITION_TOPIC,
            json.dumps(fix("2026-11-07T15:00:00Z")[:1]).encode(),
        )
        link._on_message(
            TOPIC, message("Vehicle.TraveledDistance", "2026-11-07T15:00:00Z")
        )
        assert positions == []

    async def test_an_unreadable_fix_is_not_handed_on(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        positions: list[dict[str, Any]] = []
        link = DataHubLink(hass, linked, on_position=lambda _, p: positions.append(p))
        link._on_message(
            POSITION_TOPIC,
            json.dumps(fix("2026-11-07T15:00:00Z", latitude="unknown")).encode(),
        )
        assert positions == []

    async def test_without_a_listener_a_fix_is_only_held(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        link._on_message(
            POSITION_TOPIC, json.dumps(fix("2026-11-07T15:00:00Z")).encode()
        )
        assert link.signals[KEPT]["Vehicle.CurrentLocation.Latitude"]["value"] == 51.5


class TestStreamOrdering:
    async def test_an_older_reading_does_not_replace_a_newer_one(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        # JLR deliver late and out of order. A delayed "unlocked" replaced a
        # newer "locked" while the newer time stayed on the car.
        link = DataHubLink(hass, linked)
        name = "Vehicle.Body.Door.Lock.Status"
        link._on_message(TOPIC, message(name, "2026-11-07T15:00:00Z", "TRUE"))
        link._on_message(TOPIC, message(name, "2026-11-07T14:00:00Z", "FALSE"))
        assert link.signals[KEPT][name]["value"] == "TRUE"
        assert link.reports[KEPT].isoformat() == "2026-11-07T15:00:00+00:00"

    async def test_a_reading_with_the_same_time_is_taken(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        name = "Vehicle.Body.Door.Lock.Status"
        link._on_message(TOPIC, message(name, "2026-11-07T15:00:00Z", "TRUE"))
        link._on_message(TOPIC, message(name, "2026-11-07T15:00:00Z", "FALSE"))
        assert link.signals[KEPT][name]["value"] == "FALSE"

    async def test_the_stream_says_nothing_before_the_first_full_read(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        # One streamed signal is not a car's status. Passed on before the
        # REST snapshot, it was taken for the whole of it at start-up.
        seen: list[dict[str, str]] = []
        link = DataHubLink(hass, linked, on_status=lambda _, s: seen.append(s))
        link._on_message(
            TOPIC,
            message(
                "Vehicle.Powertrain.FuelSystem.RelativeLevel", "2026-11-07T15:00:00Z", 9
            ),
        )
        assert seen == []
        await link._async_read_status()
        assert seen and "ODOMETER" in seen[-1]
        # The streamed value, newer than the snapshot, is in what was handed on.
        assert seen[-1]["FUEL_LEVEL_PERC"] == "9"
        link._on_message(
            TOPIC,
            message(
                "Vehicle.Powertrain.FuelSystem.RelativeLevel",
                "2026-11-07T16:00:00Z",
                10,
            ),
        )
        assert seen[-1]["FUEL_LEVEL_PERC"] == "10"


def journey(
    started: str,
    ended: str,
    km: float = 4.0,
    kmh: float = 43.24,
    seconds: int = 333,
    distance_unit: str = "km",
    speed_unit: str = "km/h",
) -> dict[str, Any]:
    """One journey as JLR list it, with made-up values.

    Its id from its end time: JLR's are unique, and journeys are told apart
    by them.
    """
    return {
        "journeyId": "".join(c for c in ended if c.isdigit())[:14],
        "startTime": started,
        "endTime": ended,
        "duration": seconds,
        "distance": {"value": km, "unit": distance_unit},
        "averageSpeed": {"value": kmh, "unit": speed_unit},
        "startLocation": {"latitude": "51.5000", "longitude": "-0.1200"},
        "endLocation": {"latitude": "51.6000", "longitude": "-0.1300"},
    }


def journeys(*items: dict[str, Any], pages: int = 1) -> dict[str, Any]:
    return {
        "items": list(items),
        "page": {
            "totalElements": len(items),
            "totalPages": pages,
            "currentPage": 0,
            "pageSize": 20,
        },
    }


OCTOBER = journey("2026-10-30T14:50:56Z", "2026-10-30T17:28:35Z", 172.0, 65.46, 9459)
SHORT = journey("2026-10-31T13:35:53Z", "2026-10-31T13:41:26Z")


class Pauses:
    """Waits that return at once, recorded."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


class Clock:
    """Monotonic time for the link, moved on only by its own waits.

    ``at`` runs a step once the clock reaches a time, so a fix can arrive in
    the middle of a wait the way it would on the road.
    """

    def __init__(self, link: DataHubLink) -> None:
        self.now = 1000.0
        self.waits: list[float] = []
        self.steps: list[tuple[float, Any]] = []
        link._pause = self.pause

    def monotonic(self) -> float:
        return self.now

    def at(self, when: float, step: Any) -> None:
        self.steps.append((when, step))

    async def pause(self, seconds: float) -> None:
        # Suspends, as a real wait does. Home Assistant starts background
        # tasks eagerly, and a wait that never yields runs the whole task
        # inside the call that started it.
        await asyncio.sleep(0)
        self.waits.append(seconds)
        end = self.now + seconds
        for when, step in sorted(self.steps, key=lambda s: s[0]):
            if self.now < when <= end:
                self.now = when
                step()
                # Room for the link to cut this wait short, leaving the
                # clock where the step happened, as a real wait would.
                for _ in range(5):
                    await asyncio.sleep(0)
        self.now = end


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The link's clock, replaced; asyncio's own is left alone."""

    def install(link: DataHubLink) -> Clock:
        fake = Clock(link)
        monkeypatch.setattr(
            datahub_link,
            "time",
            type("FakeTime", (), {"monotonic": fake.monotonic, "time": time.time}),
        )
        return fake

    return install


class TestJourneys:
    async def test_the_last_journey_is_kept_without_where_it_went(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        hub.datahub.journeys_result = journeys(OCTOBER, SHORT)
        await link._async_read_journeys()
        held = link.journeys[KEPT]
        assert held["ended"].isoformat() == "2026-10-31T13:41:26+00:00"
        assert held["started"].isoformat() == "2026-10-31T13:35:53+00:00"
        assert held["duration"] == 333
        assert held["distance"] == 4.0
        assert held["average_speed"] == 43.24
        # Where a car has been is not something this integration keeps.
        assert "51.6000" not in repr(link.journeys)
        assert link.journey_access[KEPT] == "permitted"

    async def test_the_newest_is_on_the_last_page(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        # Oldest first, twenty to a page: one owner's list ran from October.
        link = DataHubLink(hass, linked)
        latest = journey("2026-11-08T18:00:00Z", "2026-11-08T18:30:00Z", 30.0)
        hub.datahub.journeys_result = [
            journeys(*[OCTOBER] * 20, pages=2),
            journeys(latest, pages=2),
        ]
        await link._async_read_journeys()
        assert [c for c in hub.datahub.calls if c.startswith("journeys")] == [
            "journeys 0",
            "journeys 1",
        ]
        assert link.journeys[KEPT]["distance"] == 30.0

    async def test_a_short_last_page_is_made_up_from_the_one_before(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        # Twenty are kept; a last page of one journey needs the page before.
        # Asked only when it is short, so a full page costs nothing more.
        link = DataHubLink(hass, linked)
        older = [
            journey(f"2026-11-0{d}T08:00:00Z", f"2026-11-0{d}T08:30:00Z")
            for d in range(1, 8)
        ]
        latest = journey("2026-11-08T18:00:00Z", "2026-11-08T18:30:00Z", 30.0)
        hub.datahub.journeys_result = [
            journeys(OCTOBER, SHORT, pages=3),
            journeys(latest, pages=3),
            journeys(*older, pages=3),
        ]
        await link._async_read_journeys()
        assert [c for c in hub.datahub.calls if c.startswith("journeys")] == [
            "journeys 0",
            "journeys 2",
            "journeys 1",
        ]
        recent = link.recent_journeys[KEPT]
        assert recent[0]["ended"] == "2026-11-08T18:30:00+00:00"
        assert len(recent) == 10

    async def test_a_failed_older_page_costs_only_the_history(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        # The older page is only there to make up twenty. When it failed, the
        # newest journey went with it, and the last-journey sensors stayed on
        # the trip before.
        link = DataHubLink(hass, linked)
        latest = journey("2026-11-08T18:00:00Z", "2026-11-08T18:30:00Z", 42.0)
        hub.datahub.journeys_result = [
            journeys(OCTOBER, SHORT, pages=3),
            journeys(latest, pages=3),
            DataHubError("500"),
        ]
        await link._async_read_journeys()
        assert link.journeys[KEPT]["distance"] == 42.0
        assert link.recent_journeys[KEPT][0]["distance_km"] == 42.0
        assert link.journey_access[KEPT] == "permitted"

    async def test_a_refused_link_on_the_older_page_still_signs_out(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        latest = journey("2026-11-08T18:00:00Z", "2026-11-08T18:30:00Z")
        hub.datahub.journeys_result = [
            journeys(OCTOBER, SHORT, pages=3),
            journeys(latest, pages=3),
            DataHubAuthError("invalid_grant"),
        ]
        with pytest.raises(DataHubAuthError):
            await link._async_read_journeys()

    async def test_a_read_that_ends_late_cannot_roll_the_list_back(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        # The timer's read and the read after a trip overlap; the older answer
        # finishing last replaced the list, and it disagreed with the
        # last-journey sensors, which had kept the newer trip.
        link = DataHubLink(hass, linked)
        latest = journey("2026-11-08T18:00:00Z", "2026-11-08T18:30:00Z", 42.0)
        hub.datahub.journeys_result = journeys(OCTOBER, SHORT, latest)
        await link._async_read_journeys()
        # Named, as the read after a fix does: not due again on the timer.
        hub.datahub.journeys_result = journeys(OCTOBER, SHORT)
        await link._async_read_journeys({KEPT})
        assert hub.datahub.calls.count("journeys 0") == 2
        recent = link.recent_journeys[KEPT]
        assert recent[0]["ended"] == link.journeys[KEPT]["ended"].isoformat()
        assert [j["distance_km"] for j in recent] == [42.0, 4.0, 172.0]

    async def test_a_start_time_filled_in_later_is_still_one_journey(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        # Keyed by its times, the copy with the start time was a second trip.
        link = DataHubLink(hass, linked)
        bare = journey("2026-11-08T18:00:00Z", "2026-11-08T18:30:00Z")
        bare["startTime"] = None
        hub.datahub.journeys_result = journeys(bare)
        await link._async_read_journeys()
        hub.datahub.journeys_result = journeys(
            journey("2026-11-08T18:00:00Z", "2026-11-08T18:30:00Z")
        )
        await link._async_read_journeys({KEPT})
        (held,) = link.recent_journeys[KEPT]
        assert held["started"] == "2026-11-08T18:00:00+00:00"

    async def test_a_journey_given_its_id_later_is_still_one_journey(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        unnamed = journey("2026-11-08T18:00:00Z", "2026-11-08T18:30:00Z")
        del unnamed["journeyId"]
        hub.datahub.journeys_result = journeys(unnamed)
        await link._async_read_journeys()
        hub.datahub.journeys_result = journeys(
            journey("2026-11-08T18:00:00Z", "2026-11-08T18:30:00Z")
        )
        await link._async_read_journeys({KEPT})
        (held,) = link.recent_journeys[KEPT]
        assert held["journey_id"] == "20261108183000"

    async def test_a_rate_limit_on_the_older_page_keeps_the_trip_and_the_wait(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        # Swallowed, a 60-minute Retry-After on the older page was followed
        # by the next read 15 minutes later.
        link, pause = bare_link(hass, linked, 1)
        latest = journey("2026-11-08T18:00:00Z", "2026-11-08T18:30:00Z", 42.0)
        hub.datahub.journeys_result = [
            journeys(OCTOBER, SHORT, pages=3),
            journeys(latest, pages=3),
            DataHubRateLimited(3600),
        ]
        await run_status(link)
        assert link.journeys[KEPT]["distance"] == 42.0
        assert link.recent_journeys[KEPT][0]["distance_km"] == 42.0
        assert pause.waits[0] >= 3600

    async def test_journeys_are_ordered_by_time_not_by_text(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        # 18:30+01:00 is 17:30 in UTC, before 18:00Z, whatever the text says.
        link = DataHubLink(hass, linked)
        hub.datahub.journeys_result = journeys(
            journey("2026-11-08T17:50:00Z", "2026-11-08T18:00:00Z", 1.0),
            journey("2026-11-08T18:00:00+01:00", "2026-11-08T18:30:00+01:00", 2.0),
        )
        await link._async_read_journeys()
        assert [j["distance_km"] for j in link.recent_journeys[KEPT]] == [1.0, 2.0]

    async def test_recent_journeys_are_newest_first_with_where_they_went(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        hub.datahub.journeys_result = journeys(OCTOBER, SHORT)
        await link._async_read_journeys()
        newest, oldest = link.recent_journeys[KEPT]
        assert newest == {
            "journey_id": "20261031134126",
            "started": "2026-10-31T13:35:53+00:00",
            "ended": "2026-10-31T13:41:26+00:00",
            "duration_min": 5.5,
            "distance_km": 4.0,
            "average_speed_kmh": 43.24,
            "start": {"latitude": 51.5, "longitude": -0.12},
            "end": {"latitude": 51.6, "longitude": -0.13},
        }
        assert oldest["distance_km"] == 172.0

    async def test_no_more_than_twenty_are_kept(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        many = [
            journey(f"2026-11-{d:02d}T08:00:00Z", f"2026-11-{d:02d}T08:30:00Z")
            for d in range(1, 26)
        ]
        hub.datahub.journeys_result = journeys(*many)
        await link._async_read_journeys()
        recent = link.recent_journeys[KEPT]
        assert len(recent) == 20
        assert recent[0]["ended"].startswith("2026-11-25")
        assert recent[-1]["ended"].startswith("2026-11-06")

    async def test_energy_and_a_place_jlr_garbled_are_read_as_far_as_they_go(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        odd = journey("2026-11-08T18:00:00Z", "2026-11-08T18:30:00Z")
        odd["energyUsed"] = {"value": 5.2, "unit": "kWh"}
        odd["startLocation"] = {"latitude": "north", "longitude": "-0.1"}
        odd["endLocation"] = None
        odd["duration"] = None
        hub.datahub.journeys_result = journeys(odd)
        await link._async_read_journeys()
        (held,) = link.recent_journeys[KEPT]
        assert held["energy_used_kwh"] == 5.2
        assert held["start"] is None
        assert held["end"] is None
        assert held["duration_min"] is None

    async def test_miles_are_read_as_kilometres(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        hub.datahub.journeys_result = journeys(
            journey(
                "2026-11-08T18:00:00Z",
                "2026-11-08T18:30:00Z",
                km=10.0,
                kmh=20.0,
                distance_unit="mi",
                speed_unit="mph",
            )
        )
        await link._async_read_journeys()
        assert link.journeys[KEPT]["distance"] == 16.09
        assert link.journeys[KEPT]["average_speed"] == 32.19

    async def test_a_reading_in_a_unit_not_known_is_left_out(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        odd = journey("2026-11-08T18:00:00Z", "2026-11-08T18:30:00Z")
        odd["distance"] = {"value": 4.0, "unit": "furlongs"}
        odd["averageSpeed"] = {"value": "fast", "unit": "km/h"}
        odd["duration"] = None
        flat = journey("2026-11-08T17:00:00Z", "2026-11-08T17:30:00Z")
        flat["distance"] = "4 km"
        hub.datahub.journeys_result = journeys(flat, odd, {"endTime": None}, "junk")
        await link._async_read_journeys()
        held = link.journeys[KEPT]
        assert held["distance"] is None
        assert held["average_speed"] is None
        assert held["duration"] is None

    async def test_an_older_answer_does_not_replace_the_journey_held(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        hub.datahub.journeys_result = [journeys(SHORT), journeys(OCTOBER)]
        await link._async_read_journeys()
        link._journeys_due.clear()
        await link._async_read_journeys()
        assert link.journeys[KEPT]["distance"] == 4.0

    async def test_a_refusal_is_asked_again_daily_not_every_status_read(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link, _ = bare_link(hass, linked, steps=3)
        await run_status(link)
        assert hub.datahub.calls.count("status") == 3
        assert hub.datahub.calls.count("journeys 0") == 1
        assert link.journey_access[KEPT].startswith("refused")
        assert KEPT not in link.journeys
        wait = link._journeys_due[KEPT] - time.monotonic()
        assert wait > datahub_link.JOURNEY_REFUSED_RETRY - 60

    async def test_a_car_that_answers_is_read_every_few_hours(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        hub_journeys = journeys(SHORT)
        link, _ = bare_link(hass, linked, steps=3)
        link.client.journeys_result = hub_journeys
        await run_status(link)
        assert hub.datahub.calls.count("journeys 0") == 1
        wait = link._journeys_due[KEPT] - time.monotonic()
        assert wait > datahub_link.JOURNEY_INTERVAL - 60
        assert wait <= datahub_link.JOURNEY_INTERVAL

    async def test_a_failed_read_is_tried_again_at_the_next_status_read(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        hub.datahub.journeys_result = [DataHubError("answered 500"), journeys(SHORT)]
        await link._async_read_journeys()
        assert KEPT not in link.journeys
        await link._async_read_journeys()
        assert link.journeys[KEPT]["distance"] == 4.0

    async def test_a_fix_on_the_stream_reads_the_journey_it_ended(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        # The stream sends a fix only when a journey ends, about half a
        # minute after; JLR are given a few minutes to file the journey.
        link = DataHubLink(hass, linked, on_position=lambda *_: None)
        pauses = Pauses()
        link._pause = pauses
        hub.datahub.journeys_result = journeys(SHORT)
        await link._async_read_journeys()
        drive = journey("2026-11-08T18:00:00Z", "2026-11-08T18:30:00Z", 30.0)
        hub.datahub.journeys_result = journeys(SHORT, drive)
        link._on_message(
            POSITION_TOPIC, json.dumps(fix("2026-11-08T18:30:34Z")).encode()
        )
        assert link._journey_task is not None
        await link._journey_task
        assert pauses.waits == [pytest.approx(datahub_link.JOURNEY_SETTLE, abs=5)]
        assert link.journeys[KEPT]["distance"] == 30.0

    async def test_a_second_trip_soon_after_the_first_is_read(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        # A short stop, then another short drive: its fix is minutes after the
        # held journey ended, not the 34 seconds a redelivered fix is.
        link = DataHubLink(hass, linked, on_position=lambda *_: None)
        link._pause = Pauses()
        first = journey("2026-11-08T18:00:00Z", "2026-11-08T18:30:00Z", 30.0)
        hub.datahub.journeys_result = journeys(first)
        await link._async_read_journeys()
        second = journey("2026-11-08T18:33:00Z", "2026-11-08T18:38:00Z", 3.0)
        hub.datahub.journeys_result = journeys(first, second)
        link._on_message(
            POSITION_TOPIC, json.dumps(fix("2026-11-08T18:38:34Z")).encode()
        )
        assert link._journey_task is not None
        await link._journey_task
        assert link.journeys[KEPT]["distance"] == 3.0

    async def test_a_fix_redelivered_on_reconnect_reads_nothing(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        # The broker redelivers the last fix, stamped as before, every time
        # the stream reconnects. Only the first time it is heard costs a read.
        link = DataHubLink(hass, linked, on_position=lambda *_: None)
        link._pause = Pauses()
        drive = journey("2026-11-08T18:00:00Z", "2026-11-08T18:30:00Z", 30.0)
        hub.datahub.journeys_result = journeys(drive)
        retained = json.dumps(fix("2026-11-08T18:30:34Z")).encode()
        link._on_message(POSITION_TOPIC, retained)
        assert link._journey_task is not None
        await link._journey_task
        for _reconnect in range(3):
            link._on_message(POSITION_TOPIC, retained)
        assert link._journey_task.done()
        assert hub.datahub.calls.count("journeys 0") == 1

    async def test_a_trip_ending_a_minute_after_the_last_is_read(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        # The case the time windows got wrong: no gap between two trips is
        # too short for them to be two trips.
        link = DataHubLink(hass, linked, on_position=lambda *_: None)
        link._pause = Pauses()
        first = journey("2026-11-08T18:00:00Z", "2026-11-08T18:30:00Z", 30.0)
        hub.datahub.journeys_result = journeys(first)
        link._on_message(
            POSITION_TOPIC, json.dumps(fix("2026-11-08T18:30:34Z")).encode()
        )
        assert link._journey_task is not None
        await link._journey_task
        second = journey("2026-11-08T18:30:40Z", "2026-11-08T18:31:10Z", 0.5)
        hub.datahub.journeys_result = journeys(first, second)
        link._on_message(
            POSITION_TOPIC, json.dumps(fix("2026-11-08T18:31:44Z")).encode()
        )
        await link._journey_task
        assert link.journeys[KEPT]["distance"] == 0.5

    async def test_the_same_fix_twice_reads_once(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked, on_position=lambda *_: None)
        link._pause = Pauses()
        hub.datahub.journeys_result = journeys(SHORT)
        batch = json.dumps(fix("2026-11-08T18:30:34Z")).encode()
        link._on_message(POSITION_TOPIC, batch)
        assert link._journey_task is not None
        await link._journey_task
        link._on_message(POSITION_TOPIC, batch)
        assert link._journey_task.done()
        assert hub.datahub.calls.count("journeys 0") == 1

    async def test_a_car_refused_journeys_is_not_asked_on_each_fix(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked, on_position=lambda *_: None)
        await link._async_read_journeys()
        link._on_message(
            POSITION_TOPIC, json.dumps(fix("2026-11-08T18:30:34Z")).encode()
        )
        assert link._journey_task is None

    async def test_signed_out_while_waiting_to_read(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked, on_position=lambda *_: None)
        link._pause = Pauses()
        hub.datahub.refresh_result = DataHubAuthError("invalid_grant")
        link._on_message(
            POSITION_TOPIC, json.dumps(fix("2026-11-08T18:30:34Z")).encode()
        )
        assert link._journey_task is not None
        await link._journey_task
        assert link.signed_out

    async def test_a_failure_after_a_fix_is_left_to_the_timer(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked, on_position=lambda *_: None)
        link._pause = Pauses()
        hub.datahub.journeys_result = DataHubRateLimited(120)
        link._on_message(
            POSITION_TOPIC, json.dumps(fix("2026-11-08T18:30:34Z")).encode()
        )
        assert link._journey_task is not None
        await link._journey_task
        assert KEPT not in link.journeys
        assert not link.signed_out

    async def test_stopping_cancels_a_read_waiting_on_a_fix(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked, on_position=lambda *_: None)
        link._on_message(
            POSITION_TOPIC, json.dumps(fix("2026-11-08T18:30:34Z")).encode()
        )
        task = link._journey_task
        assert task is not None and not task.done()
        await link.async_stop()
        assert task.cancelled()
        assert "journeys 0" not in hub.datahub.calls

    async def test_diagnostics_say_what_jlr_answered_and_when_it_ended(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        hub.datahub.journeys_result = journeys(SHORT)
        await link._async_read_journeys()
        report = link.diagnostics()
        assert list(report["journeys"].values()) == ["permitted"]
        (car,) = report["vehicles"].values()
        assert car["last_journey_end"].isoformat() == "2026-10-31T13:41:26+00:00"

    async def test_a_fix_during_a_read_gets_a_read_of_its_own(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked, on_position=lambda *_: None)
        link._pause = Pauses()
        drive = journey("2026-11-08T19:00:00Z", "2026-11-08T19:30:00Z", 30.0)
        answers = [journeys(SHORT), journeys(SHORT, drive)]

        async def slow(vin: str, token: str, page: int = 0) -> Any:
            hub.datahub.calls.append(f"journeys {page}")
            if len(answers) == 2:
                # Another journey ends while JLR are answering for the first.
                link._on_message(
                    POSITION_TOPIC, json.dumps(fix("2026-11-08T19:30:34Z")).encode()
                )
            return answers.pop(0)

        link.client.async_journeys = slow
        link._on_message(
            POSITION_TOPIC, json.dumps(fix("2026-11-08T18:30:34Z")).encode()
        )
        assert link._journey_task is not None
        await link._journey_task
        assert hub.datahub.calls.count("journeys 0") == 2
        assert link.journeys[KEPT]["distance"] == 30.0

    async def test_energy_used_is_read_by_its_unit(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        # JLR's spec has energyUsed in kWh or litres: electricity or fuel.
        link = DataHubLink(hass, linked)
        electric = journey("2026-11-08T17:00:00Z", "2026-11-08T17:30:00Z")
        electric["energyUsed"] = {"value": 6.25, "unit": "kWh"}
        hub.datahub.journeys_result = journeys(electric)
        await link._async_read_journeys()
        assert link.journeys[KEPT]["energy_used"] == 6.25
        assert "fuel_used" not in link.journeys[KEPT]
        burned = journey("2026-11-09T17:00:00Z", "2026-11-09T17:30:00Z")
        burned["energyUsed"] = {"value": 2.4, "unit": "litres"}
        hub.datahub.journeys_result = journeys(burned)
        link._journeys_due.clear()
        await link._async_read_journeys()
        assert link.journeys[KEPT]["fuel_used"] == 2.4
        assert "energy_used" not in link.journeys[KEPT]

    async def test_energy_in_a_unit_not_known_is_left_out(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        odd = journey("2026-11-08T17:00:00Z", "2026-11-08T17:30:00Z")
        odd["energyUsed"] = {"value": 3, "unit": "gallons"}
        later = journey("2026-11-08T18:00:00Z", "2026-11-08T18:30:00Z")
        later["energyUsed"] = {"value": "some", "unit": "kWh"}
        hub.datahub.journeys_result = journeys(odd)
        await link._async_read_journeys()
        assert "fuel_used" not in link.journeys[KEPT]
        link._journeys_due.clear()
        hub.datahub.journeys_result = journeys(later)
        await link._async_read_journeys()
        assert "energy_used" not in link.journeys[KEPT]

    async def test_diagnostics_name_a_journeys_fields_and_never_their_values(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles
    ) -> None:
        link = DataHubLink(hass, linked)
        extra = journey("2026-11-08T17:00:00Z", "2026-11-08T17:30:00Z")
        extra["ecoScore"] = 87
        hub.datahub.journeys_result = journeys(extra)
        await link._async_read_journeys()
        (car,) = link.diagnostics()["vehicles"].values()
        assert "ecoScore" in car["journey_shape"]
        assert "endLocation.latitude" in car["journey_shape"]
        assert "distance.unit" in car["journey_shape"]
        assert "87" not in repr(car) and "51.6000" not in repr(car)

    async def test_a_fix_during_the_wait_keeps_its_own_five_minutes(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, clock: Any
    ) -> None:
        # Two cars. The second's fix arrives four minutes into the first's
        # wait; read with the first, it would have had one minute, not five.
        link = DataHubLink(hass, linked, on_position=lambda *_: None)
        hub.datahub.tokens.vehicles = [KEPT, SOLD]
        time_of = clock(link)
        read: list[tuple[float, set[str]]] = []
        reading = link._async_read_journeys

        async def recorded(vins: set[str] | None = None) -> None:
            read.append((time_of.now, set(vins or ())))
            await reading(vins)

        link._async_read_journeys = recorded
        hub.datahub.journeys_result = journeys(SHORT)
        other = f"account/acct-1/vehicles/{SOLD}/position"
        time_of.at(
            1240.0,
            lambda: link._on_message(
                other, json.dumps(fix("2026-11-08T18:34:34Z")).encode()
            ),
        )
        link._on_message(
            POSITION_TOPIC, json.dumps(fix("2026-11-08T18:30:34Z")).encode()
        )
        assert link._journey_task is not None
        await link._journey_task
        assert read == [(1300.0, {KEPT}), (1540.0, {SOLD})]

    async def test_a_journey_not_filed_yet_is_asked_for_once_more(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, clock: Any
    ) -> None:
        # The read five minutes after the fix beat JLR to filing the drive.
        # Half an hour later it is there; no third read either way.
        link = DataHubLink(hass, linked, on_position=lambda *_: None)
        time_of = clock(link)
        drive = journey("2026-11-08T18:00:00Z", "2026-11-08T18:30:00Z", 30.0)
        hub.datahub.journeys_result = [
            journeys(SHORT),
            journeys(SHORT),
            journeys(SHORT, drive),
        ]
        await link._async_read_journeys()
        link._on_message(
            POSITION_TOPIC, json.dumps(fix("2026-11-08T18:30:34Z")).encode()
        )
        assert link._journey_task is not None
        await link._journey_task
        assert time_of.waits == [
            datahub_link.JOURNEY_SETTLE,
            datahub_link.JOURNEY_UNFILED_RETRY,
        ]
        assert link.journeys[KEPT]["distance"] == 30.0
        assert hub.datahub.calls.count("journeys 0") == 3

    async def test_a_journey_never_filed_is_left_to_the_timer(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, clock: Any
    ) -> None:
        link = DataHubLink(hass, linked, on_position=lambda *_: None)
        clock(link)
        hub.datahub.journeys_result = journeys(SHORT)
        await link._async_read_journeys()
        link._on_message(
            POSITION_TOPIC, json.dumps(fix("2026-11-08T18:30:34Z")).encode()
        )
        assert link._journey_task is not None
        await link._journey_task
        # The first read, the one after the fix, and one more: then the timer.
        assert hub.datahub.calls.count("journeys 0") == 3
        assert not link._journeys_after_fix

    async def test_a_fix_during_the_retry_wait_is_read_five_minutes_after_it(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, clock: Any
    ) -> None:
        # The read after the first fix found nothing new, so the car waits
        # half an hour for a retry. A new journey ends a minute into that:
        # it is read five minutes after its own fix, not after the retry.
        link = DataHubLink(hass, linked, on_position=lambda *_: None)
        time_of = clock(link)
        read: list[float] = []
        reading = link._async_read_journeys

        async def recorded(vins: set[str] | None = None) -> None:
            read.append(time_of.now)
            await reading(vins)

        drive = journey("2026-11-08T19:00:00Z", "2026-11-08T19:05:00Z", 4.0)
        # Before the fix; after it, with nothing new; after the second fix.
        hub.datahub.journeys_result = [
            journeys(SHORT),
            journeys(SHORT),
            journeys(SHORT, drive),
        ]
        await link._async_read_journeys()
        link._async_read_journeys = recorded
        time_of.at(
            1360.0,
            lambda: link._on_message(
                POSITION_TOPIC, json.dumps(fix("2026-11-08T19:05:34Z")).encode()
            ),
        )
        link._on_message(
            POSITION_TOPIC, json.dumps(fix("2026-11-08T18:30:34Z")).encode()
        )
        assert link._journey_task is not None
        await link._journey_task
        assert read == [1300.0, 1660.0]
        assert link.journeys[KEPT]["distance"] == 4.0

    async def test_a_refusal_after_a_fix_is_not_retried(
        self, hass: HomeAssistant, linked: MockConfigEntry, hub: Doubles, clock: Any
    ) -> None:
        # Refused journeys are asked about daily. A retry naming the car
        # would have gone round that half an hour later.
        link = DataHubLink(hass, linked, on_position=lambda *_: None)
        clock(link)
        link._on_message(
            POSITION_TOPIC, json.dumps(fix("2026-11-08T18:30:34Z")).encode()
        )
        assert link._journey_task is not None
        await link._journey_task
        assert hub.datahub.calls.count("journeys 0") == 1
        assert link.journey_access[KEPT].startswith("refused")
        assert not link._journeys_after_fix

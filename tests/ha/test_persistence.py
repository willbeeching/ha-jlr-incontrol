"""What survives a restart, and what the config entry is actually handed.

The caches written back to the entry are the integration's only memory across
a restart: the attributes JLR now refuse to serve, and the record of when each
car last reported anything. Both are written by the same guarded update, and
the guard is the interesting part — it only writes when something changed, so
anything that makes a real change look unchanged silently stops saving.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import KEPT, Doubles  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.jlr_incontrol.const import (  # noqa: E402
    CONF_ATTRIBUTES,
    CONF_LAST_CHANGED,
)

FIRST = "2026-09-01T07:00:00+00:00"
SECOND = "2026-09-02T07:00:00+00:00"


class TestTheEntryKeepsItsOwnCopy:
    """The entry must not be handed the coordinator's live dictionaries.

    A config entry stores what it is given by reference. Handing it the live
    cache leaves both names pointing at one object, so every later change
    lands in the entry as well — and the "has anything changed?" guard, which
    compares the two, then finds them identical and skips the write. The
    result is a coordinator that saves once and never again: the first
    timestamp survives a restart and nothing after it does.
    """

    async def test_a_second_change_is_written_too(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # Counting the writes rather than reading the value back, because a
        # shared dictionary makes reading it back pass while nothing is
        # saved: the entry and the coordinator are then the same object, so
        # the value is already "there" and only a restart finds out it was
        # never persisted.
        writes: list[None] = []
        original = hass.config_entries.async_update_entry

        def counted(*args: object, **kwargs: object) -> object:
            writes.append(None)
            return original(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(hass.config_entries, "async_update_entry", counted)
        try:
            coord = entry.runtime_data

            coord._last_changed[KEPT] = FIRST
            coord._persist()
            assert len(writes) == 1
            assert entry.data[CONF_LAST_CHANGED][KEPT] == FIRST

            coord._last_changed[KEPT] = SECOND
            coord._persist()
            assert len(writes) == 2, (
                "the second timestamp was never written: the entry and the "
                "coordinator are sharing one dictionary, so the guard that "
                "asks whether anything changed can only ever answer no"
            )
            assert entry.data[CONF_LAST_CHANGED][KEPT] == SECOND
        finally:
            monkeypatch.undo()

    async def test_the_entry_does_not_share_the_live_cache(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        coord = entry.runtime_data
        coord._last_changed[KEPT] = FIRST
        coord._attributes[KEPT] = {"nickname": "Keeper"}
        coord._persist()

        assert entry.data[CONF_LAST_CHANGED] is not coord._last_changed
        assert entry.data[CONF_ATTRIBUTES] is not coord._attributes
        assert entry.data[CONF_ATTRIBUTES][KEPT] is not coord._attributes[KEPT]

    async def test_a_later_attributes_change_is_written_too(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # The same defect, on the cache that costs a car its name when it
        # goes wrong: attributes come from a walled endpoint, so a rename
        # that never reaches the entry is lost at the next restart.
        coord = entry.runtime_data

        coord._attributes[KEPT] = {"nickname": "Keeper"}
        coord._persist()
        assert entry.data[CONF_ATTRIBUTES][KEPT]["nickname"] == "Keeper"

        coord._attributes[KEPT] = {"nickname": "Renamed"}
        coord._persist()
        assert entry.data[CONF_ATTRIBUTES][KEPT]["nickname"] == "Renamed"

    async def test_what_was_written_comes_back_after_a_restart(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        coord = entry.runtime_data
        coord._last_changed[KEPT] = FIRST
        coord._persist()
        coord._last_changed[KEPT] = SECOND
        coord._persist()

        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

        # Only the timestamp: a live account re-serves the attributes on
        # setup, so they say nothing about what was written. This is the
        # cache with no other source — lose it and the "last updated" sensor
        # has nothing to fall back on but the position fix, which is the
        # question it was just stopped from answering.
        assert entry.runtime_data._last_changed[KEPT] == SECOND


class TestAChangeIsSavedWhenItHappens:
    """Without anything in the test calling the save helper itself.

    The tests above reach for ``_persist`` directly, which is how they missed
    the gap they were meant to cover: a status change lands on a socket push,
    and until now the only thing that wrote it was the fifteen-minute
    housekeeping poll. Reloading in between reverted the timestamp to
    whatever had last been saved, which is the same visible symptom as never
    having saved at all.
    """

    async def test_a_push_survives_a_reload(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # Above the fixture's own reading: a snapshot whose odometer counts
        # down is rejected as having arrived out of order, which is a
        # different behaviour and has its own tests.
        loaded.telemetry.push(KEPT, {"ODOMETER_MILES": "76712"})
        await hass.async_block_till_done()
        loaded.telemetry.push(KEPT, {"ODOMETER_MILES": "76713"})
        await hass.async_block_till_done()
        changed = entry.runtime_data._last_changed[KEPT]
        assert changed, "a changed status did not register as a change"

        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

        assert (
            entry.runtime_data._last_changed[KEPT] == changed
        ), "the reload went back to the last saved timestamp"

    async def test_the_entry_holds_it_before_any_reload(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # The same thing said without a reload: the point is that the write
        # happens when the change does, so a crash or a power cut between
        # housekeeping polls does not cost the timestamp either.
        # Above the fixture's own reading: a snapshot whose odometer counts
        # down is rejected as having arrived out of order, which is a
        # different behaviour and has its own tests.
        loaded.telemetry.push(KEPT, {"ODOMETER_MILES": "76712"})
        await hass.async_block_till_done()
        loaded.telemetry.push(KEPT, {"ODOMETER_MILES": "76713"})
        await hass.async_block_till_done()

        assert (
            entry.data[CONF_LAST_CHANGED][KEPT]
            == entry.runtime_data._last_changed[KEPT]
        )

    async def test_unloading_writes_what_is_still_only_in_memory(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles
    ) -> None:
        # Not everything is written the moment it changes — a rotated token
        # and a freshly seeded attribute are not — so the teardown flush has
        # to run whether or not anything pushed.
        coord = entry.runtime_data
        coord._attributes[KEPT] = {"nickname": "Written on the way out"}

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.data[CONF_ATTRIBUTES][KEPT]["nickname"] == (
            "Written on the way out"
        )


class TestACarSoldWhileHomeAssistantWasStopped:
    """The one case the removal hook could not see.

    Removal was detected by comparing the account's listing against the
    vehicles seen this run, and that starts empty on every startup. So the
    first listing after a restart had nothing to find a sold car missing
    from, and its nickname and registration — restored from the config entry
    moments earlier — stayed there for good.
    """

    async def test_its_details_do_not_outlive_it(
        self, hass: HomeAssistant, entry: MockConfigEntry, doubles: Doubles
    ) -> None:
        gone = "SALZZ0000000000ZZ"
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ATTRIBUTES: {
                    KEPT: {"nickname": "Still here"},
                    gone: {"nickname": "Sold last week", "registration": "AB12 CDE"},
                },
                CONF_LAST_CHANGED: {KEPT: FIRST, gone: FIRST},
            },
        )

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # The account lists only the car that is still owned, and that listing
        # is what decides. A registration for a car somebody no longer has is
        # exactly what should not sit in storage indefinitely.
        assert gone not in (entry.data.get(CONF_ATTRIBUTES) or {})
        assert gone not in (entry.data.get(CONF_LAST_CHANGED) or {})
        assert KEPT in entry.data[CONF_ATTRIBUTES]

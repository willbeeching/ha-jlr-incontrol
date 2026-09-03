"""The four-minute touch that keeps the owner portal's session alive.

Measured on a live account: a portal session was gone fifteen minutes and four
seconds after an interactive sign-in, and the identity session behind it
refused to mint a replacement. Only the user can recover from that, at the
cost of an emailed code — which is what makes a small request every few
minutes worth making.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from doubles import Doubles  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import issue_registry as ir  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.jlr_incontrol.const import (  # noqa: E402
    DOMAIN,
    ISSUE_PORTAL_SIGNED_OUT,
    PORTAL_KEEPALIVE_INTERVAL,
)
from custom_components.jlr_incontrol.portal import (  # noqa: E402
    JlrPortalAuthError,
    JlrPortalError,
)


async def tick(hass: HomeAssistant, freezer) -> None:
    """Advance past one keep-alive interval and let the timer fire."""
    freezer.tick(PORTAL_KEEPALIVE_INTERVAL + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def touched(doubles: Doubles) -> int:
    return doubles.portal.touches


class TestTheTouch:
    async def test_it_happens_on_the_clock(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles, freezer
    ) -> None:
        before = touched(loaded)
        await tick(hass, freezer)
        assert touched(loaded) > before

    async def test_it_keeps_happening(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles, freezer
    ) -> None:
        await tick(hass, freezer)
        after_one = touched(loaded)
        await tick(hass, freezer)
        assert touched(loaded) > after_one


class TestWhenTheTouchFails:
    async def test_a_refused_session_raises_the_repair_immediately(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles, freezer
    ) -> None:
        # Rather than waiting for the next half-hourly read to notice.
        loaded.portal.error = JlrPortalAuthError("the portal signed us out")
        await tick(hass, freezer)
        assert ir.async_get(hass).async_get_issue(
            DOMAIN, f"{ISSUE_PORTAL_SIGNED_OUT}_{entry.entry_id}"
        )

    async def test_it_stops_touching_once_the_session_is_gone(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles, freezer
    ) -> None:
        # Repeating a login chain that cannot succeed is both useless and rude.
        loaded.portal.error = JlrPortalAuthError("the portal signed us out")
        await tick(hass, freezer)
        after_failure = touched(loaded)
        await tick(hass, freezer)
        assert touched(loaded) == after_failure

    async def test_an_ordinary_failure_is_shrugged_off(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles, freezer
    ) -> None:
        loaded.portal.error = JlrPortalError("the portal is slow today")
        await tick(hass, freezer)
        assert not ir.async_get(hass).async_get_issue(
            DOMAIN, f"{ISSUE_PORTAL_SIGNED_OUT}_{entry.entry_id}"
        )

    async def test_nothing_a_timer_does_may_escape(
        self, hass: HomeAssistant, entry: MockConfigEntry, loaded: Doubles, freezer
    ) -> None:
        # A raising timer callback is logged by Home Assistant and then keeps
        # firing; the point is that it must not take anything else with it.
        loaded.portal.error = RuntimeError("something nobody predicted")
        await tick(hass, freezer)
        assert entry.runtime_data.data["vehicles"]

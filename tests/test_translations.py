"""The translation and icon files, checked for the mistakes they invite.

None of this needs Home Assistant — it is JSON on disk — but all of it is the
kind of thing that breaks silently. A key filed under the wrong section is not
an error anywhere; it just means the user reads a raw slug forever.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

INTEGRATION = (
    Path(__file__).resolve().parents[1] / "custom_components" / "jlr_incontrol"
)


def load(name: str) -> dict:
    return json.loads((INTEGRATION / name).read_text())


class TestTheFilesAgree:
    def test_english_matches_the_source_strings(self) -> None:
        # translations/en.json is a copy of strings.json. Editing one and not
        # the other means the string you fixed is not the string anyone reads.
        assert load("strings.json") == load("translations/en.json")


class TestIcons:
    def test_every_icon_is_an_mdi_name(self) -> None:
        for platform in load("icons.json")["entity"].values():
            for entry in platform.values():
                assert entry["default"].startswith("mdi:")

    def test_no_icon_is_left_hard_coded_in_python(self) -> None:
        # Icon translations are the current guidance, and a stray _attr_icon
        # silently wins over icons.json for that entity.
        stray = [
            path.name for path in INTEGRATION.glob("*.py") if "mdi:" in path.read_text()
        ]
        assert stray == []


class TestFlowStrings:
    def test_the_options_step_is_under_options(self) -> None:
        # It lived under config.step, where Home Assistant does not look for
        # an options flow, so the dialog showed untranslated keys.
        strings = load("strings.json")
        assert "init" in strings["options"]["step"]
        assert "init" not in strings["config"]["step"]

    @pytest.mark.parametrize("selector", ["distance_unit", "pressure_unit"])
    def test_the_unit_dropdowns_have_labels(self, selector: str) -> None:
        options = load("strings.json")["selector"][selector]["options"]
        assert "default" in options
        assert all(label for label in options.values())


class TestEnumStates:
    @pytest.mark.parametrize(
        "key", ["ev_charging_status", "evcc_status", "charge_now_setting"]
    )
    def test_the_enum_sensors_translate_their_states(self, key: str) -> None:
        # An ENUM sensor with no state translations shows the raw option, and
        # the whole reason for an option list is that it can be worded.
        entry = load("strings.json")["entity"]["sensor"][key]
        assert entry["state"], f"{key} has options but no wording for them"

"""Keeping identifiers out of logs and diagnostics.

Both get pasted verbatim into public GitHub issues — one already carries a
reporter's VIN, copied from a debug line this integration wrote and asked them
for. So the rule here is that nothing which identifies a car, its telematics
unit or its owner may leave the process.

Key matching is by normalised *suffix* rather than substring. The payload names
the same thing three ways (``vin``, ``fullVin``, ``TU_STATUS_IMEI``), which a
fixed list of exact keys kept missing; a substring rule instead matches
``IS_DRIVING`` because it happens to contain "VIN".
"""

from __future__ import annotations

import re
from hashlib import sha256
from typing import Any

REDACTED = "**REDACTED**"

# Compared against the key upper-cased with separators removed, so vin,
# fullVin, serial_number, serialNumber and TU_STATUS_SERIAL_NUMBER all land on
# the same rule.
_SENSITIVE_SUFFIXES = (
    "VIN",
    "IMEI",
    "SERIALNUMBER",
    "REGISTRATIONNUMBER",
    "LATITUDE",
    "LONGITUDE",
    "PORTALID",
    # The account itself, and the identifiers JLR tie to it. An email address
    # is the person; the device and user ids are the account's handles.
    "USERNAME",
    "USERID",
    "DEVICEID",
)
# These are unambiguous wherever they appear in a name.
_SENSITIVE_FRAGMENTS = (
    "PASSWORD",
    "TOKEN",
    "COOKIE",
    "AUTHORIZATION",
    "SECRET",
    "EMAIL",
)

# A VIN is exactly 17 characters and never uses I, O or Q — they are excluded
# from the standard so they cannot be misread as 1 and 0. Catching the shape
# means a VIN embedded in a URL or an error body is redacted too, not only one
# that happens to sit under a key we recognise.
_VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")


def vehicle_label(vin: str) -> str:
    """A stable, non-identifying name for a vehicle, for logs and diagnostics.

    A digest rather than the last few characters of the VIN: that tail is the
    serial, which narrows a car a long way when the make and model are sitting
    next to it. This reverses to nothing, and being stable it still lets one
    car be followed across a log and a diagnostics dump.
    """
    if not vin:
        return "vehicle_unknown"
    return f"vehicle_{sha256(vin.encode()).hexdigest()[:6]}"


def _is_sensitive(key: Any) -> bool:
    """Whether a mapping key names something that must not be reported."""
    if not isinstance(key, str):
        return False
    normalised = "".join(c for c in key.upper() if c.isalnum())
    return normalised.endswith(_SENSITIVE_SUFFIXES) or any(
        fragment in normalised for fragment in _SENSITIVE_FRAGMENTS
    )


def scrub_text(text: str) -> str:
    """Replace anything VIN-shaped in free text."""
    return _VIN_RE.sub(REDACTED, text)


def scrub(value: Any) -> Any:
    """Recursively redact identifiers from a JSON-shaped value.

    Applied to whole payloads rather than named fields, because the shape of
    what JLR sends is not ours to predict: an unrecognised message logged to
    find out what it is must not carry a VIN while we look.
    """
    if isinstance(value, dict):
        # Keys as well as values. A VIN in key position is precisely how the
        # first leak happened: ``last_push`` was keyed by VIN, so the redaction
        # applied to everything under it never saw them.
        return {
            (scrub_text(key) if isinstance(key, str) else key): (
                REDACTED if _is_sensitive(key) else scrub(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value

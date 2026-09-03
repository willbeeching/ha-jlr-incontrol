"""Stand-ins for everything below the integration that talks to JLR.

The IF9 client, the telemetry socket and the owner portal. None of what
these tests are about — setup and unload, repairs, entity creation,
availability, diagnostics — depends on any of the three being real.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

KEPT = "SAJAA1234567890AB"
SOLD = "SALBB9876543210CD"

# A snapshot with enough keys that the sensor platform builds something.
STATUS = {
    "ODOMETER": "123456",
    "FUEL_LEVEL_PERC": "42",
    "DOOR_IS_ALL_DOORS_LOCKED": "TRUE",
    "LAST_UPDATED_TIME": "2026-08-26T08:00:00.000Z",
    # The telematics unit's permanent hardware identifiers. Real payloads
    # carry these, and they are what a field-name allow-list kept missing.
    "TU_STATUS_IMEI": "356938035643809",
    "TU_STATUS_SERIAL_NUMBER": "TU-000-111-222",
}

REGISTRATION = "AB12 CDE"


class FakeClient:
    """Stands in for the IF9 API client."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.user_id = kwargs.get("user_id") or "a-user"
        self.refresh_token = kwargs.get("refresh_token") or "a-refresh-token"
        self.vehicles: list[dict[str, Any]] = [{"vin": KEPT, "role": "Owner"}]
        self.connect_error: Exception | None = None

    async def async_connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error

    async def async_get_vehicles(self) -> list[dict[str, Any]]:
        if self.connect_error is not None:
            raise self.connect_error
        return list(self.vehicles)

    async def async_get_attributes(self, vin: str) -> dict[str, Any]:
        return {
            "vehicleBrand": "Jaguar",
            "fuelType": "Diesel",
            "nickname": "Test Car",
            "registrationNumber": REGISTRATION,
        }


class FakeTelemetry:
    """Stands in for the STOMP socket, and can push on demand."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.on_status = kwargs["on_status"]
        self.on_position = kwargs["on_position"]
        self.on_connected = kwargs["on_connected"]
        self.connected = False
        self.vins: list[str] = []
        self.stopped = False

    def async_set_vehicles(self, vins: Any) -> None:
        self.vins = sorted(vins)

    async def async_start(self) -> None:
        self.connected = True
        self.on_connected(True)
        for vin in self.vins:
            self.push(vin)

    def push(self, vin: str, status: dict[str, Any] | None = None) -> None:
        self.on_status(vin, dict(status or STATUS), "2026-08-26T08:12:41.589Z")

    def drop(self) -> None:
        """Lose the socket without anyone unloading anything."""
        self.connected = False
        self.on_connected(False)

    async def async_stop(self) -> None:
        self.connected = False
        self.stopped = True


class FakePortal:
    """Stands in for the owner-portal client."""

    configured = True
    session_age = "0:01:00"
    # Class-level so a test can make the portal fail from the first read,
    # before setup has constructed one to poke.
    error: Exception | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.listing: dict[str, dict[str, Any]] = {
            KEPT: {"nickname": "Test Car", "portal_id": "id-kept"}
        }
        self.closed = False
        self.asked: list[str] = []

    async def async_get_vehicles(self) -> dict[str, dict[str, Any]]:
        if self.error is not None:
            raise self.error
        return {vin: dict(record) for vin, record in self.listing.items()}

    async def async_get_position(self, portal_id: str) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        self.asked.append(portal_id)
        return {"latitude": 51.5074, "longitude": -0.1278}

    async def async_close(self) -> None:
        self.closed = True


class Doubles:
    """Handles on the fakes a test wants to poke mid-run."""

    client: FakeClient
    telemetry: FakeTelemetry
    portal: FakePortal

    def builder(self, attribute: str, cls: type) -> Any:
        """A drop-in for a collaborator class that remembers what it made."""

        def build(*args: Any, **kwargs: Any) -> Any:
            made = cls(*args, **kwargs)
            setattr(self, attribute, made)
            return made

        return build

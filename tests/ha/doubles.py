"""Stand-ins for everything below the integration that talks to JLR.

The Data Hub link (FakeLink), for tests about what the integration does with
readings, and the Data Hub client (FakeDataHub), for tests of the link itself.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")


def device_for(hass: Any, entry: Any, vin: str) -> Any:
    """The device registry entry for a vehicle, on old and new cores alike.

    ``async_get_device`` is deprecated from 2026.x — identifiers stopped being
    unique across config entries — and calling it from a test now raises
    rather than warning. The replacement wants the config entry as well, and
    does not exist on the oldest core this integration supports, so the lookup
    is written once here rather than five times across two behaviours.
    """
    from homeassistant.helpers import device_registry as dr

    from custom_components.jlr_incontrol.const import DOMAIN

    registry = dr.async_get(hass)
    by_identifier = getattr(registry, "async_get_device_by_identifier", None)
    if by_identifier is not None:
        return by_identifier((DOMAIN, vin), entry.entry_id)
    return registry.async_get_device(identifiers={(DOMAIN, vin)})


KEPT = "SAJAA1234567890AB"
SOLD = "SALBB9876543210CD"

# A snapshot with enough keys that the sensor platform builds something.
STATUS = {
    "ODOMETER": "123456",
    # The odometer sensor reads ODOMETER_MILES; ODOMETER is the metric value
    # it exposes as an attribute. A fake carrying only the latter grew no
    # distance sensor at all, which quietly hid the unit-override code.
    "ODOMETER_MILES": "76711",
    "TYRE_PRESSURE_FRONT_LEFT": "240",
    "FUEL_LEVEL_PERC": "42",
    "DOOR_IS_ALL_DOORS_LOCKED": "TRUE",
    # Enough of the openings and the engine for the entities that depend on
    # them to exist. Three separate tests have had to be written around this
    # document being thinner than a real one — a window, an alarm and a
    # coolant reading, each of which simply had no entity to assert against.
    "WINDOW_FRONT_LEFT_STATUS": "CLOSED",
    "THEFT_ALARM_STATUS": "ALARM_ARMED",
    "ENGINE_COOLANT_TEMP": "89",
    "LAST_UPDATED_TIME": "2026-08-26T08:00:00.000Z",
    # The telematics unit's permanent hardware identifiers. Real payloads
    # carry these, and they are what a field-name allow-list kept missing.
    "TU_STATUS_IMEI": "356938035643809",
    "TU_STATUS_SERIAL_NUMBER": "TU-000-111-222",
}

REGISTRATION = "AB12 CDE"


class FakeLink:
    """Stands in for the Data Hub link, and reports status on demand.

    For the tests about entities, availability, diagnostics and the entry's
    life cycle, none of which depend on how the link reads JLR. The link
    itself is tested against FakeDataHub, in test_datahub_link and
    test_datahub_source. It hands over STATUS when it starts, as the real one
    hands over each car's first full read.
    """

    # Class-level so a test can set them before setup constructs one: details
    # that fail from the first read, and a first status other than STATUS.
    details_error: Exception | None = None
    first_status: dict[str, Any] | None = None

    def __init__(self, hass: Any, entry: Any, **kwargs: Any) -> None:
        self.on_status = kwargs["on_status"]
        self.on_position = kwargs["on_position"]
        settings = entry.data.get("datahub") or {}
        self.vehicles: list[str] = list(settings.get("vehicles") or [KEPT])
        self.signed_out = False
        self.healthy = True
        self.connected = False
        self.started = False
        self.stopped = False
        self.ev_waiting: dict[str, str] = {}
        self.journeys: dict[str, dict[str, Any]] = {}
        self.recent_journeys: dict[str, list[dict[str, Any]]] = {}
        self.reports: dict[str, Any] = {}
        self.read_now_result = True
        self.reads: list[str | None] = []
        self.details_asked = 0
        # What the Data Hub's vehicle details become, as vss.to_attributes
        # makes them. The model doubles as the device name, which is where the
        # entity ids in these tests ("test_car") come from.
        self.details: dict[str, Any] = {
            "vehicleBrand": "Jaguar",
            "vehicleType": "Test Car",
            "model": "Test Car",
            "fuelType": "Diesel",
            "registrationNumber": REGISTRATION,
        }
        self._listeners: list[Any] = []

    def async_start(self) -> None:
        if self.started:
            return
        self.started = True
        self.connected = True
        for vin in self.vehicles:
            self.push(vin, type(self).first_status)

    async def async_stop(self) -> None:
        self.connected = False
        self.stopped = True

    def push(self, vin: str, status: dict[str, Any] | None = None) -> None:
        self.on_status(vin, dict(status or STATUS))

    def fix(self, vin: str, position: dict[str, Any]) -> None:
        self.on_position(vin, dict(position))

    def drop(self) -> None:
        """Stop vouching for the readings, as a link whose reads keep failing."""
        self.healthy = False
        self.connected = False

    async def async_read_now(self, vin: str | None = None) -> bool:
        self.reads.append(vin)
        return self.read_now_result

    async def async_vehicle_details(self, vin: str) -> dict[str, Any]:
        self.details_asked += 1
        if type(self).details_error is not None:
            raise type(self).details_error
        return dict(self.details)

    def async_add_listener(self, update: Any) -> Any:
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    def diagnostics(self) -> dict[str, Any]:
        return {"connected": self.connected, "healthy": self.healthy}


class Doubles:
    """Handles on the fakes a test wants to poke mid-run."""

    link: FakeLink
    datahub: FakeDataHub

    def builder(self, attribute: str, cls: type) -> Any:
        """A drop-in for a collaborator class that remembers what it made."""

        def build(*args: Any, **kwargs: Any) -> Any:
            made = cls(*args, **kwargs)
            setattr(self, attribute, made)
            return made

        return build


class FakeDataHub:
    """Stands in for the Data Hub client: scripted answers, recorded calls.

    Each ``*_result`` is returned, or raised if it is an exception. A list is
    consumed one answer per call, for a sequence such as a sign-in that is
    pending twice and then approved.

    ``journeys`` on the class is what a new client answers for journeys, for a
    test that needs them answered from setup on. None is JLR's gateway
    refusing, which is what owners got until November 2026.
    """

    journeys: Any = None

    def __init__(self, session: Any, client_id: str, private_key: str) -> None:
        from custom_components.jlr_incontrol.datahub import Tokens

        self.client_id = client_id
        self.private_key = private_key
        self.calls: list[str] = []
        self.tokens = Tokens(
            access_token="access",
            refresh_token="offline-2",
            expires_at=time.time() + 300,
            vehicles=[KEPT],
            account_id="acct-1",
            categories=["status"],
        )
        self.refresh_result: Any = self.tokens
        self.stream_result: Any = Tokens(
            access_token="stream",
            refresh_token=None,
            expires_at=time.time() + 300,
            account_id="acct-1",
        )
        self.status_result: Any = {
            "timestamp": "2026-11-07T09:00:00.000Z",
            "Vehicle": {
                "VehicleIdentification": {"VIN": KEPT},
                "TraveledDistance": 12345000,
                "Body": {
                    "Trunk": {"Rear": {"IsOpen": "CLOSED", "IsLocked": "TRUE"}},
                    "Door": {"Lock": {"Status": "TRUE"}},
                    "Security": {"Alarm": {"Mode": "ALARM_ARMED"}},
                },
                "Cabin": {
                    "Door": {
                        "Row1": {
                            "DriverSide": {
                                "IsLocked": "LOCKED",
                                "Window": {"Position": "CLOSED"},
                            }
                        }
                    }
                },
                "Chassis": {
                    "Axle": {"Row1": {"Wheel": {"Left": {"Tire": {"Pressure": 2400}}}}}
                },
                "Powertrain": {
                    "FuelSystem": {"RelativeLevel": 42, "Range": 500},
                    "CombustionEngine": {"EngineCoolant": {"Temperature": 89}},
                },
            },
        }
        # What JLR answered for location in November 2026, for both cars.
        from custom_components.jlr_incontrol.datahub import DataHubForbidden

        self.location_result: Any = DataHubForbidden(
            "Access to category is not permitted for this vehicle: position"
        )
        self.vehicle_result: Any = {
            "Vehicle": {
                "VehicleIdentification": {
                    "VIN": KEPT,
                    "Brand": "Jaguar",
                    # Doubles as the device name the entity ids in these
                    # tests were built from ("test_car"): the Data Hub carries
                    # no nickname, so the model names the car.
                    "Model": "Test Car",
                    "Year": 2023,
                    "BodyType": "5 Door",
                    "LicensePlate": None,
                },
                "Powertrain": {"FuelType": "diesel"},
            }
        }
        # What JLR answer for a car without a charge port.
        self.ev_result: Any = DataHubForbidden(
            "Access to category is not permitted for this vehicle: ev"
        )
        self.journeys_result: Any = (
            DataHubForbidden("Access denied")
            if type(self).journeys is None
            else type(self).journeys
        )
        self.login_result: Any = None
        self.poll_results: list[Any] = []

    on_rate_limited: Any = None
    before_request: Any = None

    def _gate(self) -> None:
        """As the real client does, before a request is sent."""
        if self.before_request is not None:
            self.before_request()

    def _answer(self, result: Any) -> Any:
        if isinstance(result, list):
            result = result.pop(0)
        if isinstance(result, BaseException):
            from custom_components.jlr_incontrol.datahub import DataHubRateLimited

            # As the real client does, before anything can catch it.
            if isinstance(result, DataHubRateLimited) and self.on_rate_limited:
                self.on_rate_limited(result)
            raise result
        return result

    async def async_refresh(self, refresh_token: str) -> Any:
        self._gate()
        self.calls.append(f"refresh {refresh_token}")
        return self._answer(self.refresh_result)

    async def async_status(self, vin: str, access_token: str) -> Any:
        self._gate()
        self.calls.append("status")
        return self._answer(self.status_result)

    async def async_vehicle(self, vin: str, access_token: str) -> Any:
        self._gate()
        self.calls.append("details")
        return self._answer(self.vehicle_result)

    async def async_location(self, vin: str, access_token: str) -> Any:
        self._gate()
        self.calls.append("location")
        return self._answer(self.location_result)

    async def async_ev(self, vin: str, access_token: str) -> Any:
        self._gate()
        self.calls.append("ev")
        return self._answer(self.ev_result)

    async def async_journeys(self, vin: str, access_token: str, page: int = 0) -> Any:
        self._gate()
        self.calls.append(f"journeys {page}")
        return self._answer(self.journeys_result)

    async def async_stream_token(self) -> Any:
        self._gate()
        self.calls.append("stream token")
        return self._answer(self.stream_result)

    async def async_start_device_login(self) -> Any:
        self._gate()
        self.calls.append("start")
        return self._answer(self.login_result)

    async def async_poll_device_login(self, login: Any) -> Any:
        self._gate()
        self.calls.append("poll")
        return self._answer(self.poll_results)

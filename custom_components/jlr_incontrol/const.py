"""Constants for the Jaguar Land Rover InControl integration.

Since 3.0.0 everything comes from the JLR Data Hub, JLR's documented API for
owners. The reverse-engineered InControl connection, its sign-in and the owner
portal were removed; the status keys below keep the names that connection
used, which is why the entities did not change.
"""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "jlr_incontrol"


# The floor between Refresh presses. Pressing twice inside a minute cannot
# produce anything the first press did not: JLR answer with what the car last
# reported, and it does not change that fast.
RESUBSCRIBE_FLOOR = timedelta(minutes=1)
# The owner portal's sign-in repair, from before 3.0.0. Nothing raises it any
# more; kept so setup and removal can delete one left behind.
ISSUE_PORTAL_SIGNED_OUT = "portal_signed_out"
# An entry set up on the InControl sign-in, which 3.0.0 removed: it loads with
# nothing to read until the owner links the Data Hub.
ISSUE_INCONTROL_RETIRED = "incontrol_retired"
# Everything an InControl sign-in left in an entry. Removed from every entry by
# the 1.2 migration: none of it can be used any more, and the password was
# kept in the clear.
INCONTROL_KEYS = (
    "password",
    "device_id",
    "user_id",
    "refresh_token",
    "sso_cookies",
    "portal_cookies",
    "portal_base",
    "portal_minted",
)

# ---- JLR Data Hub ----
# JLR's documented API (developer.jlr.com). Held in the entry under one key,
# so an entry from the InControl sign-in is told apart by not having it. The
# private key and the refresh token are secrets: redact.py treats both, and the
# client id, as such.
CONF_DATAHUB = "datahub"
DATAHUB_CLIENT_ID = "client_id"
DATAHUB_PRIVATE_KEY = "private_key"
DATAHUB_CERTIFICATE = "certificate"
DATAHUB_REFRESH_TOKEN = "refresh_token"
DATAHUB_VEHICLES = "vehicles"
DATAHUB_ACCOUNT_ID = "account_id"
# Raised before 3.0.0-beta.2 when JLR stopped accepting the link. That is a
# reauth now; the id is kept only to clear a repair an earlier version left.
ISSUE_DATAHUB_SIGNED_OUT = "datahub_signed_out"
# The stream is a switch on the client at data.jlr.com that setup asks for and
# nothing else checks. Off, everything still works, a reading at a time on the
# REST timer, and an owner would not know to look.
ISSUE_DATAHUB_STREAM_OFF = "datahub_stream_off"
# The other things only the owner can put right at data.jlr.com, each of which
# otherwise showed only in diagnostics or a debug log: a car whose location is
# refused because it is not declared, a car whose status is not shared with
# the client, and the client's certificate running out after its year.
ISSUE_DATAHUB_LOCATION_REFUSED = "datahub_location_refused"
ISSUE_DATAHUB_STATUS_REFUSED = "datahub_status_refused"
ISSUE_DATAHUB_CERTIFICATE = "datahub_certificate_expiring"
DATAHUB_ISSUES = (
    ISSUE_DATAHUB_SIGNED_OUT,
    ISSUE_DATAHUB_STREAM_OFF,
    ISSUE_DATAHUB_LOCATION_REFUSED,
    ISSUE_DATAHUB_STATUS_REFUSED,
    ISSUE_DATAHUB_CERTIFICATE,
)
# How long before the certificate runs out that the owner is told, and that
# the options offer linking again. It is made valid for a year.
DATAHUB_RENEW_WITHIN = timedelta(days=30)


# Last known vehicle details, per VIN. Cached in the entry so a restart keeps
# each car's name, model and fuel type when the first details read fails, and
# the nickname an earlier version read from the owner portal.
CONF_ATTRIBUTES = "attributes"
# When each vehicle's reported status last actually differed. Persisted
# because it is the only honest answer to "when did this car last tell us
# something", and losing it on a reload sent that sensor backwards.
CONF_LAST_CHANGED = "last_changed"
# When each car was first seen in the state it is still in. Persisted for the
# same reason as the above: a restart that reset it would trust a mid-shutdown
# snapshot for another half hour, and restarts are not rare.
CONF_UNSETTLED_SINCE = "unsettled_since"
# The readings as they were when that clock started. Persisted with it and for
# the same reason: without it, the first snapshot after a restart cannot be
# told apart from the one before, so a lock that has genuinely just moved is
# treated as the broker handing back what it already held — and a reading that
# has just become true stays hidden.
CONF_VOLATILE_SEEN = "volatile_seen"
# And the same pair for readings that go out of date on their own, without
# anything about the car having to be in a particular state.
CONF_DECAYED_SINCE = "decayed_since"
CONF_DECAYED_SEEN = "decayed_seen"
# The units the distance and pressure options last wrote into the entity
# registry, by kind. Without it a unit the integration forced and a unit the
# user picked by hand look identical, and clearing "ours" cleared theirs (#40).
CONF_APPLIED_UNITS = "applied_units"

# ---- Options keys ----
OPT_DISTANCE_UNIT = "distance_unit"
OPT_PRESSURE_UNIT = "pressure_unit"
DISTANCE_UNIT_DEFAULT = "default"
DISTANCE_UNIT_MILES = "miles"
DISTANCE_UNIT_KM = "km"
PRESSURE_UNIT_DEFAULT = "default"
PRESSURE_UNIT_KPA = "kpa"
PRESSURE_UNIT_BAR = "bar"
PRESSURE_UNIT_PSI = "psi"
OPT_UNSETTLED_MINUTES = "unsettled_minutes"
UNSETTLED_MINUTES_DEFAULT = 30
UNSETTLED_MINUTES_CHOICES = (0, 15, 30, 60, 120)

# ---- Snapshots caught mid-use ----
# A snapshot taken while somebody still has the key in the car is a photograph
# of something in progress: the doors and windows in it are where they were at
# that instant, not where they were left. Twice now a car has been reported
# with its windows down for hours after being parked, and both times the held
# snapshot carried one of these states with an alarm that had not yet armed.
#
# Only states we have actually seen, and only used to *withhold* a reading.
# A state nobody has observed is treated as settled, so an unrecognised car
# behaves exactly as it does today rather than having its readings hidden on
# the strength of a guess.
UNSETTLED_VEHICLE_STATES = ("KEY_ON_ENGINE_OFF",)

# The keys behind the readings that stop being assertable, and the state key
# that decides whether they are. The clock for withholding them runs on these
# and nothing else: a parked car's 12V voltage drifts between snapshots, and a
# discharging battery counting as "the car reported" would keep a door reading
# from last night looking current for as long as the battery lasted.
#
# Kept here rather than derived from the sensor descriptions so the coordinator
# does not have to import the platform. A test asserts the two agree, because
# two lists over one idea drift.
# Readings that are true at the moment they are taken and become less true on
# their own. Coolant temperature is the whole of it so far: these cars latch it
# when the engine stops and keep pushing that figure for as long as they sit
# there, so 89 °C is a perfectly ordinary reading and also a perfectly ordinary
# thing to find on a car that went cold hours ago.
#
# Judged on how long the reading itself has stood still, not on how long ago
# the car said anything. A parked car's 12V voltage drifts down between
# snapshots, and treating that as the car reporting in would keep a coolant
# figure looking current all night — the same trap the withholding clock fell
# into.
DECAYING_STATUS_KEYS = ("ENGINE_COOLANT_TEMP",)

VOLATILE_STATUS_KEYS = (
    "DOOR_BOOT_LOCK_STATUS",
    "DOOR_BOOT_POSITION",
    "DOOR_ENGINE_HOOD_POSITION",
    "DOOR_FRONT_LEFT_LOCK_STATUS",
    "DOOR_FRONT_LEFT_POSITION",
    "DOOR_FRONT_RIGHT_LOCK_STATUS",
    "DOOR_FRONT_RIGHT_POSITION",
    "DOOR_IS_ALL_DOORS_LOCKED",
    "DOOR_REAR_LEFT_LOCK_STATUS",
    "DOOR_REAR_LEFT_POSITION",
    "DOOR_REAR_RIGHT_LOCK_STATUS",
    "DOOR_REAR_RIGHT_POSITION",
    "IS_SUNROOF_OPEN",
    "THEFT_ALARM_STATUS",
    "VEHICLE_STATE_TYPE",
    "WINDOW_FRONT_LEFT_STATUS",
    "WINDOW_FRONT_RIGHT_STATUS",
    "WINDOW_REAR_LEFT_STATUS",
    "WINDOW_REAR_RIGHT_STATUS",
)

# The list above as 1.7.x stored it. Up to then each car's volatile readings
# were saved as a bare list in this order, with nothing to say which value was
# which key; an upgraded install still holds one, and this is the only record
# of how to read it. Never edit it: it describes data already on disk.
VOLATILE_STATUS_KEYS_1_7 = (
    "DOOR_BOOT_POSITION",
    "DOOR_ENGINE_HOOD_POSITION",
    "DOOR_FRONT_LEFT_POSITION",
    "DOOR_FRONT_RIGHT_POSITION",
    "DOOR_IS_ALL_DOORS_LOCKED",
    "DOOR_REAR_LEFT_POSITION",
    "DOOR_REAR_RIGHT_POSITION",
    "IS_SUNROOF_OPEN",
    "THEFT_ALARM_STATUS",
    "VEHICLE_STATE_TYPE",
    "WINDOW_FRONT_LEFT_STATUS",
    "WINDOW_FRONT_RIGHT_STATUS",
    "WINDOW_REAR_LEFT_STATUS",
    "WINDOW_REAR_RIGHT_STATUS",
)

# ---- Refresh cadence ----
# Status, location and journeys are read by the Data Hub link on its own
# clocks (datahub_link.py). What remains here is housekeeping: notice a car
# being added or removed, and refresh vehicle details when they are due.
SCAN_INTERVAL_HOUSEKEEPING = timedelta(minutes=15)

# World Manufacturer Identifier -> brand. Only the two that are unambiguous:
# guessing a model from the rest of the VIN would put the wrong car on
# someone's dashboard, which is worse than a generic name.
VIN_BRANDS = {"SAL": "Land Rover", "SAJ": "Jaguar"}

# Vehicle details (model, year, fuel type) effectively never change.
ATTRIBUTES_TTL = timedelta(hours=24)
# How long to leave the details alone after a read fails, for a car that has
# none yet: retrying every housekeeping cycle would be dozens of requests a day.
ATTRIBUTES_RETRY = timedelta(hours=6)
# A GPS fix older than this is flagged stale. Informational: a car parked for
# two days has a two-day-old fix and nothing is wrong.
STALE_AFTER = timedelta(hours=24)
# How long a position stays trustworthy once location reads stop answering.
# A different thing from the fix being old: here we cannot tell whether the
# car has moved since, so asserting a location — and with it a zone — would be
# a confident guess. Three hourly reads, so one failure does not blank it.
POSITION_TRUST_WINDOW = timedelta(hours=3)

# Climate operating states that count as "running" (shared by the climate
# entity and the polling heuristic).
CLIMATE_ACTIVE_STATES = frozenset(
    {"COOLING", "HEATING", "PRECLIM", "ENGINE_ON", "RUNNING", "STARTUP", "ON"}
)

# NOTE: diagnostics is intentionally not here — it is not an entity platform
# (HA discovers diagnostics.py itself). Forwarding to it logged a setup warning
# and, worse, broke async_unload_platforms so any options change wedged the
# entry until a restart (#1).
# No lock or climate platform: both existed only to send remote commands, which
# JLR gate behind app attestation that Home Assistant cannot produce. Door lock
# state is still reported, as a binary sensor.
PLATFORMS = [
    "sensor",
    "binary_sensor",
    "device_tracker",
    "button",
]

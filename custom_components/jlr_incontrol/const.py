"""Constants for the Jaguar Land Rover InControl integration.

The bearer token comes from the app's **ForgeRock OIDC** client (see auth.py);
JLR edge-blocked the legacy IFAS password grant in August 2026.

That token still opens the identity and vehicle-list endpoints of the
``/if9/webview/*`` API, but **not** the per-vehicle data ones: JLR extended the
Approov attestation wall (HTTP 498) over those a week later, and the
browser-style ``Origin`` / ``Referer`` headers below no longer talk past it.
Vehicle data now comes over the telemetry websocket (see telemetry.py), which is
not attested. The command endpoints are still on the walled REST path.
"""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "jlr_incontrol"

# ---- Base hosts (all validated live) ----
IFOP_BASE = "https://ifop.prod-row.jlrmotor.com/ifop/jlr"
IF9_BASE = "https://if9.prod-row.jlrmotor.com/if9/webview"

# ---- Real-time telemetry websocket ----
# In August 2026 JLR put Approov attestation on the REST vehicle-data endpoints
# (if9/webview/vehicles/{vin}/status | attributes | position). They answer 498 to
# anything that cannot produce a device-bound attestation token, and the webview
# Origin bypass no longer helps. This socket is NOT attested: it takes the plain
# ForgeRock bearer and pushes the same VHS payload on subscribe, so it replaces
# polling rather than working around it.
WS_URL = "wss://if9-ws.prod-row.jlrmotor.com/if9_ws/websocketGateway/v2"
WS_HOST = "if9-ws.prod-row.jlrmotor.com"
# The device topic confirms each vehicle subscription; the VIN topics carry the
# data. Subscribe to the device topic first or the confirmations are missed.
WS_DEVICE_TOPIC = "/user/topic/DEVICE.{device_id}"
WS_VIN_TOPIC = "/user/topic/VIN.{vin}"
WS_ACK_DESTINATION = "/app/messageReceived"
# Heart-beat we advertise, in milliseconds (the broker asks for 10s each way).
WS_HEARTBEAT_MS = 20000
# How often we actually send one. This has to be driven by a clock, not by an
# idle read: the broker chats every 10s, so a heart-beat sent only when the read
# times out is a heart-beat never sent — and the broker closes the session for
# inactivity after a couple of minutes. Comfortably inside both its 10s ask and
# our own 20s advertisement.
WS_HEARTBEAT_SEND = timedelta(seconds=8)
# Give up on a session that has gone completely silent. Three missed beats, not
# one: a single late frame is not a fault worth tearing the socket down for.
WS_READ_TIMEOUT = timedelta(seconds=70)
WS_BACKOFF_START = timedelta(seconds=5)
WS_BACKOFF_MAX = timedelta(minutes=5)
# Message type carrying the vehicle health status (the telemetry we want).
WS_TYPE_STATUS = "VHS"

# ---- Owner web portal ----
# Where location and the real vehicle names come from. The portal's own backend
# calls if9 with whitelisted credentials, so anything it renders has already
# been through Approov on JLR's side — which is why a plain ForgeRock session
# can read it while every direct position/attributes endpoint answers 498.
PORTAL_BASES = (
    "https://incontrol.landrover.com/jlr-portal-owner-web",
    "https://incontrol.jaguar.com/jaguar-portal-owner-web",
)
PORTAL_LOCALE = "en_GB"
# The AM session cookie domain: the portal's login hand-off rides the same
# ForgeRock session the interactive sign-in established.
IDENTITY_HOST = "https://identity.jaguarlandrover.com"
# Location only moves when a journey completes and syncs, so there is nothing
# to gain from asking often — and a legacy servlet app is not somewhere to be
# impolite.
PORTAL_INTERVAL = timedelta(minutes=30)
# Vehicle names and plates change essentially never.
PORTAL_VEHICLES_TTL = timedelta(hours=24)
# How long to leave the portal alone after it refuses the stored session. Long
# enough not to knock repeatedly at a door only the user can open, short enough
# that a transient failure heals itself instead of needing a restart.
PORTAL_RETRY_AFTER = timedelta(hours=6)
# Repair issue raised when only an interactive sign-in can restore location.
ISSUE_PORTAL_SIGNED_OUT = "portal_signed_out"

# ---- Identity: ForgeRock OIDC ----
# JLR edge-blocked the whole legacy IFAS host in August 2026 (openresty 403 on
# every path, with or without credentials), killing the password grant this
# integration used to mint its bearer token. The app's own ForgeRock client is
# the replacement; the token it returns is still accepted by if9/webview below,
# so only the token *source* changed — reads and commands are untouched.
IDENTITY_BASE = "https://identity.jaguarlandrover.com/gateway"
IDENTITY_REALM = "realms/root/realms/customer"
AUTHENTICATE_URL = (
    f"{IDENTITY_BASE}/json/{IDENTITY_REALM}/authenticate"
    "?authIndexType=service&authIndexValue=b2c-acr-2"
)
AUTHORIZE_URL = f"{IDENTITY_BASE}/oauth2/{IDENTITY_REALM}/authorize"
ACCESS_TOKEN_URL = f"{IDENTITY_BASE}/oauth2/{IDENTITY_REALM}/access_token"
# ForgeRock AM refuses the authenticate endpoint without this.
AUTH_API_VERSION = "resource=2.0, protocol=1.0"

# The app's PUBLIC OAuth client (PKCE, no secret). An OAuth client identifies
# the *app*, not the car, and the vehicles you get back come from the account —
# so the Land Rover client is used for Jaguar accounts too. (The same already
# holds for TELEMATICS_PROGRAM below, which has been "landroverprogram" for
# every user including Jaguar owners since day one.)
IAM_CLIENT_ID = "icr-landrover"
IAM_REDIRECT_URI = "icr-landrover://oauth2redirect"
IAM_SCOPES = (
    "openid profile email "
    "urn:iam2-mgd-v1:scopes:customer:person "
    "urn:iam2-mgd-v1:scopes:customer:auto-id "
    "urn:iam2-mgd-v1:scopes:customer:TSDP_attributes "
    "urn:iam2-mgd-v1:scopes:vehicle:vehicle-data "
    "urn:iam2-mgd-v1:scopes:vehicle:vehicle-identity"
)

# The ForgeRock access token lives ~5 minutes, so the renewal margin has to be
# proportional: a fixed margin larger than the lifetime would mark every token
# expired the instant it arrived and refresh on every single request.
TOKEN_RENEW_RATIO = 0.2
TOKEN_RENEW_MARGIN_MIN = 15
TOKEN_RENEW_MARGIN_MAX = 300
# Guard against an unrecognised journey looping the callback chain forever.
AUTH_MAX_STEPS = 12

# ---- Browser / webview fingerprint ----
# Still required by the endpoints that do answer (identity, vehicle list): every
# /if9/webview/* request without them returns 401. They are no longer sufficient
# for the per-vehicle endpoints, which now want Approov attestation as well.
USER_AGENT = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36"
WEBVIEW_ORIGIN = "https://webview.prod-row.jlrmotor.com"
WEBVIEW_REFERER = "https://webview.prod-row.jlrmotor.com/"

# Headers attached to every webview request (host-level fingerprint).
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Origin": WEBVIEW_ORIGIN,
    "Referer": WEBVIEW_REFERER,
}

# Response headers worth capturing when a request fails: they identify whether
# JLR's API answered or an edge/WAF appliance in front of it refused us, and
# carry the reference id their support would need.
DIAGNOSTIC_HEADERS = (
    "Server",
    "Content-Type",
    "WWW-Authenticate",
    "Retry-After",
    "X-Reference-Error",
    "CF-Ray",
    "X-Akamai-Request-ID",
    "Akamai-GRN",
)

# 498 is Approov's. It means the edge wanted a device-bound attestation token
# that only the signed app can mint — there is no header that talks past it.
APPROOV_HINT = (
    "{what} returned 498 — Jaguar Land Rover require app attestation (Approov) "
    "on this endpoint, which Home Assistant cannot produce. Vehicle data now "
    "arrives over the telemetry websocket instead; remote commands still use "
    "this path and are blocked while the wall is up."
)

# A 403 means JLR understood the request and refused it — which is not the same
# as bad credentials, and is usually either a throttle or an edge rule.
FORBIDDEN_HINT = (
    "{what} returned 403 (refused by JLR — the request was understood but "
    "rejected). This is not necessarily a credentials problem: it is commonly "
    "a temporary account/IP throttle, or an edge rule change. Enable debug "
    "logging for this integration to capture the response body and headers."
)

# ---- Telematics program ----
TELEMATICS_PROGRAM = "landroverprogram"

# ---- Per-resource media types (Accept / Content-Type) ----
MEDIA_JSON = "application/json"
MEDIA_USER = "application/vnd.wirelesscar.ngtp.if9.User-v4+json"
MEDIA_HEALTHSTATUS = "application/vnd.ngtp.org.if9.healthstatus-v3+json"
# ---- Config entry keys ----
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_DEVICE_ID = "device_id"
CONF_USER_ID = "user_id"
# Persisted so a restart resumes with a cheap refresh grant instead of spending
# a full password login every time (which is what abuse detection notices).
CONF_REFRESH_TOKEN = "refresh_token"
# Last known vehicle attributes, per VIN. Cached in the entry because the
# endpoint that serves them is behind the Approov wall: without this a restart
# would lose every vehicle's name, model and fuel type for good.
CONF_ATTRIBUTES = "attributes"
# The ForgeRock session cookies captured at sign-in. The owner web portal has
# no token-based entry point — it authenticates by handing its own OAuth dance
# to the AM session in the browser — so reading location means keeping that
# session. Treated as a credential: redacted from diagnostics, never logged.
CONF_SSO_COOKIES = "sso_cookies"

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

# ---- Refresh cadence ----
# Vehicle data arrives over the telemetry socket as it happens, so there is no
# status polling any more and nothing to make adaptive. What remains on a timer
# is housekeeping: renew the token, keep the device registration alive, notice a
# vehicle being added or removed, and retry the attributes that are currently
# walled. All of it is one or two cheap requests.
SCAN_INTERVAL_HOUSEKEEPING = timedelta(minutes=15)
# Identity fields worth having, and the spellings they have been seen under.
# The canonical names on the left are what the entity layer reads.
IDENTITY_ALIASES: dict[str, tuple[str, ...]] = {
    "nickname": ("nickname", "vehicleNickname"),
    "vehicleBrand": ("vehicleBrand", "brand", "make"),
    "vehicleType": ("vehicleType", "vehicleModel", "modelName", "model"),
    "model": ("model", "vehicleType"),
    "fuelType": ("fuelType", "FuelType", "fuel"),
    "numberOfDoors": ("numberOfDoors",),
    "registrationNumber": ("registrationNumber", "registration"),
    "modelYear": ("modelYear", "vehicleModelYear"),
}

# World Manufacturer Identifier -> brand. Only the two that are unambiguous:
# guessing a model from the rest of the VIN would put the wrong car on
# someone's dashboard, which is worse than a generic name.
VIN_BRANDS = {"SAL": "Land Rover", "SAJ": "Jaguar"}

# Vehicle attributes (make/model/capabilities) effectively never change.
ATTRIBUTES_TTL = timedelta(hours=24)
# How long to leave the walled attributes endpoint alone after it refuses us.
# Retrying it every housekeeping cycle would be hundreds of requests a day at a
# door that is not going to open until JLR decide otherwise.
ATTRIBUTES_RETRY = timedelta(hours=6)
# How long the entities keep their last values after the socket drops before
# going unavailable. Reconnects are routine — the STOMP session is bound to a
# ~5 minute access token — so a brief gap must not flap every entity, while a
# long one is a real outage and should look like one.
TELEMETRY_GRACE = timedelta(minutes=30)
# A GPS fix older than this is flagged stale. Informational: a car parked for
# two days has a two-day-old fix and nothing is wrong.
STALE_AFTER = timedelta(hours=24)
# How long a position stays trustworthy once portal reads start failing. This is
# a different thing from the fix being old: here we cannot tell whether the car
# has moved since, so continuing to assert a location — and with it a zone —
# would be a confident guess. Six poll cycles, so a blip does not blank it.
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

"""Constants for the Jaguar Land Rover InControl integration.

The bearer token comes from the app's **ForgeRock OIDC** client (see auth.py);
JLR edge-blocked the legacy IFAS password grant in August 2026. That token,
combined with a registered device id and the browser-style ``Origin`` / ``Referer``
headers, is accepted by the ``/if9/webview/*`` API — which bypasses the Approov
edge wall (HTTP 498) that blocks the native-app IF9 host.
"""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "jlr_incontrol"

# ---- Base hosts (all validated live) ----
IFOP_BASE = "https://ifop.prod-row.jlrmotor.com/ifop/jlr"
IF9_BASE = "https://if9.prod-row.jlrmotor.com/if9/webview"

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
# These headers are what get the webview API past the Approov edge wall. Every
# /if9/webview/* request MUST carry the Origin + Referer below or it returns 498/401.
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
MEDIA_AUTHENTICATE = "application/vnd.wirelesscar.ngtp.if9.AuthenticateRequest-v2+json"
MEDIA_START_SERVICE = (
    "application/vnd.wirelesscar.ngtp.if9.StartServiceConfiguration-v3+json"
)
# The Accept a command POST must send for its response. Validated live on the
# classic endpoints (lock, honkBlink): v4 works; v5 and plain application/json
# both return HTTP 406. The PhevService endpoints (preconditioning,
# chargeProfile) are the opposite: they require v5 (v4 returns 406, seen live
# on an I-Pace ECC start) — matching jlrpy's native-app behaviour.
MEDIA_SERVICE_STATUS = "application/vnd.wirelesscar.ngtp.if9.ServiceStatus-v4+json"
MEDIA_SERVICE_STATUS_V5 = "application/vnd.wirelesscar.ngtp.if9.ServiceStatus-v5+json"
MEDIA_PHEV_SERVICE = "application/vnd.wirelesscar.ngtp.if9.PhevService-v1+json"

# ---- Config entry keys ----
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_PIN = "pin"
CONF_DEVICE_ID = "device_id"
CONF_USER_ID = "user_id"
# Persisted so a restart resumes with a cheap refresh grant instead of spending
# a full password login every time (which is what abuse detection notices).
CONF_REFRESH_TOKEN = "refresh_token"

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

# ---- Remote service codes (serviceName) ----
SERVICE_LOCK = "RDL"
SERVICE_UNLOCK = "RDU"
SERVICE_ENGINE_ON = "REON"  # remote-start climate (heat/precondition)
SERVICE_ENGINE_OFF = "REOFF"
SERVICE_HONK_FLASH = "HBLF"
SERVICE_ALARM_OFF = "ALOFF"
SERVICE_PRECONDITIONING = "ECC"  # electric climate control (BEV/PHEV)
SERVICE_VHS = "VHS"  # vehicle health status refresh
SERVICE_CHARGE = "CP"  # charge-now control
SERVICE_PROV = "PROV"  # provisioning (required before ICE RCC settings)

# serviceName -> path segment used to start the service.
SERVICE_ENDPOINTS: dict[str, str] = {
    SERVICE_LOCK: "lock",
    SERVICE_UNLOCK: "unlock",
    SERVICE_ENGINE_ON: "engineOn",
    SERVICE_ENGINE_OFF: "engineOff",
    SERVICE_HONK_FLASH: "honkBlink",
    SERVICE_ALARM_OFF: "alarmOff",
    SERVICE_PRECONDITIONING: "preconditioning",
    SERVICE_VHS: "healthstatus",
    SERVICE_CHARGE: "chargeProfile",
    SERVICE_PROV: "prov",
}

# Per-service start-request configuration. The PhevService endpoints take the
# charset suffix and ServiceStatus-v5 Accept exactly as the native app sends
# them (jlrpy); ECC returns 406 without the v5 Accept.
SERVICE_START_CONTENT_TYPES: dict[str, str] = {
    SERVICE_PRECONDITIONING: f"{MEDIA_PHEV_SERVICE}; charset=utf-8",
    SERVICE_CHARGE: f"{MEDIA_PHEV_SERVICE}; charset=utf-8",
}
SERVICE_START_ACCEPTS: dict[str, str] = {
    SERVICE_PRECONDITIONING: MEDIA_SERVICE_STATUS_V5,
    SERVICE_CHARGE: MEDIA_SERVICE_STATUS_V5,
}

# Services that authenticate with an empty PIN (per jlrpy / native-app behaviour).
SERVICES_EMPTY_PIN: frozenset[str] = frozenset({SERVICE_PRECONDITIONING, SERVICE_VHS})

# ---- Polling ----
# Be a polite client: poll fast only while something is happening (plugged
# in, charging, climate running, values changing, recent user command) and
# back off once the car has been quiet, so we don't hammer JLR's servers.
SCAN_INTERVAL_ACTIVE = timedelta(minutes=5)
SCAN_INTERVAL_IDLE = timedelta(minutes=20)
# How long after the last observed change / user interaction to stay fast.
ACTIVITY_WINDOW = timedelta(minutes=30)
# Vehicle attributes (make/model/capabilities) effectively never change.
ATTRIBUTES_TTL = timedelta(hours=24)
DEFAULT_SCAN_INTERVAL = SCAN_INTERVAL_ACTIVE
# A position older than this is flagged stale (informational attribute only).
STALE_AFTER = timedelta(hours=24)

# Climate operating states that count as "running" (shared by the climate
# entity and the polling heuristic).
CLIMATE_ACTIVE_STATES = frozenset(
    {"COOLING", "HEATING", "PRECLIM", "ENGINE_ON", "RUNNING", "STARTUP", "ON"}
)

# ECC target temperature bounds (degrees Celsius).
ECC_MIN_TEMP = 16.0
ECC_MAX_TEMP = 28.0
ECC_DEFAULT_TEMP = 21.0

# ICE remote climate uses an RCC scale of 31 (LO/cool) – 57 (HI/heat).
# 15.5C maps to RCC 31 = LO and 28.5C to RCC 57 = HI, matching the car's dial.
ICE_RCC_MIN = 31
ICE_RCC_MAX = 57
ICE_MIN_TEMP = 15.5
ICE_MAX_TEMP = 28.5
ICE_DEFAULT_TEMP = 21.0

# How long to assume remote climate keeps running after a confirmed start.
# The cached CLIMATE_STATUS_OPERATING_STATUS lags by minutes, so without this
# the thermostat shows Off while the engine is running and can't be stopped.
CLIMATE_ASSUMED_ON_SECONDS = 30 * 60  # JLR remote start auto-stops around here
CLIMATE_ASSUMED_OFF_SECONDS = 15 * 60

# How long the charge-now-setting sensor trusts a just-pressed Force charge
# button over the (minutes-stale) EV_CHARGE_NOW_SETTING readback.
CHARGE_NOW_ASSUMED_WINDOW = timedelta(minutes=5)

# NOTE: diagnostics is intentionally not here — it is not an entity platform
# (HA discovers diagnostics.py itself). Forwarding to it logged a setup warning
# and, worse, broke async_unload_platforms so any options change wedged the
# entry until a restart (#1).
PLATFORMS = [
    "sensor",
    "binary_sensor",
    "device_tracker",
    "lock",
    "climate",
    "button",
]

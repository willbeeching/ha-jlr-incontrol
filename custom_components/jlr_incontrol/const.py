"""Constants for the Jaguar Land Rover InControl integration.

The working backend path is the **webview / password** flow (NOT the ForgeRock /
Approov app path). A plain password grant on IFAS mints a bearer token; that token,
combined with a registered device id and the browser-style ``Origin`` / ``Referer``
headers, is accepted by the ``/if9/webview/*`` API — which bypasses the Approov
edge wall (HTTP 498) that blocks the native-app IF9 host.
"""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "jlr_incontrol"

# ---- Base hosts (all validated live) ----
IFAS_BASE = "https://ifas.prod-row.jlrmotor.com/ifas"
IFOP_BASE = "https://ifop.prod-row.jlrmotor.com/ifop/jlr"
IF9_BASE = "https://if9.prod-row.jlrmotor.com/if9/webview"

# IFAS token endpoint (password / refresh grant).
IFAS_TOKENS_URL = f"{IFAS_BASE}/webview/tokens"
# Fixed IFAS client credential ("as:aspass"), base64-encoded.
TOKENS_BASIC_AUTH = "Basic YXM6YXNwYXNz"

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

DEFAULT_SCAN_INTERVAL = timedelta(minutes=5)
# A position older than this is flagged stale (informational attribute only).
STALE_AFTER = timedelta(hours=24)

# ECC target temperature bounds (degrees Celsius).
ECC_MIN_TEMP = 16.0
ECC_MAX_TEMP = 28.0
ECC_DEFAULT_TEMP = 21.0

# ICE remote climate uses an RCC scale of 31 (LO/cool) – 57 (HI/heat).
ICE_RCC_MIN = 31
ICE_RCC_MAX = 57
ICE_MIN_TEMP = 16.0
ICE_MAX_TEMP = 28.5
ICE_DEFAULT_HEAT_TEMP = 22.0
ICE_DEFAULT_COOL_TEMP = 18.0

PLATFORMS = [
    "sensor",
    "binary_sensor",
    "device_tracker",
    "lock",
    "climate",
    "button",
    "switch",
    "diagnostics",
]

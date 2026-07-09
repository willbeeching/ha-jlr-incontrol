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
# The Accept a command POST must send for its response. Validated live: v4 works;
# v5 and plain application/json both return HTTP 406.
MEDIA_SERVICE_STATUS = "application/vnd.wirelesscar.ngtp.if9.ServiceStatus-v4+json"

# ---- Config entry keys ----
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_PIN = "pin"
CONF_DEVICE_ID = "device_id"
CONF_USER_ID = "user_id"

# ---- Remote service codes (serviceName) ----
SERVICE_LOCK = "RDL"
SERVICE_UNLOCK = "RDU"
SERVICE_ENGINE_ON = "REON"  # remote-start climate (heat/precondition)
SERVICE_ENGINE_OFF = "REOFF"
SERVICE_HONK_FLASH = "HBLF"
SERVICE_ALARM_OFF = "ALOFF"

# serviceName -> path segment used to start the service.
SERVICE_ENDPOINTS: dict[str, str] = {
    SERVICE_LOCK: "lock",
    SERVICE_UNLOCK: "unlock",
    SERVICE_ENGINE_ON: "engineOn",
    SERVICE_ENGINE_OFF: "engineOff",
    SERVICE_HONK_FLASH: "honkBlink",
    SERVICE_ALARM_OFF: "alarmOff",
}

DEFAULT_SCAN_INTERVAL = timedelta(minutes=5)
# A position older than this is flagged stale (informational attribute only).
STALE_AFTER = timedelta(hours=24)

PLATFORMS = ["sensor", "binary_sensor", "device_tracker", "lock", "climate", "button"]

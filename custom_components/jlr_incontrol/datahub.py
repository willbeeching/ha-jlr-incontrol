"""JLR Data Hub: the official, consent-based API JLR publish under the EU Data Act.

Separate from everything else in this integration on purpose. The rest of it
speaks to the endpoints JLR's own app uses, reverse-engineered; this speaks to
an API JLR document at developer.jlr.com, and it authenticates in a different
way entirely:

- No password. The owner creates a client at data.jlr.com and uploads a
  certificate; every token request is a JWT signed with the matching private
  key (``private_key_jwt``), and that key never leaves Home Assistant.
- The owner approves once, on their phone, with a device code. After that an
  offline refresh token keeps the link alive without them.
- Every REST call carries a DPoP proof signed with the same key, which binds
  the access token to this installation: a token copied out of a log is useless
  without the key.

Several of the published docs disagree with what the platform actually does.
Where they do, the code follows what was measured, and says so.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

AUTH_BASE = "https://auth.jlr-vcdp.com/realms/public/protocol/openid-connect"
TOKEN_URL = f"{AUTH_BASE}/token"
DEVICE_URL = f"{AUTH_BASE}/auth/device"
API_BASE = "https://data.jlr-vcdp.com"
PORTAL_URL = "https://data.jlr.com"

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# JLR's own example certificate lasts a year and the docs state no limit. Kept
# to that rather than guessed longer, since a certificate the portal rejects
# would fail at the one step the owner cannot see the reason for.
CERTIFICATE_DAYS = 365

TIMEOUT = aiohttp.ClientTimeout(total=30)


class DataHubError(Exception):
    """The Data Hub could not be reached or did not answer usefully."""


class DataHubAuthError(DataHubError):
    """The link itself is no longer accepted and has to be set up again.

    Raised for a rejected refresh token or a rejected client assertion — the
    owner deleted the client, replaced its certificate, or withdrew consent.
    Retrying cannot fix any of those, so callers stop rather than loop.
    """


class DataHubDenied(DataHubError):
    """The owner declined the sign-in on their phone."""


class DataHubExpired(DataHubError):
    """The device code ran out before the owner approved it."""


class DataHubForbidden(DataHubError):
    """Signed in, but not allowed this vehicle or this category."""


class DataHubRateLimited(DataHubError):
    """429. Carries how long JLR asked us to wait."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited for {retry_after:.0f}s")
        self.retry_after = retry_after


@dataclass(frozen=True)
class Credentials:
    """A key pair, as the PEM text the config entry stores."""

    private_key: str
    certificate: str


def generate_credentials() -> Credentials:
    """A fresh RSA key and the self-signed certificate the portal asks for.

    Blocking (key generation takes a noticeable fraction of a second), so run
    it in the executor. A plain RSA key signs PS256 the same as JLR's RSA-PSS
    example key, and has been accepted by the portal; a key whose certificate
    names the PSS algorithm is the less common shape and was not needed.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Home Assistant")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=CERTIFICATE_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    return Credentials(
        private_key=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        certificate=certificate.public_bytes(serialization.Encoding.PEM).decode(),
    )


def certificate_expiry(certificate: str) -> datetime:
    """When the uploaded certificate stops being accepted."""
    cert = x509.load_pem_x509_certificate(certificate.encode())
    return cert.not_valid_after_utc


@dataclass(frozen=True)
class DeviceLogin:
    """A device-code sign-in waiting for the owner to approve it."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    interval: float
    expires_at: float
    verifier: str


@dataclass
class Tokens:
    """What a sign-in or a renewal returns, with the claims we use read out."""

    access_token: str
    refresh_token: str | None
    expires_at: float
    vehicles: list[str] = field(default_factory=list)
    account_id: str | None = None
    categories: list[str] = field(default_factory=list)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _int_b64url(number: int) -> str:
    return _b64url(number.to_bytes((number.bit_length() + 7) // 8, "big"))


def _claims(token: str) -> dict[str, Any]:
    """The access token's claims, unverified.

    Read for the vehicle list and the streaming account id, both of which only
    arrive inside the token. Not trusted for anything else: the API checks the
    token, we only need to know what it says it covers.
    """
    try:
        claims: dict[str, Any] = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return {}
    return claims


def _tokens(body: dict[str, Any]) -> Tokens:
    access = body.get("access_token")
    if not isinstance(access, str):
        raise DataHubError("token response carried no access token")
    claims = _claims(access)
    vehicles = claims.get("vehicles")
    dsas = claims.get("dsas")
    categories = claims.get("categories")
    return Tokens(
        access_token=access,
        refresh_token=body.get("refresh_token"),
        # Measured at 300 seconds. The docs say 3600; trusting them would
        # spend a dead token for 55 minutes of every hour.
        expires_at=time.time() + float(body.get("expires_in") or 300),
        vehicles=(
            [v for v in vehicles if isinstance(v, str)]
            if isinstance(vehicles, list)
            else []
        ),
        account_id=dsas.get("account_id") if isinstance(dsas, dict) else None,
        categories=(
            [c for c in categories if isinstance(c, str)]
            if isinstance(categories, list)
            else []
        ),
    )


class DataHubClient:
    """Sign-in, renewal and the REST calls, for one owner's client."""

    def __init__(
        self, session: aiohttp.ClientSession, client_id: str, private_key: str
    ) -> None:
        self._session = session
        self.client_id = client_id
        key = serialization.load_pem_private_key(private_key.encode(), None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise DataHubError("the stored key is not an RSA key")
        self._key: rsa.RSAPrivateKey = key
        public = key.public_key().public_numbers()
        self._jwk = {
            "kty": "RSA",
            "n": _int_b64url(public.n),
            "e": _int_b64url(public.e),
        }
        # Echoed back from a use_dpop_nonce challenge. JLR have not sent one
        # yet, but the server is Keycloak, which can be switched to require it
        # without notice, and the answer is one retry rather than a dead link.
        self._nonce: str | None = None

    def _assertion(self) -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "iss": self.client_id,
                "sub": self.client_id,
                "aud": TOKEN_URL,
                "jti": str(uuid.uuid4()),
                "iat": now,
                "exp": now + 300,
            },
            self._key,
            algorithm="PS256",
            headers={"typ": "JWT"},
        )

    def _proof(self, method: str, url: str, access_token: str | None = None) -> str:
        claims: dict[str, Any] = {
            "jti": str(uuid.uuid4()),
            "htm": method,
            "htu": url.split("?", 1)[0],
            "iat": int(time.time()),
        }
        if access_token:
            claims["ath"] = _b64url(hashlib.sha256(access_token.encode()).digest())
        if self._nonce:
            claims["nonce"] = self._nonce
        return jwt.encode(
            claims,
            self._key,
            algorithm="PS256",
            headers={"typ": "dpop+jwt", "jwk": self._jwk},
        )

    def _client_auth(self) -> dict[str, str]:
        return {
            "client_id": self.client_id,
            "client_assertion_type": ASSERTION_TYPE,
            "client_assertion": self._assertion(),
        }

    async def _post(
        self, url: str, data: dict[str, str], dpop: bool
    ) -> tuple[int, dict[str, Any]]:
        for attempt in range(2):
            headers = {"DPoP": self._proof("POST", url)} if dpop else {}
            try:
                async with self._session.post(
                    url, data=data, headers=headers, timeout=TIMEOUT
                ) as resp:
                    body = await resp.json(content_type=None)
                    nonce = resp.headers.get("DPoP-Nonce")
                    status = resp.status
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                raise DataHubError(f"could not reach JLR: {err!r}") from err
            if not isinstance(body, dict):
                body = {}
            if (
                attempt == 0
                and nonce
                and body.get("error") == "use_dpop_nonce"
                and nonce != self._nonce
            ):
                self._nonce = nonce
                # The assertion is single-use (unique jti), so it is rebuilt.
                data = {**data, **self._client_auth()}
                continue
            return status, body
        return status, body  # pragma: no cover - the loop always returns

    async def async_start_device_login(self) -> DeviceLogin:
        """Ask for a code the owner approves on their phone."""
        verifier = secrets.token_urlsafe(48)
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        status, body = await self._post(
            DEVICE_URL,
            {
                **self._client_auth(),
                "response_type": "device_code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
            dpop=False,
        )
        if status in (400, 401) and body.get("error") in (
            "invalid_client",
            "unauthorized_client",
        ):
            raise DataHubAuthError(body.get("error_description") or body["error"])
        if status != 200 or "device_code" not in body:
            raise DataHubError(f"device sign-in refused ({status})")
        return DeviceLogin(
            device_code=body["device_code"],
            user_code=body.get("user_code", ""),
            verification_uri=body.get("verification_uri", ""),
            verification_uri_complete=body.get("verification_uri_complete")
            or body.get("verification_uri", ""),
            interval=float(body.get("interval") or 5),
            expires_at=time.time() + float(body.get("expires_in") or 600),
            verifier=verifier,
        )

    async def async_poll_device_login(self, login: DeviceLogin) -> Tokens | None:
        """One poll. None while the owner has not approved yet."""
        status, body = await self._post(
            TOKEN_URL,
            {
                **self._client_auth(),
                "grant_type": DEVICE_GRANT,
                "device_code": login.device_code,
                "code_verifier": login.verifier,
            },
            dpop=True,
        )
        if status == 200:
            return _tokens(body)
        error = body.get("error")
        if error in ("authorization_pending", "slow_down"):
            return None
        if error == "access_denied":
            raise DataHubDenied("the sign-in was declined")
        if error == "expired_token":
            raise DataHubExpired("the code expired before it was approved")
        if error in ("invalid_client", "unauthorized_client"):
            raise DataHubAuthError(body.get("error_description") or error)
        raise DataHubError(f"sign-in failed ({status} {error})")

    async def async_refresh(self, refresh_token: str) -> Tokens:
        """Renew the access token with the stored refresh token.

        The refresh token JLR issue to a device sign-in is an "Offline" token
        with no expiry of its own — the docs say 14 days; measured, it carries
        none. It is rotated on use, so the caller must keep the new one.
        """
        status, body = await self._post(
            TOKEN_URL,
            {
                **self._client_auth(),
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            dpop=True,
        )
        if status == 200:
            tokens = _tokens(body)
            if not tokens.refresh_token:
                tokens.refresh_token = refresh_token
            return tokens
        if body.get("error") in (
            "invalid_grant",
            "invalid_client",
            "unauthorized_client",
        ):
            raise DataHubAuthError(body.get("error_description") or body["error"])
        raise DataHubError(f"renewal failed ({status} {body.get('error')})")

    async def async_stream_token(self) -> Tokens:
        """A client-credentials token, which is the MQTT password.

        Refused with ``unauthorized_client`` until the owner switches the
        stream on in the portal: that toggle is what enables the grant.
        """
        status, body = await self._post(
            TOKEN_URL,
            {**self._client_auth(), "grant_type": "client_credentials"},
            dpop=False,
        )
        if status == 200:
            return _tokens(body)
        if body.get("error") in ("invalid_client", "unauthorized_client"):
            raise DataHubForbidden(body.get("error_description") or body["error"])
        raise DataHubError(f"stream token refused ({status} {body.get('error')})")

    async def async_get(self, path: str, access_token: str) -> dict[str, Any]:
        """GET a REST path with the token and a fresh DPoP proof."""
        url = f"{API_BASE}{path}"
        for attempt in range(2):
            headers = {
                "Authorization": f"DPoP {access_token}",
                "DPoP": self._proof("GET", url, access_token),
                "Accept": "application/json",
            }
            try:
                async with self._session.get(
                    url, headers=headers, timeout=TIMEOUT
                ) as resp:
                    status = resp.status
                    nonce = resp.headers.get("DPoP-Nonce")
                    retry_after = resp.headers.get("Retry-After")
                    body = await resp.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                raise DataHubError(f"could not reach JLR: {err!r}") from err
            if attempt == 0 and status == 401 and nonce and nonce != self._nonce:
                self._nonce = nonce
                continue
            break
        if status == 200 and isinstance(body, dict):
            return body
        if status == 403:
            detail = body.get("detail") if isinstance(body, dict) else None
            raise DataHubForbidden(detail or "not permitted")
        if status == 429:
            try:
                wait = float(retry_after or 60)
            except ValueError:
                wait = 60.0
            raise DataHubRateLimited(wait)
        raise DataHubError(f"GET {path.split('/')[-1]} answered {status}")

    async def async_status(self, vin: str, access_token: str) -> dict[str, Any]:
        """The latest status snapshot for one vehicle."""
        return await self.async_get(f"/v1/vehicles/{vin}/status", access_token)

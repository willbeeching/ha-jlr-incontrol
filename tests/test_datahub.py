"""The Data Hub client: the key pair, the signed requests, and what JLR answer.

Every request this client makes is signed, so the tests check the signatures
with the public half of the key rather than trusting that a JWT-shaped string
went out. And every answer is one JLR have given (or document): the docs and
the platform disagree in places, and those places are pinned here.
"""

from __future__ import annotations

import base64
import hashlib
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fakes import FakeResponse, ScriptedSession

from custom_components.jlr_incontrol import datahub
from custom_components.jlr_incontrol.datahub import (
    DataHubAuthError,
    DataHubClient,
    DataHubDenied,
    DataHubError,
    DataHubExpired,
    DataHubForbidden,
    DataHubRateLimited,
    DeviceLogin,
    certificate_expiry,
    generate_credentials,
)

VIN = "SAJAA1234567890AB"
CLIENT_ID = "dsas-0123456789ab"


@pytest.fixture(scope="module")
def credentials() -> datahub.Credentials:
    return generate_credentials()


def public_key(creds: datahub.Credentials) -> Any:
    cert = x509.load_pem_x509_certificate(creds.certificate.encode())
    return cert.public_key()


def access_token(**claims: Any) -> str:
    """An access token shaped like JLR's. Unsigned: the client never verifies."""
    return jwt.encode(claims, "x" * 32, algorithm="HS256")


def ok_token(**extra: Any) -> FakeResponse:
    return FakeResponse(
        200,
        {
            "access_token": access_token(
                vehicles=[VIN],
                categories=["status", "position"],
                dsas={"account_id": "acct-1"},
            ),
            "token_type": "DPoP",
            "expires_in": 300,
            "refresh_token": "offline-2",
            **extra,
        },
    )


def make(creds: datahub.Credentials, *responses: Any) -> tuple[DataHubClient, Any]:
    session = ScriptedSession(*responses)
    return DataHubClient(session, CLIENT_ID, creds.private_key), session


def login() -> DeviceLogin:
    return DeviceLogin(
        device_code="dev",
        user_code="ABCD-EFGH",
        verification_uri="https://auth/device",
        verification_uri_complete="https://auth/device?user_code=ABCD-EFGH",
        interval=5,
        expires_at=time.time() + 600,
        verifier="v" * 64,
    )


class TestTheKeyPair:
    def test_the_certificate_matches_the_key(
        self, credentials: datahub.Credentials
    ) -> None:
        key = serialization.load_pem_private_key(credentials.private_key.encode(), None)
        assert (
            key.public_key().public_numbers()
            == public_key(credentials).public_numbers()
        )

    def test_the_certificate_lasts_the_year_jlrs_example_does(
        self, credentials: datahub.Credentials
    ) -> None:
        expires = certificate_expiry(credentials.certificate)
        assert expires - datetime.now(UTC) > timedelta(days=364)
        assert expires - datetime.now(UTC) < timedelta(days=366)

    def test_only_the_certificate_is_pem_certificate_text(
        self, credentials: datahub.Credentials
    ) -> None:
        # The certificate is what the owner pastes; the key must never look
        # like something to paste.
        assert credentials.certificate.startswith("-----BEGIN CERTIFICATE-----")
        assert "PRIVATE KEY" in credentials.private_key
        assert "PRIVATE KEY" not in credentials.certificate

    def test_a_key_that_is_not_rsa_is_refused(self) -> None:
        other = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        with pytest.raises(DataHubError):
            DataHubClient(ScriptedSession(), CLIENT_ID, other.decode())


class TestTheSignatures:
    async def test_the_client_assertion_is_signed_for_the_token_endpoint(
        self, credentials: datahub.Credentials
    ) -> None:
        client, session = make(credentials, ok_token())
        await client.async_refresh("offline-1")
        sent = session.calls[0][2]["data"]
        assert sent["client_assertion_type"] == datahub.ASSERTION_TYPE
        claims = jwt.decode(
            sent["client_assertion"],
            public_key(credentials),
            algorithms=["PS256"],
            audience=datahub.TOKEN_URL,
        )
        assert claims["iss"] == claims["sub"] == CLIENT_ID
        assert claims["exp"] - claims["iat"] == 300

    async def test_a_dpop_proof_carries_the_public_key_and_the_call(
        self, credentials: datahub.Credentials
    ) -> None:
        token = access_token(vehicles=[VIN])
        client, session = make(
            credentials, FakeResponse(200, {"timestamp": "t", "Vehicle": {}})
        )
        await client.async_status(VIN, token)
        method, url, kwargs = session.calls[0]
        headers = kwargs["headers"]
        assert headers["Authorization"] == f"DPoP {token}"
        header = jwt.get_unverified_header(headers["DPoP"])
        assert header["typ"] == "dpop+jwt" and header["alg"] == "PS256"
        assert header["jwk"]["kty"] == "RSA"
        proof = jwt.decode(
            headers["DPoP"], public_key(credentials), algorithms=["PS256"]
        )
        assert proof["htm"] == "GET"
        assert proof["htu"] == url == f"{datahub.API_BASE}/v1/vehicles/{VIN}/status"
        digest = hashlib.sha256(token.encode()).digest()
        assert proof["ath"] == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    async def test_the_proof_leaves_the_query_out(
        self, credentials: datahub.Credentials
    ) -> None:
        client, session = make(credentials, FakeResponse(200, {}))
        await client.async_get("/v1/vehicles/X/journeys?page=0", "t")
        proof = jwt.decode(
            session.calls[0][2]["headers"]["DPoP"],
            public_key(credentials),
            algorithms=["PS256"],
        )
        assert proof["htu"].endswith("/journeys")

    async def test_every_request_has_its_own_proof(
        self, credentials: datahub.Credentials
    ) -> None:
        client, session = make(
            credentials, FakeResponse(200, {}), FakeResponse(200, {})
        )
        await client.async_get("/a", "t")
        await client.async_get("/a", "t")
        first, second = (
            jwt.decode(c[2]["headers"]["DPoP"], options={"verify_signature": False})
            for c in session.calls
        )
        assert first["jti"] != second["jti"]


class TestTheDeviceSignIn:
    async def test_it_starts_with_pkce(self, credentials: datahub.Credentials) -> None:
        client, session = make(
            credentials,
            FakeResponse(
                200,
                {
                    "device_code": "dev",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://auth/device",
                    "verification_uri_complete": "https://auth/device?user_code=X",
                    "expires_in": 600,
                    "interval": 5,
                },
            ),
        )
        started = await client.async_start_device_login()
        sent = session.calls[0][2]["data"]
        assert session.calls[0][1] == datahub.DEVICE_URL
        assert sent["code_challenge_method"] == "S256"
        challenge = hashlib.sha256(started.verifier.encode()).digest()
        assert (
            sent["code_challenge"]
            == base64.urlsafe_b64encode(challenge).rstrip(b"=").decode()
        )
        assert started.user_code == "ABCD-EFGH"
        assert started.verification_uri_complete.endswith("user_code=X")
        assert started.expires_at > time.time() + 590

    async def test_a_link_without_the_prefilled_url_falls_back(
        self, credentials: datahub.Credentials
    ) -> None:
        client, _ = make(
            credentials,
            FakeResponse(200, {"device_code": "d", "verification_uri": "https://v"}),
        )
        started = await client.async_start_device_login()
        assert started.verification_uri_complete == "https://v"
        assert started.interval == 5

    @pytest.mark.parametrize("error", ["invalid_client", "unauthorized_client"])
    async def test_a_client_jlr_do_not_know_is_an_auth_error(
        self, credentials: datahub.Credentials, error: str
    ) -> None:
        client, _ = make(credentials, FakeResponse(401, {"error": error}))
        with pytest.raises(DataHubAuthError):
            await client.async_start_device_login()

    async def test_anything_else_is_a_plain_error(
        self, credentials: datahub.Credentials
    ) -> None:
        client, _ = make(credentials, FakeResponse(503, "busy"))
        with pytest.raises(DataHubError) as raised:
            await client.async_start_device_login()
        assert not isinstance(raised.value, DataHubAuthError)

    @pytest.mark.parametrize("error", ["authorization_pending", "slow_down"])
    async def test_waiting_for_the_owner_is_none(
        self, credentials: datahub.Credentials, error: str
    ) -> None:
        client, _ = make(credentials, FakeResponse(400, {"error": error}))
        assert await client.async_poll_device_login(login()) is None

    @pytest.mark.parametrize(
        ("error", "raised"),
        [
            ("access_denied", DataHubDenied),
            ("expired_token", DataHubExpired),
            ("invalid_client", DataHubAuthError),
            ("server_error", DataHubError),
        ],
    )
    async def test_how_a_poll_can_fail(
        self, credentials: datahub.Credentials, error: str, raised: type
    ) -> None:
        client, _ = make(credentials, FakeResponse(400, {"error": error}))
        with pytest.raises(raised):
            await client.async_poll_device_login(login())

    async def test_approval_returns_what_the_token_covers(
        self, credentials: datahub.Credentials
    ) -> None:
        client, session = make(credentials, ok_token())
        tokens = await client.async_poll_device_login(login())
        assert tokens is not None
        assert tokens.vehicles == [VIN]
        assert tokens.account_id == "acct-1"
        assert tokens.categories == ["status", "position"]
        assert tokens.refresh_token == "offline-2"
        # Bound to the key from the first token on.
        assert "DPoP" in session.calls[0][2]["headers"]
        assert session.calls[0][2]["data"]["code_verifier"] == "v" * 64


class TestRenewal:
    async def test_the_token_lasts_what_jlr_say_not_what_the_docs_say(
        self, credentials: datahub.Credentials
    ) -> None:
        client, _ = make(credentials, ok_token())
        tokens = await client.async_refresh("offline-1")
        assert 290 < tokens.expires_at - time.time() <= 300

    async def test_a_renewal_without_a_new_refresh_token_keeps_the_old(
        self, credentials: datahub.Credentials
    ) -> None:
        client, _ = make(credentials, ok_token(refresh_token=None))
        tokens = await client.async_refresh("offline-1")
        assert tokens.refresh_token == "offline-1"

    @pytest.mark.parametrize("error", ["invalid_grant", "invalid_client"])
    async def test_a_revoked_link_is_an_auth_error(
        self, credentials: datahub.Credentials, error: str
    ) -> None:
        client, _ = make(credentials, FakeResponse(400, {"error": error}))
        with pytest.raises(DataHubAuthError):
            await client.async_refresh("offline-1")

    async def test_an_outage_is_not(self, credentials: datahub.Credentials) -> None:
        client, _ = make(credentials, FakeResponse(502, {}))
        with pytest.raises(DataHubError) as raised:
            await client.async_refresh("offline-1")
        assert not isinstance(raised.value, DataHubAuthError)

    async def test_a_body_that_is_not_json_is_an_error(
        self, credentials: datahub.Credentials
    ) -> None:
        client, _ = make(credentials, FakeResponse(200, ValueError("html")))
        with pytest.raises(DataHubError):
            await client.async_refresh("offline-1")

    async def test_a_token_response_without_a_token_is_an_error(
        self, credentials: datahub.Credentials
    ) -> None:
        client, _ = make(credentials, FakeResponse(200, ["not", "a", "dict"]))
        with pytest.raises(DataHubError):
            await client.async_refresh("offline-1")

    async def test_unreadable_claims_cover_nothing(
        self, credentials: datahub.Credentials
    ) -> None:
        client, _ = make(credentials, FakeResponse(200, {"access_token": "opaque"}))
        tokens = await client.async_refresh("offline-1")
        assert tokens.vehicles == [] and tokens.account_id is None

    async def test_a_network_failure_is_an_error(
        self, credentials: datahub.Credentials
    ) -> None:
        client, _ = make(credentials, aiohttp.ClientConnectionError("down"))
        with pytest.raises(DataHubError):
            await client.async_refresh("offline-1")


class TestTheNonceChallenge:
    async def test_the_token_endpoint_challenge_is_answered_once(
        self, credentials: datahub.Credentials
    ) -> None:
        client, session = make(
            credentials,
            FakeResponse(400, {"error": "use_dpop_nonce"}, {"DPoP-Nonce": "n1"}),
            ok_token(),
        )
        await client.async_refresh("offline-1")
        assert len(session.calls) == 2
        retried = session.calls[1][2]
        proof = jwt.decode(
            retried["headers"]["DPoP"], options={"verify_signature": False}
        )
        assert proof["nonce"] == "n1"
        # A fresh assertion: the first one's jti is spent.
        assert (
            retried["data"]["client_assertion"]
            != session.calls[0][2]["data"]["client_assertion"]
        )

    async def test_a_challenge_that_repeats_is_not_a_loop(
        self, credentials: datahub.Credentials
    ) -> None:
        challenge = FakeResponse(400, {"error": "use_dpop_nonce"}, {"DPoP-Nonce": "n1"})
        client, session = make(credentials, challenge, challenge)
        with pytest.raises(DataHubError):
            await client.async_refresh("offline-1")
        assert len(session.calls) == 2

    async def test_the_api_challenge_is_answered_once(
        self, credentials: datahub.Credentials
    ) -> None:
        client, session = make(
            credentials,
            FakeResponse(401, {}, {"DPoP-Nonce": "n2"}),
            FakeResponse(200, {"ok": True}),
        )
        assert await client.async_get("/v1/x", "t") == {"ok": True}
        proof = jwt.decode(
            session.calls[1][2]["headers"]["DPoP"], options={"verify_signature": False}
        )
        assert proof["nonce"] == "n2"


class TestTheStreamToken:
    async def test_it_is_client_credentials_without_dpop(
        self, credentials: datahub.Credentials
    ) -> None:
        client, session = make(credentials, ok_token(refresh_token=None))
        tokens = await client.async_stream_token()
        assert tokens.account_id == "acct-1"
        sent = session.calls[0][2]
        assert sent["data"]["grant_type"] == "client_credentials"
        assert "DPoP" not in sent["headers"]

    async def test_a_stream_that_is_switched_off_is_forbidden(
        self, credentials: datahub.Credentials
    ) -> None:
        # What JLR answer until the owner turns the stream on in the portal.
        client, _ = make(
            credentials,
            FakeResponse(
                401,
                {
                    "error": "unauthorized_client",
                    "error_description": (
                        "Client not enabled to retrieve service account"
                    ),
                },
            ),
        )
        with pytest.raises(DataHubForbidden):
            await client.async_stream_token()

    async def test_anything_else_is_a_plain_error(
        self, credentials: datahub.Credentials
    ) -> None:
        client, _ = make(credentials, FakeResponse(500, {}))
        with pytest.raises(DataHubError) as raised:
            await client.async_stream_token()
        assert not isinstance(raised.value, DataHubForbidden)


class TestTheApi:
    async def test_a_category_the_car_does_not_allow_is_forbidden(
        self, credentials: datahub.Credentials
    ) -> None:
        client, _ = make(
            credentials,
            FakeResponse(
                403,
                {
                    "detail": (
                        "Access to category is not permitted for this vehicle: "
                        "position"
                    )
                },
            ),
        )
        with pytest.raises(DataHubForbidden, match="position"):
            await client.async_get("/v1/vehicles/X/location", "t")

    async def test_the_gateways_own_refusal_is_forbidden_too(
        self, credentials: datahub.Credentials
    ) -> None:
        # Journeys answer this, from the gateway, with no problem document.
        client, _ = make(credentials, FakeResponse(403, "Access denied"))
        with pytest.raises(DataHubForbidden):
            await client.async_get("/v1/vehicles/X/journeys", "t")

    async def test_a_rate_limit_says_how_long(
        self, credentials: datahub.Credentials
    ) -> None:
        client, _ = make(credentials, FakeResponse(429, {}, {"Retry-After": "120"}))
        with pytest.raises(DataHubRateLimited) as raised:
            await client.async_get("/v1/x", "t")
        assert raised.value.retry_after == 120

    async def test_an_unreadable_retry_after_waits_a_minute(
        self, credentials: datahub.Credentials
    ) -> None:
        client, _ = make(
            credentials, FakeResponse(429, {}, {"Retry-After": "Fri, 07 Nov"})
        )
        with pytest.raises(DataHubRateLimited) as raised:
            await client.async_get("/v1/x", "t")
        assert raised.value.retry_after == 60

    async def test_a_server_error_does_not_echo_the_vin(
        self, credentials: datahub.Credentials
    ) -> None:
        client, _ = make(credentials, FakeResponse(500, {}))
        with pytest.raises(DataHubError) as raised:
            await client.async_status(VIN, "t")
        assert VIN not in str(raised.value)

    async def test_location_asks_the_location_path(
        self, credentials: datahub.Credentials
    ) -> None:
        client, session = make(credentials, FakeResponse(200, {"Vehicle": {}}))
        assert await client.async_location(VIN, "t") == {"Vehicle": {}}
        assert session.calls[0][1].endswith(f"/v1/vehicles/{VIN}/location")

    async def test_a_network_failure_is_an_error(
        self, credentials: datahub.Credentials
    ) -> None:
        client, _ = make(credentials, TimeoutError())
        with pytest.raises(DataHubError):
            await client.async_get("/v1/x", "t")

from __future__ import annotations

import hashlib
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from odoo_agent_mcp.auth import JwtTokenVerifier, StaticTokenVerifier
from odoo_agent_mcp.config import Settings


@pytest.mark.asyncio
async def test_static_token_verifier_uses_digest_and_constant_scope() -> None:
    token = "client-secret"
    settings = Settings(
        odoo_url="https://odoo.example.test",
        connector_token="connector-secret",
        static_token_digests=frozenset({hashlib.sha256(token.encode()).hexdigest()}),
        required_scopes=("odoo:read", "odoo:write"),
    )
    verifier = StaticTokenVerifier(settings)

    accepted = await verifier.verify_token(token)

    assert accepted is not None
    assert accepted.scopes == ["odoo:read", "odoo:write"]
    assert await verifier.verify_token("wrong") is None


@pytest.mark.asyncio
async def test_jwt_verifier_checks_signature_issuer_audience_and_scope() -> None:
    issuer = "https://identity.example.test"
    audience = "https://mcp.example.test"
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(
        private_key.public_key(),
        as_dict=True,
    )
    public_jwk.update({"kid": "key-1", "use": "sig", "alg": "RS256"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={"jwks_uri": f"{issuer}/jwks"},
                request=request,
            )
        return httpx.Response(
            200,
            json={"keys": [public_jwk]},
            request=request,
        )

    settings = Settings(
        odoo_url="https://odoo.example.test",
        connector_token="connector-secret",
        auth_mode="oauth",
        oauth_issuer_url=issuer,
        resource_server_url=audience,
        oauth_audience=audience,
        required_scopes=("odoo:read",),
    )
    verifier = JwtTokenVerifier(settings, transport=httpx.MockTransport(handler))
    now = int(time.time())
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": "agent-42",
        "client_id": "client-42",
        "iat": now,
        "exp": now + 300,
        "scope": "odoo:read profile",
    }
    token = jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": "key-1"},
    )
    wrong_scope = jwt.encode(
        {**claims, "scope": "profile"},
        private_key,
        algorithm="RS256",
        headers={"kid": "key-1"},
    )

    try:
        accepted = await verifier.verify_token(token)
        assert accepted is not None
        assert accepted.client_id == "client-42"
        assert accepted.subject == "agent-42"
        assert await verifier.verify_token(wrong_scope) is None
    finally:
        await verifier.aclose()

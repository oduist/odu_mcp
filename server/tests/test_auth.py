from __future__ import annotations

import httpx
import pytest

from odoo_agent_mcp.auth import OdooApiKeyVerifier
from odoo_agent_mcp.config import Settings


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "odoo_url": "https://odoo.example.test",
        "public_url": "https://mcp.example.test",
        "identity_cache_seconds": 30,
        "events_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_verifier_resolves_odoo_identity_and_caches_by_token_digest() -> None:
    requests = 0
    verified: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert request.headers["Authorization"] == "Bearer user-key"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": {
                    "database": "prod",
                    "user_id": 42,
                    "login": "agent@example.test",
                    "profile": "sales",
                },
            },
            request=request,
        )

    async def on_verified(token) -> None:
        verified.append(token.subject)

    verifier = OdooApiKeyVerifier(
        _settings(),
        transport=httpx.MockTransport(handler),
        on_verified=on_verified,
    )
    try:
        first = await verifier.verify_token("user-key")
        second = await verifier.verify_token("user-key")
    finally:
        await verifier.aclose()

    assert first is second
    assert first is not None
    assert first.client_id == "odoo:prod:42"
    assert first.subject == "odoo:prod:42"
    assert first.scopes == ["mcp"]
    assert first.claims["profile"] == "sales"
    assert requests == 1
    assert verified == ["odoo:prod:42", "odoo:prod:42"]
    assert "user-key" not in verifier._cache


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_verifier_rejects_invalid_or_unassigned_keys(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"ok": False}, request=request)

    verifier = OdooApiKeyVerifier(_settings(), transport=httpx.MockTransport(handler))
    try:
        assert await verifier.verify_token("invalid") is None
    finally:
        await verifier.aclose()


@pytest.mark.asyncio
async def test_verifier_surfaces_odoo_outage_as_upstream_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"ok": False}, request=request)

    verifier = OdooApiKeyVerifier(_settings(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await verifier.verify_token("user-key")
    finally:
        await verifier.aclose()

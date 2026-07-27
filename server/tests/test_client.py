from __future__ import annotations

import httpx
import pytest

from odoo_agent_mcp.client import OdooControlClient
from odoo_agent_mcp.config import Settings
from odoo_agent_mcp.errors import OdooApiError, ResponseTooLargeError


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "odoo_url": "https://odoo.example.test",
        "connector_token": "connector-secret",
        "retry_attempts": 3,
        "circuit_failure_threshold": 2,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_execute_sends_connector_token_and_unwraps_envelope() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["Authorization"]
        seen["request_id"] = request.headers["X-Request-ID"]
        return httpx.Response(
            200,
            json={
                "ok": True,
                "request_id": "response-id",
                "data": {"records": [{"id": 7}]},
            },
            request=request,
        )

    client = OdooControlClient(_settings(), transport=httpx.MockTransport(handler))
    try:
        result = await client.execute(
            "records.search",
            {"model": "res.partner"},
            request_id="request-id",
        )
    finally:
        await client.aclose()

    assert seen == {
        "authorization": "Bearer connector-secret",
        "request_id": "request-id",
    }
    assert result == {"records": [{"id": 7}], "request_id": "response-id"}


@pytest.mark.asyncio
async def test_safe_operation_retries_but_mutation_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, json={"ok": False}, request=request)
        return httpx.Response(
            200,
            json={"ok": True, "request_id": "ok", "data": {}},
            request=request,
        )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("odoo_agent_mcp.client.asyncio.sleep", no_sleep)
    client = OdooControlClient(_settings(), transport=httpx.MockTransport(handler))
    try:
        await client.execute("records.search")
        assert calls == 3

        calls = 0
        with pytest.raises(OdooApiError):
            await client.execute("changes.preview")
        assert calls == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_response_size_limit_is_enforced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"ok":true,"data":{"value":"' + (b"x" * 512) + b'"}}',
            request=request,
        )

    client = OdooControlClient(
        _settings(max_response_bytes=128),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ResponseTooLargeError):
            await client.execute("records.search")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_structured_connector_error_is_preserved() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "ok": False,
                "request_id": "denial-1",
                "error": {
                    "code": "policy_denied",
                    "message": "Model is not allowed.",
                    "retryable": False,
                },
            },
            request=request,
        )

    client = OdooControlClient(_settings(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(OdooApiError) as raised:
            await client.execute("records.search")
    finally:
        await client.aclose()

    assert raised.value.code == "policy_denied"
    assert raised.value.status_code == 403
    assert raised.value.request_id == "denial-1"

from __future__ import annotations

import asyncio

import httpx
import pytest

from odoo_agent_mcp.client import OdooControlClient
from odoo_agent_mcp.config import Settings
from odoo_agent_mcp.errors import CircuitOpenError, OdooApiError, ResponseTooLargeError


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "odoo_url": "https://odoo.example.test",
        "retry_attempts": 3,
        "circuit_failure_threshold": 2,
        "events_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_execute_forwards_each_user_key_only_on_its_request() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers["Authorization"], request.headers["X-Request-ID"]))
        return httpx.Response(
            200,
            json={"ok": True, "request_id": "response-id", "data": {}},
            request=request,
        )

    client = OdooControlClient(_settings(), transport=httpx.MockTransport(handler))
    try:
        await client.execute(
            "records.search",
            bearer_token="alice-key",
            request_id="alice-request",
        )
        await client.execute(
            "records.search",
            bearer_token="bob-key",
            request_id="bob-request",
        )
        assert "Authorization" not in client._client.headers
    finally:
        await client.aclose()

    assert seen == [
        ("Bearer alice-key", "alice-request"),
        ("Bearer bob-key", "bob-request"),
    ]


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
    client = OdooControlClient(
        _settings(circuit_failure_threshold=10),
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.execute("records.search", bearer_token="key")
        assert calls == 3

        calls = 0
        with pytest.raises(OdooApiError):
            await client.execute("changes.preview", bearer_token="key")
        assert calls == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_user_errors_and_large_responses_do_not_open_breaker() -> None:
    response_status = 403

    def handler(request: httpx.Request) -> httpx.Response:
        if response_status == 403:
            return httpx.Response(
                403,
                json={
                    "ok": False,
                    "error": {"code": "policy_denied", "message": "Denied."},
                },
                request=request,
            )
        return httpx.Response(200, content=b"{" + (b"x" * 512), request=request)

    client = OdooControlClient(
        _settings(max_response_bytes=128, circuit_failure_threshold=1),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(OdooApiError) as denied:
            await client.execute("records.search", bearer_token="bad-user-key")
        assert denied.value.status_code == 403
        assert client.circuit.state == "closed"

        response_status = 200
        with pytest.raises(ResponseTooLargeError):
            await client.execute("records.search", bearer_token="key")
        assert client.circuit.state == "closed"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_invalid_json_closes_a_half_open_probe_without_counting_failure() -> None:
    client = OdooControlClient(
        _settings(retry_attempts=1, circuit_failure_threshold=1, circuit_reset_seconds=1),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"not-json", request=request)
        ),
    )
    client.circuit.opened_at = 0.0
    try:
        with pytest.raises(OdooApiError) as invalid:
            await client.execute("records.search", bearer_token="key")
        assert invalid.value.code == "invalid_connector_response"
        assert client.circuit.state == "closed"
        assert client.circuit.failures == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_transport_failures_open_breaker_and_only_one_half_open_probe_runs() -> None:
    mode = "fail"
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if mode == "fail":
            return httpx.Response(503, json={"ok": False}, request=request)
        probe_started.set()
        await release_probe.wait()
        return httpx.Response(
            200,
            json={"ok": True, "request_id": "ok", "data": {}},
            request=request,
        )

    client = OdooControlClient(
        _settings(retry_attempts=1, circuit_failure_threshold=2, circuit_reset_seconds=30),
        transport=httpx.MockTransport(handler),
    )
    try:
        for _ in range(2):
            with pytest.raises(OdooApiError):
                await client.execute("records.search", bearer_token="key")
        assert client.circuit.state == "open"
        assert client.circuit.opened_at is not None

        client.circuit.opened_at -= 31
        mode = "probe"
        first = asyncio.create_task(client.execute("records.search", bearer_token="key"))
        await probe_started.wait()
        with pytest.raises(CircuitOpenError):
            await client.execute("records.search", bearer_token="key")
        release_probe.set()
        await first
        assert client.circuit.state == "closed"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_health_request_is_public() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"status": "ok"}, request=request)

    client = OdooControlClient(_settings(), transport=httpx.MockTransport(handler))
    try:
        assert await client.health() == {"status": "ok"}
    finally:
        await client.aclose()

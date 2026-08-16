from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings
from .errors import CircuitOpenError, OdooApiError, ResponseTooLargeError

SAFE_OPERATIONS = {
    "capabilities",
    "system.info",
    "identity.whoami",
    "models.list",
    "models.describe",
    "records.search",
    "records.read",
    "records.count",
    "records.aggregate",
    "attachments.read",
    "reports.render",
    "changes.status",
}
RETRYABLE_STATUS = {502, 503, 504}


@dataclass(frozen=True, slots=True)
class CircuitPermit:
    generation: int
    probe: bool = False


@dataclass(slots=True)
class CircuitBreaker:
    failure_threshold: int
    reset_seconds: int
    failures: int = 0
    opened_at: float | None = None
    _probe_in_flight: bool = False
    _generation: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if self._probe_in_flight:
            return "half_open"
        return "open"

    async def acquire(self) -> CircuitPermit:
        async with self._lock:
            if self.opened_at is None:
                return CircuitPermit(self._generation)
            if time.monotonic() - self.opened_at < self.reset_seconds:
                raise CircuitOpenError()
            if self._probe_in_flight:
                raise CircuitOpenError()
            self._probe_in_flight = True
            return CircuitPermit(self._generation, probe=True)

    async def record_success(self, permit: CircuitPermit) -> None:
        async with self._lock:
            if permit.generation != self._generation:
                return
            self.failures = 0
            self.opened_at = None
            self._probe_in_flight = False
            if permit.probe:
                self._generation += 1

    async def record_failure(self, permit: CircuitPermit) -> None:
        async with self._lock:
            if permit.generation != self._generation:
                return
            self.failures += 1
            if permit.probe or self.failures >= self.failure_threshold:
                self.opened_at = time.monotonic()
                self._probe_in_flight = False
                self._generation += 1

    async def release(self, permit: CircuitPermit) -> None:
        """Release an unresolved half-open probe without changing breaker state."""
        async with self._lock:
            if permit.generation == self._generation and permit.probe:
                self._probe_in_flight = False


class OdooControlClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        timeout = httpx.Timeout(
            settings.request_timeout_seconds,
            connect=min(10.0, settings.request_timeout_seconds),
            pool=min(10.0, settings.request_timeout_seconds),
        )
        self._client = httpx.AsyncClient(
            base_url=settings.odoo_url.rstrip("/"),
            timeout=timeout,
            verify=settings.verify_tls,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            transport=transport,
            headers={
                "Content-Type": "application/vnd.connect-mcp+json",
                "Accept": "application/json",
                "User-Agent": "connect-mcp-server/2.0",
            },
        )
        self.circuit = CircuitBreaker(
            settings.circuit_failure_threshold,
            settings.circuit_reset_seconds,
        )

    async def __aenter__(self) -> OdooControlClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> dict[str, Any]:
        return await self._request_json("GET", "/connect_mcp/v1/health", safe=True)

    async def capabilities(self, bearer_token: str) -> dict[str, Any]:
        envelope = await self._request_json(
            "GET",
            "/connect_mcp/v1/capabilities",
            bearer_token=bearer_token,
            safe=True,
        )
        return self._unwrap(envelope)

    async def execute(
        self,
        operation: str,
        params: dict[str, Any] | None = None,
        *,
        bearer_token: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = request_id or str(uuid.uuid4())
        envelope = await self._request_json(
            "POST",
            "/connect_mcp/v1/execute",
            json_body={"operation": operation, "params": params or {}},
            bearer_token=bearer_token,
            request_id=request_id,
            safe=operation in SAFE_OPERATIONS,
        )
        return self._unwrap(envelope)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        bearer_token: str | None = None,
        request_id: str | None = None,
        safe: bool,
    ) -> dict[str, Any]:
        attempts = self.settings.retry_attempts if safe else 1
        last_error: OdooApiError | None = None
        for attempt in range(attempts):
            permit = await self.circuit.acquire()
            permit_resolved = False
            current_request_id = request_id or str(uuid.uuid4())
            headers = {"X-Request-ID": current_request_id}
            if bearer_token:
                headers["Authorization"] = f"Bearer {bearer_token}"
            try:
                async with self._client.stream(
                    method,
                    path,
                    json=json_body,
                    headers=headers,
                ) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > self.settings.max_response_bytes:
                            raise ResponseTooLargeError(
                                max_bytes=self.settings.max_response_bytes,
                                request_id=current_request_id,
                            )
                    if response.status_code in RETRYABLE_STATUS:
                        last_error = OdooApiError(
                            code="connector_unavailable",
                            message="Odoo connector returned a temporary gateway error.",
                            retryable=True,
                            status_code=response.status_code,
                            request_id=current_request_id,
                        )
                        await self.circuit.record_failure(permit)
                        permit_resolved = True
                        if safe and attempt + 1 < attempts:
                            await self._retry_delay(attempt)
                            continue
                        raise last_error
                    try:
                        data = self._decode_json(raw, response, current_request_id)
                    except OdooApiError:
                        await self.circuit.record_success(permit)
                        permit_resolved = True
                        raise
                    await self.circuit.record_success(permit)
                    permit_resolved = True
                    if response.status_code >= 400:
                        raise self._api_error(data, response.status_code, current_request_id)
                    return data
            except ResponseTooLargeError:
                await self.circuit.record_success(permit)
                permit_resolved = True
                raise
            except OdooApiError:
                raise
            except httpx.TransportError as exc:
                last_error = OdooApiError(
                    code="connector_transport_error",
                    message="Could not communicate with the Odoo connector.",
                    retryable=True,
                    status_code=503,
                    request_id=current_request_id,
                )
                await self.circuit.record_failure(permit)
                permit_resolved = True
                if not (safe and attempt + 1 < attempts):
                    raise last_error from exc
                await self._retry_delay(attempt)
            finally:
                if not permit_resolved:
                    await self.circuit.release(permit)
        raise last_error or OdooApiError(
            code="connector_unavailable",
            message="Odoo connector is unavailable.",
            retryable=True,
            status_code=503,
        )

    @staticmethod
    async def _retry_delay(attempt: int) -> None:
        await asyncio.sleep((0.1 * (2**attempt)) + random.uniform(0, 0.05))

    @staticmethod
    def _decode_json(
        raw: bytes | bytearray,
        response: httpx.Response,
        request_id: str,
    ) -> dict[str, Any]:
        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OdooApiError(
                code="invalid_connector_response",
                message="Odoo connector did not return valid JSON.",
                retryable=response.status_code >= 500,
                status_code=response.status_code,
                request_id=request_id,
            ) from exc
        if not isinstance(data, dict):
            raise OdooApiError(
                code="invalid_connector_response",
                message="Odoo connector returned an invalid envelope.",
                retryable=False,
                status_code=response.status_code,
                request_id=request_id,
            )
        return data

    @staticmethod
    def _api_error(
        data: dict[str, Any],
        status_code: int,
        request_id: str,
    ) -> OdooApiError:
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        return OdooApiError(
            code=str(error.get("code") or "connector_error"),
            message=str(error.get("message") or "Odoo connector rejected the request."),
            retryable=bool(error.get("retryable", False)),
            status_code=status_code,
            request_id=str(data.get("request_id") or request_id),
        )

    @staticmethod
    def _unwrap(envelope: dict[str, Any]) -> dict[str, Any]:
        if envelope.get("ok") is not True:
            raise OdooControlClient._api_error(
                envelope,
                500,
                str(envelope.get("request_id") or ""),
            )
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise OdooApiError(
                code="invalid_connector_response",
                message="Odoo connector response data must be an object.",
                retryable=False,
                request_id=str(envelope.get("request_id") or ""),
            )
        data.setdefault("request_id", envelope.get("request_id"))
        return data

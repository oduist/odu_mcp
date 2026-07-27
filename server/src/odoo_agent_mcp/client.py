from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from dataclasses import dataclass
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


@dataclass(slots=True)
class CircuitBreaker:
    failure_threshold: int
    reset_seconds: int
    failures: int = 0
    opened_at: float | None = None

    def allow_request(self) -> bool:
        if self.opened_at is None:
            return True
        if time.monotonic() - self.opened_at >= self.reset_seconds:
            return True
        return False

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_at = time.monotonic()


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
                "Authorization": f"Bearer {settings.connector_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "odoo-agent-mcp/1.0",
            },
        )
        self.circuit = CircuitBreaker(
            settings.circuit_failure_threshold,
            settings.circuit_reset_seconds,
        )

    async def __aenter__(self) -> "OdooControlClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> dict[str, Any]:
        return await self._request_json("GET", "/odoo_mcp/v1/health", safe=True)

    async def capabilities(self) -> dict[str, Any]:
        envelope = await self._request_json(
            "GET",
            "/odoo_mcp/v1/capabilities",
            safe=True,
        )
        return self._unwrap(envelope)

    async def execute(
        self,
        operation: str,
        params: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = request_id or str(uuid.uuid4())
        envelope = await self._request_json(
            "POST",
            "/odoo_mcp/v1/execute",
            json_body={"operation": operation, "params": params or {}},
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
        request_id: str | None = None,
        safe: bool,
    ) -> dict[str, Any]:
        if not self.circuit.allow_request():
            raise CircuitOpenError()
        attempts = self.settings.retry_attempts if safe else 1
        last_error: OdooApiError | None = None
        for attempt in range(attempts):
            current_request_id = request_id or str(uuid.uuid4())
            try:
                async with self._client.stream(
                    method,
                    path,
                    json=json_body,
                    headers={"X-Request-ID": current_request_id},
                ) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > self.settings.max_response_bytes:
                            raise ResponseTooLargeError(
                                max_bytes=self.settings.max_response_bytes,
                                request_id=current_request_id,
                            )
                    data = self._decode_json(raw, response, current_request_id)
                    if response.status_code in RETRYABLE_STATUS and safe:
                        raise OdooApiError(
                            code="connector_unavailable",
                            message="Odoo connector returned a temporary gateway error.",
                            retryable=True,
                            status_code=response.status_code,
                            request_id=current_request_id,
                        )
                    if response.status_code >= 400:
                        raise self._api_error(data, response.status_code, current_request_id)
                    self.circuit.record_success()
                    return data
            except ResponseTooLargeError:
                self.circuit.record_failure()
                raise
            except OdooApiError as exc:
                last_error = exc
                if not (safe and exc.retryable and attempt + 1 < attempts):
                    self.circuit.record_failure()
                    raise
            except (
                httpx.ConnectError,
                httpx.ReadError,
                httpx.RemoteProtocolError,
                httpx.TimeoutException,
            ) as exc:
                last_error = OdooApiError(
                    code="connector_transport_error",
                    message="Could not communicate with the Odoo connector.",
                    retryable=True,
                    status_code=503,
                    request_id=current_request_id,
                )
                if not (safe and attempt + 1 < attempts):
                    self.circuit.record_failure()
                    raise last_error from exc
            await asyncio.sleep((0.1 * (2**attempt)) + random.uniform(0, 0.05))
        self.circuit.record_failure()
        raise last_error or OdooApiError(
            code="connector_unavailable",
            message="Odoo connector is unavailable.",
            retryable=True,
            status_code=503,
        )

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

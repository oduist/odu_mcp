from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class OdooMcpError(Exception):
    """Base exception safe to translate to an MCP tool error."""


@dataclass(slots=True)
class OdooApiError(OdooMcpError):
    code: str
    message: str
    retryable: bool = False
    status_code: int | None = None
    request_id: str | None = None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "request_id": self.request_id,
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
            },
        }


class ConfigurationError(OdooMcpError):
    """Configuration is incomplete or unsafe."""


class ResponseTooLargeError(OdooApiError):
    def __init__(self, *, max_bytes: int, request_id: str | None = None):
        super().__init__(
            code="response_too_large",
            message=f"Odoo response exceeded the configured {max_bytes}-byte limit.",
            retryable=False,
            status_code=413,
            request_id=request_id,
        )


class CircuitOpenError(OdooApiError):
    def __init__(self):
        super().__init__(
            code="connector_unavailable",
            message="Odoo connector circuit is open after repeated transport failures.",
            retryable=True,
            status_code=503,
        )

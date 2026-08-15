from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from .errors import ConfigurationError

TRUTHY = {"1", "true", "yes", "on"}
VALID_TOOL_GROUPS = {
    "core",
    "write",
    "collaboration",
    "documents",
    "sales",
    "accounting",
    "inventory",
    "projects",
    "hr",
}


def _env(name: str, default: str = "") -> str:
    return os.getenv(f"ODOO_MCP_{name}", default).strip()


def _int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = _env(name, str(default))
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"ODOO_MCP_{name} must be an integer.") from exc
    if not minimum <= parsed <= maximum:
        raise ConfigurationError(f"ODOO_MCP_{name} must be between {minimum} and {maximum}.")
    return parsed


def _float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    value = _env(name, str(default))
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigurationError(f"ODOO_MCP_{name} must be numeric.") from exc
    if not minimum <= parsed <= maximum:
        raise ConfigurationError(f"ODOO_MCP_{name} must be between {minimum} and {maximum}.")
    return parsed


def _bool(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").lower() in TRUTHY


@dataclass(frozen=True, slots=True)
class Settings:
    odoo_url: str
    events_url: str = ""
    host: str = "127.0.0.1"
    port: int = 8000
    mcp_path: str = "/mcp"
    request_timeout_seconds: float = 30.0
    verify_tls: bool = True
    max_response_bytes: int = 10 * 1024 * 1024
    tool_groups: frozenset[str] = frozenset({"core"})
    log_level: str = "INFO"
    log_format: str = "json"
    identity_cache_seconds: int = 10
    user_lock_timeout_seconds: float = 5.0
    events_enabled: bool = True
    event_refresh_seconds: int = 240
    retry_attempts: int = 3
    circuit_failure_threshold: int = 5
    circuit_reset_seconds: int = 30

    @classmethod
    def from_env(cls) -> "Settings":
        groups = frozenset(
            value.strip() for value in _env("TOOL_GROUPS", "core").split(",") if value.strip()
        )
        groups |= {"core"}
        unknown_groups = groups - VALID_TOOL_GROUPS
        if unknown_groups:
            raise ConfigurationError(f"Unknown tool groups: {', '.join(sorted(unknown_groups))}.")
        settings = cls(
            odoo_url=_env("ODOO_URL"),
            events_url=_env("EVENTS_URL"),
            host=_env("HOST", "127.0.0.1"),
            port=_int("PORT", 8000, minimum=1, maximum=65535),
            mcp_path=_env("MCP_PATH", "/mcp"),
            request_timeout_seconds=_float(
                "REQUEST_TIMEOUT_SECONDS",
                30.0,
                minimum=1.0,
                maximum=300.0,
            ),
            verify_tls=_bool("VERIFY_TLS", True),
            max_response_bytes=_int(
                "MAX_RESPONSE_BYTES",
                10 * 1024 * 1024,
                minimum=1024,
                maximum=100 * 1024 * 1024,
            ),
            tool_groups=groups,
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            log_format=_env("LOG_FORMAT", "json").lower(),
            identity_cache_seconds=_int(
                "IDENTITY_CACHE_SECONDS",
                10,
                minimum=0,
                maximum=300,
            ),
            user_lock_timeout_seconds=_float(
                "USER_LOCK_TIMEOUT_SECONDS",
                5.0,
                minimum=0.1,
                maximum=60.0,
            ),
            events_enabled=_bool("EVENTS_ENABLED", True),
            event_refresh_seconds=_int(
                "EVENT_REFRESH_SECONDS",
                240,
                minimum=30,
                maximum=540,
            ),
            retry_attempts=_int("RETRY_ATTEMPTS", 3, minimum=1, maximum=5),
            circuit_failure_threshold=_int(
                "CIRCUIT_FAILURE_THRESHOLD",
                5,
                minimum=1,
                maximum=100,
            ),
            circuit_reset_seconds=_int(
                "CIRCUIT_RESET_SECONDS",
                30,
                minimum=1,
                maximum=600,
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        parsed = urlparse(self.odoo_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigurationError("ODOO_MCP_ODOO_URL must be an absolute HTTP(S) URL.")
        if self.events_url:
            events = urlparse(self.events_url)
            if events.scheme not in {"ws", "wss"} or not events.netloc:
                raise ConfigurationError("ODOO_MCP_EVENTS_URL must be an absolute WS(S) URL.")
        if not self.mcp_path.startswith("/"):
            raise ConfigurationError("ODOO_MCP_MCP_PATH must start with '/'.")

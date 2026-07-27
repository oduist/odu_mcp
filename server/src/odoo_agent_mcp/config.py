from __future__ import annotations

import hashlib
import ipaddress
import os
from dataclasses import dataclass
from urllib.parse import urlparse

from .errors import ConfigurationError

TRUTHY = {"1", "true", "yes", "on"}
VALID_TRANSPORTS = {"stdio", "streamable-http"}
VALID_AUTH_MODES = {"none", "static-token", "oauth"}
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


@dataclass(frozen=True, slots=True)
class Settings:
    odoo_url: str
    connector_token: str
    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000
    mcp_path: str = "/mcp"
    request_timeout_seconds: float = 30.0
    verify_tls: bool = True
    max_response_bytes: int = 10 * 1024 * 1024
    tool_groups: frozenset[str] = frozenset({"core"})
    log_level: str = "INFO"
    log_format: str = "json"
    auth_mode: str = "none"
    static_token_digests: frozenset[str] = frozenset()
    oauth_issuer_url: str = ""
    resource_server_url: str = ""
    required_scopes: tuple[str, ...] = ("odoo:read",)
    oauth_audience: str = ""
    jwks_url: str = ""
    schema_cache_seconds: int = 60
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
        raw_static_tokens = [
            value.strip() for value in _env("STATIC_TOKENS").split(",") if value.strip()
        ]
        digests = frozenset(
            value.removeprefix("sha256:")
            if value.startswith("sha256:")
            else hashlib.sha256(value.encode()).hexdigest()
            for value in raw_static_tokens
        )
        settings = cls(
            odoo_url=_env("ODOO_URL"),
            connector_token=_env("CONNECTOR_TOKEN"),
            transport=_env("TRANSPORT", "stdio"),
            host=_env("HOST", "127.0.0.1"),
            port=_int("PORT", 8000, minimum=1, maximum=65535),
            mcp_path=_env("MCP_PATH", "/mcp"),
            request_timeout_seconds=_float(
                "REQUEST_TIMEOUT_SECONDS",
                30.0,
                minimum=1.0,
                maximum=300.0,
            ),
            verify_tls=_env("VERIFY_TLS", "true").lower() in TRUTHY,
            max_response_bytes=_int(
                "MAX_RESPONSE_BYTES",
                10 * 1024 * 1024,
                minimum=1024,
                maximum=100 * 1024 * 1024,
            ),
            tool_groups=groups,
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            log_format=_env("LOG_FORMAT", "json").lower(),
            auth_mode=_env("AUTH_MODE", "none").lower(),
            static_token_digests=digests,
            oauth_issuer_url=_env("OAUTH_ISSUER_URL"),
            resource_server_url=_env("RESOURCE_SERVER_URL"),
            required_scopes=tuple(
                scope for scope in _env("REQUIRED_SCOPES", "odoo:read").split() if scope
            ),
            oauth_audience=_env("OAUTH_AUDIENCE"),
            jwks_url=_env("JWKS_URL"),
            schema_cache_seconds=_int("SCHEMA_CACHE_SECONDS", 60, minimum=0, maximum=3600),
            retry_attempts=_int("RETRY_ATTEMPTS", 3, minimum=1, maximum=5),
            circuit_failure_threshold=_int("CIRCUIT_FAILURE_THRESHOLD", 5, minimum=1, maximum=100),
            circuit_reset_seconds=_int("CIRCUIT_RESET_SECONDS", 30, minimum=1, maximum=600),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.transport not in VALID_TRANSPORTS:
            raise ConfigurationError(
                f"ODOO_MCP_TRANSPORT must be one of {sorted(VALID_TRANSPORTS)}."
            )
        if self.auth_mode not in VALID_AUTH_MODES:
            raise ConfigurationError(
                f"ODOO_MCP_AUTH_MODE must be one of {sorted(VALID_AUTH_MODES)}."
            )
        parsed = urlparse(self.odoo_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigurationError("ODOO_MCP_ODOO_URL must be an absolute HTTP(S) URL.")
        if not self.connector_token:
            raise ConfigurationError("ODOO_MCP_CONNECTOR_TOKEN is required.")
        if not self.mcp_path.startswith("/"):
            raise ConfigurationError("ODOO_MCP_MCP_PATH must start with '/'.")
        if self.transport == "streamable-http":
            if self.auth_mode == "none" and not self._is_loopback_host():
                raise ConfigurationError(
                    "Unauthenticated HTTP mode is allowed only on a loopback host."
                )
            if self.auth_mode == "static-token" and not self.static_token_digests:
                raise ConfigurationError(
                    "ODOO_MCP_STATIC_TOKENS is required for static-token auth."
                )
            if self.auth_mode == "oauth":
                if not self.oauth_issuer_url or not self.resource_server_url:
                    raise ConfigurationError(
                        "OAuth mode requires OAUTH_ISSUER_URL and RESOURCE_SERVER_URL."
                    )
                if not self.oauth_audience:
                    raise ConfigurationError(
                        "OAuth mode requires OAUTH_AUDIENCE for token audience binding."
                    )
        if not self.verify_tls and urlparse(self.odoo_url).scheme == "https":
            # Deliberately valid for local development, but visible in logs.
            pass

    def _is_loopback_host(self) -> bool:
        if self.host.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False

from __future__ import annotations

import hashlib

import pytest

from odoo_agent_mcp.config import Settings
from odoo_agent_mcp.errors import ConfigurationError


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ODOO_URL",
        "CONNECTOR_TOKEN",
        "TRANSPORT",
        "HOST",
        "PORT",
        "MCP_PATH",
        "TOOL_GROUPS",
        "AUTH_MODE",
        "STATIC_TOKENS",
        "OAUTH_ISSUER_URL",
        "RESOURCE_SERVER_URL",
        "OAUTH_AUDIENCE",
    ):
        monkeypatch.delenv(f"ODOO_MCP_{name}", raising=False)
    monkeypatch.setenv("ODOO_MCP_ODOO_URL", "https://odoo.example.test")
    monkeypatch.setenv("ODOO_MCP_CONNECTOR_TOKEN", "connector-secret")


def test_from_env_normalizes_groups_and_hashes_static_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("ODOO_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("ODOO_MCP_AUTH_MODE", "static-token")
    monkeypatch.setenv("ODOO_MCP_STATIC_TOKENS", "first,sha256:" + ("a" * 64))
    monkeypatch.setenv("ODOO_MCP_TOOL_GROUPS", "write,sales")

    settings = Settings.from_env()

    assert settings.tool_groups == frozenset({"core", "write", "sales"})
    assert hashlib.sha256(b"first").hexdigest() in settings.static_token_digests
    assert "a" * 64 in settings.static_token_digests


def test_rejects_unauthenticated_public_http() -> None:
    settings = Settings(
        odoo_url="https://odoo.example.test",
        connector_token="connector-secret",
        transport="streamable-http",
        host="0.0.0.0",
    )

    with pytest.raises(ConfigurationError, match="loopback"):
        settings.validate()


def test_rejects_unknown_tool_group(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("ODOO_MCP_TOOL_GROUPS", "core,arbitrary_orm")

    with pytest.raises(ConfigurationError, match="Unknown tool groups"):
        Settings.from_env()

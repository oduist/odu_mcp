from __future__ import annotations

import pytest

from odoo_agent_mcp.config import Settings
from odoo_agent_mcp.errors import ConfigurationError


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ODOO_URL",
        "EVENTS_URL",
        "PUBLIC_URL",
        "HOST",
        "PORT",
        "MCP_PATH",
        "TOOL_GROUPS",
        "EVENTS_ENABLED",
        "EVENT_REFRESH_SECONDS",
    ):
        monkeypatch.delenv(f"ODOO_MCP_{name}", raising=False)
    monkeypatch.setenv("ODOO_MCP_ODOO_URL", "https://odoo.example.test")


def test_from_env_builds_http_only_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("ODOO_MCP_PUBLIC_URL", "https://mcp.example.test")
    monkeypatch.setenv("ODOO_MCP_EVENTS_URL", "wss://events.example.test/odoo_mcp/v1/events")
    monkeypatch.setenv("ODOO_MCP_TOOL_GROUPS", "write,sales")
    monkeypatch.setenv("ODOO_MCP_EVENTS_ENABLED", "false")

    settings = Settings.from_env()

    assert settings.tool_groups == frozenset({"core", "write", "sales"})
    assert settings.public_url == "https://mcp.example.test"
    assert settings.events_url == "wss://events.example.test/odoo_mcp/v1/events"
    assert settings.events_enabled is False
    assert not hasattr(settings, "transport")


@pytest.mark.parametrize("field", ["odoo_url", "public_url"])
def test_rejects_non_http_urls(field: str) -> None:
    values = {"odoo_url": "https://odoo.example.test", field: "file:///tmp/odoo"}
    settings = Settings(**values)

    with pytest.raises(ConfigurationError, match="absolute HTTP"):
        settings.validate()


def test_rejects_non_websocket_events_url() -> None:
    settings = Settings(
        odoo_url="https://odoo.example.test",
        events_url="https://odoo.example.test/odoo_mcp/v1/events",
    )

    with pytest.raises(ConfigurationError, match="absolute WS"):
        settings.validate()


def test_rejects_unknown_tool_group(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("ODOO_MCP_TOOL_GROUPS", "core,arbitrary_orm")

    with pytest.raises(ConfigurationError, match="Unknown tool groups"):
        Settings.from_env()

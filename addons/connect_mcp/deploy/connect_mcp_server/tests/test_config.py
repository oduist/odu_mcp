from __future__ import annotations

import pytest

from connect_mcp_server.config import Settings
from connect_mcp_server.errors import ConfigurationError


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ODOO_URL",
        "EVENTS_URL",
        "HOST",
        "PORT",
        "MCP_PATH",
        "TOOL_GROUPS",
        "EVENTS_ENABLED",
        "EVENT_REFRESH_SECONDS",
        "QUEUE_SCOPE",
        "QUEUE_TIMEOUT_SECONDS",
        "QUEUE_MAX_SIZE",
        "USER_LOCK_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(f"CONNECT_MCP_{name}", raising=False)
    monkeypatch.setenv("CONNECT_MCP_ODOO_URL", "https://odoo.example.test")


def test_from_env_builds_http_only_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("CONNECT_MCP_EVENTS_URL", "wss://events.example.test/connect_mcp/v1/events")
    monkeypatch.setenv("CONNECT_MCP_TOOL_GROUPS", "write,sales")
    monkeypatch.setenv("CONNECT_MCP_EVENTS_ENABLED", "false")
    monkeypatch.setenv("CONNECT_MCP_QUEUE_SCOPE", "global")
    monkeypatch.setenv("CONNECT_MCP_QUEUE_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("CONNECT_MCP_QUEUE_MAX_SIZE", "25")

    settings = Settings.from_env()

    assert settings.tool_groups == frozenset({"core", "write", "sales"})
    assert settings.events_url == "wss://events.example.test/connect_mcp/v1/events"
    assert settings.events_enabled is False
    assert settings.queue_scope == "global"
    assert settings.queue_timeout_seconds == 120
    assert settings.queue_max_size == 25
    assert not hasattr(settings, "transport")


def test_rejects_non_http_odoo_url() -> None:
    settings = Settings(odoo_url="file:///tmp/odoo")

    with pytest.raises(ConfigurationError, match="absolute HTTP"):
        settings.validate()


def test_rejects_non_websocket_events_url() -> None:
    settings = Settings(
        odoo_url="https://odoo.example.test",
        events_url="https://odoo.example.test/connect_mcp/v1/events",
    )

    with pytest.raises(ConfigurationError, match="absolute WS"):
        settings.validate()


def test_rejects_unknown_tool_group(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("CONNECT_MCP_TOOL_GROUPS", "core,arbitrary_orm")

    with pytest.raises(ConfigurationError, match="Unknown tool groups"):
        Settings.from_env()


def test_rejects_unknown_queue_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("CONNECT_MCP_QUEUE_SCOPE", "tenant")

    with pytest.raises(ConfigurationError, match="QUEUE_SCOPE"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("PORT", "invalid", "must be an integer"),
        ("PORT", "70000", "must be between"),
        ("REQUEST_TIMEOUT_SECONDS", "invalid", "must be numeric"),
        ("REQUEST_TIMEOUT_SECONDS", "0", "must be between"),
    ],
)
def test_rejects_invalid_numeric_environment_values(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    message: str,
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv(f"CONNECT_MCP_{name}", value)

    with pytest.raises(ConfigurationError, match=message):
        Settings.from_env()


def test_rejects_relative_mcp_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("CONNECT_MCP_MCP_PATH", "mcp")

    with pytest.raises(ConfigurationError, match="must start"):
        Settings.from_env()

from __future__ import annotations

import logging

import pytest

import connect_mcp_server.__main__ as main_module
from connect_mcp_server.config import Settings
from connect_mcp_server.errors import ConfigurationError


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "odoo_url": "https://odoo.example.test",
        "host": "0.0.0.0",
        "port": 9000,
        "mcp_path": "/connector",
        "events_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def test_configure_logging_supports_text_and_tls_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: calls.append(kwargs))

    with caplog.at_level(logging.WARNING):
        main_module._configure_logging(
            _settings(log_level="WARNING", log_format="text", verify_tls=False)
        )

    assert calls[0]["level"] == logging.WARNING
    assert calls[0]["format"] == "%(asctime)s %(levelname)s %(name)s %(message)s"
    assert "TLS verification" in caplog.text


def test_main_runs_http_server_with_configured_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(log_level="DEBUG")
    run_calls: list[dict[str, object]] = []

    class FakeServer:
        def run(self, **kwargs: object) -> None:
            run_calls.append(kwargs)

    monkeypatch.setattr(main_module.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(main_module, "_configure_logging", lambda value: None)
    monkeypatch.setattr(main_module, "create_server", lambda value: FakeServer())

    main_module.main()

    assert run_calls == [
        {
            "transport": "http",
            "host": "0.0.0.0",
            "port": 9000,
            "path": "/connector",
            "stateless_http": True,
            "json_response": True,
            "log_level": "DEBUG",
        }
    ]


def test_main_exits_with_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def invalid_settings() -> Settings:
        raise ConfigurationError("missing URL")

    monkeypatch.setattr(main_module.Settings, "from_env", invalid_settings)

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    assert exc_info.value.code == 2
    assert "Configuration error: missing URL" in capsys.readouterr().err

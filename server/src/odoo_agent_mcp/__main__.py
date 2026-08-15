from __future__ import annotations

import logging
import sys

from .config import Settings
from .errors import ConfigurationError
from .server import create_server


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        stream=sys.stderr,
        format=(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
            if settings.log_format != "json"
            else '{"time":"%(asctime)s","level":"%(levelname)s",'
            '"logger":"%(name)s","message":"%(message)s"}'
        ),
    )
    if not settings.verify_tls:
        logging.getLogger(__name__).warning("TLS verification for the Odoo connector is disabled")


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    _configure_logging(settings)
    mcp = create_server(settings)
    mcp.run(
        transport="http",
        host=settings.host,
        port=settings.port,
        path=settings.mcp_path,
        stateless_http=True,
        json_response=True,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    main()

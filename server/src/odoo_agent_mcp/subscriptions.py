from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
import mcp_types
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import get_access_token
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler
from mcp.shared.subscriptions import ResourceUpdated
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

from .config import Settings

LOGGER = logging.getLogger(__name__)


class SubscriptionPublisher:
    """Isolate FastMCP's beta subscription API and events by Odoo subject."""

    def __init__(self) -> None:
        self._buses: dict[str, InMemorySubscriptionBus] = {}
        self._handlers: dict[str, ListenHandler] = {}

    def install(self, mcp: FastMCP) -> None:
        async def listen(ctx: Any, params: Any) -> Any:
            access_token = get_access_token()
            if access_token is None or not access_token.subject:
                raise RuntimeError("An authenticated Odoo subject is required for subscriptions.")
            return await self._handler(access_token.subject)(ctx, params)

        mcp._mcp_server.add_request_handler(  # noqa: SLF001 - isolated beta API adapter
            "subscriptions/listen",
            mcp_types.SubscriptionsListenRequestParams,
            listen,
        )

    def subscribe(self, subject: str, listener: Any) -> Any:
        """Expose a narrow test seam without sharing subjects on one bus."""
        return self._bus(subject).subscribe(listener)

    async def publish(self, subject: str, uri: str) -> None:
        await self._bus(subject).publish(ResourceUpdated(uri=uri))

    def _bus(self, subject: str) -> InMemorySubscriptionBus:
        return self._buses.setdefault(subject, InMemorySubscriptionBus())

    def _handler(self, subject: str) -> ListenHandler:
        handler = self._handlers.get(subject)
        if handler is None:
            handler = ListenHandler(self._bus(subject))
            self._handlers[subject] = handler
        return handler

    def close(self) -> None:
        for handler in self._handlers.values():
            handler.close()


@dataclass(frozen=True, slots=True)
class EventTicket:
    token: str
    expires_at: float


class OdooEventBridge:
    def __init__(
        self,
        settings: Settings,
        publisher: SubscriptionPublisher,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.publisher = publisher
        self._client = httpx.AsyncClient(
            base_url=settings.odoo_url.rstrip("/"),
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            verify=settings.verify_tls,
            follow_redirects=False,
            transport=transport,
        )
        self._watchers: dict[str, asyncio.Task[None]] = {}
        self._versions: dict[tuple[str, str], int] = {}
        self._closed = False

    async def ensure_watcher(self, access_token: AccessToken) -> None:
        if self._closed or not self.settings.events_enabled or not access_token.subject:
            return
        current = self._watchers.get(access_token.subject)
        if current and not current.done():
            return
        task = asyncio.create_task(
            self._watch(access_token.subject, access_token.token),
            name=f"odoo-mcp-events:{access_token.subject}",
        )
        self._watchers[access_token.subject] = task
        task.add_done_callback(
            lambda completed, subject=access_token.subject: self._watcher_done(subject, completed)
        )

    def _watcher_done(self, subject: str, task: asyncio.Task[None]) -> None:
        if self._watchers.get(subject) is task:
            self._watchers.pop(subject, None)
        if not task.cancelled() and (error := task.exception()) is not None:
            LOGGER.warning("Odoo event watcher stopped for %s: %s", subject, error)

    async def _watch(self, subject: str, bearer_token: str) -> None:
        ticket = await self._mint_ticket(bearer_token)
        bearer_token = ""
        websocket_url = self._websocket_url()
        origin = self._origin()
        ssl_context = self._ssl_context(websocket_url)
        while not self._closed:
            remaining = ticket.expires_at - time.monotonic()
            if remaining <= 0:
                return
            try:
                async with connect(
                    websocket_url,
                    origin=origin,
                    additional_headers={"Authorization": f"Bearer {ticket.token}"},
                    user_agent_header=None,
                    proxy=None,
                    ssl=ssl_context,
                    open_timeout=min(10.0, self.settings.request_timeout_seconds),
                    max_size=1024 * 1024,
                ) as websocket:
                    await websocket.send(
                        json.dumps(
                            {
                                "event_name": "subscribe",
                                "data": {"channels": [], "last": 0},
                            }
                        )
                    )
                    try:
                        async with asyncio.timeout(
                            min(self.settings.event_refresh_seconds, remaining)
                        ):
                            while not self._closed:
                                message = await websocket.recv()
                                await self._handle_message(subject, message)
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                raise
            except InvalidStatus as exc:
                if exc.response.status_code in {401, 403}:
                    return
                LOGGER.info("Odoo event WebSocket reconnect for %s: %s", subject, exc)
                await asyncio.sleep(1)
            except WebSocketException as exc:
                LOGGER.info("Odoo event WebSocket reconnect for %s: %s", subject, exc)
                await asyncio.sleep(1)

    async def _mint_ticket(self, bearer_token: str) -> EventTicket:
        response = await self._client.post(
            "/odoo_mcp/v1/events/ticket",
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "Accept": "application/json",
                "User-Agent": "odoo-agent-mcp/2.0",
            },
        )
        response.raise_for_status()
        envelope: Any = response.json()
        data = envelope.get("data") if isinstance(envelope, dict) else None
        ticket = data.get("ticket") if isinstance(data, dict) else None
        expires_in = data.get("expires_in") if isinstance(data, dict) else None
        if (
            not isinstance(ticket, str)
            or not ticket
            or not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or expires_in <= 0
        ):
            raise ValueError("Odoo returned an invalid MCP event ticket")
        return EventTicket(token=ticket, expires_at=time.monotonic() + expires_in)

    async def _handle_message(self, subject: str, message: str | bytes) -> None:
        try:
            notifications = json.loads(message)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(notifications, list):
            return
        for notification in notifications:
            event = notification.get("message") if isinstance(notification, dict) else None
            if not isinstance(event, dict) or event.get("type") != "odoo_mcp_resource_updated":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "resource.updated":
                continue
            uri = payload.get("uri")
            version = payload.get("version")
            if not isinstance(uri, str) or not uri.startswith("odoo://"):
                continue
            if not isinstance(version, int):
                continue
            key = (subject, uri)
            if version <= self._versions.get(key, 0):
                continue
            self._versions[key] = version
            await self.publisher.publish(subject, uri)

    def _websocket_url(self) -> str:
        if self.settings.events_url:
            return self.settings.events_url
        parsed = urlparse(self.settings.odoo_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunparse((scheme, parsed.netloc, "/odoo_mcp/v1/events", "", "", ""))

    def _origin(self) -> str:
        parsed = urlparse(self._websocket_url())
        scheme = "https" if parsed.scheme == "wss" else "http"
        return urlunparse((scheme, parsed.netloc, "", "", "", ""))

    def _ssl_context(self, websocket_url: str) -> ssl.SSLContext | None:
        if not websocket_url.startswith("wss://"):
            return None
        context = ssl.create_default_context()
        if not self.settings.verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE  # noqa: S501 - explicit operator setting
        return context

    async def aclose(self) -> None:
        self._closed = True
        tasks = list(self._watchers.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._watchers.clear()
        self.publisher.close()
        await self._client.aclose()

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncContextManager
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

    def install(
        self,
        mcp: FastMCP,
        *,
        watch_subscription: Callable[[AccessToken], AsyncContextManager[None]] | None = None,
    ) -> None:
        async def listen(ctx: Any, params: Any) -> Any:
            access_token = get_access_token()
            if access_token is None or not access_token.subject:
                raise RuntimeError("An authenticated Odoo subject is required for subscriptions.")
            if watch_subscription is None:
                return await self._handler(access_token.subject)(ctx, params)
            async with watch_subscription(access_token):
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

    def remove_subject(self, subject: str) -> None:
        handler = self._handlers.pop(subject, None)
        if handler is not None:
            handler.close()
        self._buses.pop(subject, None)

    def close(self) -> None:
        for handler in self._handlers.values():
            handler.close()
        self._handlers.clear()
        self._buses.clear()


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
        self._references: dict[str, int] = {}
        self._versions: dict[tuple[str, str], int] = {}
        self._closed = False

    @asynccontextmanager
    async def watch_subscription(self, access_token: AccessToken) -> AsyncIterator[None]:
        subject = access_token.subject
        if self._closed or not self.settings.events_enabled or not subject:
            yield
            return
        self._references[subject] = self._references.get(subject, 0) + 1
        await self.ensure_watcher(access_token)
        try:
            yield
        finally:
            remaining = self._references.get(subject, 1) - 1
            if remaining > 0:
                self._references[subject] = remaining
            else:
                self._references.pop(subject, None)
                task = self._watchers.pop(subject, None)
                for key in [key for key in self._versions if key[0] == subject]:
                    self._versions.pop(key, None)
                self.publisher.remove_subject(subject)
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def ensure_watcher(self, access_token: AccessToken) -> None:
        if self._closed or not self.settings.events_enabled or not access_token.subject:
            return
        current = self._watchers.get(access_token.subject)
        if current and not current.done():
            return
        task = asyncio.create_task(
            self._watch(access_token.subject, access_token.token),
            name=f"connect-mcp-events:{access_token.subject}",
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
        websocket_url = self._websocket_url()
        origin = self._origin()
        ssl_context = self._ssl_context(websocket_url)
        try:
            ticket = await self._mint_ticket(bearer_token)
        except asyncio.CancelledError:
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {401, 403}:
                LOGGER.info("Odoo event ticket failed for %s: %s", subject, exc)
            return
        except (httpx.HTTPError, ValueError) as exc:
            LOGGER.info("Odoo event ticket failed for %s: %s", subject, exc)
            return
        finally:
            del bearer_token

        while not self._closed and (remaining := ticket.expires_at - time.monotonic()) > 0:
            try:
                connection_lifetime = min(self.settings.event_refresh_seconds, remaining)
                async with asyncio.timeout(connection_lifetime):
                    async with connect(
                        websocket_url,
                        origin=origin,
                        additional_headers={"Authorization": f"Bearer {ticket.token}"},
                        user_agent_header=None,
                        proxy=None,
                        ssl=ssl_context,
                        open_timeout=min(
                            10.0,
                            self.settings.request_timeout_seconds,
                            connection_lifetime,
                        ),
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
                await asyncio.sleep(min(1.0, max(0.0, ticket.expires_at - time.monotonic())))
            except (OSError, WebSocketException) as exc:
                LOGGER.info("Odoo event WebSocket reconnect for %s: %s", subject, exc)
                await asyncio.sleep(min(1.0, max(0.0, ticket.expires_at - time.monotonic())))

    async def _mint_ticket(self, bearer_token: str) -> EventTicket:
        response = await self._client.post(
            "/connect_mcp/v1/events/ticket",
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "Accept": "application/json",
                "User-Agent": "connect-mcp-server/2.0",
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
            if not isinstance(event, dict) or event.get("type") != "connect_mcp_resource_updated":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "resource.updated":
                continue
            uri = payload.get("uri")
            version = payload.get("version")
            if not isinstance(uri, str) or not uri.startswith("odoo://"):
                continue
            if not isinstance(version, int) or isinstance(version, bool):
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
        return urlunparse((scheme, parsed.netloc, "/connect_mcp/v1/events", "", "", ""))

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
        self._references.clear()
        self._versions.clear()
        self.publisher.close()
        await self._client.aclose()

from __future__ import annotations

import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from fastmcp.server.auth import AccessToken, TokenVerifier

from .config import Settings


@dataclass(slots=True)
class CachedIdentity:
    access_token: AccessToken
    expires_at: float


class OdooApiKeyVerifier(TokenVerifier):
    """Validate MCP-scoped Odoo API keys without persisting their raw value."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        on_verified: Callable[[AccessToken], Awaitable[None]] | None = None,
    ) -> None:
        base_url = settings.public_url or f"http://{settings.host}:{settings.port}"
        super().__init__(base_url=base_url, required_scopes=["mcp"])
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.odoo_url.rstrip("/"),
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            verify=settings.verify_tls,
            follow_redirects=False,
            transport=transport,
        )
        self._cache: dict[str, CachedIdentity] = {}
        self._on_verified = on_verified

    async def aclose(self) -> None:
        await self._client.aclose()

    async def verify_token(self, token: str) -> AccessToken | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        cached = self._cache.get(digest)
        if cached and cached.expires_at > time.monotonic():
            if self._on_verified:
                await self._on_verified(cached.access_token)
            return cached.access_token
        self._cache.pop(digest, None)

        response = await self._client.get(
            "/odoo_mcp/v1/identity",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "odoo-agent-mcp/2.0",
            },
        )
        if response.status_code in {401, 403}:
            return None
        response.raise_for_status()
        payload: Any = response.json()
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            return None
        identity = payload.get("data")
        if not isinstance(identity, dict):
            return None
        database = identity.get("database")
        user_id = identity.get("user_id")
        profile = identity.get("profile")
        if (
            not isinstance(database, str)
            or not isinstance(user_id, int)
            or not isinstance(profile, str)
        ):
            return None

        subject = f"odoo:{database}:{user_id}"
        access_token = AccessToken(
            token=token,
            client_id=subject,
            subject=subject,
            scopes=["mcp"],
            claims={
                "database": database,
                "user_id": user_id,
                "login": identity.get("login"),
                "profile": profile,
            },
        )
        if self.settings.identity_cache_seconds:
            self._cache[digest] = CachedIdentity(
                access_token=access_token,
                expires_at=time.monotonic() + self.settings.identity_cache_seconds,
            )
        if self._on_verified:
            await self._on_verified(access_token)
        return access_token

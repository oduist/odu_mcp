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
    client_id: str
    subject: str
    scopes: tuple[str, ...]
    claims: dict[str, Any]
    expires_at: float

    def to_access_token(self, token: str) -> AccessToken:
        return AccessToken(
            token=token,
            client_id=self.client_id,
            subject=self.subject,
            scopes=list(self.scopes),
            claims=dict(self.claims),
        )


class OdooApiKeyVerifier(TokenVerifier):
    """Validate MCP-scoped Odoo API keys without persisting their raw value."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        on_verified: Callable[[AccessToken], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(required_scopes=["mcp"])
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
        now = time.monotonic()
        for expired_digest in [
            digest for digest, identity in self._cache.items() if identity.expires_at <= now
        ]:
            self._cache.pop(expired_digest, None)

        digest = hashlib.sha256(token.encode()).hexdigest()
        cached = self._cache.get(digest)
        if cached:
            access_token = cached.to_access_token(token)
            if self._on_verified:
                await self._on_verified(access_token)
            return access_token

        response = await self._client.get(
            "/connect_mcp/v1/identity",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "connect-mcp-server/2.0",
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
                client_id=subject,
                subject=subject,
                scopes=("mcp",),
                claims=dict(access_token.claims),
                expires_at=time.monotonic() + self.settings.identity_cache_seconds,
            )
        if self._on_verified:
            await self._on_verified(access_token)
        return access_token

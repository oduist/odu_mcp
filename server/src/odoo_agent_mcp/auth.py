from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import httpx
import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

from .config import Settings


class StaticTokenVerifier(TokenVerifier):
    def __init__(self, settings: Settings) -> None:
        self._digests = settings.static_token_digests
        self._scopes = list(settings.required_scopes)
        self._resource = settings.resource_server_url or None

    async def verify_token(self, token: str) -> AccessToken | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        if not any(hmac.compare_digest(digest, expected) for expected in self._digests):
            return None
        return AccessToken(
            token=token,
            client_id=f"static:{digest[:12]}",
            scopes=self._scopes,
            resource=self._resource,
            subject=f"static:{digest[:12]}",
        )


class JwtTokenVerifier(TokenVerifier):
    """OIDC/JWT verifier with cached discovery and JWKS."""

    ALGORITHMS = ["RS256", "PS256", "ES256"]

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            timeout=10.0,
            verify=True,
            follow_redirects=False,
            transport=transport,
        )
        self._jwks: dict[str, Any] | None = None
        self._jwks_expires_at = 0.0
        self._jwks_url = settings.jwks_url

    async def aclose(self) -> None:
        await self._client.aclose()

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            key_id = header.get("kid")
            if algorithm not in self.ALGORITHMS or not isinstance(key_id, str):
                return None
            jwks = await self._get_jwks()
            key = self._select_key(jwks, key_id)
            if key is None:
                self._jwks_expires_at = 0
                jwks = await self._get_jwks()
                key = self._select_key(jwks, key_id)
            if key is None:
                return None
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[algorithm],
                audience=self.settings.oauth_audience,
                issuer=self.settings.oauth_issuer_url.rstrip("/"),
                options={
                    "require": ["exp", "iat", "iss", "aud", "sub"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_iss": True,
                    "verify_aud": True,
                },
                leeway=30,
            )
        except (jwt.PyJWTError, httpx.HTTPError, ValueError, TypeError):
            return None
        scopes = self._scopes(claims)
        if not set(self.settings.required_scopes).issubset(scopes):
            return None
        audience = claims.get("aud")
        resource = audience if isinstance(audience, str) else self.settings.oauth_audience
        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id") or claims.get("azp") or claims["sub"]),
            scopes=sorted(scopes),
            expires_at=int(claims["exp"]),
            resource=resource,
            subject=str(claims["sub"]),
            claims=claims,
        )

    async def _get_jwks(self) -> dict[str, Any]:
        if self._jwks and time.monotonic() < self._jwks_expires_at:
            return self._jwks
        if not self._jwks_url:
            discovery_url = (
                f"{self.settings.oauth_issuer_url.rstrip('/')}/.well-known/openid-configuration"
            )
            response = await self._client.get(discovery_url)
            response.raise_for_status()
            discovery = response.json()
            jwks_uri = discovery.get("jwks_uri")
            if not isinstance(jwks_uri, str) or not jwks_uri.startswith("https://"):
                raise ValueError("OIDC discovery returned an unsafe JWKS URI.")
            self._jwks_url = jwks_uri
        response = await self._client.get(self._jwks_url)
        response.raise_for_status()
        jwks = response.json()
        if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
            raise ValueError("Invalid JWKS document.")
        self._jwks = jwks
        self._jwks_expires_at = time.monotonic() + 300
        return jwks

    @staticmethod
    def _select_key(jwks: dict[str, Any], key_id: str) -> Any | None:
        for item in jwks.get("keys", []):
            if item.get("kid") == key_id and item.get("use", "sig") == "sig":
                try:
                    return jwt.PyJWK.from_dict(item).key
                except jwt.PyJWTError:
                    return None
        return None

    @staticmethod
    def _scopes(claims: dict[str, Any]) -> set[str]:
        raw = claims.get("scope", claims.get("scp", []))
        if isinstance(raw, str):
            return set(raw.split())
        if isinstance(raw, list):
            return {str(item) for item in raw}
        return set()

"""Authentication helpers: Clerk JWKS-based RS256 JWT verification."""
from cachetools import TTLCache
from jose import JWTError, jwt

import httpx

from app.config import settings

# JWKS cache: one entry, refreshed every hour
_jwks_cache: TTLCache = TTLCache(maxsize=1, ttl=3600)


async def _fetch_jwks() -> dict:
    """Fetch Clerk's JWKS, cached for 1 hour."""
    if "keys" in _jwks_cache:
        return _jwks_cache["keys"]
    async with httpx.AsyncClient() as client:
        r = await client.get(settings.clerk_jwks_url, timeout=5)
        r.raise_for_status()
    keys = r.json()
    _jwks_cache["keys"] = keys
    return keys


async def verify_clerk_jwt(token: str) -> str:
    """
    Verify a Clerk-issued RS256 JWT via JWKS.

    Returns the Clerk user ID (``sub`` claim, format: ``user_xxxx``).
    Raises ``jose.JWTError`` on any verification failure.
    """
    jwks = await _fetch_jwks()
    payload = jwt.decode(
        token,
        jwks,
        algorithms=["RS256"],
        audience=settings.clerk_audience or None,
    )
    sub = payload.get("sub")
    if not sub:
        raise JWTError("Missing sub claim")
    return sub

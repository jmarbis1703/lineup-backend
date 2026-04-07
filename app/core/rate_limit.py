"""
Shared SlowAPI rate-limiter instance.

Global default : 200 requests / minute per IP
Per-user key   : _get_user_id_key() — extracts JWT sub claim from the
                 Authorization header without performing full verification.
                 Full verification still happens via get_current_user().
                 Using the unverified sub here is safe: slowapi only needs a
                 stable string key; the real auth check happens independently.

Storage backend: Redis (settings.redis_url) — limits survive restarts and
                 work correctly across multiple Uvicorn workers.
"""
from __future__ import annotations

from fastapi import Request
from jose import jwt as jose_jwt
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings


def _get_user_id_key(request: Request) -> str:
    """Return 'user:<sub>' when a Bearer JWT is present, else fall back to IP."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            claims = jose_jwt.get_unverified_claims(auth[7:])
            sub = claims.get("sub")
            if sub:
                return f"user:{sub}"
        except Exception:
            pass
    return get_remote_address(request)


limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200/minute"],
    storage_uri=settings.redis_url,
    swallow_errors=True,  # Redis outage must not abort requests (returns 500 otherwise)
)

"""
Tests for Clerk JWT verification and the Clerk webhook endpoint.

No real Clerk account needed — all JWTs are signed with a local RSA test key
and _fetch_jwks is patched (via conftest.patch_clerk_jwks) to return the
matching public JWKS.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient, ASGITransport
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession
from svix.webhooks import WebhookVerificationError

from app.core.auth import verify_clerk_jwt
from app.dependencies import get_db
from app.main import app
from app.models.portfolio import Portfolio
from app.models.user import User


# ---------------------------------------------------------------------------
# Helper: mint a test RS256 JWT
# ---------------------------------------------------------------------------


def _mint_token(
    rsa_test_keys: dict,
    *,
    sub: str = "user_test123",
    exp_delta: timedelta = timedelta(hours=1),
    algorithm: str = "RS256",
    kid: str = "test-key-1",
) -> str:
    exp = datetime.now(timezone.utc) + exp_delta
    return jwt.encode(
        {"sub": sub, "exp": exp},
        rsa_test_keys["private_pem"],
        algorithm=algorithm,
        headers={"kid": kid},
    )


# ---------------------------------------------------------------------------
# 1. Valid token
# ---------------------------------------------------------------------------


async def test_valid_token(rsa_test_keys: dict) -> None:
    """A properly signed RS256 JWT returns the sub claim."""
    token = _mint_token(rsa_test_keys, sub="user_test123")
    result = await verify_clerk_jwt(token)
    assert result == "user_test123"


# ---------------------------------------------------------------------------
# 2. Expired token
# ---------------------------------------------------------------------------


async def test_expired_token(rsa_test_keys: dict) -> None:
    """An RS256 JWT with exp in the past raises JWTError."""
    token = _mint_token(rsa_test_keys, exp_delta=timedelta(seconds=-1))
    with pytest.raises(JWTError):
        await verify_clerk_jwt(token)


# ---------------------------------------------------------------------------
# 3. Wrong algorithm (HS256)
# ---------------------------------------------------------------------------


async def test_wrong_algorithm_rejected(rsa_test_keys: dict) -> None:
    """An HS256 token is rejected because verify_clerk_jwt only allows RS256."""
    exp = datetime.now(timezone.utc) + timedelta(hours=1)
    hs256_token = jwt.encode(
        {"sub": "user_test123", "exp": exp},
        "some-secret",
        algorithm="HS256",
    )
    with pytest.raises(JWTError):
        await verify_clerk_jwt(hs256_token)


# ---------------------------------------------------------------------------
# 4. Tampered payload (bad signature)
# ---------------------------------------------------------------------------


async def test_tampered_token_rejected(rsa_test_keys: dict) -> None:
    """A token with a corrupted signature raises JWTError."""
    token = _mint_token(rsa_test_keys)
    # Flip a character in the signature segment
    header, payload, sig = token.split(".")
    bad_token = f"{header}.{payload}.{'A' + sig[1:]}"
    with pytest.raises(JWTError):
        await verify_clerk_jwt(bad_token)


# ---------------------------------------------------------------------------
# 5 & 6. Webhook: valid and invalid Svix signature
# ---------------------------------------------------------------------------


async def test_webhook_valid_signature() -> None:
    """POST /api/webhooks/clerk with a valid event returns 204."""
    event = {
        "type": "user.created",
        "data": {
            "id": "user_webhook_test",
            "email_addresses": [{"email_address": "hook@example.com"}],
            "username": "hookuser",
        },
    }
    with (
        patch(
            "app.api.webhooks.Webhook.verify",
            return_value=event,
        ),
        patch("app.api.webhooks._upsert_user", new=AsyncMock()) as mock_upsert,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/webhooks/clerk",
                content=b'{"type":"user.created"}',
                headers={
                    "svix-id": "msg_test",
                    "svix-timestamp": "1234567890",
                    "svix-signature": "v1,test",
                    "content-type": "application/json",
                },
            )
    assert resp.status_code == 204
    mock_upsert.assert_called_once()


async def test_webhook_invalid_signature() -> None:
    """POST /api/webhooks/clerk with a bad Svix signature returns 400."""
    with patch(
        "app.api.webhooks.Webhook.verify",
        side_effect=WebhookVerificationError("bad sig"),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/webhooks/clerk",
                content=b"{}",
                headers={
                    "svix-id": "msg_test",
                    "svix-timestamp": "1234567890",
                    "svix-signature": "v1,badsig",
                    "content-type": "application/json",
                },
            )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 7. get_current_user: known clerk_id → returns user
# ---------------------------------------------------------------------------


async def test_get_current_user_known(
    db_session: AsyncSession, rsa_test_keys: dict
) -> None:
    """Valid Clerk JWT for a user present in DB returns 200."""
    clerk_id = "user_known_test"
    user = User(email="known@test.com", clerk_id=clerk_id, username="knownuser")
    db_session.add(user)
    portfolio = Portfolio(user_id=user.id, available_points=Decimal("1000.0000"))
    db_session.add(portfolio)
    await db_session.flush()

    token = _mint_token(rsa_test_keys, sub=clerk_id)

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get(
                "/api/portfolio/me",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 8. get_current_user: unknown clerk_id → 401
# ---------------------------------------------------------------------------


async def test_get_current_user_unknown(
    db_session: AsyncSession, rsa_test_keys: dict
) -> None:
    """Valid Clerk JWT for a clerk_id not in DB returns 401."""
    token = _mint_token(rsa_test_keys, sub="user_nobody")

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get(
                "/api/portfolio/me",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Bonus: no token → 403
# ---------------------------------------------------------------------------


async def test_protected_route_no_token() -> None:
    """A request with no Authorization header is rejected by HTTPBearer (403)."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get("/api/portfolio/me")
    assert resp.status_code in (401, 403)

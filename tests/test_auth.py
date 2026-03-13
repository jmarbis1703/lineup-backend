"""
TDD: Auth endpoints — POST /api/auth/register and POST /api/auth/login.

These tests override the get_db dependency so every request uses the
function-scoped db_session (which rolls back after the test).
"""
from __future__ import annotations

from decimal import Decimal

import pytest
import sqlalchemy as sa
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.main import app
from app.models.portfolio import Portfolio
from app.models.user import User


# ---------------------------------------------------------------------------
# Helper fixture: HTTP client bound to the test's DB session
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_client(db_session: AsyncSession):
    """
    AsyncClient whose get_db dependency is overridden to use db_session.

    This means every request goes through the same rolled-back transaction,
    so no data leaks between tests.
    """

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------

_VALID_REG = {
    "email": "alice@example.com",
    "username": "alice",
    "password": "securepass123",
}


async def test_register_success(db_client: AsyncClient) -> None:
    """POST /register returns 201 with a JWT and 1000-point portfolio info."""
    resp = await db_client.post("/api/auth/register", json=_VALID_REG)
    assert resp.status_code == 201
    body = resp.json()
    assert "token" in body
    assert body["token"]  # non-empty string
    assert body["username"] == "alice"
    assert Decimal(body["available_points"]) == Decimal("1000.0000")
    # user_id should be a valid UUID string
    import uuid
    uuid.UUID(body["user_id"])


async def test_register_creates_one_portfolio(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Registration creates exactly 1 portfolio row for the new user."""
    resp = await db_client.post("/api/auth/register", json=_VALID_REG)
    assert resp.status_code == 201

    import uuid
    user_id = uuid.UUID(resp.json()["user_id"])

    result = await db_session.execute(
        sa.select(Portfolio).where(Portfolio.user_id == user_id)
    )
    portfolios = result.scalars().all()
    assert len(portfolios) == 1
    assert portfolios[0].available_points == Decimal("1000.0000")


async def test_register_duplicate_email(db_client: AsyncClient) -> None:
    """Second registration with the same e-mail returns 409."""
    await db_client.post("/api/auth/register", json=_VALID_REG)
    resp2 = await db_client.post(
        "/api/auth/register",
        json={**_VALID_REG, "username": "alice2"},
    )
    assert resp2.status_code == 409


async def test_register_duplicate_username(db_client: AsyncClient) -> None:
    """Second registration with the same username returns 409."""
    await db_client.post("/api/auth/register", json=_VALID_REG)
    resp2 = await db_client.post(
        "/api/auth/register",
        json={**_VALID_REG, "email": "alice2@example.com"},
    )
    assert resp2.status_code == 409


async def test_register_weak_password(db_client: AsyncClient) -> None:
    """Password shorter than 8 characters returns 422 (validation error)."""
    resp = await db_client.post(
        "/api/auth/register",
        json={**_VALID_REG, "password": "short"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Login tests
# ---------------------------------------------------------------------------


async def test_login_success(db_client: AsyncClient) -> None:
    """Valid credentials return 200 with a JWT."""
    await db_client.post("/api/auth/register", json=_VALID_REG)
    resp = await db_client.post(
        "/api/auth/login",
        json={"email": _VALID_REG["email"], "password": _VALID_REG["password"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "token" in body
    assert body["token"]


async def test_login_wrong_password(db_client: AsyncClient) -> None:
    """Wrong password returns 401."""
    await db_client.post("/api/auth/register", json=_VALID_REG)
    resp = await db_client.post(
        "/api/auth/login",
        json={"email": _VALID_REG["email"], "password": "wrongpassword"},
    )
    assert resp.status_code == 401


async def test_login_nonexistent(db_client: AsyncClient) -> None:
    """Login for an e-mail that was never registered returns 401."""
    resp = await db_client.post(
        "/api/auth/login",
        json={"email": "nobody@example.com", "password": "password123"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Protected route tests (uses get_current_user dependency)
# ---------------------------------------------------------------------------


async def test_protected_route_no_token(db_client: AsyncClient) -> None:
    """A request with no Authorization header returns 403 (HTTPBearer rejects it)."""
    resp = await db_client.get("/api/portfolio/me")
    # HTTPBearer returns 403 when no credentials are provided at all
    assert resp.status_code in (401, 403)


async def test_protected_route_invalid_token(db_client: AsyncClient) -> None:
    """A request with a garbage Bearer token returns 401."""
    resp = await db_client.get(
        "/api/portfolio/me",
        headers={"Authorization": "Bearer this.is.garbage"},
    )
    assert resp.status_code == 401

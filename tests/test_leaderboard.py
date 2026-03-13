"""
Tests for GET /api/leaderboard.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.main import app as fastapi_app
from app.models.portfolio import Portfolio
from app.models.user import User


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    fastapi_app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app), base_url="http://test"
    ) as client:
        yield client
    fastapi_app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_user(
    db: AsyncSession,
    username: str,
    points: Decimal,
) -> User:
    user = User(
        email=f"{username}@lb.test",
        password_hash="$2b$12$testhashplaceholderfortestingonly",
        username=username,
    )
    db.add(user)
    await db.flush()
    db.add(Portfolio(user_id=user.id, available_points=points))
    await db.flush()
    return user


# ---------------------------------------------------------------------------
# test_leaderboard_ordering
# ---------------------------------------------------------------------------


async def test_leaderboard_ordering(
    db_session: AsyncSession,
    api_client: AsyncClient,
) -> None:
    """Leaderboard is ordered by total_value DESC."""
    await _create_user(db_session, "lb_rich", Decimal("2000.0000"))
    await _create_user(db_session, "lb_poor", Decimal("500.0000"))

    resp = await api_client.get("/api/leaderboard")
    assert resp.status_code == 200
    data = resp.json()

    # Verify monotonically non-increasing
    values = [e["total_value"] for e in data]
    assert values == sorted(values, reverse=True)

    # Rich user must rank above poor user
    usernames = [e["username"] for e in data]
    assert "lb_rich" in usernames
    assert "lb_poor" in usernames
    assert usernames.index("lb_rich") < usernames.index("lb_poor")


# ---------------------------------------------------------------------------
# test_leaderboard_max_50
# ---------------------------------------------------------------------------


async def test_leaderboard_max_50(
    db_session: AsyncSession,
    api_client: AsyncClient,
) -> None:
    """Leaderboard returns at most 50 entries."""
    for i in range(60):
        await _create_user(db_session, f"lb_user_{i:03d}", Decimal("100.0000"))

    resp = await api_client.get("/api/leaderboard")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) <= 50


# ---------------------------------------------------------------------------
# test_leaderboard_rank_field
# ---------------------------------------------------------------------------


async def test_leaderboard_rank_field(
    db_session: AsyncSession,
    api_client: AsyncClient,
) -> None:
    """Rank starts at 1 and increments sequentially."""
    await _create_user(db_session, "lb_rank_a", Decimal("1000.0000"))
    await _create_user(db_session, "lb_rank_b", Decimal("800.0000"))

    resp = await api_client.get("/api/leaderboard")
    assert resp.status_code == 200
    data = resp.json()

    ranks = [e["rank"] for e in data]
    assert ranks[0] == 1
    assert ranks == list(range(1, len(data) + 1))

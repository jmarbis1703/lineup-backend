"""
Tests for GET /api/portfolio/me.

Verifies:
- Fresh user portfolio (no positions).
- Portfolio after buying UP: unrealized_pnl < 0 (AMM spread), total_value < 1000.
- Portfolio after buying DOWN: DOWN position appears.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.main import app as fastapi_app
from app.models.portfolio import Portfolio
from app.models.user import User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token(user_id) -> str:
    from jose import jwt

    from app.config import settings

    exp = datetime.now(timezone.utc) + timedelta(minutes=60)
    return jwt.encode(
        {"sub": str(user_id), "exp": exp},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


async def _create_user_portfolio(
    db: AsyncSession,
    username: str,
    points: Decimal = Decimal("1000.0000"),
):
    user = User(
        email=f"{username}@portfolio.test",
        password_hash="$2b$12$testhashplaceholderfortestingonly",
        username=username,
    )
    db.add(user)
    await db.flush()
    portfolio = Portfolio(user_id=user.id, available_points=points)
    db.add(portfolio)
    await db.flush()
    return user, portfolio


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
# test_portfolio_initial
# ---------------------------------------------------------------------------


async def test_portfolio_initial(
    db_session: AsyncSession,
    api_client: AsyncClient,
) -> None:
    """Fresh user: available_points=1000, no positions, total_value≈1000."""
    user, _ = await _create_user_portfolio(db_session, "port_initial")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}

    resp = await api_client.get("/api/portfolio/me", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert Decimal(str(data["available_points"])) == Decimal("1000.0000")
    assert data["positions"] == []
    assert abs(data["total_value"] - 1000.0) < 0.01


# ---------------------------------------------------------------------------
# test_portfolio_after_buy_up
# ---------------------------------------------------------------------------


async def test_portfolio_after_buy_up(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """
    After buying UP with budget=100:
    - One UP position with shares > 0.
    - unrealized_pnl < 0 immediately (AMM spread).
    - total_value < 1000 (spread loss reflected).
    """
    user, _ = await _create_user_portfolio(db_session, "port_buy_up")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}

    buy_resp = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "UP", "budget": 100.0},
        headers=headers,
    )
    assert buy_resp.status_code == 200

    resp = await api_client.get("/api/portfolio/me", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert len(data["positions"]) == 1
    pos = data["positions"][0]
    assert pos["player_id"] == 99
    assert pos["direction"] == "UP"
    assert Decimal(str(pos["shares_owned"])) > 0
    # AMM spread: immediate unrealized PnL is negative
    assert pos["unrealized_pnl"] < 0
    # total_value slightly below 1000 due to spread
    assert data["total_value"] < 1000.0
    assert data["total_value"] > 850.0  # sanity check — not catastrophically low


# ---------------------------------------------------------------------------
# test_portfolio_after_buy_down
# ---------------------------------------------------------------------------


async def test_portfolio_after_buy_down(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """After buying DOWN: a DOWN position appears with shares > 0."""
    user, _ = await _create_user_portfolio(db_session, "port_buy_down")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}

    buy_resp = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "DOWN", "budget": 50.0},
        headers=headers,
    )
    assert buy_resp.status_code == 200

    resp = await api_client.get("/api/portfolio/me", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    down_positions = [p for p in data["positions"] if p["direction"] == "DOWN"]
    assert len(down_positions) == 1
    assert Decimal(str(down_positions[0]["shares_owned"])) > 0


# ---------------------------------------------------------------------------
# test_portfolio_requires_auth
# ---------------------------------------------------------------------------


async def test_portfolio_requires_auth(api_client: AsyncClient) -> None:
    """GET /api/portfolio/me without a token → 401."""
    resp = await api_client.get("/api/portfolio/me")
    assert resp.status_code in (401, 403)  # HTTPBearer returns 401 for missing credentials

"""
Tests for GET /api/market/players, GET /api/market/players/{id},
and GET /api/market/players/{id}/chart.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.main import app as fastapi_app
from app.models.market_state import LmsrMarketState
from app.models.player import Player
from app.models.rating_history import RatingHistory


# ---------------------------------------------------------------------------
# Shared fixture — overrides get_db so HTTP requests see the test session
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
# test_get_players_returns_all
# ---------------------------------------------------------------------------


async def test_get_players_returns_all(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """GET /api/market/players returns all active players."""
    resp = await api_client.get("/api/market/players")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    player_ids = [p["id"] for p in data]
    assert 99 in player_ids


# ---------------------------------------------------------------------------
# test_includes_oracle_data
# ---------------------------------------------------------------------------


async def test_includes_oracle_data(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """Response includes oracle_rating, oracle_source, b_effective, total_shares."""
    resp = await api_client.get("/api/market/players")
    assert resp.status_code == 200
    data = resp.json()
    player_data = next(p for p in data if p["id"] == 99)

    assert "oracle_rating" in player_data
    assert "oracle_source" in player_data
    assert "b_effective" in player_data
    assert "total_shares" in player_data
    assert player_data["oracle_rating"] is not None
    assert player_data["b_effective"] > 0


# ---------------------------------------------------------------------------
# test_league_id_filter
# ---------------------------------------------------------------------------


async def test_league_id_filter(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """?league_id= filters players to the given league."""
    resp = await api_client.get("/api/market/players?league_id=999")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert all(p["league_id"] == 999 for p in data)

    # Non-existent league → empty list
    resp2 = await api_client.get("/api/market/players?league_id=1")
    assert resp2.status_code == 200
    assert resp2.json() == []


# ---------------------------------------------------------------------------
# test_search_filter
# ---------------------------------------------------------------------------


async def test_search_filter(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """?search= filters by player name (case-insensitive)."""
    resp = await api_client.get("/api/market/players?search=test")
    assert resp.status_code == 200
    data = resp.json()
    assert any(p["id"] == 99 for p in data)

    # Non-matching search → empty list
    resp2 = await api_client.get("/api/market/players?search=zzznomatch")
    assert resp2.status_code == 200
    assert resp2.json() == []


# ---------------------------------------------------------------------------
# test_get_chart_chronological
# ---------------------------------------------------------------------------


async def test_get_chart_chronological(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """GET /api/market/players/{id}/chart returns entries in chronological order."""
    now = datetime.now(timezone.utc)
    older = now - timedelta(days=30)

    rh1 = RatingHistory(
        player_id=99,
        rating=Decimal("7.0000"),
        source="market_init",
        recorded_at=older,
    )
    rh2 = RatingHistory(
        player_id=99,
        rating=Decimal("7.5000"),
        source="trade",
        recorded_at=now,
    )
    db_session.add_all([rh1, rh2])
    await db_session.flush()

    resp = await api_client.get("/api/market/players/99/chart")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    # Chronological: older first
    assert data[0]["source"] == "market_init"
    assert data[1]["source"] == "trade"


# ---------------------------------------------------------------------------
# test_get_single_player_returns_inactive  (Close-Only)
# ---------------------------------------------------------------------------


async def test_get_single_player_returns_inactive(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """
    Close-Only: GET /api/market/players/{id} MUST return HTTP 200 even when
    the player is inactive. Delisted asset data must remain accessible for
    portfolio viewing and sell-order routing (PRD §5.1).
    """
    # Deactivate player 99
    player = (
        await db_session.execute(sa.select(Player).where(Player.id == 99))
    ).scalar_one()
    player.is_active = False
    await db_session.flush()

    resp = await api_client.get("/api/market/players/99")
    assert resp.status_code == 200, (
        f"Expected 200 for inactive player, got {resp.status_code}: {resp.text}"
    )

    data = resp.json()
    assert data["is_active"] is False
    assert data["current_rating"] is not None
    assert data["q_up"] is not None
    assert data["q_down"] is not None


# ---------------------------------------------------------------------------
# test_inactive_player_excluded_from_list
# ---------------------------------------------------------------------------


async def test_inactive_player_excluded_from_list(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """Inactive players do NOT appear in GET /api/market/players list."""
    player = (
        await db_session.execute(sa.select(Player).where(Player.id == 99))
    ).scalar_one()
    player.is_active = False
    await db_session.flush()

    resp = await api_client.get("/api/market/players")
    assert resp.status_code == 200
    data = resp.json()
    assert all(p["id"] != 99 for p in data)


# ---------------------------------------------------------------------------
# test_chart_404_unknown_player
# ---------------------------------------------------------------------------


async def test_chart_404_unknown_player(
    db_session: AsyncSession,
    api_client: AsyncClient,
) -> None:
    """Chart endpoint returns 404 for unknown player_id."""
    resp = await api_client.get("/api/market/players/99999/chart")
    assert resp.status_code == 404

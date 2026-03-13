"""
Tests for check_finished_fixtures oracle update worker — §7.4.

test_oracle_update_sets_oracle_rating       — oracle_rating updated after run
test_oracle_update_does_not_modify_market_price — q_up, q_down, current_rating unchanged (INV-09)
test_oracle_update_records_history          — RatingHistory row source='oracle_update'
test_oracle_update_publishes_ws_event       — WebSocket event published
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fixture import Fixture
from app.models.market_state import LmsrMarketState
from app.models.player import Player
from app.models.rating_history import RatingHistory
from app.workers.oracle_update import check_finished_fixtures


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def oracle_test_data(db_session: AsyncSession) -> dict[str, Any]:
    """Player (id=201, FW), fixture (id=5001, finished, last_synced_at=now), market."""
    now = datetime.now(timezone.utc)

    player = Player(
        id=201,
        name="Oracle Test Player",
        team="Test FC",
        position="13",
        position_group="FW",
        league="Test League",
        league_id=8,
        is_active=True,
        last_synced_at=now,
    )
    db_session.add(player)

    fixture = Fixture(
        id=5001,
        home_team="Home FC",
        away_team="Away FC",
        kickoff_time=now,
        status="finished",
        league_id=8,
        matchday=28,
        last_synced_at=now,  # within lookback window → will be processed
    )
    db_session.add(fixture)
    await db_session.flush()

    market = LmsrMarketState(
        player_id=201,
        alpha=Decimal("0.050000"),
        b_min=Decimal("100.0000"),
        q_up=Decimal("0.000000"),
        q_down=Decimal("0.000000"),
        current_rating=Decimal("6.5000"),
        oracle_rating=Decimal("6.5000"),
        oracle_source="layer_3",
        updated_at=now,
    )
    db_session.add(market)
    await db_session.flush()

    return {"player": player, "fixture": fixture, "market": market}


def _make_mock_client(player_ids: list[int], rating: float = 7.5) -> AsyncMock:
    """Build an AsyncMock SportmonksClient returning realistic fixture + stat data."""
    client = AsyncMock()

    client.get_fixture_with_lineups.return_value = {
        "lineups": [
            {"player_id": pid, "player": {"id": pid}, "position_id": 13}
            for pid in player_ids
        ]
    }

    client.get_current_season_id.return_value = 2024

    client.get_player_statistics.return_value = {
        "sportmonks_rating": rating,
        "minutes_played": 90,
        "goals": 1,
        "assists": 0,
        "shots_on_target": 3,
        "key_passes": 2,
        "tackles": 0,
        "dribbles_won": 2,
        "pass_accuracy": 75.0,
        "xg": 1.2,
        "interceptions": 0,
        "clearances": 0,
        "duels_won": 3,
        "saves": None,
        "goals_conceded": None,
        "clean_sheet": None,
    }

    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_oracle_update_sets_oracle_rating(
    db_session: AsyncSession,
    oracle_test_data: dict,
) -> None:
    """After running check_finished_fixtures, oracle_rating is updated."""
    player = oracle_test_data["player"]
    client = _make_mock_client([player.id], rating=8.0)

    await check_finished_fixtures(db_session, client)

    market = (
        await db_session.execute(
            sa.select(LmsrMarketState).where(LmsrMarketState.player_id == player.id)
        )
    ).scalar_one()

    assert market.oracle_rating is not None
    # Layer 1 oracle with a single 8.0 rating → oracle_rating should be close to 8.0
    assert abs(float(market.oracle_rating) - 8.0) < 0.5
    assert market.oracle_source in ("layer_1", "layer_2", "layer_3")


async def test_oracle_update_does_not_modify_market_price(
    db_session: AsyncSession,
    oracle_test_data: dict,
) -> None:
    """INV-09: q_up, q_down, current_rating must NOT be modified by oracle update."""
    player = oracle_test_data["player"]
    market = oracle_test_data["market"]
    q_up_before = market.q_up
    q_down_before = market.q_down
    rating_before = market.current_rating

    client = _make_mock_client([player.id], rating=9.0)
    await check_finished_fixtures(db_session, client)

    await db_session.refresh(market)

    assert market.q_up == q_up_before, "q_up must not be modified by oracle update"
    assert market.q_down == q_down_before, "q_down must not be modified by oracle update"
    assert market.current_rating == rating_before, (
        "current_rating must not be modified by oracle update"
    )


async def test_oracle_update_records_history(
    db_session: AsyncSession,
    oracle_test_data: dict,
) -> None:
    """A RatingHistory row with source='oracle_update' is inserted after update."""
    player = oracle_test_data["player"]
    client = _make_mock_client([player.id], rating=7.5)

    await check_finished_fixtures(db_session, client)

    history = (
        await db_session.execute(
            sa.select(RatingHistory).where(
                RatingHistory.player_id == player.id,
                RatingHistory.source == "oracle_update",
            )
        )
    ).scalars().all()

    assert len(history) >= 1, (
        f"Expected at least one oracle_update history row for player {player.id}"
    )


async def test_oracle_update_publishes_ws_event(
    db_session: AsyncSession,
    oracle_test_data: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WebSocket rating_update event is published after oracle update."""
    player = oracle_test_data["player"]
    client = _make_mock_client([player.id], rating=7.5)

    published: list[dict] = []

    async def mock_publish(player_id, rating_before, rating_after, direction):
        published.append(
            {
                "player_id": player_id,
                "rating_before": rating_before,
                "rating_after": rating_after,
                "direction": direction,
            }
        )

    monkeypatch.setattr(
        "app.workers.oracle_update.publish_rating_update",
        mock_publish,
    )

    await check_finished_fixtures(db_session, client)

    assert len(published) >= 1, "Expected at least one WS event to be published"
    event = published[0]
    assert event["player_id"] == player.id
    assert event["direction"] in ("UP", "DOWN")

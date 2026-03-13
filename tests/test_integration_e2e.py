"""
End-to-end integration tests for the LineUp backend.

Covers the full system lifecycle:
  1–5:   Player import + market initialisation (oracle layers, rating history)
  6–11:  Multi-user trading (buy UP/DOWN, sell, preview)
  12–13: Portfolio valuation + leaderboard ranking
  14–16: Oracle update worker (INV-09: never touches current_rating)
  17–23: Tournament full lifecycle (pending → active → completed)
  24–26: Cost basis tracking (average_entry_price formula)
  27–28: LS-LMSR liquidity dynamics (b_eff growth, diminishing price impact)

Pattern
-------
All DB tests use `db_session` (function-scoped SAVEPOINT rollback).
HTTP tests use `api_client` which overrides `get_db` with `db_session`.
No inter-test state leaks — every test starts with a clean DB snapshot.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.lmsr import effective_b, initialize_market, lmsr_rating
from app.core.oracle import compute_oracle_rating
from app.core.trading import execute_buy, execute_sell
from app.dependencies import get_db
from app.main import app as fastapi_app
from app.models.fixture import Fixture
from app.models.market_state import LmsrMarketState
from app.models.player import Player
from app.models.player_match_rating import PlayerMatchRating
from app.models.portfolio import Portfolio
from app.models.position import Position
from app.models.rating_history import RatingHistory
from app.models.tournament import Tournament
from app.models.tournament_member import TournamentMember
from app.models.tournament_snapshot import TournamentSnapshot
from app.models.user import User
from app.workers.oracle_update import _process_fixture
from app.workers.tournaments import activate_pending_tournaments, freeze_expired_tournaments

# ---------------------------------------------------------------------------
# Constants — player IDs are non-overlapping across test functions
# ---------------------------------------------------------------------------

_B_MIN = Decimal("100.0000")
_ALPHA = Decimal("0.050000")
_QUANT4 = Decimal("0.0001")
_QUANT6 = Decimal("0.000001")

_IMPORT_PLAYER_IDS = [1001, 1002, 1003, 1004, 1005]
_TRADE_P1 = 2001   # used in trading + portfolio/leaderboard tests
_TRADE_P2 = 2002   # used in cost-basis test
_ORACLE_PID = 3001
_TOURN_PID = 4001
_LMSR_PID = 5001

_FIXTURE_BASE = 9000


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _make_token(user_id: uuid.UUID) -> str:
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
) -> tuple[User, Portfolio]:
    user = User(
        email=f"{username}@e2e.test",
        password_hash="$2b$12$testhashplaceholderfortestingonly",
        username=username,
    )
    db.add(user)
    await db.flush()
    portfolio = Portfolio(user_id=user.id, available_points=points)
    db.add(portfolio)
    await db.flush()
    return user, portfolio


async def _create_player_with_market(
    db: AsyncSession,
    player_id: int,
    name: str,
    position_group: str,
    oracle_rating: float,
    oracle_source: str,
    league_id: int = 999,
    is_active: bool = True,
) -> tuple[Player, LmsrMarketState]:
    """Insert Player + LmsrMarketState initialised from oracle_rating.

    current_rating is set equal to oracle_rating — initialize_market()
    guarantees lmsr_rating(q, b_min) == r_base, so the target rating is
    correctly encoded in (q_up, q_down) and current_rating reflects it.
    """
    now = datetime.now(timezone.utc)
    player = Player(
        id=player_id,
        name=name,
        team="Test FC",
        position=position_group,
        position_group=position_group,
        league="Test League",
        league_id=league_id,
        is_active=is_active,
        last_synced_at=now,
    )
    db.add(player)
    await db.flush()

    b_min_f = float(_B_MIN)
    q_up_f, q_down_f = initialize_market(oracle_rating, b_min_f)

    market = LmsrMarketState(
        player_id=player_id,
        alpha=_ALPHA,
        b_min=_B_MIN,
        q_up=Decimal(str(q_up_f)).quantize(_QUANT6),
        q_down=Decimal(str(q_down_f)).quantize(_QUANT6),
        current_rating=Decimal(str(oracle_rating)).quantize(_QUANT4),
        oracle_rating=Decimal(str(oracle_rating)).quantize(_QUANT4),
        oracle_source=oracle_source,
        updated_at=now,
    )
    db.add(market)
    await db.flush()
    return player, market


async def _create_fixture(
    db: AsyncSession,
    fixture_id: int,
    status: str = "finished",
    league_id: int = 999,
    last_synced_at: datetime | None = None,
) -> Fixture:
    now = last_synced_at or datetime.now(timezone.utc)
    fixture = Fixture(
        id=fixture_id,
        home_team="Home FC",
        away_team="Away FC",
        kickoff_time=now - timedelta(hours=2),
        status=status,
        league_id=league_id,
        matchday=1,
        last_synced_at=now,
    )
    db.add(fixture)
    await db.flush()
    return fixture


async def _create_pmr(
    db: AsyncSession,
    player_id: int,
    fixture_id: int,
    sportmonks_rating: float | None,
    **kwargs,
) -> PlayerMatchRating:
    now = datetime.now(timezone.utc)
    pmr = PlayerMatchRating(
        player_id=player_id,
        fixture_id=fixture_id,
        sportmonks_rating=(
            Decimal(str(sportmonks_rating)).quantize(Decimal("0.01"))
            if sportmonks_rating is not None
            else None
        ),
        minutes_played=kwargs.get("minutes_played", 90),
        goals=kwargs.get("goals", 0),
        assists=kwargs.get("assists", 0),
        shots_on_target=kwargs.get("shots_on_target", 0),
        key_passes=kwargs.get("key_passes", 0),
        tackles=kwargs.get("tackles", 0),
        recorded_at=now,
    )
    db.add(pmr)
    await db.flush()
    return pmr


async def _get_portfolio(db: AsyncSession, user_id: uuid.UUID) -> Portfolio:
    result = await db.execute(
        sa.select(Portfolio).where(Portfolio.user_id == user_id)
    )
    return result.scalar_one()


async def _get_market(db: AsyncSession, player_id: int) -> LmsrMarketState:
    result = await db.execute(
        sa.select(LmsrMarketState).where(LmsrMarketState.player_id == player_id)
    )
    return result.scalar_one()


async def _get_position(
    db: AsyncSession,
    portfolio_id: uuid.UUID,
    player_id: int,
    direction: str,
) -> Position | None:
    result = await db.execute(
        sa.select(Position).where(
            Position.portfolio_id == portfolio_id,
            Position.player_id == player_id,
            Position.direction == direction,
        )
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client wired to the FastAPI app, sharing db_session via override.

    Data created in db_session is visible to HTTP requests; everything
    rolls back with the SAVEPOINT at test teardown.
    """

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    fastapi_app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app), base_url="http://test"
    ) as ac:
        yield ac
    fastapi_app.dependency_overrides.pop(get_db, None)


# ===========================================================================
# Tests 1–5: Player Import + Market Initialisation
# ===========================================================================


async def test_e2e_player_import_and_market_init(
    db_session: AsyncSession,
) -> None:
    """
    Simulate initial_player_import for 5 mock players.

    1. 5 players in DB, 5 market states, 5 rating_history rows.
    2. Players with match ratings → oracle_source = 'layer_1'.
    3. Players without data → oracle = 6.5, oracle_source = 'layer_3'.
    4. market.current_rating matches oracle_rating within 1e-4.
    """
    now = datetime.now(timezone.utc)

    # Shared fixture for players 1001–1004 (all have Sportmonks match ratings)
    fixture = await _create_fixture(db_session, fixture_id=_FIXTURE_BASE + 1)

    players_with_ratings = [
        {"player_id": 1001, "name": "Player One",   "pg": "FW", "rating": 8.0},
        {"player_id": 1002, "name": "Player Two",   "pg": "MF", "rating": 7.5},
        {"player_id": 1003, "name": "Player Three", "pg": "DF", "rating": 6.0},
        {"player_id": 1004, "name": "Player Four",  "pg": "GK", "rating": 7.0},
    ]

    all_players_stats = [
        {
            "player_id": p["player_id"],
            "position_group": p["pg"],
            "goals": 1, "shots_on_target": 2, "xg": 0.3,
            "key_passes": 1, "dribbles_won": 1, "pass_accuracy": 80.0,
            "tackles": 2, "minutes_played": 90,
            "saves": None, "goals_conceded": None, "clean_sheet": None,
            "interceptions": None, "clearances": None, "duels_won": None,
        }
        for p in players_with_ratings
    ]

    imported: list[dict] = []

    for p in players_with_ratings:
        player_stats = {
            "goals": 1, "shots_on_target": 2, "xg": 0.3,
            "key_passes": 1, "dribbles_won": 1, "pass_accuracy": 80.0,
            "tackles": 2, "minutes_played": 90,
            "saves": None, "goals_conceded": None, "clean_sheet": None,
            "interceptions": None, "clearances": None, "duels_won": None,
        }
        oracle_rating, oracle_source = compute_oracle_rating(
            match_ratings=[p["rating"]],
            player_stats=player_stats,
            position_group=p["pg"],
            all_players_stats=all_players_stats,
        )
        await _create_player_with_market(
            db_session, p["player_id"], p["name"], p["pg"],
            oracle_rating, oracle_source,
        )
        await _create_pmr(db_session, p["player_id"], fixture.id, p["rating"])
        db_session.add(RatingHistory(
            player_id=p["player_id"],
            rating=Decimal(str(oracle_rating)).quantize(_QUANT4),
            source="market_init",
            recorded_at=now,
        ))
        imported.append({
            "player_id": p["player_id"],
            "oracle_rating": oracle_rating,
            "oracle_source": oracle_source,
        })

    # Player 1005 — no match data at all → Layer 3 fallback
    oracle_rating_5, oracle_source_5 = compute_oracle_rating(
        match_ratings=[],
        player_stats=None,
        position_group="MF",
        all_players_stats=[],
    )
    await _create_player_with_market(
        db_session, 1005, "Player Five", "MF",
        oracle_rating_5, oracle_source_5,
    )
    db_session.add(RatingHistory(
        player_id=1005,
        rating=Decimal(str(oracle_rating_5)).quantize(_QUANT4),
        source="market_init",
        recorded_at=now,
    ))
    await db_session.flush()

    # --- 1. 5 players in DB ---
    player_rows = (await db_session.execute(
        sa.select(Player).where(Player.id.in_(_IMPORT_PLAYER_IDS))
    )).scalars().all()
    assert len(player_rows) == 5

    # --- 2. 5 market states ---
    market_rows = (await db_session.execute(
        sa.select(LmsrMarketState).where(
            LmsrMarketState.player_id.in_(_IMPORT_PLAYER_IDS)
        )
    )).scalars().all()
    assert len(market_rows) == 5

    # --- 3. 5 rating_history rows (source='market_init') ---
    hist_rows = (await db_session.execute(
        sa.select(RatingHistory).where(
            RatingHistory.player_id.in_(_IMPORT_PLAYER_IDS),
            RatingHistory.source == "market_init",
        )
    )).scalars().all()
    assert len(hist_rows) == 5

    # --- 4. Players 1001–1004 → oracle_source = 'layer_1' ---
    market_map = {m.player_id: m for m in market_rows}
    for imp in imported:
        assert imp["oracle_source"] == "layer_1", (
            f"Player {imp['player_id']}: expected layer_1, got {imp['oracle_source']}"
        )
        assert market_map[imp["player_id"]].oracle_source == "layer_1"

    # --- 5. Player 1005 → oracle = 6.5, oracle_source = 'layer_3' ---
    assert oracle_source_5 == "layer_3"
    assert abs(oracle_rating_5 - 6.5) < 1e-9
    assert market_map[1005].oracle_source == "layer_3"
    assert abs(float(market_map[1005].oracle_rating) - 6.5) < 1e-4

    # --- 6. current_rating matches oracle_rating within 1e-4 for all 5 ---
    for mkt in market_rows:
        assert abs(float(mkt.current_rating) - float(mkt.oracle_rating)) < 1e-4, (
            f"Player {mkt.player_id}: current_rating={mkt.current_rating} "
            f"!= oracle_rating={mkt.oracle_rating}"
        )


# ===========================================================================
# Tests 6–11: Trading (Alice, Bob, Charlie)
# ===========================================================================


async def test_e2e_trading_session(
    db_session: AsyncSession,
    api_client: AsyncClient,
) -> None:
    """
    6.  Alice, Bob, Charlie each have 1000 points.
    7.  Alice buys UP on Player #1 (budget=100) → shares > 0, rating UP.
    8.  Bob buys DOWN on Player #1 (budget=80) → same market, rating DOWN.
    9.  Charlie sells UP with no position → 400.
    10. Alice sells half her UP shares → refund > 0.
    11. Preview a buy; execute it; assert shares match within 1e-6.
    """
    await _create_player_with_market(
        db_session, _TRADE_P1, "Trade Player One", "FW",
        oracle_rating=7.0, oracle_source="layer_1",
    )
    await _create_player_with_market(
        db_session, _TRADE_P2, "Trade Player Two", "MF",
        oracle_rating=6.5, oracle_source="layer_3",
    )

    alice, alice_port = await _create_user_portfolio(db_session, "e2e_alice")
    bob, _ = await _create_user_portfolio(db_session, "e2e_bob")
    charlie, _ = await _create_user_portfolio(db_session, "e2e_charlie")

    # --- 6. Assert 1000 points each ---
    assert alice_port.available_points == Decimal("1000.0000")

    alice_h = {"Authorization": f"Bearer {_make_token(alice.id)}"}
    bob_h = {"Authorization": f"Bearer {_make_token(bob.id)}"}
    charlie_h = {"Authorization": f"Bearer {_make_token(charlie.id)}"}

    # --- 7. Alice buys UP (budget=100) ---
    r = await api_client.post(
        "/api/trade/buy",
        json={"player_id": _TRADE_P1, "direction": "UP", "budget": 100.0},
        headers=alice_h,
    )
    assert r.status_code == 200, r.text
    d = r.json()
    alice_shares = Decimal(str(d["shares"]))
    assert alice_shares > 0
    assert Decimal(str(d["rating_after"])) > Decimal(str(d["rating_before"])), (
        "UP buy must increase rating"
    )

    # --- 8. Bob buys DOWN (budget=80) on same player ---
    r = await api_client.post(
        "/api/trade/buy",
        json={"player_id": _TRADE_P1, "direction": "DOWN", "budget": 80.0},
        headers=bob_h,
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert Decimal(str(d["shares"])) > 0
    assert Decimal(str(d["rating_after"])) < Decimal(str(d["rating_before"])), (
        "DOWN buy must decrease rating"
    )

    # Both trades hit the ONE global market for _TRADE_P1
    mkt = await _get_market(db_session, _TRADE_P1)
    assert mkt.q_up > 0, "q_up > 0 after Alice's UP buy"
    assert mkt.q_down > 0, "q_down > 0 after Bob's DOWN buy"

    # --- 9. Charlie sells UP with no position → 400 ---
    r = await api_client.post(
        "/api/trade/sell",
        json={"player_id": _TRADE_P1, "direction": "UP", "shares": 1.0},
        headers=charlie_h,
    )
    assert r.status_code == 400, f"Expected 400, got {r.status_code}"

    # --- 10. Alice sells half her UP shares → refund > 0 ---
    half = float(alice_shares) / 2.0
    r = await api_client.post(
        "/api/trade/sell",
        json={"player_id": _TRADE_P1, "direction": "UP", "shares": half},
        headers=alice_h,
    )
    assert r.status_code == 200, r.text
    assert Decimal(str(r.json()["refund"])) > 0

    # --- 11. Preview then execute; shares match within 1e-6 ---
    r_prev = await api_client.post(
        "/api/trade/preview",
        json={"player_id": _TRADE_P1, "direction": "UP", "budget": 50.0},
        headers=alice_h,
    )
    assert r_prev.status_code == 200, r_prev.text
    preview_shares = Decimal(str(r_prev.json()["shares"]))

    r_exec = await api_client.post(
        "/api/trade/buy",
        json={"player_id": _TRADE_P1, "direction": "UP", "budget": 50.0},
        headers=alice_h,
    )
    assert r_exec.status_code == 200, r_exec.text
    exec_shares = Decimal(str(r_exec.json()["shares"]))

    assert abs(float(preview_shares - exec_shares)) < 1e-6, (
        f"Preview shares {preview_shares} != execution shares {exec_shares}"
    )


# ===========================================================================
# Tests 12–13: Portfolio + Leaderboard
# ===========================================================================


async def test_e2e_portfolio_and_leaderboard(
    db_session: AsyncSession,
    api_client: AsyncClient,
) -> None:
    """
    12. GET /api/portfolio/me → UP position with correct PnL.
    13. GET /api/leaderboard → 3 users ranked by total_value DESC.
    """
    await _create_player_with_market(
        db_session, _TRADE_P1, "Port Player", "FW",
        oracle_rating=7.0, oracle_source="layer_1",
    )
    alice, _ = await _create_user_portfolio(db_session, "port_alice")
    _, _ = await _create_user_portfolio(db_session, "port_bob")
    _, _ = await _create_user_portfolio(db_session, "port_charlie")

    alice_h = {"Authorization": f"Bearer {_make_token(alice.id)}"}

    # Alice buys UP (budget=200)
    r = await api_client.post(
        "/api/trade/buy",
        json={"player_id": _TRADE_P1, "direction": "UP", "budget": 200.0},
        headers=alice_h,
    )
    assert r.status_code == 200, r.text

    # --- 12. Portfolio endpoint ---
    r = await api_client.get("/api/portfolio/me", headers=alice_h)
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["positions"]) == 1
    pos = data["positions"][0]
    assert pos["player_id"] == _TRADE_P1
    assert pos["direction"] == "UP"
    assert Decimal(str(pos["shares_owned"])) > 0
    # total_value = 800 points cash + sell_refund (≤ 200 due to AMM spread)
    assert 0 < data["total_value"] <= 1000.0

    # --- 13. Leaderboard ---
    r = await api_client.get("/api/leaderboard")
    assert r.status_code == 200, r.text
    lb = r.json()

    usernames = {e["username"] for e in lb}
    assert "port_alice" in usernames
    assert "port_bob" in usernames
    assert "port_charlie" in usernames

    values = [e["total_value"] for e in lb]
    assert values == sorted(values, reverse=True), "Leaderboard must be DESC by total_value"

    ranks = [e["rank"] for e in lb]
    assert ranks[0] == 1
    assert ranks == list(range(1, len(ranks) + 1))


# ===========================================================================
# Tests 14–16: Oracle Update Worker
# ===========================================================================


async def test_e2e_oracle_update(
    db_session: AsyncSession,
) -> None:
    """
    14. Post-match oracle update for Player #1 with new Sportmonks rating 8.5.
    15. oracle_rating updated. current_rating NOT changed (INV-09).
    16. rating_history row with source='oracle_update' inserted.
    """
    now = datetime.now(timezone.utc)

    await _create_player_with_market(
        db_session, _ORACLE_PID, "Oracle Player", "FW",
        oracle_rating=6.5, oracle_source="layer_3",
    )
    # Fixture within the 20-minute lookback window so check_finished_fixtures picks it up
    fixture = await _create_fixture(
        db_session,
        fixture_id=_FIXTURE_BASE + 100,
        status="finished",
        league_id=888,
        last_synced_at=now,
    )

    market_before = await _get_market(db_session, _ORACLE_PID)
    current_rating_before = market_before.current_rating
    oracle_rating_before = market_before.oracle_rating

    new_rating = 8.5

    mock_client = AsyncMock()
    mock_client.get_fixture_with_lineups.return_value = {
        "lineups": [{"player_id": _ORACLE_PID, "position_id": "13"}]  # FW
    }
    mock_client.get_current_season_id.return_value = 1
    mock_client.get_player_statistics.return_value = {
        "sportmonks_rating": new_rating,
        "minutes_played": 90,
        "goals": 2,
        "assists": 1,
        "shots_on_target": 4,
        "xg": 1.2,
        "key_passes": 3,
        "dribbles_won": 2,
        "pass_accuracy": 85.0,
        "tackles": 1,
        "interceptions": None,
        "clearances": None,
        "duels_won": None,
        "saves": None,
        "goals_conceded": None,
        "clean_sheet": None,
    }

    with patch(
        "app.workers.oracle_update.publish_rating_update",
        new_callable=AsyncMock,
    ):
        updated = await _process_fixture(db_session, mock_client, fixture)

    assert updated == 1, f"Expected 1 player updated, got {updated}"

    # --- 15. oracle_rating changed; current_rating unchanged (INV-09) ---
    await db_session.refresh(market_before)
    mkt = market_before

    assert float(mkt.oracle_rating) != float(oracle_rating_before), (
        "oracle_rating must change after oracle update"
    )
    assert abs(float(mkt.oracle_rating) - new_rating) < 1e-3, (
        f"oracle_rating should be ~{new_rating}, got {mkt.oracle_rating}"
    )
    assert mkt.current_rating == current_rating_before, (
        f"INV-09 violated: current_rating changed from "
        f"{current_rating_before} to {mkt.current_rating}"
    )

    # --- 16. rating_history row with source='oracle_update' ---
    hist = (await db_session.execute(
        sa.select(RatingHistory).where(
            RatingHistory.player_id == _ORACLE_PID,
            RatingHistory.source == "oracle_update",
        )
    )).scalars().all()
    assert len(hist) == 1, f"Expected 1 oracle_update history row, got {len(hist)}"
    assert abs(float(hist[0].rating) - new_rating) < 1e-3


# ===========================================================================
# Tests 17–23: Tournament Full Lifecycle (pending → active → completed)
# ===========================================================================


async def test_e2e_tournament_lifecycle(
    db_session: AsyncSession,
    api_client: AsyncClient,
) -> None:
    """
    17. Alice creates 'Friends Cup' (future start_time). Status='pending'. Bob joins.
    18. Bob's starting_value = 0 (placeholder while pending).
    19. Time advanced past start_time; activate_pending_tournaments runs.
    20. Status='active'. Alice+Bob starting_values = real total_value at activation.
    21. Alice trades; tournament leaderboard shows Alice+Bob only, by profit_loss Δ.
    22. Freeze → snapshots created, status='completed'.
    23. Alice+Bob still have their positions and points after freeze.
    """
    now = datetime.now(timezone.utc)

    await _create_player_with_market(
        db_session, _TOURN_PID, "Tournament Player", "MF",
        oracle_rating=6.5, oracle_source="layer_3",
    )
    alice, alice_port = await _create_user_portfolio(db_session, "t_alice")
    bob, _ = await _create_user_portfolio(db_session, "t_bob")
    charlie, _ = await _create_user_portfolio(db_session, "t_charlie")

    alice_h = {"Authorization": f"Bearer {_make_token(alice.id)}"}
    bob_h = {"Authorization": f"Bearer {_make_token(bob.id)}"}

    # --- 17. Alice creates tournament (start_time 2s in future so it's pending) ---
    start_time = now + timedelta(seconds=2)
    end_time = now + timedelta(hours=1)
    r = await api_client.post(
        "/api/tournaments",
        json={
            "name": "Friends Cup",
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
        },
        headers=alice_h,
    )
    assert r.status_code == 201, r.text
    t_data = r.json()
    t_id = t_data["id"]
    assert t_data["status"] == "pending"

    # Alice joins her own tournament (create does NOT auto-join)
    r = await api_client.post(f"/api/tournaments/{t_id}/join", headers=alice_h)
    assert r.status_code in (200, 409), r.text  # 409 if API auto-joins on create

    # Bob joins
    r = await api_client.post(f"/api/tournaments/{t_id}/join", headers=bob_h)
    assert r.status_code in (200, 201), r.text

    # --- 18. Bob's starting_value = 0 while tournament is pending ---
    assert Decimal(str(r.json()["starting_value"])) == Decimal("0"), (
        f"Pending tournament: starting_value must be 0, got {r.json()['starting_value']}"
    )

    # Charlie does NOT join — confirm absence from member table
    charlie_mbr = (await db_session.execute(
        sa.select(TournamentMember).where(
            TournamentMember.tournament_id == uuid.UUID(t_id),
            TournamentMember.user_id == charlie.id,
        )
    )).scalar_one_or_none()
    assert charlie_mbr is None

    # --- 19. Simulate time passing: backdate start_time, then activate ---
    t_row = (await db_session.execute(
        sa.select(Tournament).where(Tournament.id == uuid.UUID(t_id))
    )).scalar_one()
    t_row.start_time = now - timedelta(seconds=1)
    await db_session.flush()

    activated = await activate_pending_tournaments(db_session)
    assert activated >= 1

    # --- 20. Status = 'active'; starting_values locked to real portfolio values ---
    await db_session.refresh(t_row)
    assert t_row.status == "active"

    members = (await db_session.execute(
        sa.select(TournamentMember).where(
            TournamentMember.tournament_id == uuid.UUID(t_id)
        )
    )).scalars().all()
    member_map = {m.user_id: m for m in members}

    assert alice.id in member_map, "Alice must be a member"
    assert bob.id in member_map, "Bob must be a member"
    assert charlie.id not in member_map, "Charlie must NOT be a member"

    # Each member has 1000 points cash, no positions → starting_value ≈ 1000
    assert member_map[alice.id].starting_value > 0
    assert member_map[bob.id].starting_value > 0

    # --- 21. Alice trades; leaderboard shows Alice+Bob only, sorted by Δ ---
    r = await api_client.post(
        "/api/trade/buy",
        json={"player_id": _TOURN_PID, "direction": "UP", "budget": 100.0},
        headers=alice_h,
    )
    assert r.status_code == 200, r.text

    r = await api_client.get(
        f"/api/tournaments/{t_id}/leaderboard",
        headers=alice_h,
    )
    assert r.status_code == 200, r.text
    lb = r.json()

    lb_user_ids = {str(e["user_id"]) for e in lb}
    assert str(alice.id) in lb_user_ids
    assert str(bob.id) in lb_user_ids
    assert str(charlie.id) not in lb_user_ids

    pls = [e["profit_loss"] for e in lb]
    assert pls == sorted(pls, reverse=True), (
        "Tournament leaderboard must be sorted by profit_loss DESC"
    )

    # --- 22. Freeze tournament → snapshots, status='completed' ---
    # end_time must be > start_time (now - 1s) and <= actual now to trigger freeze
    t_row.end_time = now
    await db_session.flush()

    frozen = await freeze_expired_tournaments(db_session)
    assert frozen >= 1

    await db_session.refresh(t_row)
    assert t_row.status == "completed"

    snapshots = (await db_session.execute(
        sa.select(TournamentSnapshot).where(
            TournamentSnapshot.tournament_id == uuid.UUID(t_id)
        )
    )).scalars().all()
    assert len(snapshots) >= 2, "Snapshots for Alice and Bob must exist"

    for snap in snapshots:
        assert snap.rank >= 1
        expected_pl = float(snap.total_value) - float(snap.starting_value)
        assert abs(float(snap.profit_loss) - expected_pl) < 0.01

    # --- 23. Positions and points survive the freeze ---
    alice_port_now = await _get_portfolio(db_session, alice.id)
    bob_port_now = await _get_portfolio(db_session, bob.id)
    assert alice_port_now.available_points >= 0
    assert bob_port_now.available_points >= 0

    alice_pos = await _get_position(db_session, alice_port.id, _TOURN_PID, "UP")
    assert alice_pos is not None, "Alice's position must survive after freeze"
    assert alice_pos.shares_owned > 0, "Alice's shares must be untouched"


# ===========================================================================
# Tests 24–26: Cost Basis Verification
# ===========================================================================


async def test_e2e_cost_basis(
    db_session: AsyncSession,
) -> None:
    """
    24. Alice buys UP on Player #2 (budget=100). Note average_entry_price.
    25. Alice buys more UP (budget=120, price has moved). VWAP formula verified.
    26. Alice sells half shares. average_entry_price UNCHANGED after sell.
    """
    await _create_player_with_market(
        db_session, _TRADE_P2, "Cost Basis Player", "MF",
        oracle_rating=6.5, oracle_source="layer_3",
    )
    alice, alice_port = await _create_user_portfolio(db_session, "cb_alice")

    # --- 24. First buy ---
    res1 = await execute_buy(
        db_session, alice.id, _TRADE_P2, "UP", Decimal("100.0000")
    )
    shares1 = res1["shares"]

    pos1 = await _get_position(db_session, alice_port.id, _TRADE_P2, "UP")
    assert pos1 is not None
    assert pos1.shares_owned == shares1
    avg1 = pos1.average_entry_price

    # --- 25. Second buy (q_up > 0 now so price has moved) ---
    res2 = await execute_buy(
        db_session, alice.id, _TRADE_P2, "UP", Decimal("120.0000")
    )
    shares2 = res2["shares"]

    pos2 = await _get_position(db_session, alice_port.id, _TRADE_P2, "UP")
    total_shares = pos2.shares_owned
    avg2 = pos2.average_entry_price

    # PRD §2.6 VWAP formula:
    #   new_avg = (old_shares × old_avg + budget) / new_total_shares
    expected_avg = (float(shares1) * float(avg1) + 120.0) / float(total_shares)
    assert abs(float(avg2) - expected_avg) < 1e-5, (
        f"VWAP mismatch: expected {expected_avg:.6f}, got {float(avg2):.6f}"
    )

    # --- 26. Sell half shares — average_entry_price must NOT change ---
    half_sell = (total_shares / 2).quantize(_QUANT6, rounding=ROUND_DOWN)
    await execute_sell(db_session, alice.id, _TRADE_P2, "UP", half_sell)

    pos3 = await _get_position(db_session, alice_port.id, _TRADE_P2, "UP")
    assert pos3.average_entry_price == avg2, (
        f"average_entry_price must not change on sell: "
        f"before={avg2}, after={pos3.average_entry_price}"
    )
    assert pos3.shares_owned == total_shares - half_sell


# ===========================================================================
# Tests 27–28: LS-LMSR Liquidity Dynamics
# ===========================================================================


async def test_e2e_ls_lmsr_dynamics(
    db_session: AsyncSession,
) -> None:
    """
    27. Execute 50 UP buys (budget=20) on one player.
    28a. b_eff > initial b_min after the 50 buys.
    28b. 50th buy moves rating LESS than the 1st buy.
    """
    # oracle_rating=5.0 → P=0.5 → q_up=q_down=0 (symmetric), b_eff == b_min at start
    await _create_player_with_market(
        db_session, _LMSR_PID, "LS-LMSR Player", "MF",
        oracle_rating=5.0, oracle_source="layer_3",
    )
    # 5000 points — 50 × 20 = 1000 total spend, leaves plenty of headroom
    alice, _ = await _create_user_portfolio(
        db_session, "ls_alice", points=Decimal("5000.0000")
    )

    initial_mkt = await _get_market(db_session, _LMSR_PID)
    b_min_f = float(initial_mkt.b_min)
    alpha_f = float(initial_mkt.alpha)
    initial_b_eff = effective_b(
        float(initial_mkt.q_up), float(initial_mkt.q_down), alpha_f, b_min_f
    )
    # At symmetric init (q_up=q_down=0), b_eff == b_min
    assert abs(initial_b_eff - b_min_f) < 1e-9

    budget = Decimal("20.0000")
    rating_deltas: list[float] = []

    for _ in range(50):
        res = await execute_buy(db_session, alice.id, _LMSR_PID, "UP", budget)
        delta = float(res["rating_after"]) - float(res["rating_before"])
        rating_deltas.append(delta)

    # --- 28a. b_eff grew above b_min ---
    final_mkt = await _get_market(db_session, _LMSR_PID)
    final_b_eff = effective_b(
        float(final_mkt.q_up), float(final_mkt.q_down), alpha_f, b_min_f
    )
    assert final_b_eff > b_min_f, (
        f"b_eff ({final_b_eff:.4f}) must exceed b_min ({b_min_f:.4f}) after 50 buys"
    )
    assert final_b_eff > initial_b_eff

    # --- 28b. 50th buy moves rating LESS than 1st ---
    first_delta = rating_deltas[0]
    last_delta = rating_deltas[-1]
    assert first_delta > 0, "First UP buy must increase rating"
    assert last_delta >= 0, "50th UP buy must not decrease rating"
    assert last_delta < first_delta, (
        f"50th buy delta ({last_delta:.6f}) must be < 1st buy delta ({first_delta:.6f}): "
        "LS-LMSR liquidity growth must reduce per-budget price impact"
    )

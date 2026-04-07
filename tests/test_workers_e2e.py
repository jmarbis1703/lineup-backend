"""
Worker End-to-End Tests — Phase 1.3

Three focused tests that validate worker invariants not covered by the
fine-grained unit tests in test_tournaments.py and test_oracle_update.py:

  Test 1  activate_pending_tournaments
          Validates: status transition, no ZeroDivisionError, REPEATABLE READ.

  Test 2  freeze_expired_tournaments
          Validates: snapshot profit_loss via LS-LMSR sell_refund (not naive multiply),
          idempotency (second call returns 0; snapshot count unchanged).

  Test 3  check_finished_fixtures (oracle update)
          Validates: RatingHistory source='oracle_update', oracle_rating in [3, 10],
          INV-09 (q_up / q_down / current_rating never mutated), completes < 10 s.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.lmsr import effective_b, sell_refund_up
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
from app.workers.oracle_update import check_finished_fixtures
from app.workers.tournaments import (
    activate_pending_tournaments,
    freeze_expired_tournaments,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _make_user_portfolio(
    db: AsyncSession,
    prefix: str,
    points: Decimal = Decimal("1000.0000"),
) -> tuple[User, Portfolio]:
    user = User(
        email=f"{prefix}@e2e.test",
        username=prefix,
    )
    db.add(user)
    await db.flush()
    portfolio = Portfolio(user_id=user.id, available_points=points)
    db.add(portfolio)
    await db.flush()
    return user, portfolio


async def _make_player_market(
    db: AsyncSession,
    player_id: int,
    q_up: float = 200.0,
    q_down: float = 0.0,
    b_min: float = 100.0,
    alpha: float = 0.0,
    current_rating: float = 7.5,
) -> LmsrMarketState:
    now = datetime.now(timezone.utc)
    db.add(
        Player(
            id=player_id,
            name=f"E2E Player {player_id}",
            team="Test FC",
            position_group="FW",
            league_id=99,
            is_active=True,
            last_synced_at=now,
        )
    )
    await db.flush()
    market = LmsrMarketState(
        player_id=player_id,
        alpha=Decimal(str(alpha)),
        b_min=Decimal(str(b_min)).quantize(Decimal("0.0001")),
        q_up=Decimal(str(q_up)).quantize(Decimal("0.000001")),
        q_down=Decimal(str(q_down)).quantize(Decimal("0.000001")),
        current_rating=Decimal(str(current_rating)).quantize(Decimal("0.0001")),
        oracle_rating=Decimal(str(current_rating)).quantize(Decimal("0.0001")),
        oracle_source="layer_3",
        updated_at=now,
    )
    db.add(market)
    await db.flush()
    return market


def _mock_client(player_ids: list[int], sportmonks_rating: float = 7.5) -> AsyncMock:
    client = AsyncMock()
    client.get_fixture_with_lineups.return_value = {
        "lineups": [
            {"player_id": pid, "player": {"id": pid}, "position_id": 13}
            for pid in player_ids
        ]
    }
    client.get_current_season_id.return_value = 2024
    client.get_player_statistics.return_value = {
        "sportmonks_rating": sportmonks_rating,
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


# ===========================================================================
# Test 1 — activate_pending_tournaments
# ===========================================================================


async def test_activate_e2e_no_zerodivision_and_status_active(
    db_session: AsyncSession,
) -> None:
    """
    E2E: activate_pending_tournaments
    - Transitions pending → active without ZeroDivisionError
    - Works correctly when members have NO open positions (divide-by-zero guard)
    - Requests REPEATABLE READ isolation
    """
    now = datetime.now(timezone.utc)

    # Creator + 2 members, no open positions (pure cash → tests zero-weight guard)
    creator_user, _ = await _make_user_portfolio(db_session, "act_e2e_creator")
    user_a, port_a = await _make_user_portfolio(db_session, "act_e2e_a")
    user_b, port_b = await _make_user_portfolio(db_session, "act_e2e_b")

    tournament = Tournament(
        name="E2E Activate Test",
        status="pending",
        created_by=creator_user.id,
        start_time=now - timedelta(seconds=1),
        end_time=now + timedelta(days=7),
    )
    db_session.add(tournament)
    await db_session.flush()

    for user in (user_a, user_b):
        db_session.add(TournamentMember(
            tournament_id=tournament.id,
            user_id=user.id,
            starting_value=Decimal("0"),
        ))
    await db_session.flush()

    # Track isolation level requests
    requested_levels: list[str] = []
    original_connection = db_session.connection

    async def spy_connection(**kwargs):
        eo = kwargs.get("execution_options", {})
        if "isolation_level" in eo:
            requested_levels.append(eo["isolation_level"])
        return None

    db_session.connection = spy_connection

    # Must not raise ZeroDivisionError
    count = await activate_pending_tournaments(db_session)

    db_session.connection = original_connection

    assert count == 1, f"Expected 1 tournament activated, got {count}"

    t = (
        await db_session.execute(
            sa.select(Tournament).where(Tournament.id == tournament.id)
        )
    ).scalar_one()
    assert t.status == "active", f"Expected status='active', got {t.status!r}"

    # Verify starting_value locked from cash balance (1000 each, no positions)
    for user in (user_a, user_b):
        member = (
            await db_session.execute(
                sa.select(TournamentMember).where(
                    TournamentMember.tournament_id == tournament.id,
                    TournamentMember.user_id == user.id,
                )
            )
        ).scalar_one()
        assert abs(float(member.starting_value) - 1000.0) < 0.01, (
            f"Expected starting_value≈1000 for {user.username}, "
            f"got {member.starting_value}"
        )

    # REPEATABLE READ must be requested
    assert any(
        level in ("REPEATABLE READ", "SERIALIZABLE")
        for level in requested_levels
    ), f"REPEATABLE READ isolation not requested; got: {requested_levels}"


# ===========================================================================
# Test 2 — freeze_expired_tournaments
# ===========================================================================


async def test_freeze_e2e_lmsr_profit_loss_and_idempotency(
    db_session: AsyncSession,
) -> None:
    """
    E2E: freeze_expired_tournaments
    - snapshot.profit_loss computed via LS-LMSR sell_refund (§2.6 prohibition)
    - 2 members → exactly 2 TournamentSnapshot rows
    - Second call returns 0 (tournament already 'completed'); snapshot count unchanged
    """
    now = datetime.now(timezone.utc)

    # Market: q_up=500, q_down=0, b_min=100, alpha=0
    # Position: 200 UP shares — sell_refund > shares * naive_price due to AMM curve
    market = await _make_player_market(
        db_session,
        player_id=8001,
        q_up=500.0,
        q_down=0.0,
        b_min=100.0,
        alpha=0.0,
    )

    creator_user, _ = await _make_user_portfolio(db_session, "frz_e2e_creator")

    tournament = Tournament(
        name="E2E Freeze Test",
        status="active",
        created_by=creator_user.id,
        start_time=now - timedelta(days=8),
        end_time=now - timedelta(seconds=1),
    )
    db_session.add(tournament)
    await db_session.flush()

    member_users = []
    starting_val = Decimal("500.0000")
    for idx in range(2):
        user, portfolio = await _make_user_portfolio(
            db_session, f"frz_e2e_m{idx}", points=Decimal("500.0000")
        )
        # Open position: 200 UP shares
        db_session.add(Position(
            portfolio_id=portfolio.id,
            player_id=8001,
            direction="UP",
            shares_owned=Decimal("200.000000"),
            average_entry_price=Decimal("1.000000"),
        ))
        db_session.add(TournamentMember(
            tournament_id=tournament.id,
            user_id=user.id,
            starting_value=starting_val,
        ))
        member_users.append(user)
    await db_session.flush()

    # ── First call ──────────────────────────────────────────────────────────
    count_1 = await freeze_expired_tournaments(db_session)
    assert count_1 == 1, f"Expected 1 tournament frozen, got {count_1}"

    t = (
        await db_session.execute(
            sa.select(Tournament).where(Tournament.id == tournament.id)
        )
    ).scalar_one()
    assert t.status == "completed", f"Expected 'completed', got {t.status!r}"

    snaps = (
        await db_session.execute(
            sa.select(TournamentSnapshot).where(
                TournamentSnapshot.tournament_id == tournament.id
            )
        )
    ).scalars().all()
    assert len(snaps) == 2, f"Expected 2 snapshots for 2 members, got {len(snaps)}"

    # Verify profit_loss uses LS-LMSR sell_refund, not naive shares * price
    # Naive: 200 * (q_up / (q_up + q_down)) * 10  (rough price proxy)
    # Real: sell_refund_up(500, 0, 100, 200) computed below
    b = effective_b(500.0, 0.0, 0.0, 100.0)
    expected_refund = sell_refund_up(500.0, 0.0, b, 200.0)
    expected_total_value = float(Decimal("500.0000")) + expected_refund

    for snap in snaps:
        assert abs(float(snap.total_value) - expected_total_value) < 0.01, (
            f"total_value {snap.total_value} doesn't match expected "
            f"{expected_total_value:.4f} (cash + LS-LMSR sell_refund). "
            "profit_loss may be using naive price * shares."
        )
        expected_pnl = expected_total_value - float(starting_val)
        assert abs(float(snap.profit_loss) - expected_pnl) < 0.01, (
            f"profit_loss {snap.profit_loss} doesn't match expected {expected_pnl:.4f}"
        )
        assert snap.rank >= 1

    # ── Second call (idempotency guard) ─────────────────────────────────────
    count_2 = await freeze_expired_tournaments(db_session)
    assert count_2 == 0, (
        f"Second call should return 0 (tournament already 'completed'), got {count_2}"
    )

    # Snapshot count must still be exactly 2 — no duplicate rows
    snap_count = (
        await db_session.execute(
            sa.select(sa.func.count()).where(
                TournamentSnapshot.tournament_id == tournament.id
            )
        )
    ).scalar_one()
    assert snap_count == 2, (
        f"Expected 2 snapshots after two freeze calls (idempotency), got {snap_count}. "
        "Duplicate snapshots would mean the idempotency guard is broken."
    )


async def test_freeze_e2e_concurrent_idempotency() -> None:
    """
    Concurrent idempotency guard: two coroutines racing to freeze the same
    tournament via *separate* AsyncSessions must produce exactly member_count
    snapshots (no duplicates).

    Each freeze call gets its own session (separate connection) so SQLAlchemy
    session state is never shared — sharing a single db_session across concurrent
    coroutines causes PendingRollbackError because the session's internal savepoint
    stack is mutated by both coroutines interleaving at await points.

    Setup data is committed for real (so both sessions can see it on READ COMMITTED)
    and cleaned up in a finally block regardless of test outcome.
    """
    # Build a session factory that connects to the same test DB as the rest of the suite.
    _FALLBACK = "postgresql+asyncpg://lineup:lineup_secret@localhost:5432/lineup"
    db_url = os.getenv("TEST_DATABASE_URL", os.getenv("DATABASE_URL", _FALLBACK))
    db_url = db_url.replace("+psycopg2", "+asyncpg")

    engine = create_async_engine(db_url, pool_pre_ping=True)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    tournament_id = None
    all_user_ids: list = []

    try:
        # ── Setup: commit real rows so concurrent sessions can see them ──────
        now = datetime.now(timezone.utc)
        member_count = 3

        async with SessionLocal() as setup_db:
            creator = User(
                email="cidmp_creator@e2e.test",
                username="cidmp_creator",
            )
            setup_db.add(creator)
            await setup_db.flush()
            all_user_ids.append(creator.id)

            tournament = Tournament(
                name="Concurrent Idempotency Test",
                status="active",
                created_by=creator.id,
                start_time=now - timedelta(days=8),
                end_time=now - timedelta(seconds=1),
            )
            setup_db.add(tournament)
            await setup_db.flush()
            tournament_id = tournament.id

            for idx in range(member_count):
                member = User(
                    email=f"cidmp_m{idx}@e2e.test",
                    username=f"cidmp_m{idx}",
                )
                setup_db.add(member)
                await setup_db.flush()
                all_user_ids.append(member.id)

                portfolio = Portfolio(
                    user_id=member.id,
                    available_points=Decimal("1000.0000"),
                )
                setup_db.add(portfolio)
                await setup_db.flush()

                setup_db.add(TournamentMember(
                    tournament_id=tournament_id,
                    user_id=member.id,
                    starting_value=Decimal("1000.0000"),
                ))

            await setup_db.commit()

        # ── Concurrent freeze: each call gets its own isolated session ───────
        async def run_freeze() -> int:
            async with SessionLocal() as db:
                return await freeze_expired_tournaments(db)

        results = await asyncio.gather(
            run_freeze(),
            run_freeze(),
            return_exceptions=True,
        )

        total_frozen = sum(r for r in results if isinstance(r, int))
        assert total_frozen <= 1, (
            f"Combined frozen count should be ≤ 1, got {total_frozen}. "
            "Both coroutines processed the same tournament — double-freeze detected."
        )

        # ── Verify snapshot count via a fresh read session ───────────────────
        async with SessionLocal() as verify_db:
            snap_count = (
                await verify_db.execute(
                    sa.select(sa.func.count()).where(
                        TournamentSnapshot.tournament_id == tournament_id
                    )
                )
            ).scalar_one()

        assert snap_count == member_count, (
            f"Expected exactly {member_count} snapshots, got {snap_count}. "
            "Duplicate snapshots indicate the idempotency guard failed under concurrency."
        )

    finally:
        # ── Cleanup: remove committed test data so it doesn't linger ─────────
        async with SessionLocal() as cleanup_db:
            if tournament_id is not None:
                await cleanup_db.execute(
                    sa.delete(TournamentSnapshot).where(
                        TournamentSnapshot.tournament_id == tournament_id
                    )
                )
                await cleanup_db.execute(
                    sa.delete(TournamentMember).where(
                        TournamentMember.tournament_id == tournament_id
                    )
                )
                await cleanup_db.execute(
                    sa.delete(Tournament).where(Tournament.id == tournament_id)
                )
            if all_user_ids:
                await cleanup_db.execute(
                    sa.delete(Portfolio).where(
                        Portfolio.user_id.in_(all_user_ids)
                    )
                )
                await cleanup_db.execute(
                    sa.delete(User).where(User.id.in_(all_user_ids))
                )
            await cleanup_db.commit()
        await engine.dispose()


# ===========================================================================
# Test 3 — check_finished_fixtures (oracle update)
# ===========================================================================


async def test_oracle_e2e_invariants(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    E2E: check_finished_fixtures

    - RatingHistory row created with source='oracle_update'
    - oracle_rating updated to value in [3.0, 10.0] (INV-09 range enforced)
    - INV-09: q_up, q_down, current_rating NEVER modified
    - Completes in < 10 seconds
    """
    now = datetime.now(timezone.utc)

    player = Player(
        id=7701,
        name="E2E Oracle Player",
        team="Test FC",
        position="13",
        position_group="FW",
        league="Test League",
        league_id=8,
        is_active=True,
        last_synced_at=now,
    )
    db_session.add(player)

    # last_synced_at = now → within the 20-minute lookback window
    fixture = Fixture(
        id=9901,
        home_team="Home FC",
        away_team="Away FC",
        kickoff_time=now - timedelta(hours=2),
        status="finished",
        league_id=8,
        matchday=28,
        last_synced_at=now,
    )
    db_session.add(fixture)
    await db_session.flush()

    # PlayerMatchRating with a known Sportmonks rating
    db_session.add(PlayerMatchRating(
        player_id=7701,
        fixture_id=9901,
        sportmonks_rating=Decimal("8.50"),
        minutes_played=90,
        goals=2,
        assists=1,
        shots_on_target=5,
        key_passes=3,
        tackles=0,
        recorded_at=now,
    ))

    q_up_original = Decimal("150.000000")
    q_down_original = Decimal("50.000000")
    current_rating_original = Decimal("7.2000")

    market = LmsrMarketState(
        player_id=7701,
        alpha=Decimal("0.050000"),
        b_min=Decimal("100.0000"),
        q_up=q_up_original,
        q_down=q_down_original,
        current_rating=current_rating_original,
        oracle_rating=Decimal("7.2000"),
        oracle_source="layer_3",
        updated_at=now,
    )
    db_session.add(market)
    await db_session.flush()

    # Mock publish to avoid Redis dependency in test environment
    published: list[dict] = []

    async def mock_publish(player_id: Any, rating_before: Any, rating_after: Any, direction: Any) -> None:
        published.append({"player_id": player_id, "direction": direction})

    monkeypatch.setattr("app.workers.oracle_update.publish_rating_update", mock_publish)

    client = _mock_client([7701], sportmonks_rating=8.5)

    start = time.monotonic()
    updated = await check_finished_fixtures(db_session, client)
    elapsed = time.monotonic() - start

    assert updated >= 1, f"Expected at least 1 player updated, got {updated}"
    assert elapsed < 10.0, (
        f"check_finished_fixtures took {elapsed:.2f}s — must complete < 10 seconds"
    )

    # ── RatingHistory: source must be 'oracle_update' ───────────────────────
    history_rows = (
        await db_session.execute(
            sa.select(RatingHistory).where(
                RatingHistory.player_id == 7701,
                RatingHistory.source == "oracle_update",
            )
        )
    ).scalars().all()
    assert len(history_rows) >= 1, (
        "Expected at least 1 RatingHistory row with source='oracle_update'"
    )

    # ── Oracle rating in valid range [3.0, 10.0] ────────────────────────────
    await db_session.refresh(market)
    assert market.oracle_rating is not None
    oracle_val = float(market.oracle_rating)
    assert 3.0 <= oracle_val <= 10.0, (
        f"oracle_rating {oracle_val} is outside [3.0, 10.0] — INV-09 range violated"
    )
    assert market.oracle_source in ("layer_1", "layer_2", "layer_3")

    # ── INV-09: q_up, q_down, current_rating must NOT change ────────────────
    assert market.q_up == q_up_original, (
        f"INV-09 violated: q_up changed from {q_up_original} to {market.q_up}"
    )
    assert market.q_down == q_down_original, (
        f"INV-09 violated: q_down changed from {q_down_original} to {market.q_down}"
    )
    assert market.current_rating == current_rating_original, (
        f"INV-09 violated: current_rating changed from "
        f"{current_rating_original} to {market.current_rating}"
    )


async def test_oracle_e2e_skips_fixture_outside_lookback_window(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A finished fixture whose last_synced_at is older than the lookback window
    (default 20 minutes) must NOT trigger an oracle update.
    This guards against indefinite reprocessing of historical fixtures.
    """
    now = datetime.now(timezone.utc)

    player = Player(
        id=7702,
        name="E2E Stale Fixture Player",
        team="Test FC",
        position_group="FW",
        league_id=8,
        is_active=True,
        last_synced_at=now,
    )
    db_session.add(player)

    # last_synced_at = 30 minutes ago — outside the 20-minute window
    stale_fixture = Fixture(
        id=9902,
        home_team="Home FC",
        away_team="Away FC",
        kickoff_time=now - timedelta(hours=3),
        status="finished",
        league_id=8,
        matchday=28,
        last_synced_at=now - timedelta(minutes=30),
    )
    db_session.add(stale_fixture)
    # Flush player + fixture first so the FK on lmsr_market_state.player_id resolves.
    await db_session.flush()

    market = LmsrMarketState(
        player_id=7702,
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

    async def mock_publish(*args: Any, **kwargs: Any) -> None:
        pass

    monkeypatch.setattr("app.workers.oracle_update.publish_rating_update", mock_publish)

    client = _mock_client([7702])
    updated = await check_finished_fixtures(db_session, client)

    assert updated == 0, (
        f"Expected 0 players updated for stale fixture (>20 min old), got {updated}. "
        "The lookback window guard may be broken."
    )

"""
Tournament lifecycle workers — §7.5 and §7.6.

activate_pending_tournaments():
  Find pending tournaments whose start_time <= NOW(), lock each member's
  current total_value into tournament_members.starting_value, set status='active'.
  Uses batch queries (O(1) DB round-trips) + REPEATABLE READ isolation.

freeze_expired_tournaments():
  Find active tournaments whose end_time <= NOW(), compute final total_values,
  bulk-insert tournament_snapshots ranked by profit_loss DESC, set status='completed'.
  Uses batch queries (O(1) DB round-trips) + REPEATABLE READ isolation.

CRITICAL INVARIANTS:
  - No N+1 queries (loop-based per-member DB fetches are FORBIDDEN).
  - All batch reads wrapped in REPEATABLE READ to prevent snapshot tearing.
  - Portfolio valuation uses pure LS-LMSR math (sell_refund_up/down) — never
    linear price × shares (§2.6 Fake Portfolio Wealth prohibition).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.core.lmsr import effective_b, sell_refund_down, sell_refund_up
from app.models.market_state import LmsrMarketState
from app.models.portfolio import Portfolio
from app.models.position import Position
from app.models.tournament import Tournament
from app.models.tournament_member import TournamentMember
from app.models.tournament_snapshot import TournamentSnapshot
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

_QUANT = Decimal("0.0001")


def _compute_member_value(
    portfolio: Portfolio,
    positions: list[Position],
    markets_by_player: dict[int, LmsrMarketState],
) -> Decimal:
    """Compute total_value = available_points + Σ sell_refund(position) in memory.

    Uses pure LS-LMSR math functions — never linear price × shares (§2.6).
    """
    total = Decimal(str(portfolio.available_points))
    for pos in positions:
        market = markets_by_player.get(pos.player_id)
        if market is None:
            continue
        q_up = float(market.q_up)
        q_down = float(market.q_down)
        alpha = float(market.alpha)
        b_min = float(market.b_min)
        b = effective_b(b_min, alpha, q_up, q_down)
        shares = float(pos.shares_owned)
        if pos.direction == "UP":
            refund = sell_refund_up(q_up, q_down, b, shares)
        else:
            refund = sell_refund_down(q_up, q_down, b, shares)
        total += Decimal(str(refund)).quantize(_QUANT)
    return total.quantize(_QUANT)


async def activate_pending_tournaments(db: AsyncSession) -> int:
    """Activate pending tournaments whose start_time <= NOW().

    For each member, computes current global total_value in memory (batch) and
    locks it into tournament_members.starting_value.  Sets tournament status
    to 'active'.

    Performance: all DB reads are single IN-clause batch queries — no N+1.
    Isolation:   REPEATABLE READ prevents snapshot tearing between reads.

    Returns the number of tournaments activated.
    """
    now = datetime.now(timezone.utc)

    # Set REPEATABLE READ isolation to prevent concurrent-trade snapshot tearing
    try:
        await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    except Exception:
        logger.debug(
            "Could not set REPEATABLE READ (likely nested transaction in test context)"
        )

    # 1. Find pending tournaments past start_time
    t_res = await db.execute(
        sa.select(Tournament).where(
            Tournament.status == "pending",
            Tournament.start_time <= now,
        )
    )
    pending = t_res.scalars().all()
    if not pending:
        return 0

    tournament_ids = [t.id for t in pending]

    # 2. Batch fetch ALL members for these tournaments (single IN query)
    m_res = await db.execute(
        sa.select(TournamentMember).where(
            TournamentMember.tournament_id.in_(tournament_ids)
        )
    )
    all_members = m_res.scalars().all()

    if not all_members:
        for t in pending:
            t.status = "active"
        await db.flush()
        return len(pending)

    member_user_ids = list({m.user_id for m in all_members})

    # 3. Batch fetch ALL portfolios (single IN query — no N+1)
    p_res = await db.execute(
        sa.select(Portfolio).where(Portfolio.user_id.in_(member_user_ids))
    )
    all_portfolios = p_res.scalars().all()
    portfolio_by_user: dict[uuid.UUID, Portfolio] = {
        p.user_id: p for p in all_portfolios
    }
    portfolio_ids = [p.id for p in all_portfolios]

    # 4. Batch fetch ALL open positions (single IN query — no N+1)
    pos_res = await db.execute(
        sa.select(Position).where(
            Position.portfolio_id.in_(portfolio_ids),
            Position.shares_owned > 0,
        )
    )
    all_positions = pos_res.scalars().all()

    # 5. Batch fetch ALL relevant market states (single IN query — no N+1)
    player_ids = list({pos.player_id for pos in all_positions})
    markets_by_player: dict[int, LmsrMarketState] = {}
    if player_ids:
        mkt_res = await db.execute(
            sa.select(LmsrMarketState).where(
                LmsrMarketState.player_id.in_(player_ids)
            )
        )
        markets_by_player = {m.player_id: m for m in mkt_res.scalars().all()}

    # Group positions by portfolio_id for O(1) lookup
    positions_by_portfolio: dict[uuid.UUID, list[Position]] = {}
    for pos in all_positions:
        positions_by_portfolio.setdefault(pos.portfolio_id, []).append(pos)

    # 6. Compute total_value in memory; 7. Bulk-update starting_values
    for member in all_members:
        portfolio = portfolio_by_user.get(member.user_id)
        if portfolio is None:
            continue
        positions = positions_by_portfolio.get(portfolio.id, [])
        total_value = _compute_member_value(portfolio, positions, markets_by_player)
        member.starting_value = total_value

    # 8. Activate tournaments
    for t in pending:
        t.status = "active"

    await db.flush()
    return len(pending)


async def freeze_expired_tournaments(db: AsyncSession) -> int:
    """Freeze active tournaments whose end_time <= NOW().

    Computes each member's final total_value in memory (batch), bulk-inserts
    tournament_snapshots ranked by profit_loss DESC, sets status='completed'.

    Performance: all DB reads are single IN-clause batch queries — no N+1.
    Isolation:   REPEATABLE READ prevents concurrent-trade snapshot tearing.

    Returns the number of tournaments frozen.
    """
    now = datetime.now(timezone.utc)

    # Set REPEATABLE READ isolation
    try:
        await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    except Exception:
        logger.debug(
            "Could not set REPEATABLE READ (likely nested transaction in test context)"
        )

    # 1. Find active tournaments past end_time
    t_res = await db.execute(
        sa.select(Tournament).where(
            Tournament.status == "active",
            Tournament.end_time <= now,
        )
    )
    expired = t_res.scalars().all()
    if not expired:
        return 0

    tournament_ids = [t.id for t in expired]

    # 2. Batch fetch ALL members (single IN query)
    m_res = await db.execute(
        sa.select(TournamentMember).where(
            TournamentMember.tournament_id.in_(tournament_ids)
        )
    )
    all_members = m_res.scalars().all()

    member_user_ids = list({m.user_id for m in all_members})

    # 3. Batch fetch ALL portfolios (single IN query — no N+1)
    p_res = await db.execute(
        sa.select(Portfolio).where(Portfolio.user_id.in_(member_user_ids))
    )
    all_portfolios = p_res.scalars().all()
    portfolio_by_user: dict[uuid.UUID, Portfolio] = {
        p.user_id: p for p in all_portfolios
    }
    portfolio_ids = [p.id for p in all_portfolios]

    # 4. Batch fetch ALL open positions (single IN query — no N+1)
    pos_res = await db.execute(
        sa.select(Position).where(
            Position.portfolio_id.in_(portfolio_ids),
            Position.shares_owned > 0,
        )
    )
    all_positions = pos_res.scalars().all()

    # 5. Batch fetch ALL relevant market states (single IN query — no N+1)
    player_ids = list({pos.player_id for pos in all_positions})
    markets_by_player: dict[int, LmsrMarketState] = {}
    if player_ids:
        mkt_res = await db.execute(
            sa.select(LmsrMarketState).where(
                LmsrMarketState.player_id.in_(player_ids)
            )
        )
        markets_by_player = {m.player_id: m for m in mkt_res.scalars().all()}

    # Group positions by portfolio_id
    positions_by_portfolio: dict[uuid.UUID, list[Position]] = {}
    for pos in all_positions:
        positions_by_portfolio.setdefault(pos.portfolio_id, []).append(pos)

    # 6. Per tournament: compute values, rank by profit_loss, bulk-insert snapshots
    for tournament in expired:
        t_members = [m for m in all_members if m.tournament_id == tournament.id]

        member_values: list[tuple[TournamentMember, Decimal]] = []
        for member in t_members:
            portfolio = portfolio_by_user.get(member.user_id)
            if portfolio is None:
                # Member has no portfolio (edge case) — use 0
                total_value = Decimal("0")
            else:
                positions = positions_by_portfolio.get(portfolio.id, [])
                total_value = _compute_member_value(
                    portfolio, positions, markets_by_player
                )
            member_values.append((member, total_value))

        # Sort by profit_loss DESC
        member_values.sort(
            key=lambda x: float(x[1]) - float(x[0].starting_value),
            reverse=True,
        )

        # Bulk-insert tournament_snapshots
        for rank, (member, total_value) in enumerate(member_values, start=1):
            profit_loss = (total_value - member.starting_value).quantize(_QUANT)
            db.add(
                TournamentSnapshot(
                    tournament_id=tournament.id,
                    user_id=member.user_id,
                    rank=rank,
                    total_value=total_value,
                    starting_value=member.starting_value,
                    profit_loss=profit_loss,
                )
            )

        tournament.status = "completed"

    await db.flush()
    await db.commit()
    return len(expired)


# ---------------------------------------------------------------------------
# Celery task wrappers
# ---------------------------------------------------------------------------


def _make_session():
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@celery_app.task(name="app.workers.tournaments.activate_pending_tournaments_task")
def activate_pending_tournaments_task() -> dict:
    """Celery entry point for §7.5 tournament activation."""

    async def _run() -> int:
        engine, factory = _make_session()
        try:
            async with factory() as db:
                return await activate_pending_tournaments(db)
        finally:
            await engine.dispose()

    return {"activated": asyncio.run(_run())}


@celery_app.task(name="app.workers.tournaments.freeze_expired_tournaments_task")
def freeze_expired_tournaments_task() -> dict:
    """Celery entry point for §7.6 tournament freeze."""

    async def _run() -> int:
        engine, factory = _make_session()
        try:
            async with factory() as db:
                return await freeze_expired_tournaments(db)
        finally:
            await engine.dispose()

    return {"frozen": asyncio.run(_run())}

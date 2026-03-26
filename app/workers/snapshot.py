"""
Portfolio snapshot worker — §7.8.

snapshot_all_portfolios():
  Reads every user's current total_value (available_points + LS-LMSR sell_refunds)
  and bulk-inserts a row into portfolio_snapshots.  Used by the portfolio history
  endpoint (GET /api/portfolio/history) to build time-series charts.

Performance: batch queries only — no N+1 per user.
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
from app.models.portfolio_snapshot import PortfolioSnapshot
from app.models.position import Position
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

_QUANT = Decimal("0.0001")


def _compute_portfolio_value(
    portfolio: Portfolio,
    positions: list[Position],
    markets_by_player: dict[int, LmsrMarketState],
) -> Decimal:
    """Compute total_value = available_points + Σ sell_refund(position) in memory.

    Uses pure LS-LMSR math — never linear price × shares (§2.6).
    """
    total = Decimal(str(portfolio.available_points))
    for pos in positions:
        market = markets_by_player.get(pos.player_id)
        if market is None:
            continue
        q_up = float(market.q_up)
        q_down = float(market.q_down)
        b = effective_b(q_up, q_down, float(market.alpha), float(market.b_min))
        shares = float(pos.shares_owned)
        if pos.direction == "UP":
            refund = sell_refund_up(q_up, q_down, b, shares)
        else:
            refund = sell_refund_down(q_up, q_down, b, shares)
        total += Decimal(str(refund)).quantize(_QUANT)
    return total.quantize(_QUANT)


async def snapshot_all_portfolios(db: AsyncSession) -> int:
    """Snapshot every user's current portfolio value.

    All DB reads are single IN-clause batch queries — no N+1.

    Returns the number of snapshots inserted.
    """
    now = datetime.now(timezone.utc)

    # 1. Fetch all portfolios
    all_portfolios = (
        await db.execute(sa.select(Portfolio))
    ).scalars().all()

    if not all_portfolios:
        return 0

    portfolio_ids = [p.id for p in all_portfolios]
    portfolio_by_id: dict[uuid.UUID, Portfolio] = {p.id: p for p in all_portfolios}

    # 2. Batch fetch all open positions (single IN query)
    all_positions = (
        await db.execute(
            sa.select(Position).where(
                Position.portfolio_id.in_(portfolio_ids),
                Position.shares_owned > 0,
            )
        )
    ).scalars().all()

    # 3. Batch fetch market states for all relevant players
    player_ids = list({pos.player_id for pos in all_positions})
    markets_by_player: dict[int, LmsrMarketState] = {}
    if player_ids:
        markets_by_player = {
            m.player_id: m
            for m in (
                await db.execute(
                    sa.select(LmsrMarketState).where(
                        LmsrMarketState.player_id.in_(player_ids)
                    )
                )
            ).scalars().all()
        }

    # Group positions by portfolio_id
    positions_by_portfolio: dict[uuid.UUID, list[Position]] = {}
    for pos in all_positions:
        positions_by_portfolio.setdefault(pos.portfolio_id, []).append(pos)

    # 4. Compute values and bulk-insert snapshots
    for portfolio in all_portfolios:
        positions = positions_by_portfolio.get(portfolio.id, [])
        total_value = _compute_portfolio_value(portfolio, positions, markets_by_player)
        db.add(
            PortfolioSnapshot(
                user_id=portfolio.user_id,
                total_value=total_value,
                recorded_at=now,
            )
        )

    await db.flush()
    await db.commit()
    logger.info("Snapshotted %d portfolios at %s", len(all_portfolios), now.isoformat())
    return len(all_portfolios)


# ---------------------------------------------------------------------------
# Celery task wrapper
# ---------------------------------------------------------------------------


def _make_session():
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@celery_app.task(name="app.workers.snapshot.snapshot_all_portfolios_task")
def snapshot_all_portfolios_task() -> dict:
    """Celery entry point for §7.8 portfolio snapshots (runs every 60 minutes)."""

    async def _run() -> int:
        engine, factory = _make_session()
        try:
            async with factory() as db:
                return await snapshot_all_portfolios(db)
        finally:
            await engine.dispose()

    return {"snapshotted": asyncio.run(_run())}

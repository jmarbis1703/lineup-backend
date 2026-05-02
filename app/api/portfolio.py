"""Portfolio API endpoints."""
from datetime import datetime, timedelta, timezone
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lmsr import effective_b, sell_refund_down, sell_refund_up
from app.dependencies import get_current_user, get_db
from app.models.market_state import LmsrMarketState
from app.models.player import Player
from app.models.portfolio import Portfolio
from app.models.portfolio_snapshot import PortfolioSnapshot
from app.models.position import Position
from app.models.trade import Trade
from app.models.user import User
from app.schemas.portfolio import (
    PortfolioHistoryResponse,
    PortfolioPoint,
    PortfolioResponse,
    PositionOut,
    TradeHistoryEntry,
    TradeHistoryResponse,
)

router = APIRouter()


@router.get("/me", response_model=PortfolioResponse)
async def get_my_portfolio(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PortfolioResponse:
    """
    Return the authenticated user's single global portfolio.

    Unrealized PnL per position is computed via LS-LMSR sell_refund functions
    (not linear price × shares, which ignores AMM slippage — PRD §2.6).
    """
    portfolio = (
        await db.execute(
            sa.select(Portfolio).where(Portfolio.user_id == current_user.id)
        )
    ).scalar_one()

    rows = (
        await db.execute(
            sa.select(Position, LmsrMarketState)
            .join(LmsrMarketState, Position.player_id == LmsrMarketState.player_id)
            .where(
                Position.portfolio_id == portfolio.id,
                Position.shares_owned > 0,
            )
        )
    ).all()

    positions_out: list[PositionOut] = []
    total_refund = 0.0

    for position, market in rows:
        q_up = float(market.q_up)
        q_down = float(market.q_down)
        b = effective_b(float(market.b_min), float(market.alpha), q_up, q_down)
        shares = float(position.shares_owned)
        avg_entry = float(position.average_entry_price)

        if position.direction == "UP":
            refund = sell_refund_up(q_up, q_down, b, shares)
        else:
            refund = sell_refund_down(q_up, q_down, b, shares)

        # Cost basis = shares × average_entry_price (PRD §2.6)
        unrealized_pnl = refund - (shares * avg_entry)
        total_refund += refund

        positions_out.append(
            PositionOut(
                player_id=position.player_id,
                direction=position.direction,
                shares_owned=position.shares_owned,
                average_entry_price=position.average_entry_price,
                unrealized_pnl=unrealized_pnl,
            )
        )

    return PortfolioResponse(
        available_points=portfolio.available_points,
        total_value=float(portfolio.available_points) + total_refund,
        positions=positions_out,
    )


@router.get("/trades", response_model=TradeHistoryResponse)
async def get_trade_history(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TradeHistoryResponse:
    """Return paginated trade history for the authenticated user."""
    # Base join: trades → portfolios (to filter by user) → players (for name)
    base_stmt = (
        sa.select(Trade, Player.name.label("player_name"))
        .join(Portfolio, Trade.portfolio_id == Portfolio.id)
        .outerjoin(Player, Trade.player_id == Player.id)
        .where(Portfolio.user_id == current_user.id)
    )

    # Count query
    count_stmt = (
        sa.select(sa.func.count())
        .select_from(Trade)
        .join(Portfolio, Trade.portfolio_id == Portfolio.id)
        .where(Portfolio.user_id == current_user.id)
    )
    total = (await db.execute(count_stmt)).scalar_one()

    # Data query with pagination
    data_stmt = (
        base_stmt.order_by(Trade.timestamp.desc()).limit(limit).offset(offset)
    )
    rows = (await db.execute(data_stmt)).all()

    trades = [
        TradeHistoryEntry(
            id=str(trade.id),
            player_id=trade.player_id,
            player_name=player_name or "Unknown",
            type=trade.type,
            shares=float(trade.shares),
            cost_or_refund=float(trade.cost_or_refund),
            price_per_share=float(trade.price_per_share),
            timestamp=trade.timestamp.isoformat(),
        )
        for trade, player_name in rows
    ]

    return TradeHistoryResponse(trades=trades, total=total)


@router.get("/history", response_model=PortfolioHistoryResponse)
async def get_portfolio_history(
    timeframe: Literal["1D", "7D", "30D"] = Query(default="7D"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PortfolioHistoryResponse:
    """Return portfolio value history for the given timeframe.

    Data is populated by the hourly Celery snapshot task. Returns an empty
    list until the first snapshot has been recorded.
    """
    now = datetime.now(timezone.utc)
    if timeframe == "1D":
        since = now - timedelta(hours=24)
    elif timeframe == "7D":
        since = now - timedelta(days=7)
    else:  # 30D
        since = now - timedelta(days=30)

    rows = (
        await db.execute(
            sa.select(PortfolioSnapshot)
            .where(
                PortfolioSnapshot.user_id == current_user.id,
                PortfolioSnapshot.recorded_at >= since,
            )
            .order_by(PortfolioSnapshot.recorded_at.asc())
        )
    ).scalars().all()

    data = [
        PortfolioPoint(
            time=row.recorded_at.isoformat(),
            value=float(row.total_value),
        )
        for row in rows
    ]

    return PortfolioHistoryResponse(timeframe=timeframe, data=data)

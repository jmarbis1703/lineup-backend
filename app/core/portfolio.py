"""
Portfolio valuation: calculate_total_value.

total_value = available_points + Σ sell_refund(position) for all open positions.

Uses pure LS-LMSR math from app/core/lmsr.py — no linear multiplication
(which ignores AMM slippage and produces Fake Portfolio Wealth, PRD §2.6).
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lmsr import effective_b, sell_refund_down, sell_refund_up
from app.models.market_state import LmsrMarketState
from app.models.portfolio import Portfolio
from app.models.position import Position

_VALUE_QUANT = Decimal("0.000001")


async def calculate_total_value(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> Decimal:
    """
    Return the user's total portfolio value:

        total_value = available_points + Σ sell_refund(each open position)

    Each position's sell_refund is computed via the LS-LMSR math functions
    to account for AMM slippage (linear price × shares is forbidden, §2.6).
    """
    port_result = await db.execute(
        sa.select(Portfolio).where(Portfolio.user_id == user_id)
    )
    portfolio = port_result.scalar_one()

    total = portfolio.available_points

    # Fetch all open positions with their market state in one join
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

    for position, market in rows:
        q_up_f = float(market.q_up)
        q_down_f = float(market.q_down)
        alpha_f = float(market.alpha)
        b_min_f = float(market.b_min)
        b = effective_b(b_min_f, alpha_f, q_up_f, q_down_f)
        shares_f = float(position.shares_owned)

        if position.direction == "UP":
            refund_f = sell_refund_up(q_up_f, q_down_f, b, shares_f)
        else:
            refund_f = sell_refund_down(q_up_f, q_down_f, b, shares_f)

        total += Decimal(str(refund_f)).quantize(_VALUE_QUANT)

    return total

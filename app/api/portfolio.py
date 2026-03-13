"""Portfolio API endpoints."""
import sqlalchemy as sa
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lmsr import effective_b, sell_refund_down, sell_refund_up
from app.dependencies import get_current_user, get_db
from app.models.market_state import LmsrMarketState
from app.models.portfolio import Portfolio
from app.models.position import Position
from app.models.user import User
from app.schemas.portfolio import PortfolioResponse, PositionOut

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
        b = effective_b(q_up, q_down, float(market.alpha), float(market.b_min))
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

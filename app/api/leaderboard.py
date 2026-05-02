from collections import defaultdict

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lmsr import effective_b, sell_refund_down, sell_refund_up
from app.dependencies import get_db
from app.models.market_state import LmsrMarketState
from app.models.portfolio import Portfolio
from app.models.position import Position
from app.models.user import User
from app.schemas.leaderboard import LeaderboardEntry

router = APIRouter()


@router.get("", response_model=list[LeaderboardEntry])
async def get_leaderboard(db: AsyncSession = Depends(get_db)) -> list[LeaderboardEntry]:
    """
    Return the global top-50 users ranked by live total_value DESC.

    Batch-fetches all market states, portfolios, and positions in three
    queries to avoid N+1 (PRD §7.5 batch optimization guidance).
    """
    portfolio_rows = (
        await db.execute(
            sa.select(Portfolio, User).join(User, Portfolio.user_id == User.id)
        )
    ).all()

    market_map: dict[int, LmsrMarketState] = {
        m.player_id: m
        for m in (await db.execute(sa.select(LmsrMarketState))).scalars().all()
    }

    position_rows = (
        await db.execute(sa.select(Position).where(Position.shares_owned > 0))
    ).scalars().all()

    positions_by_portfolio: dict = defaultdict(list)
    for pos in position_rows:
        positions_by_portfolio[pos.portfolio_id].append(pos)

    entries: list[tuple[str, float]] = []
    for portfolio, user in portfolio_rows:
        total = float(portfolio.available_points)
        for pos in positions_by_portfolio.get(portfolio.id, []):
            market = market_map.get(pos.player_id)
            if market is None:
                continue
            q_up = float(market.q_up)
            q_down = float(market.q_down)
            b = effective_b(float(market.b_min), float(market.alpha), q_up, q_down)
            shares = float(pos.shares_owned)
            if pos.direction == "UP":
                refund = sell_refund_up(q_up, q_down, b, shares)
            else:
                refund = sell_refund_down(q_up, q_down, b, shares)
            total += refund
        entries.append((user.username, total))

    entries.sort(key=lambda x: x[1], reverse=True)
    entries = entries[:50]

    return [
        LeaderboardEntry(rank=i + 1, username=name, total_value=val)
        for i, (name, val) in enumerate(entries)
    ]

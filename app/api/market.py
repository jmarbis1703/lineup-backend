from typing import Optional

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lmsr import effective_b
from app.dependencies import get_db
from app.models.market_state import LmsrMarketState
from app.models.player import Player
from app.models.rating_history import RatingHistory
from app.schemas.market import ChartPoint, PlayerMarketResponse

router = APIRouter()


def _build_response(player: Player, market: LmsrMarketState) -> PlayerMarketResponse:
    b_eff = effective_b(
        float(market.q_up),
        float(market.q_down),
        float(market.alpha),
        float(market.b_min),
    )
    return PlayerMarketResponse(
        id=player.id,
        name=player.name,
        team=player.team,
        position_group=player.position_group,
        league=player.league,
        league_id=player.league_id,
        photo_url=player.photo_url,
        current_rating=market.current_rating,
        oracle_rating=market.oracle_rating,
        oracle_source=market.oracle_source,
        q_up=market.q_up,
        q_down=market.q_down,
        b_effective=b_eff,
        total_shares=market.q_up + market.q_down,
        is_active=player.is_active,
    )


@router.get("/players", response_model=list[PlayerMarketResponse])
async def get_players(
    league_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> list[PlayerMarketResponse]:
    stmt = (
        sa.select(Player, LmsrMarketState)
        .join(LmsrMarketState, Player.id == LmsrMarketState.player_id)
        .where(Player.is_active.is_(True))
    )
    if league_id is not None:
        stmt = stmt.where(Player.league_id == league_id)
    if search:
        stmt = stmt.where(Player.name.ilike(f"%{search}%"))

    rows = (await db.execute(stmt)).all()
    return [_build_response(player, market) for player, market in rows]


@router.get("/players/{player_id}", response_model=PlayerMarketResponse)
async def get_player(
    player_id: int,
    db: AsyncSession = Depends(get_db),
) -> PlayerMarketResponse:
    """
    Returns a single player's market data.
    Returns 200 even when is_active=False (close-only mode — PRD §5.1):
    delisted player data must remain accessible for sell-order routing.
    """
    row = (
        await db.execute(
            sa.select(Player, LmsrMarketState)
            .join(LmsrMarketState, Player.id == LmsrMarketState.player_id)
            .where(Player.id == player_id)
        )
    ).one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail="Player not found")

    return _build_response(row[0], row[1])


@router.get("/players/{player_id}/chart", response_model=list[ChartPoint])
async def get_player_chart(
    player_id: int,
    db: AsyncSession = Depends(get_db),
) -> list[ChartPoint]:
    player_exists = (
        await db.execute(sa.select(Player.id).where(Player.id == player_id))
    ).scalar_one_or_none()
    if player_exists is None:
        raise HTTPException(status_code=404, detail="Player not found")

    rows = (
        await db.execute(
            sa.select(RatingHistory)
            .where(RatingHistory.player_id == player_id)
            .order_by(RatingHistory.recorded_at.asc())
        )
    ).scalars().all()

    return [
        ChartPoint(rating=rh.rating, source=rh.source, recorded_at=rh.recorded_at)
        for rh in rows
    ]

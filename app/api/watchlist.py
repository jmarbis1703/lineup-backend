"""Watchlist endpoints — saved players per user."""
import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models.player import Player
from app.models.user import User
from app.models.watchlist import Watchlist
from app.schemas.watchlist import WatchlistAddRequest, WatchlistResponse

router = APIRouter()


@router.get("", response_model=WatchlistResponse)
async def get_watchlist(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WatchlistResponse:
    """Return the authenticated user's watchlist as a list of player IDs."""
    rows = (
        await db.execute(
            sa.select(Watchlist.player_id)
            .where(Watchlist.user_id == current_user.id)
            .order_by(Watchlist.added_at.desc())
        )
    ).scalars().all()
    return WatchlistResponse(player_ids=list(rows))


@router.post("", status_code=201)
async def add_to_watchlist(
    body: WatchlistAddRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Add a player to the watchlist. Idempotent — duplicate adds are ignored."""
    # Verify player exists
    player_exists = (
        await db.execute(
            sa.select(Player.id).where(Player.id == body.player_id)
        )
    ).scalar_one_or_none()
    if player_exists is None:
        raise HTTPException(status_code=404, detail="Player not found")

    # INSERT ... ON CONFLICT DO NOTHING
    stmt = (
        pg_insert(Watchlist)
        .values(user_id=current_user.id, player_id=body.player_id)
        .on_conflict_do_nothing(constraint="uq_watchlist_user_player")
    )
    await db.execute(stmt)
    await db.flush()

    return {"player_id": body.player_id}


@router.delete("/{player_id}", status_code=204)
async def remove_from_watchlist(
    player_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Remove a player from the watchlist. Idempotent — no error if not found."""
    await db.execute(
        sa.delete(Watchlist).where(
            Watchlist.user_id == current_user.id,
            Watchlist.player_id == player_id,
        )
    )
    await db.flush()
    return Response(status_code=204)

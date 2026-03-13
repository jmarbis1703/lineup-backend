import uuid
from decimal import Decimal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.portfolio import calculate_total_value
from app.dependencies import get_current_user, get_db
from app.models.tournament import Tournament
from app.models.tournament_member import TournamentMember
from app.models.tournament_snapshot import TournamentSnapshot
from app.models.user import User
from app.schemas.tournament import (
    JoinResponse,
    TournamentCreate,
    TournamentLeaderboardEntry,
    TournamentResponse,
)

router = APIRouter()


@router.post("", response_model=TournamentResponse, status_code=201)
async def create_tournament(
    data: TournamentCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Tournament:
    """Create a new custom tournament. Starts as 'pending'."""
    tournament = Tournament(
        name=data.name,
        status="pending",
        created_by=current_user.id,
        start_time=data.start_time,
        end_time=data.end_time,
    )
    db.add(tournament)
    await db.flush()
    return tournament


@router.get("/mine", response_model=list[TournamentResponse])
async def get_my_tournaments(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Tournament]:
    """Return all tournaments the current user has joined."""
    rows = (
        await db.execute(
            sa.select(Tournament)
            .join(TournamentMember, Tournament.id == TournamentMember.tournament_id)
            .where(TournamentMember.user_id == current_user.id)
            .order_by(Tournament.created_at.desc())
        )
    ).scalars().all()
    return list(rows)


@router.post("/{tournament_id}/join", response_model=JoinResponse)
async def join_tournament(
    tournament_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JoinResponse:
    """
    Join a tournament.

    - If tournament is 'active': locks starting_value immediately to the
      user's current total_value (PRD §5.4).
    - If tournament is 'pending': sets starting_value = 0 (placeholder;
      overwritten by activate_pending_tournaments task at start_time).
    - 409 if the user is already a member.
    """
    tournament = (
        await db.execute(
            sa.select(Tournament).where(Tournament.id == tournament_id)
        )
    ).scalar_one_or_none()

    if tournament is None:
        raise HTTPException(status_code=404, detail="Tournament not found")

    existing = (
        await db.execute(
            sa.select(TournamentMember).where(
                TournamentMember.tournament_id == tournament_id,
                TournamentMember.user_id == current_user.id,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        raise HTTPException(status_code=409, detail="Already a member of this tournament")

    if tournament.status == "active":
        starting_value = await calculate_total_value(db, current_user.id)
    else:
        starting_value = Decimal("0")

    member = TournamentMember(
        tournament_id=tournament_id,
        user_id=current_user.id,
        starting_value=starting_value,
    )
    db.add(member)
    await db.flush()

    return JoinResponse(
        tournament_id=tournament_id,
        user_id=current_user.id,
        starting_value=starting_value,
        message="Joined successfully",
    )


@router.get("/{tournament_id}/leaderboard", response_model=list[TournamentLeaderboardEntry])
async def get_tournament_leaderboard(
    tournament_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TournamentLeaderboardEntry]:
    """
    Return the tournament leaderboard ranked by profit_loss (Δ total_value) DESC.

    - 'pending' tournaments → 400 (no rankings yet).
    - 'completed' tournaments → frozen snapshot data.
    - 'active' tournaments → live computation.
    """
    tournament = (
        await db.execute(
            sa.select(Tournament).where(Tournament.id == tournament_id)
        )
    ).scalar_one_or_none()

    if tournament is None:
        raise HTTPException(status_code=404, detail="Tournament not found")

    if tournament.status == "pending":
        raise HTTPException(status_code=400, detail="Tournament not yet active")

    if tournament.status == "completed":
        snapshot_rows = (
            await db.execute(
                sa.select(TournamentSnapshot, User)
                .join(User, TournamentSnapshot.user_id == User.id)
                .where(TournamentSnapshot.tournament_id == tournament_id)
                .order_by(TournamentSnapshot.rank.asc())
            )
        ).all()
        return [
            TournamentLeaderboardEntry(
                rank=snap.rank,
                user_id=user.id,
                username=user.username,
                starting_value=snap.starting_value,
                current_total_value=float(snap.total_value),
                profit_loss=float(snap.profit_loss),
            )
            for snap, user in snapshot_rows
        ]

    # Active tournament: compute live profit_loss for each member
    member_rows = (
        await db.execute(
            sa.select(TournamentMember, User)
            .join(User, TournamentMember.user_id == User.id)
            .where(TournamentMember.tournament_id == tournament_id)
        )
    ).all()

    entries: list[dict] = []
    for member, user in member_rows:
        current_value = float(await calculate_total_value(db, user.id))
        profit_loss = current_value - float(member.starting_value)
        entries.append(
            {
                "user_id": user.id,
                "username": user.username,
                "starting_value": member.starting_value,
                "current_total_value": current_value,
                "profit_loss": profit_loss,
            }
        )

    entries.sort(key=lambda x: x["profit_loss"], reverse=True)

    return [
        TournamentLeaderboardEntry(rank=i + 1, **entry)
        for i, entry in enumerate(entries)
    ]

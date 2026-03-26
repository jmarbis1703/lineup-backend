import secrets
import string
import uuid
from decimal import Decimal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rate_limit import _get_user_id_key, limiter

from app.core.portfolio import calculate_total_value
from app.dependencies import get_current_user, get_db
from app.models.tournament import Tournament
from app.models.tournament_member import TournamentMember
from app.models.tournament_snapshot import TournamentSnapshot
from app.models.user import User
from app.schemas.tournament import (
    JoinByIdRequest,
    JoinResponse,
    TournamentCreate,
    TournamentLeaderboardEntry,
    TournamentPublicResponse,
    TournamentResponse,
)

router = APIRouter()


def generate_invite_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


def _tournament_to_response(t: Tournament, participant_count: int = 0) -> TournamentResponse:
    return TournamentResponse(
        id=t.id,
        name=t.name,
        status=t.status,
        created_by=t.created_by,
        start_time=t.start_time,
        end_time=t.end_time,
        created_at=t.created_at,
        participant_count=participant_count,
        invite_code=t.invite_code,
    )


def _tournament_to_public_response(t: Tournament, participant_count: int = 0) -> TournamentPublicResponse:
    return TournamentPublicResponse(
        id=t.id,
        name=t.name,
        status=t.status,
        created_by=t.created_by,
        start_time=t.start_time,
        end_time=t.end_time,
        created_at=t.created_at,
        participant_count=participant_count,
    )


@router.get("", response_model=list[TournamentPublicResponse])
async def list_tournaments(db: AsyncSession = Depends(get_db)) -> list[TournamentPublicResponse]:
    """Return all tournaments (public, no auth required). invite_code is excluded."""
    count_subq = (
        sa.select(sa.func.count(TournamentMember.id))
        .where(TournamentMember.tournament_id == Tournament.id)
        .correlate(Tournament)
        .scalar_subquery()
    )
    rows = (
        await db.execute(
            sa.select(Tournament, count_subq.label("participant_count"))
            .order_by(Tournament.created_at.desc())
        )
    ).all()
    return [_tournament_to_public_response(t, participant_count=count) for t, count in rows]


@router.post("", response_model=TournamentResponse, status_code=201)
@limiter.limit("10/hour", key_func=_get_user_id_key)
async def create_tournament(
    request: Request,
    data: TournamentCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TournamentResponse:
    """Create a new custom tournament. Starts as 'pending'."""
    # T-3: Cap active tournaments per user to prevent spam.
    active_count = await db.scalar(
        sa.select(sa.func.count()).where(
            Tournament.created_by == current_user.id,
            Tournament.status != "completed",
        )
    )
    if active_count >= 10:
        raise HTTPException(status_code=400, detail="Maximum of 10 active tournaments per user")

    tournament = Tournament(
        name=data.name,
        status="pending",
        created_by=current_user.id,
        start_time=data.start_time,
        end_time=data.end_time,
        invite_code=generate_invite_code(),
    )
    db.add(tournament)
    await db.flush()
    await db.commit()
    # Newly created tournament has 0 participants
    return _tournament_to_response(tournament, participant_count=0)


@router.get("/mine", response_model=list[TournamentResponse])
async def get_my_tournaments(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TournamentResponse]:
    """Return all tournaments the current user has joined."""
    # Correlated subquery for participant count
    count_subq = (
        sa.select(sa.func.count(TournamentMember.id))
        .where(TournamentMember.tournament_id == Tournament.id)
        .correlate(Tournament)
        .scalar_subquery()
    )

    rows = (
        await db.execute(
            sa.select(Tournament, count_subq.label("participant_count"))
            .join(TournamentMember, Tournament.id == TournamentMember.tournament_id)
            .where(TournamentMember.user_id == current_user.id)
            .order_by(Tournament.created_at.desc())
        )
    ).all()

    return [
        _tournament_to_response(t, participant_count=count)
        for t, count in rows
    ]


@router.post("/join/{code}", response_model=JoinResponse)
@limiter.limit("20/hour", key_func=_get_user_id_key)
async def join_tournament_by_code(
    request: Request,
    code: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JoinResponse:
    """
    Join a tournament by invite code.

    - If tournament is 'active': locks starting_value immediately.
    - If tournament is 'pending': sets starting_value = 0.
    - 404 if code is invalid, 409 if already a member.
    """
    tournament = (
        await db.execute(
            sa.select(Tournament).where(Tournament.invite_code == code.upper())
        )
    ).scalar_one_or_none()

    if tournament is None:
        raise HTTPException(status_code=404, detail="Invalid invite code")

    # T-2: Prevent creator from joining their own tournament.
    if tournament.created_by == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot join your own tournament")

    existing = (
        await db.execute(
            sa.select(TournamentMember).where(
                TournamentMember.tournament_id == tournament.id,
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
        tournament_id=tournament.id,
        user_id=current_user.id,
        starting_value=starting_value,
    )
    db.add(member)
    await db.flush()
    await db.commit()

    return JoinResponse(
        tournament_id=tournament.id,
        user_id=current_user.id,
        starting_value=starting_value,
        message="Joined successfully",
    )


@router.post("/{tournament_id}/join", response_model=JoinResponse)
@limiter.limit("20/hour", key_func=_get_user_id_key)
async def join_tournament(
    request: Request,
    tournament_id: uuid.UUID,
    data: JoinByIdRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JoinResponse:
    """
    Join a tournament by UUID.

    Requires the tournament's invite_code in the request body (SEC-07).
    - If tournament is 'active': locks starting_value immediately to the
      user's current total_value (PRD §5.4).
    - If tournament is 'pending': sets starting_value = 0 (placeholder;
      overwritten by activate_pending_tournaments task at start_time).
    - 403 if the invite code does not match.
    - 409 if the user is already a member.
    """
    tournament = (
        await db.execute(
            sa.select(Tournament).where(Tournament.id == tournament_id)
        )
    ).scalar_one_or_none()

    if tournament is None:
        raise HTTPException(status_code=404, detail="Tournament not found")

    # SEC-07: Verify the caller knows the invite code even when using the UUID route.
    if data.invite_code.upper() != (tournament.invite_code or "").upper():
        raise HTTPException(status_code=403, detail="Invalid invite code")

    # T-2: Prevent creator from joining their own tournament.
    if tournament.created_by == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot join your own tournament")

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
    await db.commit()

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

    # T-1: Only members may view a tournament leaderboard.
    membership_check = await db.execute(
        sa.select(TournamentMember).where(
            TournamentMember.tournament_id == tournament_id,
            TournamentMember.user_id == current_user.id,
        )
    )
    if membership_check.scalar_one_or_none() is None:
        raise HTTPException(status_code=403, detail="Not a member of this tournament")

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
                display_name=user.clerk_id[:8],
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
                "display_name": user.clerk_id[:8],
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

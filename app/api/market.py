import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.lmsr import effective_b, compute_volatility_tiers
from app.core.oracle import compute_layer1_rating
from app.services.redis_cache import (
    get_volatility_tier_from_cache,
    write_volatility_tier_cache,
)

from app.dependencies import get_db
from app.models.fixture import Fixture
from app.models.market_state import LmsrMarketState
from app.models.player import Player
from app.models.player_match_rating import PlayerMatchRating
from app.models.rating_history import RatingHistory
from app.schemas.market import (
    ChartPoint,
    MatchFormEntry,
    PlayerMarketResponse,
    PlayerMatchFormEntry,
    PlayerStatsResponse,
    PublicPlayerResponse,
)

router = APIRouter()


async def _get_stats_map(
    db: AsyncSession, player_ids: list[int]
) -> dict[int, dict]:
    """Single bulk query for last-5 per-match stats per player.  No N+1.

    Returns {player_id: stats_dict}.  Players with no match data are absent
    from the result; callers should use .get(pid) and fall back to defaults.
    """
    if not player_ids:
        return {}

    # One query: all non-sentinel PMR rows for the given players ordered
    # newest-first within each player.  We cap at 5 per player in Python.
    rows = (
        await db.execute(
            sa.select(PlayerMatchRating)
            .join(Fixture, PlayerMatchRating.fixture_id == Fixture.id)
            .where(
                PlayerMatchRating.player_id.in_(player_ids),
                PlayerMatchRating.fixture_id != 0,
            )
            .order_by(PlayerMatchRating.player_id, Fixture.kickoff_time.desc())
        )
    ).scalars().all()

    # Group, keeping newest-first and capping at 5
    per_player: dict[int, list[PlayerMatchRating]] = {}
    for pmr in rows:
        bucket = per_player.setdefault(pmr.player_id, [])
        if len(bucket) < 5:
            bucket.append(pmr)

    result: dict[int, dict] = {}
    for pid, pmr_rows in per_player.items():
        # pmr_rows are newest-first
        goals = sum(r.goals or 0 for r in pmr_rows)
        assists = sum(r.assists or 0 for r in pmr_rows)
        tackles = sum(r.tackles or 0 for r in pmr_rows)
        goals_conceded = sum(r.goals_conceded or 0 for r in pmr_rows)
        minutes_played = sum(r.minutes_played or 0 for r in pmr_rows)
        n = len(pmr_rows)
        minutes_per_game = (
            round(minutes_played / n) if n > 0 and minutes_played > 0 else None
        )
        sportmonks_ratings = [
            float(r.sportmonks_rating) for r in pmr_rows if r.sportmonks_rating is not None
        ]
        rating = compute_layer1_rating(sportmonks_ratings) or 0.0
        # recent_form: oldest-first per-match form objects
        recent_form = [
            PlayerMatchFormEntry(
                # Use fixture kickoff date (§9B); fall back to recorded_at for pre-0006 rows where match_date is NULL
                match_date=(r.match_date or r.recorded_at).strftime("%Y-%m-%d") if (r.match_date or r.recorded_at) else "",
                goals=r.goals or 0,
                assists=r.assists or 0,
                minutes=r.minutes_played or 0,
                rating=float(r.sportmonks_rating) if r.sportmonks_rating is not None else 0.0,
            )
            for r in reversed(pmr_rows)
            if r.sportmonks_rating is not None
        ]
        result[pid] = {
            "recent_form": recent_form,
            "goals": goals,
            "assists": assists,
            "minutes_per_game": minutes_per_game,
            "tackles": tackles,
            "goals_conceded": goals_conceded,
            "rating": rating,
        }

    return result


async def _get_change_24h_map(
    db: AsyncSession, player_ids: list[int]
) -> dict[int, float | None]:
    """Returns {player_id: change_24h_pct} for a list of player IDs.

    Fetches the most recent rating_history row per player recorded > 24 hours
    ago (single batched query — no N+1). Returns None for players with no
    rating_history older than 24h.
    """
    if not player_ids:
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    # Fetch all qualifying rows sorted by (player_id, recorded_at DESC),
    # then keep only the first (most-recent) row per player in Python.
    # This uses the existing ix_rating_history_player_recorded index efficiently.
    history_rows = (
        await db.execute(
            sa.select(RatingHistory.player_id, RatingHistory.rating)
            .where(
                RatingHistory.player_id.in_(player_ids),
                RatingHistory.recorded_at <= cutoff,
                RatingHistory.source.in_(["trade", "market_init"]),
            )
            .order_by(RatingHistory.player_id, RatingHistory.recorded_at.desc())
        )
    ).all()

    old_rating_by_player: dict[int, float] = {}
    for row in history_rows:
        if row.player_id not in old_rating_by_player:
            old_rating_by_player[row.player_id] = float(row.rating)

    # Get current ratings to compute percentage change
    current_rows = (
        await db.execute(
            sa.select(LmsrMarketState.player_id, LmsrMarketState.current_rating)
            .where(LmsrMarketState.player_id.in_(player_ids))
        )
    ).all()
    current_by_player: dict[int, float] = {
        row.player_id: float(row.current_rating) for row in current_rows
    }

    result: dict[int, float | None] = {}
    for pid in player_ids:
        old = old_rating_by_player.get(pid)
        current = current_by_player.get(pid)
        if old is None or current is None or old == 0:
            result[pid] = None
        else:
            result[pid] = (current - old) / old * 100.0

    return result


_LEAGUE_ID_TO_NAME: dict[int, str] = {
    8: "Premier League",
    564: "La Liga",
    384: "Serie A",
    301: "Ligue 1",
    82: "Bundesliga",
}


def _build_response(
    player: Player,
    market: LmsrMarketState,
    change_24h: Optional[float] = None,
    volatility_tier: str = "medium",
    stats: Optional[dict] = None,
) -> PlayerMarketResponse:
    b_eff = effective_b(
        float(market.q_up),
        float(market.q_down),
        float(market.alpha),
        float(market.b_min),
    )
    league = player.league or (
        _LEAGUE_ID_TO_NAME.get(player.league_id) if player.league_id else None
    )
    s = stats or {}
    return PlayerMarketResponse(
        id=player.id,
        name=player.name,
        team=player.team,
        position_group=player.position_group,
        league=league,
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
        change_24h=change_24h,
        bio=player.bio,
        play_style=player.play_style,
        volatility_tier=volatility_tier,
        recent_form=s.get("recent_form", []),
        goals=s.get("goals", 0),
        assists=s.get("assists", 0),
        minutes_per_game=s.get("minutes_per_game"),
        tackles=s.get("tackles", 0),
        goals_conceded=s.get("goals_conceded", 0),
        rating=s.get("rating", 0.0),
    )


@router.get("/players/public", response_model=list[PublicPlayerResponse])
async def get_public_players(
    limit: int = Query(default=10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[PublicPlayerResponse]:
    """Public (unauthenticated) player list for the landing page ticker.

    Registered BEFORE /players/{player_id} to prevent FastAPI treating
    'public' as an integer player_id.
    """
    stmt = (
        sa.select(Player, LmsrMarketState)
        .join(LmsrMarketState, Player.id == LmsrMarketState.player_id)
        .where(Player.is_active.is_(True))
        .order_by(LmsrMarketState.current_rating.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()

    player_ids = [player.id for player, _ in rows]
    change_map = await _get_change_24h_map(db, player_ids)

    return [
        PublicPlayerResponse(
            name=player.name,
            team=player.team,
            rating=float(market.current_rating),
            change_24h=change_map.get(player.id),
        )
        for player, market in rows
    ]


@router.get("/trending", response_model=list[PlayerMarketResponse])
async def get_trending_players(
    limit: int = Query(default=8, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> list[PlayerMarketResponse]:
    """Top N active players sorted by 24h rating change (descending).

    Public — no auth required.
    """
    stmt = (
        sa.select(Player, LmsrMarketState)
        .join(LmsrMarketState, Player.id == LmsrMarketState.player_id)
        .where(Player.is_active.is_(True))
    )
    rows = (await db.execute(stmt)).all()

    player_ids = [player.id for player, _ in rows]
    change_map = await _get_change_24h_map(db, player_ids)

    b_eff_map: dict[int, float] = {
        player.id: effective_b(
            float(market.q_up), float(market.q_down),
            float(market.alpha), float(market.b_min),
        )
        for player, market in rows
    }
    tier_map = compute_volatility_tiers(b_eff_map)
    asyncio.create_task(write_volatility_tier_cache(tier_map, settings.redis_url))

    # Sort by change_24h descending; players with no history go last
    sorted_rows = sorted(
        rows,
        key=lambda r: change_map.get(r[0].id) or float("-inf"),
        reverse=True,
    )

    return [
        _build_response(
            player, market,
            change_24h=change_map.get(player.id),
            volatility_tier=tier_map.get(player.id, "medium"),
        )
        for player, market in sorted_rows[:limit]
    ]


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

    player_ids = [player.id for player, _ in rows]
    change_map = await _get_change_24h_map(db, player_ids)
    stats_map = await _get_stats_map(db, player_ids)

    b_eff_map: dict[int, float] = {
        player.id: effective_b(
            float(market.q_up), float(market.q_down),
            float(market.alpha), float(market.b_min),
        )
        for player, market in rows
    }
    tier_map = compute_volatility_tiers(b_eff_map)
    asyncio.create_task(write_volatility_tier_cache(tier_map, settings.redis_url))

    return [
        _build_response(
            player, market,
            change_24h=change_map.get(player.id),
            volatility_tier=tier_map.get(player.id, "medium"),
            stats=stats_map.get(player.id),
        )
        for player, market in rows
    ]


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

    change_map = await _get_change_24h_map(db, [player_id])
    stats_map = await _get_stats_map(db, [player_id])

    # Try Redis cache first (warmed by /players and /trending on every bulk call).
    # On cache miss (key expired, cold start, or Redis unavailable), load the full
    # active-player population to compute the population-relative tier.
    volatility_tier = await get_volatility_tier_from_cache(player_id, settings.redis_url)
    if volatility_tier is None:
        all_rows = (
            await db.execute(
                sa.select(Player.id, LmsrMarketState)
                .join(LmsrMarketState, Player.id == LmsrMarketState.player_id)
                .where(Player.is_active.is_(True))
            )
        ).all()
        b_eff_map_all: dict[int, float] = {
            pid: effective_b(
                float(ms.q_up), float(ms.q_down),
                float(ms.alpha), float(ms.b_min),
            )
            for pid, ms in all_rows
        }
        full_tier_map = compute_volatility_tiers(b_eff_map_all)
        # Warm the cache for all active players (fire-and-forget)
        asyncio.create_task(write_volatility_tier_cache(full_tier_map, settings.redis_url))
        # Inactive players (close-only mode §5.1) are absent from all_rows — fall back to "medium"
        volatility_tier = full_tier_map.get(player_id, "medium")

    return _build_response(
        row[0], row[1],
        change_24h=change_map.get(player_id),
        volatility_tier=volatility_tier,
        stats=stats_map.get(player_id),
    )


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

    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    rows = (
        await db.execute(
            sa.select(RatingHistory)
            .where(
                RatingHistory.player_id == player_id,
                RatingHistory.recorded_at >= cutoff,
            )
            .order_by(RatingHistory.recorded_at.asc())
        )
    ).scalars().all()

    return [
        ChartPoint(rating=rh.rating, source=rh.source, recorded_at=rh.recorded_at)
        for rh in rows
    ]


@router.get("/players/{player_id}/stats", response_model=PlayerStatsResponse)
async def get_player_stats(
    player_id: int,
    db: AsyncSession = Depends(get_db),
) -> PlayerStatsResponse:
    """Last-5-match aggregated stats for a player. Public — no auth required.

    Returns zeros/empty when no match data exists (not 404).
    vaep is 0.0 until Sportmonks event-level VAEP data is available.
    """
    player_exists = (
        await db.execute(sa.select(Player.id).where(Player.id == player_id))
    ).scalar_one_or_none()
    if player_exists is None:
        raise HTTPException(status_code=404, detail="Player not found")

    # Fetch last 5 player_match_ratings ordered by fixture kickoff_time DESC.
    # Also join Player to derive opponent/home_away for MatchFormEntry.
    # Exclude fixture_id=0 (seed sentinel — season totals, not per-match).
    raw_rows = (
        await db.execute(
            sa.select(PlayerMatchRating, Fixture, Player)
            .join(Fixture, PlayerMatchRating.fixture_id == Fixture.id)
            .join(Player, PlayerMatchRating.player_id == Player.id)
            .where(
                PlayerMatchRating.player_id == player_id,
                PlayerMatchRating.fixture_id != 0,
            )
            .order_by(Fixture.kickoff_time.desc())
            .limit(5)
        )
    ).all()

    if not raw_rows:
        return PlayerStatsResponse(
            player_id=player_id,
            goals=0,
            assists=0,
            xg=0.0,
            key_passes=0,
            rating=0.0,
            form=[],
            minutes_played=0,
            minutes_per_game=None,
            vaep=0.0,
            matches_available=0,
            goals_conceded=0,
            tackles=0,
            recent_matches=[],
        )

    # Unpack tuples; rows are newest-first
    rows = [pmr for pmr, _f, _p in raw_rows]
    player_obj = raw_rows[0][2]

    # rows are newest-first; reverse for oldest-first form list
    rows_oldest_first = list(reversed(rows))

    goals = sum(r.goals or 0 for r in rows)
    assists = sum(r.assists or 0 for r in rows)
    key_passes = sum(r.key_passes or 0 for r in rows)
    xg = sum(float(r.xg or 0) for r in rows)
    minutes_played = sum(r.minutes_played or 0 for r in rows)
    goals_conceded = sum(r.goals_conceded or 0 for r in rows)
    tackles = sum(r.tackles or 0 for r in rows)

    matches_available = len(rows)
    minutes_per_game = round(minutes_played / matches_available) if matches_available > 0 and minutes_played > 0 else None

    # Layer 1 weighted average (most-recent first)
    sportmonks_ratings = [
        float(r.sportmonks_rating) for r in rows if r.sportmonks_rating is not None
    ]
    rating = compute_layer1_rating(sportmonks_ratings) or 0.0

    # form: oldest-first list of non-null sportmonks ratings
    form = [
        float(r.sportmonks_rating)
        for r in rows_oldest_first
        if r.sportmonks_rating is not None
    ]

    # Build recent_matches — newest-first, up to 5
    team = player_obj.team or ""
    recent_matches: list[MatchFormEntry] = []
    for pmr, fixture, _ in raw_rows:
        if team and fixture.home_team == team:
            opponent = fixture.away_team or "Unknown"
            home_away = "H"
        elif team and fixture.away_team == team:
            opponent = fixture.home_team or "Unknown"
            home_away = "A"
        else:
            opponent = "Unknown"
            home_away = "H"

        if pmr.match_date:
            md = pmr.match_date
            match_date = md.strftime("%Y-%m-%d") if hasattr(md, "strftime") else str(md)
        elif fixture.kickoff_time:
            match_date = fixture.kickoff_time.date().strftime("%Y-%m-%d")
        else:
            match_date = ""

        recent_matches.append(MatchFormEntry(
            match_date=match_date,
            opponent=opponent,
            home_away=home_away,
            rating=float(pmr.sportmonks_rating) if pmr.sportmonks_rating is not None else 0.0,
            goals=pmr.goals or 0,
            assists=pmr.assists or 0,
            minutes=pmr.minutes_played or 0,
            shots_on_target=pmr.shots_on_target or 0,
            xg=float(pmr.xg or 0),
            saves=pmr.saves or 0,
            clean_sheet=bool(pmr.clean_sheet) if pmr.clean_sheet is not None else False,
        ))

    vaep = 0.0  # TODO: awaiting Sportmonks event-level VAEP data

    return PlayerStatsResponse(
        player_id=player_id,
        goals=goals,
        assists=assists,
        xg=xg,
        key_passes=key_passes,
        rating=rating,
        form=form,
        minutes_played=minutes_played,
        minutes_per_game=minutes_per_game,
        vaep=vaep,
        matches_available=matches_available,
        goals_conceded=goals_conceded,
        tackles=tackles,
        recent_matches=recent_matches,
    )

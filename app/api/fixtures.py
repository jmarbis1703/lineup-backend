"""Fixtures endpoint — GET /api/fixtures/upcoming (no auth required)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.market import _get_change_24h_map
from app.dependencies import get_db
from app.models.fixture import Fixture
from app.models.market_state import LmsrMarketState
from app.models.player import Player
from app.models.player_match_rating import PlayerMatchRating
from app.schemas.fixtures import FixtureResponse, HotPlayer

router = APIRouter()


def _predict_for_player(position_group: str) -> str:
    if position_group == "FW":
        return "Goal scorer"
    elif position_group == "MF":
        return "Key creator"
    elif position_group == "GK":
        return "Clean sheet"
    else:
        return "Defensive rock"


async def _build_hot_players(
    db: AsyncSession,
    fixtures: list[Fixture],
) -> dict[int, list[HotPlayer]]:
    """Returns {fixture_id: list[HotPlayer]} for the given fixtures.

    Single batch of DB queries — no N+1 per fixture.
    """
    team_names: set[str] = set()
    for f in fixtures:
        team_names.add(f.home_team)
        team_names.add(f.away_team)

    if not team_names:
        return {}

    # Batch fetch all active players on these teams with their market state
    player_rows = (
        await db.execute(
            sa.select(Player, LmsrMarketState)
            .join(LmsrMarketState, Player.id == LmsrMarketState.player_id)
            .where(Player.is_active.is_(True), Player.team.in_(team_names))
        )
    ).all()

    if not player_rows:
        return {}

    all_player_ids = [p.id for p, _ in player_rows]

    # Batch fetch last match stats per player (most recent fixture per player)
    pmr_rows = (
        await db.execute(
            sa.select(PlayerMatchRating)
            .join(Fixture, PlayerMatchRating.fixture_id == Fixture.id)
            .where(PlayerMatchRating.player_id.in_(all_player_ids))
            .order_by(
                PlayerMatchRating.player_id,
                Fixture.kickoff_time.desc(),
            )
        )
    ).scalars().all()

    # Keep only last match per player
    last_match: dict[int, PlayerMatchRating] = {}
    for pmr in pmr_rows:
        if pmr.player_id not in last_match:
            last_match[pmr.player_id] = pmr

    # Batch fetch change_24h for all players
    change_map = await _get_change_24h_map(db, all_player_ids)

    # Group players by team
    players_by_team: dict[str, list[tuple[Player, LmsrMarketState]]] = {}
    for player, market in player_rows:
        if player.team:
            players_by_team.setdefault(player.team, []).append((player, market))

    result: dict[int, list[HotPlayer]] = {}
    top_player_by_fixture: dict[int, str] = {}

    for fixture in fixtures:
        home = players_by_team.get(fixture.home_team, [])
        away = players_by_team.get(fixture.away_team, [])
        all_players = home + away

        # Sort by oracle_rating desc (fall back to current_rating)
        all_players.sort(
            key=lambda r: float(r[1].oracle_rating or r[1].current_rating),
            reverse=True,
        )

        top3 = all_players[:3]
        hot: list[HotPlayer] = []

        for player, market in top3:
            pmr = last_match.get(player.id)
            goals = pmr.goals or 0 if pmr else 0
            assists = pmr.assists or 0 if pmr else 0
            key_passes = pmr.key_passes or 0 if pmr else 0
            xg = float(pmr.xg or 0) if pmr else 0.0

            epm_pts = goals * 6 + assists * 4 + key_passes * 2 + 2

            change_24h = change_map.get(player.id)
            if change_24h is None or change_24h == 0.0:
                form = "→"
            elif change_24h > 0:
                form = "↑"
            else:
                form = "↓"

            hot.append(
                HotPlayer(
                    name=player.name,
                    club=player.team or "",
                    position=player.position_group,
                    rating=float(market.oracle_rating or market.current_rating),
                    xg=xg,
                    key_passes=float(key_passes),
                    form=form,
                    prediction=_predict_for_player(player.position_group),
                    epm_pts=epm_pts,
                )
            )

        result[fixture.id] = hot
        if top3:
            top_player_by_fixture[fixture.id] = top3[0][0].name

    return result


@router.get("/upcoming", response_model=list[FixtureResponse])
async def get_upcoming_fixtures(
    db: AsyncSession = Depends(get_db),
    limit: int = 10,
    include_hot_players: bool = Query(default=False),
) -> list[FixtureResponse]:
    now = datetime.now(timezone.utc)
    fixtures = (
        await db.execute(
            sa.select(Fixture)
            .where(Fixture.kickoff_time > now)
            .order_by(Fixture.kickoff_time)
            .limit(limit)
        )
    ).scalars().all()

    hot_players_map: dict[int, list[HotPlayer]] = {}
    if include_hot_players and fixtures:
        hot_players_map = await _build_hot_players(db, list(fixtures))

    responses = []
    for f in fixtures:
        hot = hot_players_map.get(f.id)
        analysis: Optional[str] = None
        if hot:
            analysis = f"{f.home_team} vs {f.away_team} — watch {hot[0].name}"

        responses.append(
            FixtureResponse(
                id=f.id,
                home_team=f.home_team,
                away_team=f.away_team,
                kickoff_time=f.kickoff_time,
                status=f.status,
                league_id=f.league_id,
                matchday=f.matchday,
                hot_players=(hot or []) if include_hot_players else None,
                analysis=analysis if include_hot_players else None,
            )
        )

    return responses

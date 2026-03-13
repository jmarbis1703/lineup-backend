"""Initial player import and ongoing squad sync worker."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lmsr import initialize_market, lmsr_rating
from app.core.oracle import compute_oracle_rating
from app.models.fixture import Fixture
from app.models.liquidity_config import LiquidityConfig
from app.models.market_state import LmsrMarketState
from app.models.player import Player
from app.models.player_match_rating import PlayerMatchRating
from app.models.rating_history import RatingHistory
from app.services.sportmonks import SportmonksClient, map_position_to_group

logger = logging.getLogger(__name__)

# Sentinel fixture ID used for seed PlayerMatchRating rows during initial import
_SEED_FIXTURE_ID: int = 0


async def _ensure_seed_fixture(db: AsyncSession) -> None:
    """Create the sentinel seed fixture (id=0) if it doesn't already exist."""
    if await db.get(Fixture, _SEED_FIXTURE_ID) is None:
        db.add(
            Fixture(
                id=_SEED_FIXTURE_ID,
                home_team="SEED",
                away_team="SEED",
                kickoff_time=datetime(2000, 1, 1, tzinfo=timezone.utc),
                status="finished",
            )
        )
        await db.flush()


async def _get_b_min(db: AsyncSession) -> float:
    """Return base_liquidity from LiquidityConfig; default 100.0 if no row exists."""
    result = await db.execute(select(LiquidityConfig).limit(1))
    config = result.scalar_one_or_none()
    return float(config.base_liquidity) if config else 100.0


def _build_oracle_stats(stats: dict[str, Any], position_group: str) -> dict[str, Any]:
    """Build the oracle-compatible peer stats dict from raw Sportmonks stats."""
    return {
        "position_group": position_group,
        "goals": stats.get("goals"),
        "shots_on_target": stats.get("shots_on_target"),
        "xg": stats.get("xg"),
        "key_passes": stats.get("key_passes"),
        "dribbles_won": stats.get("dribbles_won"),
        "pass_accuracy": stats.get("pass_accuracy"),
        "tackles": stats.get("tackles"),
        "interceptions": stats.get("interceptions"),
        "clearances": stats.get("clearances"),
        "duels_won": stats.get("duels_won"),
        "saves": stats.get("saves"),
        "clean_sheet": stats.get("clean_sheet"),
        "goals_conceded": stats.get("goals_conceded"),
        "minutes_played": stats.get("minutes_played"),
    }


async def _upsert_player(
    db: AsyncSession,
    squad_entry: dict,
    team_name: str,
    league_id: int,
    position_group: str,
    stats: dict[str, Any],
    b_min: float,
    all_players_stats: list[dict],
) -> Player | None:
    """Upsert one player, seed match rating, compute oracle, and init market.

    Makes no external API calls — all stat data is passed in.
    """
    player_data = squad_entry.get("player", {})
    player_id: int | None = player_data.get("id") or squad_entry.get("player_id")
    if not player_id:
        return None

    # Upsert Player row
    player = await db.get(Player, player_id)
    if player is None:
        player = Player(
            id=player_id,
            name=player_data.get("name", "Unknown"),
            team=team_name,
            position=str(squad_entry.get("position_id", "")),
            position_group=position_group,
            league_id=league_id,
            photo_url=player_data.get("image_path"),
            is_active=True,
        )
        db.add(player)
    else:
        player.team = team_name
        player.position = str(squad_entry.get("position_id", ""))
        player.position_group = position_group
        player.is_active = True
        player.last_synced_at = datetime.now(timezone.utc)
    await db.flush()

    # Upsert seed PlayerMatchRating
    pmr_result = await db.execute(
        select(PlayerMatchRating).where(
            PlayerMatchRating.player_id == player_id,
            PlayerMatchRating.fixture_id == _SEED_FIXTURE_ID,
        )
    )
    pmr = pmr_result.scalar_one_or_none()
    if pmr is None:
        pmr = PlayerMatchRating(
            player_id=player_id,
            fixture_id=_SEED_FIXTURE_ID,
        )
        db.add(pmr)

    pmr.sportmonks_rating = stats.get("sportmonks_rating")
    pmr.minutes_played = stats.get("minutes_played") or 0
    pmr.goals = stats.get("goals") or 0
    pmr.assists = stats.get("assists") or 0
    pmr.shots_on_target = stats.get("shots_on_target") or 0
    pmr.pass_accuracy = stats.get("pass_accuracy")
    pmr.key_passes = stats.get("key_passes") or 0
    pmr.tackles = stats.get("tackles") or 0
    pmr.dribbles_won = stats.get("dribbles_won") or 0
    pmr.duels_won = stats.get("duels_won") or 0
    await db.flush()

    # Compute oracle rating
    match_ratings: list[float | None] = [stats.get("sportmonks_rating")]
    oracle_rating, oracle_source = compute_oracle_rating(
        match_ratings=match_ratings,
        player_stats=_build_oracle_stats(stats, position_group),
        position_group=position_group,
        all_players_stats=all_players_stats,
    )

    # Init or update LmsrMarketState
    market_result = await db.execute(
        select(LmsrMarketState).where(LmsrMarketState.player_id == player_id)
    )
    market = market_result.scalar_one_or_none()
    q_up, q_down = initialize_market(oracle_rating, b_min)
    market_rating = lmsr_rating(q_up, q_down, b_min)

    if market is None:
        db.add(
            LmsrMarketState(
                player_id=player_id,
                b_min=b_min,
                q_up=q_up,
                q_down=q_down,
                current_rating=market_rating,
                oracle_rating=oracle_rating,
                oracle_source=oracle_source,
            )
        )
    else:
        market.oracle_rating = oracle_rating
        market.oracle_source = oracle_source
        market.updated_at = datetime.now(timezone.utc)
    await db.flush()

    # Append RatingHistory entry
    db.add(
        RatingHistory(
            player_id=player_id,
            rating=market_rating,
            source="market_init",
        )
    )
    await db.flush()
    return player


async def initial_player_import(
    db: AsyncSession,
    client: SportmonksClient,
    league_ids: list[int],
    top_n_per_league: int = 50,
) -> int:
    """Import top N players per league and initialise their markets.

    Returns the total number of players imported/upserted.
    """
    await _ensure_seed_fixture(db)
    b_min = await _get_b_min(db)
    total = 0

    for league_id in league_ids:
        try:
            season_id = await client.get_current_season_id(league_id)
        except Exception:
            logger.warning("Could not get season_id for league %d; skipping", league_id)
            continue

        try:
            teams = await client.get_teams_by_league(league_id, season_id)
        except Exception:
            logger.warning("Could not fetch teams for league %d; skipping", league_id)
            continue

        # Fix 1: Unwrap Sportmonks nested include envelope
        # Live API returns team["squads"] as {"data": [...]}, not [...]
        squad_entries: list[tuple[dict, str]] = []
        for team in teams:
            team_name = team.get("name", "Unknown")
            squads = team.get("squads", [])
            if isinstance(squads, dict):
                squads = squads.get("data", [])
            for entry in squads:
                squad_entries.append((entry, team_name))

        # Fix 2: Fetch stats for ALL squad members before slicing
        stats_cache: dict[int, dict] = {}
        for entry, _team_name in squad_entries:
            player_data = entry.get("player", {})
            pid: int | None = player_data.get("id") or entry.get("player_id")
            if not pid:
                continue
            try:
                stats_cache[pid] = await client.get_player_statistics(pid, season_id)
            except Exception:
                logger.warning("Could not fetch stats for player %d", pid)
                stats_cache[pid] = {}

        # Fix 3: Activity filter — keep only players with minutes_played > 0
        # Fall back to full list if no player has minutes (new season).
        def _get_minutes(entry_team: tuple[dict, str]) -> int:
            entry, _ = entry_team
            pid = entry.get("player", {}).get("id") or entry.get("player_id")
            return stats_cache.get(pid, {}).get("minutes_played") or 0

        active_entries = [e for e in squad_entries if _get_minutes(e) > 0]
        if not active_entries:
            logger.warning(
                "League %d: no players with minutes_played > 0 — "
                "new season or gap; including all %d squad entries.",
                league_id,
                len(squad_entries),
            )
            active_entries = squad_entries

        # Fix 2 (continued): Sort by minutes_played DESC, THEN slice
        active_entries.sort(key=_get_minutes, reverse=True)
        selected_entries = active_entries[:top_n_per_league]

        # Build peer stats list for Layer 2 oracle percentile context
        all_players_stats: list[dict] = []
        for entry, _team_name in selected_entries:
            player_data = entry.get("player", {})
            pid = player_data.get("id") or entry.get("player_id")
            if not pid:
                continue
            pg = map_position_to_group(str(entry.get("position_id", "")))
            stats = stats_cache.get(pid, {})
            all_players_stats.append({"player_id": pid, "position_group": pg, **stats})

        # Upsert each player
        for entry, team_name in selected_entries:
            player_data = entry.get("player", {})
            pid = player_data.get("id") or entry.get("player_id")
            if not pid:
                continue
            pg = map_position_to_group(str(entry.get("position_id", "")))
            stats = stats_cache.get(pid, {})
            p = await _upsert_player(
                db, entry, team_name, league_id, pg, stats, b_min, all_players_stats
            )
            if p:
                total += 1

    await db.commit()
    return total


async def sync_new_players(
    db: AsyncSession,
    client: SportmonksClient,
    league_ids: list[int],
) -> dict[str, int]:
    """Sync squads: import new players; mark players no longer in squad inactive.

    Returns ``{"new": N, "deactivated": M}``.
    """
    await _ensure_seed_fixture(db)
    b_min = await _get_b_min(db)
    new_count = 0
    deactivated_count = 0

    for league_id in league_ids:
        try:
            season_id = await client.get_current_season_id(league_id)
            teams = await client.get_teams_by_league(league_id, season_id)
        except Exception:
            logger.warning("Could not sync league %d; skipping", league_id)
            continue

        current_ids: set[int] = set()
        for team in teams:
            team_name = team.get("name", "Unknown")
            squads = team.get("squads", [])
            if isinstance(squads, dict):
                squads = squads.get("data", [])
            for entry in squads:
                player_data = entry.get("player", {})
                pid: int | None = player_data.get("id") or entry.get("player_id")
                if not pid:
                    continue
                current_ids.add(pid)

                if await db.get(Player, pid) is None:
                    try:
                        stats = await client.get_player_statistics(pid, season_id)
                    except Exception:
                        stats = {}
                    pg = map_position_to_group(str(entry.get("position_id", "")))
                    p = await _upsert_player(
                        db, entry, team_name, league_id, pg, stats, b_min, []
                    )
                    if p:
                        new_count += 1

        # Deactivate players no longer in the squad
        active_result = await db.execute(
            select(Player).where(
                Player.league_id == league_id,
                Player.is_active.is_(True),
            )
        )
        for player in active_result.scalars():
            if player.id not in current_ids:
                player.is_active = False
                deactivated_count += 1

    await db.commit()
    return {"new": new_count, "deactivated": deactivated_count}

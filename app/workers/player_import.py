"""Initial player import and ongoing squad sync worker."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
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


def _extract_match_ratings(fixtures: list[dict]) -> dict[int, list[float]]:
    """Build player_id → [last 5 match ratings] from fixture lineups (type_id 118).

    Fixtures should be ordered most-recent-first.  Ratings beyond 5 are discarded.
    """
    from app.services.sportmonks import _TYPE_MATCH_RATING
    ratings_map: dict[int, list[float]] = {}
    for fixture in fixtures:
        for lineup_entry in fixture.get("lineups", []):
            pid = lineup_entry.get("player_id")
            if not pid:
                continue
            for detail in lineup_entry.get("details", []):
                if detail.get("type_id") != _TYPE_MATCH_RATING:
                    continue
                data_block = detail.get("data") or {}
                raw = data_block.get("value")
                if raw is None:
                    val_block = detail.get("value") or {}
                    raw = val_block.get("rating") or val_block.get("total")
                if raw is None:
                    continue
                try:
                    r = float(raw)
                except (ValueError, TypeError):
                    continue
                bucket = ratings_map.setdefault(pid, [])
                if len(bucket) < 5:
                    bucket.append(r)
    return ratings_map


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


def _collect_league_entries(
    teams: list[dict],
    stats_cache: dict[int, dict],
    league_id: int,
    top_n: int,
) -> list[tuple[dict, str, int, str, dict]]:
    """Filter, sort, and slice squad entries for one league; return tuples for pass 2."""
    squad_entries: list[tuple[dict, str]] = []
    for team in teams:
        team_name = team.get("name", "Unknown")
        squads = team.get("squads", [])
        if isinstance(squads, dict):
            squads = squads.get("data", [])
        for entry in squads:
            squad_entries.append((entry, team_name))

    def _is_active(entry_team: tuple[dict, str]) -> bool:
        entry, _ = entry_team
        pid = entry.get("player", {}).get("id") or entry.get("player_id")
        stats = stats_cache.get(pid, {})
        return bool(stats.get("minutes_played")) or bool(stats.get("match_ratings"))

    def sort_key(entry_team: tuple[dict, str]) -> tuple[float, int]:
        entry, _ = entry_team
        pid = entry.get("player", {}).get("id") or entry.get("player_id")
        stats = stats_cache.get(pid, {})
        match_ratings = stats.get("match_ratings", [])
        if match_ratings:
            primary = sum(match_ratings) / len(match_ratings)
        else:
            try:
                primary = float(stats.get("sportmonks_rating") or 0)
            except (ValueError, TypeError):
                primary = 0.0
        secondary = stats.get("minutes_played", 0) or 0
        return (primary, secondary)

    active_entries = [e for e in squad_entries if _is_active(e)]
    if not active_entries:
        logger.warning(
            "League %d: no players with minutes_played > 0 — "
            "new season or gap; including all %d squad entries.",
            league_id,
            len(squad_entries),
        )
        active_entries = squad_entries

    active_entries.sort(key=sort_key, reverse=True)
    selected = active_entries[:top_n]

    result: list[tuple[dict, str, int, str, dict]] = []
    for entry, team_name in selected:
        player_data = entry.get("player", {})
        pid = player_data.get("id") or entry.get("player_id")
        if not pid:
            continue
        pg = map_position_to_group(str(entry.get("position_id", "")))
        stats = stats_cache.get(pid, {})
        result.append((entry, team_name, league_id, pg, stats))
    return result


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
    match_ratings_raw: list[float] = stats.get("match_ratings") or []
    if match_ratings_raw:
        match_ratings: list[float | None] = match_ratings_raw
    else:
        # Fallback: season-level single rating (may be None → triggers Layer 2/3)
        match_ratings = [stats.get("sportmonks_rating")]
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

    Uses a two-pass approach: Pass 1 collects all data across every league;
    Pass 2 upserts all players against the full cross-league peer pool so
    that Layer 2 percentile math is not compressed to a single-league sample.

    Returns the total number of players imported/upserted.
    """
    await _ensure_seed_fixture(db)
    b_min = await _get_b_min(db)
    # Commit setup queries so the session is not idle-in-transaction during API calls
    await db.commit()

    # Pass 1 — fetch all data, collect (entry, team_name, league_id, pg, stats)
    # No DB writes happen here; session is idle so no long-held transaction.
    all_league_data: list[tuple[dict, str, int, str, dict]] = []

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

        # Fetch stats for ALL squad members across every team before slicing
        all_squad_entries: list[tuple[dict, str]] = []
        for team in teams:
            team_name = team.get("name", "Unknown")
            squads = team.get("squads", [])
            if isinstance(squads, dict):
                squads = squads.get("data", [])
            for entry in squads:
                all_squad_entries.append((entry, team_name))

        stats_cache: dict[int, dict] = {}
        for entry, _team_name in all_squad_entries:
            player_data = entry.get("player", {})
            pid: int | None = player_data.get("id") or entry.get("player_id")
            if not pid:
                continue
            try:
                stats_cache[pid] = await client.get_player_statistics(pid, season_id)
            except Exception:
                logger.warning("Could not fetch stats for player %d", pid)
                stats_cache[pid] = {}

        # Fetch recent fixtures per team to collect per-match ratings (Layer 1)
        now = datetime.now(timezone.utc)
        date_to = now.strftime("%Y-%m-%d")
        date_from = (now - timedelta(days=45)).strftime("%Y-%m-%d")

        match_ratings_cache: dict[int, list[float]] = {}
        for team in teams:
            team_id = team.get("id")
            if not team_id:
                continue
            try:
                fixtures = await client.get_team_fixtures_with_ratings(
                    team_id, date_from, date_to
                )
                team_ratings = _extract_match_ratings(fixtures)
                for pid, ratings in team_ratings.items():
                    existing = match_ratings_cache.get(pid, [])
                    match_ratings_cache[pid] = (existing + ratings)[:5]
            except Exception:
                logger.warning("Could not fetch fixtures for team %d", team_id)

        # Inject match_ratings into stats_cache
        for pid in stats_cache:
            stats_cache[pid]["match_ratings"] = match_ratings_cache.get(pid, [])

        league_entries = _collect_league_entries(
            teams, stats_cache, league_id, top_n_per_league
        )
        all_league_data.extend(league_entries)

    # Build cross-league peer pool (all selected players across all leagues)
    all_players_stats: list[dict] = []
    for entry, _team_name, _league_id, pg, stats in all_league_data:
        player_data = entry.get("player", {})
        pid = player_data.get("id") or entry.get("player_id")
        if not pid:
            continue
        all_players_stats.append({"player_id": pid, "position_group": pg, **stats})

    # Pass 2 — upsert per-league with a commit after each league.
    # Each transaction covers ~50 players and completes in ≤8 minutes,
    # preventing idle-in-transaction timeouts and limiting rollback blast radius.
    league_groups: dict[int, list[tuple[dict, str, int, str, dict]]] = {}
    for item in all_league_data:
        league_groups.setdefault(item[2], []).append(item)

    total = 0
    for lid, items in league_groups.items():
        league_count = 0
        for entry, team_name, _lid, pg, stats in items:
            try:
                p = await _upsert_player(
                    db, entry, team_name, lid, pg, stats, b_min, all_players_stats
                )
                if p:
                    total += 1
                    league_count += 1
            except Exception as exc:
                logger.warning(
                    "Failed to upsert player in league %d, skipping: %s", lid, exc
                )
        await db.commit()
        logger.info("Committed %d players for league %d", league_count, lid)

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

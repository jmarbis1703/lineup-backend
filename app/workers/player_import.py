"""Initial player import and ongoing squad sync worker."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.core.lmsr import initialize_market, lmsr_rating
from app.core.oracle import compute_oracle_rating
from app.models.fixture import Fixture
from app.models.liquidity_config import LiquidityConfig
from app.models.market_state import LmsrMarketState
from app.models.player import Player
from app.models.player_match_rating import PlayerMatchRating
from app.models.rating_history import RatingHistory
from app.services.sportmonks import SportmonksClient, map_position_to_group
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def _team_cap(standing_position: int) -> int:
    """Return the max players to import from a team based on league standing.

    Top half (positions 1-10): 4 players.
    Bottom half (positions 11-20): 2 players.
    """
    return 4 if standing_position <= 10 else 2


# Sentinel fixture ID used for seed PlayerMatchRating rows during initial import
_SEED_FIXTURE_ID: int = 0

# Sportmonks league IDs to include in the daily ratings refresh (§9A).
# To add a league in Step 7 (multi-league), append its Sportmonks league ID here.
MATCH_RATING_LEAGUES: list[int] = [8, 82, 301, 384, 564]  # 8 = Premier League, 82 = Bundesliga, 301 = Ligue 1, 384 = Serie A, 564 = La Liga

# Rolling window passed to refresh_match_ratings() by the beat task.
# The CLI script (scripts/refresh_match_ratings.py) uses --days independently.
MATCH_RATING_DAYS_BACK: int = 7



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
) -> list[tuple[dict, str, int, str, str | None, dict]]:
    """Select squad entries per team using standings-driven caps; return tuples for pass 2.

    Cap per team: 4 players for positions 1-10, 2 for positions 11-20.
    Fallback to cap=2 with a warning if a team has no standings position.
    Sort criterion per team: avg(match_ratings) DESC, then total minutes DESC;
    players with no data sort last.
    """
    # Warn once at league level if no match rating data exists at import time.
    has_any_ratings = any(bool(s.get("match_ratings")) for s in stats_cache.values())
    if not has_any_ratings:
        logger.warning(
            "WARNING: No match rating data for league %d — player selection is arbitrary. "
            "Run refresh_match_ratings after import.",
            league_id,
        )

    def sort_key(entry: dict) -> tuple[float, int]:
        pid = entry.get("player", {}).get("id") or entry.get("player_id")
        stats = stats_cache.get(pid, {})
        match_ratings = stats.get("match_ratings") or []
        if match_ratings:
            primary = sum(match_ratings) / len(match_ratings)
        else:
            try:
                primary = float(stats.get("sportmonks_rating") or 0)
            except (ValueError, TypeError):
                primary = 0.0
        secondary = stats.get("minutes_played", 0) or 0
        return (primary, secondary)

    result: list[tuple[dict, str, int, str, str | None, dict]] = []

    for team in teams:
        team_name = team.get("name", "Unknown")
        team_id = team.get("id")
        standing_position = team.get("position")

        if standing_position is None:
            logger.warning(
                "No standing found for team %s — defaulting to cap 2", team_id
            )
            cap = 2
        else:
            cap = _team_cap(standing_position)

        squads = team.get("squads", [])
        if isinstance(squads, dict):
            squads = squads.get("data", [])

        def _is_active(entry: dict) -> bool:
            pid = entry.get("player", {}).get("id") or entry.get("player_id")
            stats = stats_cache.get(pid, {})
            return bool(stats.get("minutes_played")) or bool(stats.get("match_ratings"))

        active = [e for e in squads if _is_active(e)]
        if not active:
            active = list(squads)

        active.sort(key=sort_key, reverse=True)
        selected = active[:cap]

        for entry in selected:
            player_data = entry.get("player", {})
            pid = player_data.get("id") or entry.get("player_id")
            if not pid:
                continue
            raw_pos = str(entry.get("position_id", ""))
            pg = map_position_to_group(raw_pos)
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
            name=player_data.get("display_name") or player_data.get("name", "Unknown"),
            team=team_name,
            position=str(squad_entry.get("position_id", "")),
            position_group=position_group,
            league_id=league_id,
            photo_url=player_data.get("image_path"),
            is_active=True,
        )
        db.add(player)
    else:
        player.name = (player_data.get("display_name") or player_data.get("name", player.name)).strip()
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
) -> int:
    """Import players per league using standings-driven per-team caps; initialise markets.

    Cap per team: 4 players (positions 1-10) or 2 players (positions 11-20).
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

        # Fetch recent fixtures per team to collect per-match ratings (Layer 1).
        # 21-day window (~3 fixtures/team) keeps entity quota under daily limit.
        # lineups.detailedposition is NOT included — Starter plan entity quota is
        # too small to afford it at 44 entities/fixture.
        now = datetime.now(timezone.utc)
        date_to = now.strftime("%Y-%m-%d")
        date_from = (now - timedelta(days=21)).strftime("%Y-%m-%d")

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
            teams, stats_cache, league_id
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
        imported_ids: set[int] = set()
        for entry, team_name, _lid, pg, stats in items:
            try:
                p = await _upsert_player(
                    db, entry, team_name, lid, pg, stats, b_min, all_players_stats
                )
                if p:
                    total += 1
                    league_count += 1
                    imported_ids.add(p.id)
            except Exception as exc:
                logger.warning(
                    "Failed to upsert player in league %d, skipping: %s", lid, exc
                )
        await db.commit()
        logger.info("Committed %d players for league %d", league_count, lid)

        # Deactivate players in this league who were not selected by the new cap logic.
        # Guard: skip if nothing was imported (total failure) to avoid deactivating everyone.
        if imported_ids:
            deactivated = await db.execute(
                update(Player)
                .where(
                    Player.league_id == lid,
                    Player.id.not_in(imported_ids),
                    Player.is_active.is_(True),
                )
                .values(is_active=False)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            logger.info(
                "Deactivated %d out-of-cap players for league %d",
                deactivated.rowcount,
                lid,
            )

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
                    raw_pos = str(entry.get("position_id", ""))
                    pg = map_position_to_group(raw_pos)
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


# NOTE: type_ids in lineups.details differ from statistics.details.
# Do NOT reuse _TYPE_MINUTES (78) or other season-stat constants here.
# Confirmed lineup type_ids (verified against Sportmonks fixture 19427186):
#   1584 → minutes played
# See Incident Log in README.md for full investigation.
def _parse_lineup_stats(details: list[dict]) -> dict[str, Any]:
    """Parse per-match stat type_ids from a lineup entry's details array.

    Returns a dict with known stat keys and their values (0 / None if missing).
    """
    from app.services.sportmonks import (
        _TYPE_GOALS,
        _TYPE_ASSISTS,
        _TYPE_LINEUP_MINUTES,
        # _TYPE_KEY_PASSES not imported — type_id 119 = pass accuracy % in lineup context, not key passes
        # TODO: import and re-enable once correct type_id for key passes is verified
        _TYPE_TACKLES,
        _TYPE_GOALS_CONCEDED,
        _TYPE_SAVES,
        _TYPE_CLEAN_SHEET,
        _TYPE_XG,
        _TYPE_MATCH_RATING,
        _TYPE_SHOTS,
    )

    type_map = {
        _TYPE_GOALS: "goals",
        _TYPE_ASSISTS: "assists",
        _TYPE_LINEUP_MINUTES: "minutes_played",
        # type_id 44 (tackles) not returned by fixtures/lineups endpoint
        # on current Sportmonks plan — omitted to avoid silent zero fills.
        # _TYPE_TACKLES: "tackles",
        _TYPE_GOALS_CONCEDED: "goals_conceded",
        _TYPE_SAVES: "saves",
        _TYPE_CLEAN_SHEET: "clean_sheet",
        _TYPE_XG: "xg",
        _TYPE_SHOTS: "shots_on_target",
    }

    result: dict[str, Any] = {}
    rating = None

    for detail in details:
        tid = detail.get("type_id")

        if tid == _TYPE_MATCH_RATING:
            raw = (detail.get("data") or {}).get("value")
            if raw is None:
                val = detail.get("value") or {}
                raw = val.get("rating") or val.get("average") or val.get("total")
            try:
                rating = float(raw) if raw is not None else None
            except (ValueError, TypeError):
                pass
            continue

        if tid in type_map:
            raw = (detail.get("data") or {}).get("value")
            try:
                result[type_map[tid]] = int(float(raw)) if raw is not None else None
            except (ValueError, TypeError):
                pass

    result["sportmonks_rating"] = rating
    return result


async def refresh_match_ratings(
    league_id: int,
    db: AsyncSession,
    client: SportmonksClient,
    days_back: int = 90,
) -> dict[str, int]:
    """Fetch recent fixtures for all teams in a league and upsert per-match PlayerMatchRating rows.

    This populates sportmonks_rating, minutes_played, goals, assists, etc. from real
    fixture lineup data — replacing reliance on the seed sentinel (fixture_id=0) rows.

    Rate limit cost: 1 call per team (~20 teams for PL) + 1 for season_id = ~21 calls.
    Returns {"fixtures_synced": N, "ratings_upserted": M}.
    """
    from datetime import date

    date_to = date.today().isoformat()
    date_from = (date.today() - timedelta(days=days_back)).isoformat()

    season_id = await client.get_current_season_id(league_id)
    teams = await client.get_teams_by_league(league_id, season_id)

    fixtures_synced = 0
    ratings_upserted = 0

    seen_fixture_ids: set[int] = set()

    for team in teams:
        team_id = team.get("id")
        if not team_id:
            continue

        try:
            fixtures = await client.get_team_fixtures_with_ratings(team_id, date_from, date_to)
        except Exception as exc:
            logger.warning("Could not fetch fixtures for team %s: %s", team_id, exc)
            continue

        for fixture in fixtures:
            fid = fixture.get("id")
            if not fid or fid in seen_fixture_ids:
                continue
            seen_fixture_ids.add(fid)

            # Determine fixture status — only process finished matches.
            # Sportmonks v3 returns state_id (int) on the fixture object, not a
            # text "status" field.  5=FT, 23=AET, 27=Awarded.
            FINISHED_STATE_IDS = {5, 23, 27}
            if fixture.get("state_id") not in FINISHED_STATE_IDS:
                continue

            # Parse kickoff time
            starting_at = fixture.get("starting_at") or fixture.get("date")
            try:
                from datetime import datetime as dt
                if starting_at:
                    kickoff = dt.fromisoformat(starting_at.replace(" ", "T")).replace(tzinfo=timezone.utc)
                else:
                    kickoff = datetime.now(timezone.utc)
            except (ValueError, AttributeError):
                kickoff = datetime.now(timezone.utc)

            # Upsert Fixture row
            db_fixture = await db.get(Fixture, fid)
            if db_fixture is None:
                participants = fixture.get("participants") or []
                home_name = "Home"
                away_name = "Away"
                if isinstance(participants, list):
                    for p in participants:
                        meta = (p.get("meta") or {})
                        loc = meta.get("location", "")
                        pname = p.get("name", "")
                        if loc == "home":
                            home_name = pname
                        elif loc == "away":
                            away_name = pname
                if home_name == "Home":
                    name = fixture.get("name", "")
                    if " vs " in name:
                        parts = name.split(" vs ", 1)
                        home_name, away_name = parts[0].strip(), parts[1].strip()
                db_fixture = Fixture(
                    id=fid,
                    home_team=home_name,
                    away_team=away_name,
                    kickoff_time=kickoff,
                    status="finished",
                    league_id=league_id,
                )
                db.add(db_fixture)
                await db.flush()
                fixtures_synced += 1
            else:
                if db_fixture.league_id is None:
                    db_fixture.league_id = league_id

            # Process lineup entries
            lineups = fixture.get("lineups") or []
            if isinstance(lineups, dict):
                lineups = lineups.get("data", [])

            for entry in lineups:
                pid = entry.get("player_id")
                if not pid:
                    continue

                # Only update players we track
                if await db.get(Player, pid) is None:
                    continue

                details = entry.get("details") or []
                parsed = _parse_lineup_stats(details)

                pmr = (
                    await db.execute(
                        select(PlayerMatchRating).where(
                            PlayerMatchRating.player_id == pid,
                            PlayerMatchRating.fixture_id == fid,
                        )
                    )
                ).scalar_one_or_none()

                if pmr is None:
                    pmr = PlayerMatchRating(
                        player_id=pid,
                        fixture_id=fid,
                        match_date=kickoff,
                        sportmonks_rating=parsed.get("sportmonks_rating"),
                        minutes_played=parsed.get("minutes_played"),
                        goals=parsed.get("goals"),
                        assists=parsed.get("assists"),
                        shots_on_target=parsed.get("shots_on_target"),
                        key_passes=parsed.get("key_passes"),
                        tackles=parsed.get("tackles"),
                        goals_conceded=parsed.get("goals_conceded"),
                        saves=parsed.get("saves"),
                        clean_sheet=bool(parsed.get("clean_sheet")),
                        xg=parsed.get("xg"),
                    )
                    db.add(pmr)
                else:
                    # Always update match_date — kickoff is stable and may be NULL on pre-0006 rows
                    pmr.match_date = kickoff
                    # Update only if we got a rating (don't overwrite good data with None)
                    if parsed.get("sportmonks_rating") is not None:
                        pmr.sportmonks_rating = parsed["sportmonks_rating"]
                    if parsed.get("minutes_played") is not None:
                        pmr.minutes_played = parsed["minutes_played"]
                    if parsed.get("goals") is not None:
                        pmr.goals = parsed["goals"]
                    if parsed.get("assists") is not None:
                        pmr.assists = parsed["assists"]
                    if parsed.get("tackles") is not None:
                        pmr.tackles = parsed["tackles"]
                    if parsed.get("goals_conceded") is not None:
                        pmr.goals_conceded = parsed["goals_conceded"]
                    if parsed.get("key_passes") is not None:
                        pmr.key_passes = parsed["key_passes"]

                ratings_upserted += 1

        await db.commit()
        logger.info(
            "League %d — team %s: %d fixtures synced, %d ratings upserted so far",
            league_id, team_id, fixtures_synced, ratings_upserted,
        )

    return {"fixtures_synced": fixtures_synced, "ratings_upserted": ratings_upserted}


# ---------------------------------------------------------------------------
# Celery task wrapper — §9A
# ---------------------------------------------------------------------------


def _make_session() -> tuple:
    """Create a fresh async engine + session factory for use inside a Celery task."""
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


@celery_app.task(name="app.workers.player_import.refresh_b_floor_task")
def refresh_b_floor_task() -> dict:
    """Celery entry point for §market-stability hourly b floor refresh.

    Invalidates the cached user count, recomputes B_FLOOR from the live
    user count, and rewrites the Redis cache key.  Returns the new floor
    value for logging and monitoring.
    """

    async def _run() -> dict:
        from app.services.redis_cache import refresh_b_floor

        engine, factory = _make_session()
        try:
            async with factory() as db:
                floor = await refresh_b_floor(settings.redis_url, db)
                logger.info("refresh_b_floor_task: B_FLOOR=%.2f", floor)
                return {"b_floor": floor}
        finally:
            await engine.dispose()

    return asyncio.run(_run())


@celery_app.task(name="app.workers.player_import.refresh_match_ratings_task")
def refresh_match_ratings_task() -> dict:
    """Celery entry point for §9A daily match ratings refresh.

    Runs refresh_match_ratings() for every league in MATCH_RATING_LEAGUES with
    a MATCH_RATING_DAYS_BACK rolling window. Returns aggregated fixture/rating counts.
    To add leagues for Step 7 (multi-league), update MATCH_RATING_LEAGUES above.
    """

    async def _run() -> dict:
        engine, factory = _make_session()
        client = SportmonksClient()
        total: dict[str, int] = {"fixtures_synced": 0, "ratings_upserted": 0}
        try:
            async with factory() as db:
                for league_id in MATCH_RATING_LEAGUES:
                    result = await refresh_match_ratings(
                        league_id, db, client, days_back=MATCH_RATING_DAYS_BACK
                    )
                    total["fixtures_synced"] += result["fixtures_synced"]
                    total["ratings_upserted"] += result["ratings_upserted"]
        finally:
            await client.close()
            await engine.dispose()
        return total

    return asyncio.run(_run())

"""
Oracle update worker — §7.4.

check_finished_fixtures(): Detect newly finished fixtures and update
oracle ratings for every player who participated.

INV-09: ONLY oracle_rating and oracle_source are written.
        q_up, q_down, current_rating are NEVER touched.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.core.oracle import compute_oracle_rating
from app.models.fixture import Fixture
from app.models.market_state import LmsrMarketState
from app.models.player import Player
from app.models.player_match_rating import PlayerMatchRating
from app.models.rating_history import RatingHistory
from app.services.redis_pubsub import publish_rating_update
from app.services.sportmonks import SportmonksClient, map_position_to_group
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Fixtures whose last_synced_at falls within this window are considered "new"
_LOOKBACK_MINUTES: int = 20

_QUANT = Decimal("0.0001")


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


async def _process_fixture(
    db: AsyncSession,
    client: SportmonksClient,
    fixture: Fixture,
) -> int:
    """Process one finished fixture: upsert PMRs and update oracle ratings.

    Returns the number of players updated.
    """
    fixture_data = await client.get_fixture_with_lineups(fixture.id)
    lineups = fixture_data.get("lineups", [])
    if not lineups:
        logger.info("No lineup data for fixture %d; skipping", fixture.id)
        return 0

    # Resolve current season_id for stats fetching
    season_id: int | None = None
    if fixture.league_id:
        try:
            season_id = await client.get_current_season_id(fixture.league_id)
        except Exception:
            logger.warning(
                "Could not resolve season_id for league %d; "
                "statistics will use empty fallback",
                fixture.league_id,
            )

    now = datetime.now(timezone.utc)

    # --- Pass 1: collect stats and build the peer pool for Layer 2 oracle ---
    player_stats_cache: dict[int, dict] = {}
    all_players_stats: list[dict] = []

    for entry in lineups:
        pid = entry.get("player_id") or (entry.get("player") or {}).get("id")
        if not pid:
            continue
        position_group = map_position_to_group(str(entry.get("position_id", "")))
        try:
            stats = (
                await client.get_player_statistics(pid, season_id)
                if season_id
                else {}
            )
        except Exception:
            logger.warning("Could not fetch stats for player %d", pid)
            stats = {}
        player_stats_cache[pid] = stats
        all_players_stats.append(
            {"player_id": pid, "position_group": position_group,
             **_build_oracle_stats(stats, position_group)}
        )

    # --- Pass 2: upsert PMRs, compute oracle, update market state ---
    updated = 0
    for entry in lineups:
        pid = entry.get("player_id") or (entry.get("player") or {}).get("id")
        if not pid:
            continue

        player = await db.get(Player, pid)
        if player is None:
            logger.debug("Player %d not in DB; skipping oracle update", pid)
            continue

        stats = player_stats_cache.get(pid, {})
        position_group = player.position_group or "MF"

        # Upsert PlayerMatchRating for this fixture
        pmr_res = await db.execute(
            sa.select(PlayerMatchRating).where(
                PlayerMatchRating.player_id == pid,
                PlayerMatchRating.fixture_id == fixture.id,
            )
        )
        pmr = pmr_res.scalar_one_or_none()
        if pmr is None:
            pmr = PlayerMatchRating(player_id=pid, fixture_id=fixture.id)
            db.add(pmr)

        pmr.sportmonks_rating = (
            Decimal(str(stats["sportmonks_rating"])).quantize(_QUANT)
            if stats.get("sportmonks_rating") is not None
            else None
        )
        pmr.minutes_played = stats.get("minutes_played") or 0
        pmr.goals = stats.get("goals") or 0
        pmr.assists = stats.get("assists") or 0
        pmr.shots_on_target = stats.get("shots_on_target") or 0
        pmr.pass_accuracy = stats.get("pass_accuracy")
        pmr.key_passes = stats.get("key_passes") or 0
        pmr.tackles = stats.get("tackles") or 0
        pmr.dribbles_won = stats.get("dribbles_won") or 0
        pmr.duels_won = stats.get("duels_won") or 0
        pmr.recorded_at = now
        await db.flush()

        # Fetch last 5 match ratings for this player (most-recent first) for Layer 1
        recent_res = await db.execute(
            sa.select(PlayerMatchRating.sportmonks_rating)
            .where(PlayerMatchRating.player_id == pid)
            .order_by(PlayerMatchRating.recorded_at.desc())
            .limit(5)
        )
        match_ratings: list[float | None] = [
            float(r) if r is not None else None for (r,) in recent_res.all()
        ]

        # Compute oracle rating — Layer 1 → Layer 2 → Layer 3
        oracle_rating, oracle_source = compute_oracle_rating(
            match_ratings=match_ratings,
            player_stats=_build_oracle_stats(stats, position_group),
            position_group=position_group,
            all_players_stats=all_players_stats,
        )

        # Fetch market state
        market_res = await db.execute(
            sa.select(LmsrMarketState).where(LmsrMarketState.player_id == pid)
        )
        market = market_res.scalar_one_or_none()
        if market is None:
            logger.warning("No market state for player %d; skipping oracle write", pid)
            continue

        # INV-09: ONLY update oracle_rating and oracle_source — never q_up, q_down, current_rating
        rating_before = market.oracle_rating if market.oracle_rating is not None else market.current_rating
        new_oracle = Decimal(str(oracle_rating)).quantize(_QUANT)
        market.oracle_rating = new_oracle
        market.oracle_source = oracle_source
        market.updated_at = now
        await db.flush()

        # Record in rating_history with source='oracle_update'
        db.add(
            RatingHistory(
                player_id=pid,
                rating=new_oracle,
                source="oracle_update",
                recorded_at=now,
            )
        )
        await db.flush()

        # Publish WebSocket event (fire-and-forget; failures must not abort the task)
        direction = "UP" if float(new_oracle) >= float(rating_before) else "DOWN"
        try:
            await publish_rating_update(
                player_id=pid,
                rating_before=rating_before,
                rating_after=new_oracle,
                direction=direction,
            )
        except Exception:
            logger.warning(
                "WS publish failed for player %d (oracle_update)", pid
            )

        updated += 1

    return updated


async def check_finished_fixtures(
    db: AsyncSession,
    client: SportmonksClient,
    lookback_minutes: int = _LOOKBACK_MINUTES,
) -> int:
    """Detect newly finished fixtures and update oracle ratings for all participants.

    "Newly finished" is defined as: status='finished' AND last_synced_at within
    the last `lookback_minutes` minutes (default 20).  Because sync_fixtures
    updates last_synced_at when a fixture transitions to 'finished', this window
    ensures we process the fixture on the first or second run after it ends
    without indefinitely reprocessing every historical fixture.

    Returns total number of player oracle ratings updated.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)

    result = await db.execute(
        sa.select(Fixture).where(
            Fixture.status == "finished",
            Fixture.id != 0,  # exclude sentinel seed fixture
            Fixture.last_synced_at >= cutoff,
        )
    )
    finished = result.scalars().all()

    if not finished:
        return 0

    total = 0
    for fixture in finished:
        try:
            total += await _process_fixture(db, client, fixture)
        except Exception:
            logger.exception("Error processing fixture %d", fixture.id)

    if total > 0:
        await db.commit()
    return total


# ---------------------------------------------------------------------------
# Celery task wrapper
# ---------------------------------------------------------------------------


@celery_app.task(name="app.workers.oracle_update.check_finished_fixtures_task")
def check_finished_fixtures_task() -> dict:
    """Celery entry point for §7.4 oracle update."""

    async def _run() -> int:
        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        client = SportmonksClient()
        try:
            async with factory() as db:
                return await check_finished_fixtures(db, client)
        finally:
            await client.close()
            await engine.dispose()

    return {"updated": asyncio.run(_run())}

"""
Fixture sync worker — §7.3.

sync_fixtures(): Fetch upcoming fixtures from Sportmonks for all 5 beta leagues
and upsert them into the fixtures table.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.fixture import Fixture
from app.services.sportmonks import SportmonksClient
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Leagues for the beta: PL, La Liga, Serie A, Bundesliga, Ligue 1 (Sportmonks IDs)
BETA_LEAGUE_IDS: list[int] = [8, 564, 384, 82, 301]

# How many days ahead to sync
_SYNC_DAYS: int = 30

# Map Sportmonks state short_name → our fixture status
_STATUS_MAP: dict[str, str] = {
    "FT": "finished",
    "AET": "finished",
    "PEN": "finished",
    "FT_PEN": "finished",
    "AWARDED": "finished",
    "WO": "finished",
    "1H": "live",
    "HT": "live",
    "2H": "live",
    "ET": "live",
    "BREAK": "live",
    "LIVE": "live",
    "NS": "scheduled",
    "TBA": "scheduled",
    "TBD": "scheduled",
    "POSTP": "postponed",
    "SUSP": "suspended",
    "INT": "interrupted",
    "DELAYED": "delayed",
    "CANC": "cancelled",
    "ABD": "abandoned",
}


async def sync_fixtures(
    db: AsyncSession,
    client: SportmonksClient,
    league_ids: list[int] | None = None,
    sync_days: int = _SYNC_DAYS,
) -> int:
    """Fetch upcoming fixtures from Sportmonks for all beta leagues and upsert into DB.

    Returns the number of fixture rows upserted.
    """
    if league_ids is None:
        league_ids = BETA_LEAGUE_IDS

    now = datetime.now(timezone.utc)
    date_from = now.strftime("%Y-%m-%d")
    date_to = (now + timedelta(days=sync_days)).strftime("%Y-%m-%d")

    upserted = 0

    for league_id in league_ids:
        try:
            fixtures_data = await client.get_fixtures_by_date_range(
                league_id, date_from, date_to
            )
        except Exception:
            logger.warning(
                "Could not fetch fixtures for league %d; skipping", league_id
            )
            continue

        for fx in fixtures_data:
            fixture_id = fx.get("id")
            if not fixture_id:
                continue

            # Parse kickoff time
            kickoff_raw = fx.get("starting_at") or fx.get("kickoff_time")
            if not kickoff_raw:
                continue
            try:
                if isinstance(kickoff_raw, str):
                    kickoff = datetime.fromisoformat(
                        kickoff_raw.replace("Z", "+00:00")
                    )
                else:
                    kickoff = kickoff_raw
                if kickoff.tzinfo is None:
                    kickoff = kickoff.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                logger.warning(
                    "Unparseable kickoff for fixture %s: %s", fixture_id, kickoff_raw
                )
                continue

            # Resolve team names from participants array
            home_team = "Unknown"
            away_team = "Unknown"
            for participant in fx.get("participants", []):
                meta = participant.get("meta", {})
                location = meta.get("location")
                name = participant.get("name", "Unknown")
                if location == "home":
                    home_team = name
                elif location == "away":
                    away_team = name

            # Map state to our status vocabulary
            state = fx.get("state") or {}
            short_name = state.get("short_name") or fx.get("status", "scheduled")
            status = _STATUS_MAP.get(short_name, "scheduled")

            matchday = fx.get("round") or fx.get("matchday")
            if isinstance(matchday, dict):
                matchday = matchday.get("name") or matchday.get("round")
                try:
                    matchday = int(matchday)
                except (TypeError, ValueError):
                    matchday = None

            existing = await db.get(Fixture, fixture_id)
            if existing is None:
                db.add(
                    Fixture(
                        id=fixture_id,
                        home_team=home_team,
                        away_team=away_team,
                        kickoff_time=kickoff,
                        status=status,
                        league_id=league_id,
                        matchday=matchday,
                        last_synced_at=now,
                    )
                )
            else:
                existing.home_team = home_team
                existing.away_team = away_team
                existing.kickoff_time = kickoff
                existing.status = status
                existing.league_id = league_id
                if matchday is not None:
                    existing.matchday = matchday
                existing.last_synced_at = now

            upserted += 1

        await db.flush()

    await db.commit()
    return upserted


# ---------------------------------------------------------------------------
# Celery task wrapper
# ---------------------------------------------------------------------------


def _make_session() -> tuple:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


@celery_app.task(name="app.workers.fixture_sync.sync_fixtures_task")
def sync_fixtures_task() -> dict:
    """Celery entry point for §7.3 fixture sync."""

    async def _run() -> int:
        engine, factory = _make_session()
        client = SportmonksClient()
        try:
            async with factory() as db:
                return await sync_fixtures(db, client)
        finally:
            await client.close()
            await engine.dispose()

    return {"upserted": asyncio.run(_run())}

"""
Manual pipeline: import recent finished fixtures from Sportmonks then
compute oracle ratings for all players involved.

Run inside the api container:
    docker compose -f docker-compose.prod.yml exec api python3 sync_market.py

Steps
-----
1. Fetch fixtures from the last LOOKBACK_DAYS for every beta league.
2. Upsert finished ones into the Fixture table (last_synced_at = now).
3. Call check_finished_fixtures with a matching lookback window so every
   fixture upserted in step 2 is processed.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config import settings
from app.models.fixture import Fixture
from app.services.sportmonks import SportmonksClient
from app.workers.fixture_sync import BETA_LEAGUE_IDS, _STATUS_MAP
from app.workers.oracle_update import check_finished_fixtures

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# How many days back to look for finished fixtures
LOOKBACK_DAYS: int = 60


async def _import_past_fixtures(db, client: SportmonksClient) -> int:
    """Fetch the last LOOKBACK_DAYS of fixtures and upsert finished ones."""
    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    date_to = now.strftime("%Y-%m-%d")

    upserted = 0
    for league_id in BETA_LEAGUE_IDS:
        try:
            fixtures_data = await client.get_fixtures_by_date_range(league_id, date_from, date_to)
        except Exception:
            logger.warning("Could not fetch fixtures for league %d; skipping", league_id)
            continue

        for fx in fixtures_data:
            fixture_id = fx.get("id")
            if not fixture_id:
                continue

            state = fx.get("state") or {}
            short_name = state.get("short_name") or fx.get("status", "scheduled")
            status = _STATUS_MAP.get(short_name, "scheduled")
            if status != "finished":
                continue  # only import finished fixtures

            kickoff_raw = fx.get("starting_at") or fx.get("kickoff_time")
            if not kickoff_raw:
                continue
            try:
                kickoff = datetime.fromisoformat(kickoff_raw.replace("Z", "+00:00"))
                if kickoff.tzinfo is None:
                    kickoff = kickoff.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

            home_team, away_team = "Unknown", "Unknown"
            for participant in fx.get("participants", []):
                meta = participant.get("meta", {})
                name = participant.get("name", "Unknown")
                if meta.get("location") == "home":
                    home_team = name
                elif meta.get("location") == "away":
                    away_team = name

            matchday = fx.get("round") or fx.get("matchday")
            if isinstance(matchday, dict):
                matchday = matchday.get("name") or matchday.get("round")
                try:
                    matchday = int(matchday)
                except (TypeError, ValueError):
                    matchday = None

            existing = await db.get(Fixture, fixture_id)
            if existing is None:
                db.add(Fixture(
                    id=fixture_id,
                    home_team=home_team,
                    away_team=away_team,
                    kickoff_time=kickoff,
                    status=status,
                    league_id=league_id,
                    matchday=matchday,
                    last_synced_at=now,
                ))
            else:
                existing.status = status
                existing.last_synced_at = now  # mark as freshly synced so oracle sees it

            upserted += 1

        await db.flush()

    await db.commit()
    return upserted


async def main() -> None:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    client = SportmonksClient()

    try:
        async with factory() as db:
            logger.info(
                "Phase 1 — importing finished fixtures from the last %d days…",
                LOOKBACK_DAYS,
            )
            imported = await _import_past_fixtures(db, client)
            logger.info("Upserted %d finished fixture(s) into the DB.", imported)

            if imported == 0:
                logger.warning(
                    "No finished fixtures found from Sportmonks for the last %d days. "
                    "Check that SPORTMONKS_API_TOKEN and BETA_LEAGUE_IDS are correct.",
                    LOOKBACK_DAYS,
                )
                return

        async with factory() as db:
            logger.info("Phase 2 — running oracle update over those fixtures…")
            # Lookback covers the full import window plus a small buffer
            lookback_minutes = LOOKBACK_DAYS * 24 * 60 + 60
            updated = await check_finished_fixtures(db, client, lookback_minutes=lookback_minutes)
            logger.info("Oracle ratings updated for %d player(s).", updated)

    finally:
        await client.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

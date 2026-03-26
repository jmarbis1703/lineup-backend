"""Refresh player match ratings from Sportmonks fixture lineup data.

Fetches the last N days of fixtures for each team in the specified league(s)
and upserts per-match PlayerMatchRating rows — populating sportmonks_rating,
minutes_played, goals, assists, tackles, goals_conceded, etc.

Usage:
    python scripts/refresh_match_ratings.py [--league 8] [--days 90]

Defaults to Premier League (8). Pass multiple --league flags for more leagues.

Rate limit cost: ~1 Sportmonks API call per team (≈20 for PL) + 1 for season ID.
"""

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services.sportmonks import SportmonksClient
from app.workers.player_import import refresh_match_ratings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def main(league_ids: list[int], days_back: int) -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    async_session = async_sessionmaker(engine, expire_on_commit=False)
    client = SportmonksClient(
        api_token=settings.sportmonks_api_token,
        base_url=settings.sportmonks_base_url,
    )

    try:
        async with async_session() as db:
            for lid in league_ids:
                logger.info("Refreshing match ratings for league %d (last %d days)…", lid, days_back)
                result = await refresh_match_ratings(lid, db, client, days_back=days_back)
                logger.info(
                    "League %d done: %d fixtures synced, %d player-match rows upserted",
                    lid,
                    result["fixtures_synced"],
                    result["ratings_upserted"],
                )
    finally:
        await client.close()
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Refresh player match ratings from Sportmonks")
    parser.add_argument("--league", type=int, action="append", dest="leagues",
                        default=[], metavar="LEAGUE_ID",
                        help="Sportmonks league ID (may repeat; default: 8 = Premier League)")
    parser.add_argument("--days", type=int, default=90, dest="days",
                        help="Number of days back to fetch fixtures for (default: 90)")
    args = parser.parse_args()

    league_ids = args.leagues or [8]
    asyncio.run(main(league_ids, args.days))

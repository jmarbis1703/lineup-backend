"""One-shot script: import players from Sportmonks for configured leagues."""
import argparse
import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services.sportmonks import SportmonksClient
from app.workers.player_import import initial_player_import

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Big 5 European leagues: Premier League (8), La Liga (564), Bundesliga (82), Ligue 1 (384), Serie A (301)
DEFAULT_LEAGUE_IDS = [8, 564, 384, 82, 301]


async def main(league_ids: list[int]) -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    client = SportmonksClient(
        api_token=settings.sportmonks_api_token,
        base_url=settings.sportmonks_base_url,
    )
    try:
        async with session_factory() as db:
            logger.info("Starting initial player import for leagues: %s", league_ids)
            count = await initial_player_import(db, client, league_ids)
            logger.info("Imported %d players.", count)
    finally:
        await client.close()
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import players from Sportmonks.")
    parser.add_argument(
        "--league-id",
        type=int,
        default=None,
        help="Import a single league by ID (default: all 5 leagues)",
    )
    args = parser.parse_args()
    league_ids = [args.league_id] if args.league_id is not None else DEFAULT_LEAGUE_IDS
    asyncio.run(main(league_ids))

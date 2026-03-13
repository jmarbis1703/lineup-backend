"""One-shot script: import players from Sportmonks for configured leagues."""
import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services.sportmonks import SportmonksClient
from app.workers.player_import import initial_player_import

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Big 5 European leagues: Premier League (8), La Liga (564), Bundesliga (82), Ligue 1 (384), Serie A (301)
DEFAULT_LEAGUE_IDS = [82, 384, 301]


async def main() -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    client = SportmonksClient(
        api_token=settings.sportmonks_api_token,
        base_url=settings.sportmonks_base_url,
    )
    try:
        async with session_factory() as db:
            logger.info(
                "Starting initial player import for leagues: %s", DEFAULT_LEAGUE_IDS
            )
            count = await initial_player_import(db, client, DEFAULT_LEAGUE_IDS)
            logger.info("Imported %d players.", count)
    finally:
        await client.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

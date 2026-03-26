"""One-off script: backfill players.league from league_id where league IS NULL.

Usage:
    python scripts/backfill_league_names.py

Sportmonks league IDs:
    8   → Premier League
    564 → La Liga
    384 → Serie A
    301 → Ligue 1
    82  → Bundesliga
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.models.player import Player

LEAGUE_ID_TO_NAME = {
    8: "Premier League",
    564: "La Liga",
    384: "Serie A",
    301: "Ligue 1",
    82: "Bundesliga",
}

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+asyncpg://lineup:lineup_secret@localhost:5432/lineup")


async def main() -> None:
    engine = create_async_engine(DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        for league_id, league_name in LEAGUE_ID_TO_NAME.items():
            result = await session.execute(
                sa.update(Player)
                .where(Player.league_id == league_id, Player.league.is_(None))
                .values(league=league_name)
                .returning(Player.id)
            )
            updated = len(result.fetchall())
            if updated:
                print(f"  Updated {updated} players: league_id={league_id} → '{league_name}'")
        await session.commit()
        print("Done.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

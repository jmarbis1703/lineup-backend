"""
Prints the top 10 players by oracle rating (Sportmonks-derived).

oracle_rating  = computed by oracle_update after finished fixtures are processed.
current_rating = LMSR market price driven by user trades (not shown here).

Run inside the api container:
    docker compose -f docker-compose.prod.yml exec api python3 check_market.py
"""
import asyncio

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config import settings
from app.models.player import Player
from app.models.market_state import LmsrMarketState


async def main() -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as session:
        rows = await session.execute(
            sa.select(
                Player.name,
                Player.team,
                LmsrMarketState.oracle_rating,
                LmsrMarketState.oracle_source,
                LmsrMarketState.current_rating,
            )
            .join(LmsrMarketState, LmsrMarketState.player_id == Player.id)
            .where(LmsrMarketState.oracle_rating.isnot(None))
            .order_by(LmsrMarketState.oracle_rating.desc())
            .limit(10)
        )
        results = rows.all()

    await engine.dispose()

    if not results:
        print("No oracle ratings found — run sync_market.py first.")
        return

    print(f"{'#':<3} {'Name':<30} {'Team':<20} {'Oracle':>7} {'Source':<12} {'Market':>7}")
    print("-" * 82)
    for i, (name, team, oracle, source, market) in enumerate(results, 1):
        print(
            f"{i:<3} {name:<30} {(team or ''):<20} "
            f"{float(oracle):>7.4f} {(source or ''):<12} {float(market):>7.4f}"
        )


if __name__ == "__main__":
    asyncio.run(main())

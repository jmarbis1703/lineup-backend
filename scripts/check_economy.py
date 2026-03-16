"""Read-only economy audit: prints player market state summary."""
from __future__ import annotations

import asyncio
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config import settings
from app.models.player import Player
from app.models.market_state import LmsrMarketState


async def main() -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as db:
        # --- Summary counts ---
        total_q = await db.execute(select(func.count()).select_from(Player))
        total = total_q.scalar_one()

        for layer in ("layer_1", "layer_2", "layer_3"):
            count_q = await db.execute(
                select(func.count())
                .select_from(LmsrMarketState)
                .where(LmsrMarketState.oracle_source == layer)
            )
            count = count_q.scalar_one()
            print(f"  {layer}: {count} players")

        print(f"\nTotal players imported: {total}")
        print()

        # --- Top 15 by oracle_rating ---
        top_q = await db.execute(
            select(Player.name, LmsrMarketState.oracle_source, LmsrMarketState.oracle_rating)
            .join(LmsrMarketState, LmsrMarketState.player_id == Player.id)
            .where(LmsrMarketState.oracle_rating.isnot(None))
            .order_by(LmsrMarketState.oracle_rating.desc())
            .limit(15)
        )
        rows = top_q.all()

        print(f"{'Rank':<5} {'Name':<30} {'Source':<10} {'Rating':>6}")
        print("-" * 55)
        for i, (name, source, rating) in enumerate(rows, 1):
            print(f"{i:<5} {name:<30} {source:<10} {float(rating):>6.2f}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

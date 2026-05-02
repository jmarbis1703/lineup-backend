"""
One-off repair script: recalculate current_rating for all lmsr_market_state rows.

Root cause (fixed in trading.py on fix/b-display-rating):
  After any trade, current_rating was written using b_eff (with b_floor ~200,000
  at early user counts), collapsing all ratings to ~5.0. q values are encoded at
  b_min=100 scale, so decoding must always use b_min, not b_eff.

This script recomputes current_rating = lmsr_rating(q_up, q_down, b_min) for
every row and updates rows where the stored value differs by more than 0.0001.

Idempotent: safe to run more than once. Second run reports 0 rows changed.

Usage:
    python scripts/repair_current_rating.py [--dry-run]

Requirements:
    DATABASE_URL must be set in .env (or environment).
    Install requirements: pip install -r requirements.txt
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

# Patch sys.path so app imports work when run from lineup-backend/
sys.path.insert(0, ".")

from app.config import settings  # noqa: E402 — path must be set first
from app.core.lmsr import lmsr_rating  # noqa: E402
from app.models.market_state import LmsrMarketState  # noqa: E402

_TOLERANCE = Decimal("0.0001")  # NUMERIC(6,4) precision; skip rows within this band
_RATING_QUANT = Decimal("0.0001")


def _d4(value: float) -> Decimal:
    return Decimal(str(value)).quantize(_RATING_QUANT)


async def repair(dry_run: bool) -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as db:
        result = await db.execute(sa.select(LmsrMarketState))
        markets = result.scalars().all()

    total = len(markets)
    changed = 0
    skipped_correct = 0

    print(f"Rows to inspect: {total}")
    print()

    async with async_session() as db:
        async with db.begin():
            for market in markets:
                q_up_f = float(market.q_up)
                q_down_f = float(market.q_down)
                b_min_f = float(market.b_min)

                correct = _d4(lmsr_rating(q_up_f, q_down_f, b_min_f))
                stored = market.current_rating

                if stored is None or abs(correct - Decimal(str(stored))) > _TOLERANCE:
                    print(
                        f"  player_id={market.player_id:>6}  "
                        f"q_up={q_up_f:>12.6f}  q_down={q_down_f:>12.6f}  "
                        f"b_min={b_min_f:>7.2f}  "
                        f"stored={stored}  correct={correct}"
                    )
                    changed += 1
                    if not dry_run:
                        stmt = (
                            sa.update(LmsrMarketState)
                            .where(LmsrMarketState.id == market.id)
                            .values(current_rating=correct)
                        )
                        await db.execute(stmt)
                else:
                    skipped_correct += 1

    await engine.dispose()

    print()
    print("=" * 60)
    if dry_run:
        print(f"DRY RUN — no changes written.")
    print(f"Total rows:           {total}")
    print(f"Rows needing repair:  {changed}")
    print(f"Rows already correct: {skipped_correct}")
    if not dry_run and changed > 0:
        print(f"Updated {changed} rows successfully.")
    elif not dry_run and changed == 0:
        print("Nothing to do — all rows already correct.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print rows that would change without writing to the DB.",
    )
    args = parser.parse_args()

    if args.dry_run:
        print("DRY RUN mode — database will not be modified.")
        print()

    asyncio.run(repair(dry_run=args.dry_run))


if __name__ == "__main__":
    main()

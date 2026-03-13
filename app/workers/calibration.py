"""
Liquidity recalibration worker — §7.7.

liquidity_recalibration():
  Count active users; compute b_min_target via calibrate_b_min().
  For each player market apply the ONE-WAY RATCHET: b_min can only increase.
  When b_min grows, rescale q_up and q_down by the same factor to preserve
  the market rating invariant:

      lmsr_rating(q * s, b * s) == lmsr_rating(q, b)  for any s > 0

  FORBIDDEN: scaling positions.shares_owned.  Doing so would multiply every
  user's sell_refund by scale_factor (linear homogeneity), which is a confirmed
  hyper-inflation exploit (§7.7 PRD).

  FORBIDDEN: updating b_min WITHOUT rescaling q vectors.  Without rescaling,
  prices drift toward 5.0 with no trades.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.core.calibration import calibrate_b_min
from app.models.liquidity_config import LiquidityConfig
from app.models.market_state import LmsrMarketState
from app.models.user import User
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

_B_QUANT = Decimal("0.0001")
_Q_QUANT = Decimal("0.000001")


async def liquidity_recalibration(db: AsyncSession) -> int:
    """Apply one-way b_min ratchet with proportional q vector scaling.

    Steps per market:
      1. Read b_min, q_up, q_down.
      2. b_min_new = max(b_min, b_min_target).  Skip if b_min_new <= b_min.
      3. scale_factor = b_min_new / b_min  (always >= 1.0 by ratchet).
      4. new_q_up = q_up * scale_factor; new_q_down = q_down * scale_factor.
      5. Write atomically; do NOT touch positions.shares_owned.

    Returns number of markets updated.
    """
    # Count total users as the "active users" proxy
    users_res = await db.execute(sa.select(sa.func.count(User.id)))
    active_users = int(users_res.scalar() or 0)

    # Load global liquidity config
    config_res = await db.execute(sa.select(LiquidityConfig).limit(1))
    config = config_res.scalar_one_or_none()
    if config is None:
        logger.warning("No LiquidityConfig row; skipping recalibration")
        return 0

    base_liquidity = float(config.base_liquidity)
    n_reference = int(config.n_reference)

    # Compute target b_min (pure math function — one-way ratchet applied here)
    b_min_target = calibrate_b_min(base_liquidity, n_reference, active_users)

    # Fetch all market states
    mkt_res = await db.execute(sa.select(LmsrMarketState))
    markets = mkt_res.scalars().all()

    updated = 0
    for market in markets:
        b_min_current = float(market.b_min)

        # One-way ratchet: skip if target does not exceed current
        b_min_new = max(b_min_current, b_min_target)
        if b_min_new <= b_min_current:
            continue

        scale_factor = b_min_new / b_min_current  # always > 1.0

        new_q_up = float(market.q_up) * scale_factor
        new_q_down = float(market.q_down) * scale_factor

        market.b_min = Decimal(str(b_min_new)).quantize(_B_QUANT)
        market.q_up = Decimal(str(new_q_up)).quantize(_Q_QUANT)
        market.q_down = Decimal(str(new_q_down)).quantize(_Q_QUANT)
        market.updated_at = datetime.now(timezone.utc)
        updated += 1

    if updated > 0:
        config.last_calibrated_at = datetime.now(timezone.utc)
        await db.flush()
        await db.commit()

    return updated


# ---------------------------------------------------------------------------
# Celery task wrapper
# ---------------------------------------------------------------------------


@celery_app.task(name="app.workers.calibration.liquidity_recalibration_task")
def liquidity_recalibration_task() -> dict:
    """Celery entry point for §7.7 liquidity recalibration."""

    async def _run() -> int:
        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as db:
                return await liquidity_recalibration(db)
        finally:
            await engine.dispose()

    return {"updated": asyncio.run(_run())}

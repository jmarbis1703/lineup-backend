"""
Core trading engine: execute_buy, execute_sell, preview_buy.

All writes occur inside a transaction with SELECT ... FOR UPDATE locks.
Lock order (INV-05): portfolios row FIRST, then lmsr_market_state row.

Precision: fractional shares are quantised to exactly 6 decimal places
(NUMERIC(14,6)) *before* being applied to q_up / q_down to prevent the
Phantom Shares crash (see PRD §5.2).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.lmsr import (
    calculate_shares_for_budget,
    effective_b,
    lmsr_rating,
    sell_refund_down,
    sell_refund_up,
)
from app.models.market_state import LmsrMarketState
from app.models.player import Player
from app.models.portfolio import Portfolio
from app.models.position import Position
from app.models.rating_history import RatingHistory
from app.models.trade import Trade
from app.services.redis_cache import get_b_floor

# ---------------------------------------------------------------------------
# Precision constant
# ---------------------------------------------------------------------------

_QUANT = Decimal("0.000001")  # NUMERIC(14,6) — 6 decimal places
_RATING_QUANT = Decimal("0.0001")  # NUMERIC(6,4)


def _d6(value: float) -> Decimal:
    """Convert float → Decimal quantised to 6 dp, truncating (ROUND_DOWN)."""
    return Decimal(str(value)).quantize(_QUANT, rounding=ROUND_DOWN)


def _d4(value: float) -> Decimal:
    """Convert float → Decimal quantised to 4 dp (rating), half-even."""
    return Decimal(str(value)).quantize(_RATING_QUANT)


# ---------------------------------------------------------------------------
# Custom application exceptions — mapped to HTTP codes by the API layer
# ---------------------------------------------------------------------------


class PlayerNotFoundError(Exception):
    pass


class InactivePlayerError(Exception):
    """Player is in close-only mode (is_active = FALSE). Buys are blocked."""


class InsufficientPointsError(Exception):
    """User's available_points < requested budget (INV-02)."""


class InsufficientSharesError(Exception):
    """Sell quantity exceeds shares_owned (INV-03)."""


class DustBufferError(Exception):
    """Sell would drive q below -0.01 (INV-06 Dust Buffer)."""


class MarketNotFoundError(Exception):
    pass


class ZeroSharesError(Exception):
    """Budget too small — quantised share allocation is zero (INV-10).

    Raised in execute_buy when the computed shares, after truncation to 6
    decimal places (NUMERIC(14,6) precision), equal exactly zero.  Proceeding
    would deduct the budget from the portfolio and record a trade with zero
    shares, permanently losing the user's points with nothing in return.

    The caller should surface this as HTTP 400 with a user-facing message
    explaining that the budget is below the minimum effective trade size.
    """


# ---------------------------------------------------------------------------
# execute_buy
# ---------------------------------------------------------------------------


async def execute_buy(
    db: AsyncSession,
    user_id: uuid.UUID,
    player_id: int,
    direction: str,
    budget: Decimal,
) -> dict:
    """
    Budget-based buy.

    Lock order (INV-05):
      1. SELECT ... FOR UPDATE on portfolios
      2. SELECT ... FOR UPDATE on lmsr_market_state

    Returns dict: {shares, cost, rating_before, rating_after}.
    Caller is responsible for committing the session.
    """
    # ------------------------------------------------------------------
    # 0. Fetch dynamic b floor BEFORE any DB locks (§market-stability).
    #    B_FLOOR: high when few users, decreases toward LMSR_B_BASE as
    #    user count grows.  b is fixed for the duration of this trade —
    #    preserves LMSR budget-balance (b must be consistent across
    #    C_before / C_after).  See README "LMSR Dynamic b Floor" section
    #    and lmsr.py:b_floor_for_users() for formula and removal criteria.
    # ------------------------------------------------------------------
    b_floor_f = await get_b_floor(settings.redis_url, db)

    # ------------------------------------------------------------------
    # 1. Lock portfolio row FIRST (INV-05)
    # ------------------------------------------------------------------
    port_result = await db.execute(
        sa.select(Portfolio).where(Portfolio.user_id == user_id).with_for_update()
    )
    portfolio = port_result.scalar_one()

    # ------------------------------------------------------------------
    # 2. Check player.is_active BEFORE balance (Close-Only mode, §5.2)
    #    Surfaces a clear 403 rather than a confusing 400/422.
    # ------------------------------------------------------------------
    player_result = await db.execute(
        sa.select(Player).where(Player.id == player_id)
    )
    player = player_result.scalar_one_or_none()
    if player is None:
        raise PlayerNotFoundError(f"Player {player_id} not found")
    if not player.is_active:
        raise InactivePlayerError("Player is in close-only mode (buys blocked)")

    # ------------------------------------------------------------------
    # 3. Balance check (INV-02)
    # ------------------------------------------------------------------
    if budget > portfolio.available_points:
        raise InsufficientPointsError(
            f"Budget {budget} exceeds available_points {portfolio.available_points}"
        )

    # ------------------------------------------------------------------
    # 4. Lock market state SECOND (INV-05)
    # ------------------------------------------------------------------
    market_result = await db.execute(
        sa.select(LmsrMarketState)
        .where(LmsrMarketState.player_id == player_id)
        .with_for_update()
    )
    market = market_result.scalar_one_or_none()
    if market is None:
        raise MarketNotFoundError(f"No market found for player {player_id}")

    # ------------------------------------------------------------------
    # 5. LS-LMSR share calculation
    # ------------------------------------------------------------------
    q_up_f = float(market.q_up)
    q_down_f = float(market.q_down)
    alpha_f = float(market.alpha)
    b_min_f = float(market.b_min)
    b = effective_b(b_min_f, alpha_f, q_up_f, q_down_f, b_floor=b_floor_f)
    is_up = direction == "UP"

    raw_shares = calculate_shares_for_budget(
        q_up_f, q_down_f, b, float(budget), is_up
    )

    # ------------------------------------------------------------------
    # 6. Quantise to 6 dp (PRECISION guard — Phantom Shares prevention)
    # ------------------------------------------------------------------
    shares_dec = _d6(raw_shares)
    shares_f = float(shares_dec)

    # ------------------------------------------------------------------
    # 6a. Zero-shares guard (INV-10 — Phantom Deduction prevention)
    #
    #     If the budget is so small that no shares survive 6-dp truncation,
    #     proceeding would deduct the budget and record a 0-share trade —
    #     permanently losing the user's points with nothing in return.
    #     Raise before ANY database writes so the portfolio is never touched.
    # ------------------------------------------------------------------
    if shares_dec == Decimal("0"):
        raise ZeroSharesError(
            "Budget too small — no shares can be allocated at the current market "
            "price. Increase your budget or try a more active market."
        )

    # ------------------------------------------------------------------
    # 7. Rating before trade
    #    Use b_min (canonical coordinate space) — q values were seeded at
    #    b_min scale. b_eff (with b_floor) is only for cost calculation.
    # ------------------------------------------------------------------
    rating_before = _d4(lmsr_rating(q_up_f, q_down_f, b_min_f))

    # ------------------------------------------------------------------
    # 8. Update q vectors using exact Decimal arithmetic, then rating
    # ------------------------------------------------------------------
    if is_up:
        new_q_up = market.q_up + shares_dec
        new_q_down = market.q_down
    else:
        new_q_up = market.q_up
        new_q_down = market.q_down + shares_dec

    rating_after = _d4(lmsr_rating(float(new_q_up), float(new_q_down), b_min_f))

    market.q_up = new_q_up
    market.q_down = new_q_down
    market.current_rating = rating_after
    market.updated_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # 9. Deduct budget from portfolio (INV-02)
    # ------------------------------------------------------------------
    portfolio.available_points = portfolio.available_points - budget

    # ------------------------------------------------------------------
    # 10. Upsert position
    #     Cost basis rule (PRD §2.6):
    #       new_avg = ((old_shares × old_avg) + budget) / (old_shares + new_shares)
    # ------------------------------------------------------------------
    pos_result = await db.execute(
        sa.select(Position).where(
            Position.portfolio_id == portfolio.id,
            Position.player_id == player_id,
            Position.direction == direction,
        )
    )
    position = pos_result.scalar_one_or_none()

    if position is None:
        # First purchase: avg_entry = budget / shares
        avg_entry = _d6(float(budget) / shares_f) if shares_f > 0 else Decimal("0")
        position = Position(
            portfolio_id=portfolio.id,
            player_id=player_id,
            direction=direction,
            shares_owned=shares_dec,
            average_entry_price=avg_entry,
        )
        db.add(position)
    else:
        old_shares_f = float(position.shares_owned)
        old_avg_f = float(position.average_entry_price)
        new_total_f = old_shares_f + shares_f
        # Absolute-budget weighted average (PRD §2.6)
        new_avg_f = (
            ((old_shares_f * old_avg_f) + float(budget)) / new_total_f
            if new_total_f > 0
            else 0.0
        )
        position.shares_owned = position.shares_owned + shares_dec
        position.average_entry_price = _d6(new_avg_f)

    # ------------------------------------------------------------------
    # 11. Record trade ledger entry
    # ------------------------------------------------------------------
    price_per_share = _d6(float(budget) / shares_f) if shares_f > 0 else Decimal("0")
    db.add(
        Trade(
            portfolio_id=portfolio.id,
            player_id=player_id,
            type=f"BUY_{direction}",
            shares=shares_dec,
            cost_or_refund=budget,
            price_per_share=price_per_share,
            rating_before=rating_before,
            rating_after=rating_after,
        )
    )

    # ------------------------------------------------------------------
    # 12. Rating history
    # ------------------------------------------------------------------
    db.add(RatingHistory(player_id=player_id, rating=rating_after, source="trade"))

    await db.flush()

    return {
        "shares": shares_dec,
        "cost": budget,
        "rating_before": rating_before,
        "rating_after": rating_after,
    }


# ---------------------------------------------------------------------------
# execute_sell
# ---------------------------------------------------------------------------


async def execute_sell(
    db: AsyncSession,
    user_id: uuid.UUID,
    player_id: int,
    direction: str,
    shares: Decimal,
) -> dict:
    """
    Share-quantity sell.

    Lock order (INV-05): portfolios → lmsr_market_state.

    CRITICAL — Close-Only Mode: is_active is NOT checked here.
    Selling an inactive player's shares MUST always be allowed (§5.2).

    CRITICAL — Cost Basis: average_entry_price is NEVER modified on sells.

    Returns dict: {refund, rating_before, rating_after}.
    Caller is responsible for committing the session.
    """
    # ------------------------------------------------------------------
    # 0. Fetch dynamic b floor BEFORE any DB locks (§market-stability).
    #    B_FLOOR: high when few users, decreases toward LMSR_B_BASE as
    #    user count grows.  See README "LMSR Dynamic b Floor" and
    #    lmsr.py:b_floor_for_users() for formula and removal criteria.
    # ------------------------------------------------------------------
    b_floor_f = await get_b_floor(settings.redis_url, db)

    # ------------------------------------------------------------------
    # 1. Lock portfolio row FIRST (INV-05)
    # ------------------------------------------------------------------
    port_result = await db.execute(
        sa.select(Portfolio).where(Portfolio.user_id == user_id).with_for_update()
    )
    portfolio = port_result.scalar_one()

    # ------------------------------------------------------------------
    # 2. Validate share ownership (INV-03)
    # ------------------------------------------------------------------
    pos_result = await db.execute(
        sa.select(Position).where(
            Position.portfolio_id == portfolio.id,
            Position.player_id == player_id,
            Position.direction == direction,
        ).with_for_update()
    )
    position = pos_result.scalar_one_or_none()
    if position is None or position.shares_owned < shares:
        raise InsufficientSharesError(
            f"Cannot sell {shares} shares — only "
            f"{position.shares_owned if position else 0} owned"
        )

    # ------------------------------------------------------------------
    # 3. Lock market state SECOND (INV-05)
    # ------------------------------------------------------------------
    market_result = await db.execute(
        sa.select(LmsrMarketState)
        .where(LmsrMarketState.player_id == player_id)
        .with_for_update()
    )
    market = market_result.scalar_one_or_none()
    if market is None:
        raise MarketNotFoundError(f"No market found for player {player_id}")

    q_up_f = float(market.q_up)
    q_down_f = float(market.q_down)
    alpha_f = float(market.alpha)
    b_min_f = float(market.b_min)
    b = effective_b(b_min_f, alpha_f, q_up_f, q_down_f, b_floor=b_floor_f)
    shares_f = float(shares)

    # ------------------------------------------------------------------
    # 4. Dust Buffer guard (INV-06): q - shares >= -0.01
    # ------------------------------------------------------------------
    if direction == "UP":
        if q_up_f - shares_f < -0.01:
            raise DustBufferError(
                f"Sell of {shares} UP shares would violate Dust Buffer (q_up={q_up_f})"
            )
    else:
        if q_down_f - shares_f < -0.01:
            raise DustBufferError(
                f"Sell of {shares} DOWN shares would violate Dust Buffer (q_down={q_down_f})"
            )

    # ------------------------------------------------------------------
    # 5. LS-LMSR sell refund (pure math — accounts for AMM slippage)
    #    Linear multiplication is FORBIDDEN (PRD §2.6).
    #    Rating uses b_min (canonical coordinate space); refund uses b
    #    (with b_floor) for economic stability.
    # ------------------------------------------------------------------
    rating_before = _d4(lmsr_rating(q_up_f, q_down_f, b_min_f))

    if direction == "UP":
        refund_f = sell_refund_up(q_up_f, q_down_f, b, shares_f)
        new_q_up = market.q_up - shares
        new_q_down = market.q_down
    else:
        refund_f = sell_refund_down(q_up_f, q_down_f, b, shares_f)
        new_q_up = market.q_up
        new_q_down = market.q_down - shares

    rating_after = _d4(lmsr_rating(float(new_q_up), float(new_q_down), b_min_f))
    refund = _d6(refund_f)

    # ------------------------------------------------------------------
    # 6. Update market state
    # ------------------------------------------------------------------
    market.q_up = new_q_up
    market.q_down = new_q_down
    market.current_rating = rating_after
    market.updated_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # 7. Credit refund to portfolio
    # ------------------------------------------------------------------
    portfolio.available_points = portfolio.available_points + refund

    # ------------------------------------------------------------------
    # 8. Reduce position shares (do NOT touch average_entry_price — §2.6)
    # ------------------------------------------------------------------
    position.shares_owned = position.shares_owned - shares

    # ------------------------------------------------------------------
    # 9. Record trade
    # ------------------------------------------------------------------
    price_per_share = _d6(refund_f / shares_f) if shares_f > 0 else Decimal("0")
    db.add(
        Trade(
            portfolio_id=portfolio.id,
            player_id=player_id,
            type=f"SELL_{direction}",
            shares=shares,
            cost_or_refund=refund,
            price_per_share=price_per_share,
            rating_before=rating_before,
            rating_after=rating_after,
        )
    )

    # ------------------------------------------------------------------
    # 10. Rating history
    # ------------------------------------------------------------------
    db.add(RatingHistory(player_id=player_id, rating=rating_after, source="trade"))

    await db.flush()

    return {
        "refund": refund,
        "rating_before": rating_before,
        "rating_after": rating_after,
    }


# ---------------------------------------------------------------------------
# preview_buy  (read-only — no DB writes)
# ---------------------------------------------------------------------------


async def preview_buy(
    db: AsyncSession,
    player_id: int,
    direction: str,
    budget: Decimal,
) -> dict:
    """
    Calculate projected buy outcome without executing it (no DB writes).

    Returns dict: {shares, cost, rating_after}.
    """
    # Fetch b floor before market query — no locks here, but consistent with
    # execute_buy / execute_sell so preview accurately reflects real trade
    # behaviour.  See README "LMSR Dynamic b Floor" for details.
    b_floor_f = await get_b_floor(settings.redis_url, db)

    market_result = await db.execute(
        sa.select(LmsrMarketState).where(LmsrMarketState.player_id == player_id)
    )
    market = market_result.scalar_one_or_none()
    if market is None:
        raise MarketNotFoundError(f"No market found for player {player_id}")

    q_up_f = float(market.q_up)
    q_down_f = float(market.q_down)
    alpha_f = float(market.alpha)
    b_min_f = float(market.b_min)
    b = effective_b(b_min_f, alpha_f, q_up_f, q_down_f, b_floor=b_floor_f)
    is_up = direction == "UP"

    raw_shares = calculate_shares_for_budget(
        q_up_f, q_down_f, b, float(budget), is_up
    )
    shares_dec = _d6(raw_shares)
    shares_f = float(shares_dec)

    if is_up:
        new_q_up_f = q_up_f + shares_f
        new_q_down_f = q_down_f
    else:
        new_q_up_f = q_up_f
        new_q_down_f = q_down_f + shares_f

    rating_after = _d4(lmsr_rating(new_q_up_f, new_q_down_f, b_min_f))

    return {
        "shares": shares_dec,
        "cost": budget,
        "rating_after": rating_after,
    }

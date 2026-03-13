"""
Tests for liquidity_recalibration worker — §7.7.

test_calibration_updates_b_min              — b_min increases; q_up and q_down scale
test_calibration_thick_market_unaffected    — market with b_min > target is skipped
test_calibration_preserves_rating_invariant — lmsr_rating unchanged after scaling (NEW)
test_calibration_vector_scales_proportionally — q_up/old_q_up == new_b/old_b (NEW)
test_calibration_is_one_way_ratchet         — b_min never decreases (NEW)
test_calibration_does_not_modify_user_positions — shares_owned and avg_price unchanged (NEW)
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lmsr import lmsr_rating
from app.models.liquidity_config import LiquidityConfig
from app.models.market_state import LmsrMarketState
from app.models.player import Player
from app.models.portfolio import Portfolio
from app.models.position import Position
from app.models.user import User
from app.workers.calibration import liquidity_recalibration


# ---------------------------------------------------------------------------
# Helper: create the minimal rows needed for calibration tests
# ---------------------------------------------------------------------------


async def _make_player_market(
    db: AsyncSession,
    player_id: int,
    b_min: float,
    q_up: float,
    q_down: float,
    alpha: float = 0.0,
) -> LmsrMarketState:
    """Insert a Player + LmsrMarketState and return the market row."""
    now = datetime.now(timezone.utc)
    player = Player(
        id=player_id,
        name=f"Calib Player {player_id}",
        team="Calib FC",
        position_group="MF",
        league_id=999,
        is_active=True,
        last_synced_at=now,
    )
    db.add(player)
    await db.flush()

    # Compute initial current_rating from the given q/b values
    initial_rating = lmsr_rating(q_up, q_down, b_min)
    market = LmsrMarketState(
        player_id=player_id,
        alpha=Decimal(str(alpha)),
        b_min=Decimal(str(b_min)).quantize(Decimal("0.0001")),
        q_up=Decimal(str(q_up)).quantize(Decimal("0.000001")),
        q_down=Decimal(str(q_down)).quantize(Decimal("0.000001")),
        current_rating=Decimal(str(initial_rating)).quantize(Decimal("0.0001")),
        oracle_rating=Decimal(str(initial_rating)).quantize(Decimal("0.0001")),
        oracle_source="layer_1",
        updated_at=now,
    )
    db.add(market)
    await db.flush()
    return market


async def _make_users(db: AsyncSession, n: int, prefix: str = "calib_user") -> list[User]:
    """Insert n users and return them."""
    users = []
    for i in range(n):
        user = User(
            email=f"{prefix}_{i}@calib.test",
            password_hash="$2b$12$testhashplaceholderfortestingonly",
            username=f"{prefix}_{i}",
        )
        db.add(user)
        users.append(user)
    await db.flush()
    return users


async def _make_config(
    db: AsyncSession,
    base_liquidity: float,
    n_reference: int,
    alpha_default: float = 0.0,
) -> LiquidityConfig:
    """Insert a LiquidityConfig row and return it."""
    cfg = LiquidityConfig(
        base_liquidity=Decimal(str(base_liquidity)).quantize(Decimal("0.0001")),
        n_reference=n_reference,
        alpha_default=Decimal(str(alpha_default)),
        last_calibrated_at=datetime.now(timezone.utc),
    )
    db.add(cfg)
    await db.flush()
    return cfg


# ---------------------------------------------------------------------------
# test_calibration_updates_b_min
# ---------------------------------------------------------------------------


async def test_calibration_updates_b_min(db_session: AsyncSession) -> None:
    """When active_users > n_reference, b_min increases and q vectors scale."""
    # 4 users, n_reference=1, base_liquidity=100
    # target = 100 * sqrt(4/1) = 200  →  scale_factor = 200/100 = 2.0
    await _make_users(db_session, 4, prefix="upd_user")
    await _make_config(db_session, base_liquidity=100, n_reference=1)

    market = await _make_player_market(
        db_session, player_id=3001, b_min=100, q_up=50.0, q_down=10.0
    )
    old_q_up = float(market.q_up)
    old_q_down = float(market.q_down)

    updated = await liquidity_recalibration(db_session)

    assert updated == 1
    await db_session.refresh(market)

    assert float(market.b_min) == pytest.approx(200.0, rel=1e-4)
    # q vectors must have changed (scaled)
    assert float(market.q_up) != old_q_up
    assert float(market.q_down) != old_q_down
    assert float(market.q_up) == pytest.approx(old_q_up * 2.0, rel=1e-4)
    assert float(market.q_down) == pytest.approx(old_q_down * 2.0, rel=1e-4)


# ---------------------------------------------------------------------------
# test_calibration_thick_market_unaffected
# ---------------------------------------------------------------------------


async def test_calibration_thick_market_unaffected(db_session: AsyncSession) -> None:
    """A market with b_min already >= target is skipped (ratchet no-op)."""
    # 1 user, n_reference=100, base_liquidity=100
    # target = 100 * sqrt(1/100) = 10.0  <  b_min=300  →  ratchet skips
    await _make_users(db_session, 1, prefix="thick_user")
    await _make_config(db_session, base_liquidity=100, n_reference=100)

    market = await _make_player_market(
        db_session, player_id=3002, b_min=300, q_up=120.0, q_down=30.0
    )
    old_b_min = float(market.b_min)
    old_q_up = float(market.q_up)
    old_q_down = float(market.q_down)

    updated = await liquidity_recalibration(db_session)

    assert updated == 0
    await db_session.refresh(market)
    assert float(market.b_min) == old_b_min
    assert float(market.q_up) == old_q_up
    assert float(market.q_down) == old_q_down


# ---------------------------------------------------------------------------
# test_calibration_preserves_rating_invariant  (NEW)
# ---------------------------------------------------------------------------


async def test_calibration_preserves_rating_invariant(db_session: AsyncSession) -> None:
    """lmsr_rating must be identical before and after recalibration (homogeneity)."""
    # target = 100 * sqrt(4/1) = 200  →  scale_factor = 2.0
    await _make_users(db_session, 4, prefix="rating_inv_user")
    await _make_config(db_session, base_liquidity=100, n_reference=1)

    q_up_init, q_down_init, b_min_init = 50.0, 10.0, 100.0
    market = await _make_player_market(
        db_session,
        player_id=3003,
        b_min=b_min_init,
        q_up=q_up_init,
        q_down=q_down_init,
    )

    rating_before = lmsr_rating(q_up_init, q_down_init, b_min_init)

    await liquidity_recalibration(db_session)
    await db_session.refresh(market)

    rating_after = lmsr_rating(
        float(market.q_up), float(market.q_down), float(market.b_min)
    )

    assert abs(rating_after - rating_before) < 1e-6, (
        f"Rating must be preserved: before={rating_before:.8f}, after={rating_after:.8f}"
    )


# ---------------------------------------------------------------------------
# test_calibration_vector_scales_proportionally  (NEW)
# ---------------------------------------------------------------------------


async def test_calibration_vector_scales_proportionally(db_session: AsyncSession) -> None:
    """new_q_up / old_q_up == new_b_min / old_b_min (same ratio for both q vectors)."""
    # scale_factor = 2.0
    await _make_users(db_session, 4, prefix="prop_user")
    await _make_config(db_session, base_liquidity=100, n_reference=1)

    q_up_init, q_down_init, b_min_init = 60.0, 20.0, 100.0
    market = await _make_player_market(
        db_session,
        player_id=3004,
        b_min=b_min_init,
        q_up=q_up_init,
        q_down=q_down_init,
    )
    old_b_min = float(market.b_min)
    old_q_up = float(market.q_up)
    old_q_down = float(market.q_down)

    await liquidity_recalibration(db_session)
    await db_session.refresh(market)

    new_b = float(market.b_min)
    expected_scale = new_b / old_b_min

    assert float(market.q_up) == pytest.approx(old_q_up * expected_scale, rel=1e-4)
    assert float(market.q_down) == pytest.approx(old_q_down * expected_scale, rel=1e-4)


# ---------------------------------------------------------------------------
# test_calibration_is_one_way_ratchet  (NEW)
# ---------------------------------------------------------------------------


async def test_calibration_is_one_way_ratchet(db_session: AsyncSession) -> None:
    """b_min never decreases even when calibrate_b_min() returns a lower value."""
    # 1 user, n_reference=100, base_liquidity=200
    # target = 200 * sqrt(1/100) = 20.0  <  b_min=200  →  ratchet: keep 200
    await _make_users(db_session, 1, prefix="ratchet_user")
    await _make_config(db_session, base_liquidity=200, n_reference=100)

    market = await _make_player_market(
        db_session, player_id=3005, b_min=200, q_up=80.0, q_down=40.0
    )
    old_b_min = float(market.b_min)
    old_q_up = float(market.q_up)
    old_q_down = float(market.q_down)

    updated = await liquidity_recalibration(db_session)

    # No DB writes should have occurred
    assert updated == 0
    await db_session.refresh(market)
    assert float(market.b_min) == old_b_min, "b_min must not decrease"
    assert float(market.q_up) == old_q_up, "q_up must not change when ratchet fires"
    assert float(market.q_down) == old_q_down, "q_down must not change when ratchet fires"


# ---------------------------------------------------------------------------
# test_calibration_does_not_modify_user_positions  (NEW)
# ---------------------------------------------------------------------------


async def test_calibration_does_not_modify_user_positions(
    db_session: AsyncSession,
) -> None:
    """positions.shares_owned and average_entry_price are NEVER modified.

    After recalibration with scale_factor > 1:
    - market.q_up == old_q_up * scale_factor  (market IS scaled)
    - position.shares_owned == 10  (positions are NOT scaled)
    - After a hypothetical sell of 10 shares: new_q_up - 10 >= -0.01  (INV-06)
    """
    # scale_factor = sqrt(5) (5 users total: 4 here + 1 position-test user below)
    # active_users=5, n_reference=1 → target = 100*√5 ≈ 223.6 → scale=√5
    await _make_users(db_session, 4, prefix="pos_user")
    await _make_config(db_session, base_liquidity=100, n_reference=1)

    q_up_init, q_down_init, b_min_init = 50.0, 10.0, 100.0
    market = await _make_player_market(
        db_session,
        player_id=3006,
        b_min=b_min_init,
        q_up=q_up_init,
        q_down=q_down_init,
    )

    # Create a user + portfolio + UP position of 10 shares in this market
    user = User(
        email="postest@calib.test",
        password_hash="$2b$12$testhashplaceholderfortestingonly",
        username="postest_calib",
    )
    db_session.add(user)
    await db_session.flush()

    portfolio = Portfolio(user_id=user.id, available_points=Decimal("1000.0000"))
    db_session.add(portfolio)
    await db_session.flush()

    position = Position(
        portfolio_id=portfolio.id,
        player_id=3006,
        direction="UP",
        shares_owned=Decimal("10.000000"),
        average_entry_price=Decimal("5.000000"),
    )
    db_session.add(position)
    await db_session.flush()

    old_q_up = float(market.q_up)

    await liquidity_recalibration(db_session)

    await db_session.refresh(market)
    await db_session.refresh(position)

    # Market IS scaled
    assert float(market.q_up) == pytest.approx(old_q_up * math.sqrt(5), rel=1e-4), (
        "market.q_up must be scaled by scale_factor"
    )

    # Position is NOT scaled
    assert float(position.shares_owned) == pytest.approx(10.0), (
        "shares_owned must NOT be modified by recalibration"
    )
    assert float(position.average_entry_price) == pytest.approx(5.0), (
        "average_entry_price must NOT be modified by recalibration"
    )

    # INV-06: selling 10 UP shares from the scaled market must not violate Dust Buffer
    new_q_up_after_sell = float(market.q_up) - 10.0
    assert new_q_up_after_sell >= -0.01, (
        f"q_up after sell = {new_q_up_after_sell:.6f}; violates INV-06 Dust Buffer"
    )

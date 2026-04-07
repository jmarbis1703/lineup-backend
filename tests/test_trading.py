"""
Tests for the trading engine: execute_buy, execute_sell, preview_buy,
and the /api/trade/* HTTP endpoints.

Pattern for HTTP tests:
  - `api_client` fixture overrides `get_db` with `db_session` so that
    data created in the test transaction is visible to HTTP requests AND
    everything rolls back automatically at test teardown.

Pattern for the concurrent test:
  - Uses db_engine directly to create sessions that actually commit,
    enabling real cross-session locking (SELECT ... FOR UPDATE).
    Test data is cleaned up in a finally block.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lmsr import calculate_shares_for_budget, effective_b, lmsr_rating
from app.core.trading import (
    InactivePlayerError,
    InsufficientPointsError,
    InsufficientSharesError,
    execute_buy,
    execute_sell,
    preview_buy,
)
from app.dependencies import get_db
from app.main import app as fastapi_app
from app.models.fixture import Fixture
from app.models.market_state import LmsrMarketState
from app.models.player import Player
from app.models.portfolio import Portfolio
from app.models.position import Position
from app.models.tournament import Tournament
from app.models.tournament_member import TournamentMember
from app.models.user import User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token(user_id: uuid.UUID) -> str:
    from app.config import settings
    from datetime import timedelta

    exp = datetime.now(timezone.utc) + timedelta(minutes=60)
    return jwt.encode(
        {"sub": str(user_id), "exp": exp},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


async def _create_user_portfolio(
    db: AsyncSession,
    username: str,
    points: Decimal = Decimal("1000.0000"),
) -> tuple[User, Portfolio]:
    user = User(
        email=f"{username}@trade.test",
        password_hash="$2b$12$testhashplaceholderfortestingonly",
        username=username,
    )
    db.add(user)
    await db.flush()
    portfolio = Portfolio(user_id=user.id, available_points=points)
    db.add(portfolio)
    await db.flush()
    return user, portfolio


async def _get_portfolio(db: AsyncSession, user_id: uuid.UUID) -> Portfolio:
    result = await db.execute(sa.select(Portfolio).where(Portfolio.user_id == user_id))
    return result.scalar_one()


async def _get_position(
    db: AsyncSession,
    portfolio_id: uuid.UUID,
    player_id: int,
    direction: str,
) -> Position | None:
    result = await db.execute(
        sa.select(Position).where(
            Position.portfolio_id == portfolio_id,
            Position.player_id == player_id,
            Position.direction == direction,
        )
    )
    return result.scalar_one_or_none()


async def _get_market(db: AsyncSession, player_id: int) -> LmsrMarketState:
    result = await db.execute(
        sa.select(LmsrMarketState).where(LmsrMarketState.player_id == player_id)
    )
    return result.scalar_one()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """
    HTTP client that shares `db_session` via dependency override.
    Data created in db_session is visible to HTTP requests; everything
    rolls back when the test ends (SAVEPOINT mode).
    """

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    fastapi_app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app), base_url="http://test"
    ) as client:
        yield client
    fastapi_app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# test_buy_up_success
# ---------------------------------------------------------------------------


async def test_buy_up_success(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """Budget=50 on UP → shares > 0, rating moves UP."""
    user, portfolio = await _create_user_portfolio(db_session, "buyer_up")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}

    resp = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "UP", "budget": 50.0},
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert Decimal(str(data["shares"])) > 0
    assert Decimal(str(data["rating_after"])) > Decimal(str(data["rating_before"]))


# ---------------------------------------------------------------------------
# test_buy_down_success
# ---------------------------------------------------------------------------


async def test_buy_down_success(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """Budget=50 on DOWN → shares > 0, rating moves DOWN."""
    user, portfolio = await _create_user_portfolio(db_session, "buyer_down")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}

    resp = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "DOWN", "budget": 50.0},
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert Decimal(str(data["shares"])) > 0
    # DOWN buy should push rating toward 0
    assert Decimal(str(data["rating_after"])) < Decimal(str(data["rating_before"]))

    # Verify position was created for DOWN direction
    port = await _get_portfolio(db_session, user.id)
    pos = await _get_position(db_session, port.id, 99, "DOWN")
    assert pos is not None
    assert pos.shares_owned > 0


# ---------------------------------------------------------------------------
# test_buy_budget_equals_deduction
# ---------------------------------------------------------------------------


async def test_buy_budget_equals_deduction(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """Points deducted from portfolio equals exactly the budget spent."""
    user, portfolio = await _create_user_portfolio(db_session, "buyer_deduct")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}
    budget = Decimal("75.0")

    resp = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "UP", "budget": float(budget)},
        headers=headers,
    )
    assert resp.status_code == 200

    port_after = await _get_portfolio(db_session, user.id)
    assert port_after.available_points == Decimal("1000.0000") - budget


# ---------------------------------------------------------------------------
# test_buy_budget_roundtrip
# ---------------------------------------------------------------------------


async def test_buy_budget_roundtrip(
    db_session: AsyncSession,
    sample_player_with_market,
) -> None:
    """Shares returned by execute_buy match LS-LMSR math within 1e-6."""
    user, portfolio = await _create_user_portfolio(db_session, "buyer_rt")
    budget = Decimal("50.0")

    result = await execute_buy(db_session, user.id, 99, "UP", budget)
    actual_shares = float(result["shares"])

    # Independently recalculate
    market = await _get_market(db_session, 99)
    # market is now updated; get original q from before the buy
    # Use the pre-buy market (q_up=0, q_down=0, b_min=100, alpha=0.05 — from fixture)
    q_up_orig = 0.0
    q_down_orig = 0.0
    alpha = 0.05
    b_min = 100.0
    b = effective_b(q_up_orig, q_down_orig, alpha, b_min)
    expected_shares = calculate_shares_for_budget(
        q_up_orig, q_down_orig, b, float(budget), True
    )

    assert abs(actual_shares - expected_shares) < 1e-6


# ---------------------------------------------------------------------------
# test_buy_insufficient_points
# ---------------------------------------------------------------------------


async def test_buy_insufficient_points(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """Budget > available_points → HTTP 400."""
    user, _ = await _create_user_portfolio(db_session, "buyer_broke", Decimal("10.0"))
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}

    resp = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "UP", "budget": 100.0},
        headers=headers,
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# test_buy_zero_budget
# ---------------------------------------------------------------------------


async def test_buy_zero_budget(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """Budget = 0 → HTTP 422 (Pydantic validation: budget must be > 0)."""
    user, _ = await _create_user_portfolio(db_session, "buyer_zero")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}

    resp = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "UP", "budget": 0},
        headers=headers,
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# test_sell_up_success
# ---------------------------------------------------------------------------


async def test_sell_up_success(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """Buy UP then sell UP → refund > 0, shares reduced."""
    user, portfolio = await _create_user_portfolio(db_session, "seller_up")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}

    buy_resp = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "UP", "budget": 50.0},
        headers=headers,
    )
    assert buy_resp.status_code == 200
    bought_shares = Decimal(str(buy_resp.json()["shares"]))

    sell_resp = await api_client.post(
        "/api/trade/sell",
        json={"player_id": 99, "direction": "UP", "shares": float(bought_shares)},
        headers=headers,
    )
    assert sell_resp.status_code == 200
    data = sell_resp.json()
    assert Decimal(str(data["refund"])) > 0

    # Shares should be 0 after full sell
    port = await _get_portfolio(db_session, user.id)
    pos = await _get_position(db_session, port.id, 99, "UP")
    assert pos is None or pos.shares_owned == 0


# ---------------------------------------------------------------------------
# test_sell_down_success
# ---------------------------------------------------------------------------


async def test_sell_down_success(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """Buy DOWN then sell DOWN → refund > 0."""
    user, portfolio = await _create_user_portfolio(db_session, "seller_down")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}

    buy_resp = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "DOWN", "budget": 50.0},
        headers=headers,
    )
    assert buy_resp.status_code == 200
    bought_shares = Decimal(str(buy_resp.json()["shares"]))

    sell_resp = await api_client.post(
        "/api/trade/sell",
        json={"player_id": 99, "direction": "DOWN", "shares": float(bought_shares)},
        headers=headers,
    )
    assert sell_resp.status_code == 200
    assert Decimal(str(sell_resp.json()["refund"])) > 0


# ---------------------------------------------------------------------------
# test_sell_insufficient_shares
# ---------------------------------------------------------------------------


async def test_sell_insufficient_shares(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """Sell more shares than owned → HTTP 400."""
    user, _ = await _create_user_portfolio(db_session, "seller_insufficient")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}

    resp = await api_client.post(
        "/api/trade/sell",
        json={"player_id": 99, "direction": "UP", "shares": 999.0},
        headers=headers,
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# test_buy_sell_roundtrip_ls_lmsr
# ---------------------------------------------------------------------------


async def test_buy_sell_roundtrip_ls_lmsr(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """
    Buy then immediately sell all shares: refund ≤ budget (AMM spread).
    This verifies the LMSR cost function direction is correct.
    """
    user, _ = await _create_user_portfolio(db_session, "rt_user")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}
    budget = 100.0

    buy_resp = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "UP", "budget": budget},
        headers=headers,
    )
    assert buy_resp.status_code == 200
    shares = float(buy_resp.json()["shares"])

    sell_resp = await api_client.post(
        "/api/trade/sell",
        json={"player_id": 99, "direction": "UP", "shares": shares},
        headers=headers,
    )
    assert sell_resp.status_code == 200
    refund = float(sell_resp.json()["refund"])

    # The AMM spread means the refund is AT MOST the original budget
    assert refund <= budget + 1e-6, f"Refund {refund} exceeded budget {budget}"


# ---------------------------------------------------------------------------
# test_concurrent_buys_no_race
# ---------------------------------------------------------------------------


async def test_concurrent_buys_no_race(db_engine, apply_migrations) -> None:
    """
    5 concurrent buys by the same user should never produce a negative balance.
    Uses real committed sessions so SELECT ... FOR UPDATE serialises access.
    """
    from datetime import datetime, timezone

    player_id = 88801  # unique ID for this test
    user_email = "concurrent_trade@test.invalid"

    # --- Setup: commit test data so it's visible to all sessions ------------
    user_id_holder: list[uuid.UUID] = []
    try:
        async with AsyncSession(db_engine) as setup:
            async with setup.begin():
                user = User(
                    email=user_email,
                    password_hash="$2b$12$testhashplaceholderfortestingonly",
                    username="concurrent_trader",
                )
                setup.add(user)
                await setup.flush()

                # Capture user.id while session is active (avoids lazy-load after commit)
                captured_user_id = user.id
                user_id_holder.append(captured_user_id)

                portfolio = Portfolio(
                    user_id=captured_user_id,
                    available_points=Decimal("500.0000"),  # 5 × 100 = exactly 500
                )
                setup.add(portfolio)

                player = Player(
                    id=player_id,
                    name="Concurrent Player",
                    team="Test FC",
                    position="Midfielder",
                    position_group="MF",
                    league="Test League",
                    league_id=9999,
                    is_active=True,
                    last_synced_at=datetime.now(timezone.utc),
                )
                setup.add(player)
                await setup.flush()

                market = LmsrMarketState(
                    player_id=player_id,
                    alpha=Decimal("0.050000"),
                    b_min=Decimal("100.0000"),
                    q_up=Decimal("0.000000"),
                    q_down=Decimal("0.000000"),
                    current_rating=Decimal("7.0000"),
                    oracle_rating=Decimal("7.0000"),
                    oracle_source="layer_3",
                    updated_at=datetime.now(timezone.utc),
                )
                setup.add(market)

        user_id = user_id_holder[0]

        # --- Run 5 concurrent buys of 100 each (total = 500 = available) ----
        async def single_buy() -> str:
            async with AsyncSession(db_engine) as session:
                try:
                    await execute_buy(session, user_id, player_id, "UP", Decimal("100"))
                    await session.commit()
                    return "ok"
                except (InsufficientPointsError, Exception):
                    await session.rollback()
                    return "failed"

        results = await asyncio.gather(*[single_buy() for _ in range(5)])

        # --- Verify: no negative balance ------------------------------------
        async with AsyncSession(db_engine) as check:
            port = (
                await check.execute(
                    sa.select(Portfolio).where(Portfolio.user_id == user_id)
                )
            ).scalar_one()
            assert port.available_points >= Decimal("0"), (
                f"Negative balance detected: {port.available_points}"
            )

        # At least some buys should have succeeded (we have exactly enough)
        ok_count = results.count("ok")
        assert ok_count > 0, "No buys succeeded — unexpected"

    finally:
        # --- Cleanup: delete committed test data ----------------------------
        async with AsyncSession(db_engine) as cleanup:
            async with cleanup.begin():
                await cleanup.execute(
                    sa.delete(Portfolio).where(Portfolio.user_id.in_(
                        sa.select(User.id).where(User.email == user_email)
                    ))
                )
                await cleanup.execute(
                    sa.delete(LmsrMarketState).where(
                        LmsrMarketState.player_id == player_id
                    )
                )
                await cleanup.execute(
                    sa.delete(Player).where(Player.id == player_id)
                )
                await cleanup.execute(
                    sa.delete(User).where(User.email == user_email)
                )


# ---------------------------------------------------------------------------
# test_rating_bounds_after_massive_buy
# ---------------------------------------------------------------------------


async def test_rating_bounds_after_massive_buy(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """Rating must stay ≤ 10.0 even after an enormous UP buy (INV-01)."""
    user, _ = await _create_user_portfolio(
        db_session, "massive_buyer", Decimal("999999.0000")
    )
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}

    resp = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "UP", "budget": 999999.0},
        headers=headers,
    )
    assert resp.status_code == 200
    rating_after = float(resp.json()["rating_after"])
    assert rating_after <= 10.0
    assert rating_after >= 0.0


# ---------------------------------------------------------------------------
# test_down_buy_lowers_rating
# ---------------------------------------------------------------------------


async def test_down_buy_lowers_rating(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """Buying DOWN shares decreases the market rating."""
    user, _ = await _create_user_portfolio(db_session, "down_buyer_rating")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}

    resp = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "DOWN", "budget": 200.0},
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert Decimal(str(data["rating_after"])) < Decimal(str(data["rating_before"]))


# ---------------------------------------------------------------------------
# test_preview_matches_execution
# ---------------------------------------------------------------------------


async def test_preview_matches_execution(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """Preview shares must match actual execute_buy shares within 1e-6."""
    user, _ = await _create_user_portfolio(db_session, "preview_user")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}
    budget = 75.0

    preview_resp = await api_client.post(
        "/api/trade/preview",
        json={"player_id": 99, "direction": "UP", "budget": budget},
        headers=headers,
    )
    assert preview_resp.status_code == 200
    preview_shares = float(preview_resp.json()["shares"])

    buy_resp = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "UP", "budget": budget},
        headers=headers,
    )
    assert buy_resp.status_code == 200
    actual_shares = float(buy_resp.json()["shares"])

    assert abs(preview_shares - actual_shares) < 1e-6


# ---------------------------------------------------------------------------
# test_two_users_same_market
# ---------------------------------------------------------------------------


async def test_two_users_same_market(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """Two users buying UP both move the same q_up vector."""
    alice, _ = await _create_user_portfolio(db_session, "alice_market")
    bob, _ = await _create_user_portfolio(db_session, "bob_market")
    alice_hdr = {"Authorization": f"Bearer {_make_token(alice.id)}"}
    bob_hdr = {"Authorization": f"Bearer {_make_token(bob.id)}"}

    market_before = await _get_market(db_session, 99)
    q_up_before = market_before.q_up

    await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "UP", "budget": 50.0},
        headers=alice_hdr,
    )
    await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "UP", "budget": 50.0},
        headers=bob_hdr,
    )

    # Refresh market state from the shared session
    await db_session.refresh(market_before)
    q_up_after = market_before.q_up

    assert q_up_after > q_up_before, "Both buys should have increased q_up"


# ---------------------------------------------------------------------------
# test_buy_updates_average_entry_price
# ---------------------------------------------------------------------------


async def test_buy_updates_average_entry_price(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """
    Cost basis formula (PRD §2.6):
      new_avg = ((old_shares × old_avg) + budget) / (old_shares + new_shares)

    We verify the formula is applied correctly across two consecutive buys.
    """
    user, portfolio = await _create_user_portfolio(db_session, "avg_price_user")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}

    # First buy
    r1 = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "UP", "budget": 50.0},
        headers=headers,
    )
    assert r1.status_code == 200
    s1 = Decimal(str(r1.json()["shares"]))  # first_shares

    # Second buy
    r2 = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "UP", "budget": 100.0},
        headers=headers,
    )
    assert r2.status_code == 200
    s2 = Decimal(str(r2.json()["shares"]))  # second_shares

    # Expected avg: (50 + 100) / (s1 + s2)  — absolute budget formula (PRD §2.6)
    expected_avg = Decimal("150") / (s1 + s2)

    pos = await _get_position(db_session, portfolio.id, 99, "UP")
    assert pos is not None
    # Allow for 6dp rounding
    assert abs(pos.average_entry_price - expected_avg) < Decimal("0.000002"), (
        f"Expected avg ≈ {expected_avg}, got {pos.average_entry_price}"
    )


# ---------------------------------------------------------------------------
# test_sell_preserves_average_entry_price  (CRITICAL)
# ---------------------------------------------------------------------------


async def test_sell_preserves_average_entry_price(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """
    After a partial sell, average_entry_price MUST remain unchanged.
    Sells reduce shares_owned only (PRD §2.6).
    """
    user, portfolio = await _create_user_portfolio(db_session, "sell_preserve_avg")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}

    buy_resp = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "UP", "budget": 100.0},
        headers=headers,
    )
    assert buy_resp.status_code == 200
    bought_shares = Decimal(str(buy_resp.json()["shares"]))

    pos_after_buy = await _get_position(db_session, portfolio.id, 99, "UP")
    avg_before_sell = pos_after_buy.average_entry_price

    # Partial sell (half the shares)
    sell_amount = Decimal(str(float(bought_shares) / 2)).quantize(Decimal("0.000001"))
    sell_resp = await api_client.post(
        "/api/trade/sell",
        json={"player_id": 99, "direction": "UP", "shares": float(sell_amount)},
        headers=headers,
    )
    assert sell_resp.status_code == 200

    # Refresh position
    await db_session.refresh(pos_after_buy)
    assert pos_after_buy.average_entry_price == avg_before_sell, (
        f"average_entry_price changed from {avg_before_sell} to "
        f"{pos_after_buy.average_entry_price} after a sell"
    )
    assert pos_after_buy.shares_owned < bought_shares


# ---------------------------------------------------------------------------
# test_shares_quantized_to_six_decimals
# ---------------------------------------------------------------------------


async def test_shares_quantized_to_six_decimals(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """shares_owned must not exceed 6 decimal places (NUMERIC(14,6))."""
    user, portfolio = await _create_user_portfolio(db_session, "quant_user")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}

    resp = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "UP", "budget": 37.5},
        headers=headers,
    )
    assert resp.status_code == 200

    pos = await _get_position(db_session, portfolio.id, 99, "UP")
    assert pos is not None

    # Check string representation has at most 6 decimal places
    shares_str = str(pos.shares_owned)
    if "." in shares_str:
        decimal_places = len(shares_str.split(".")[1])
        assert decimal_places <= 6, (
            f"shares_owned has {decimal_places} decimal places: {shares_str}"
        )


# ---------------------------------------------------------------------------
# test_buy_not_blocked_by_completed_tournament  (NEW)
# ---------------------------------------------------------------------------


async def test_buy_not_blocked_by_completed_tournament(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """
    A user who is a member of a completed tournament MUST still be able to buy.
    The trade engine MUST NOT query tournament status (PRD §5.2 EXPLICIT PROHIBITION).
    """
    from datetime import timedelta

    user, _ = await _create_user_portfolio(db_session, "tourney_member_buyer")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}
    now = datetime.now(timezone.utc)

    # Create a completed tournament
    tournament = Tournament(
        name="Old Tournament",
        status="completed",
        created_by=user.id,
        start_time=now - timedelta(days=10),
        end_time=now - timedelta(days=3),
        created_at=now - timedelta(days=11),
    )
    db_session.add(tournament)
    await db_session.flush()

    member = TournamentMember(
        tournament_id=tournament.id,
        user_id=user.id,
        starting_value=Decimal("1000.0000"),
    )
    db_session.add(member)
    await db_session.flush()

    # Trade MUST succeed despite the completed tournament
    resp = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "UP", "budget": 50.0},
        headers=headers,
    )
    assert resp.status_code == 200, (
        f"Buy was blocked by completed tournament (HTTP {resp.status_code}): {resp.text}"
    )


# ---------------------------------------------------------------------------
# test_buy_inactive_player_fails  (NEW — Close-Only)
# ---------------------------------------------------------------------------


async def test_buy_inactive_player_fails(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """
    Buying an inactive player → HTTP 403 Forbidden (close-only mode).
    No position created. No points deducted.
    """
    user, portfolio = await _create_user_portfolio(db_session, "inactive_buyer")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}

    # Deactivate player 99
    player_result = await db_session.execute(sa.select(Player).where(Player.id == 99))
    player = player_result.scalar_one()
    player.is_active = False
    await db_session.flush()

    resp = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "UP", "budget": 50.0},
        headers=headers,
    )
    assert resp.status_code == 403

    # No points deducted
    port_after = await _get_portfolio(db_session, user.id)
    assert port_after.available_points == Decimal("1000.0000")

    # No position created
    pos = await _get_position(db_session, portfolio.id, 99, "UP")
    assert pos is None


# ---------------------------------------------------------------------------
# test_sell_inactive_player_succeeds  (CRITICAL — Close-Only)
# ---------------------------------------------------------------------------


async def test_sell_inactive_player_succeeds(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """
    Selling an inactive player's shares MUST succeed (HTTP 200).
    User funds MUST NOT be trapped by is_active flag (PRD §5.2).
    """
    user, portfolio = await _create_user_portfolio(db_session, "inactive_seller")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}

    # First, buy while player is active
    buy_resp = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "UP", "budget": 50.0},
        headers=headers,
    )
    assert buy_resp.status_code == 200
    bought_shares = Decimal(str(buy_resp.json()["shares"]))

    # Now deactivate the player
    player_result = await db_session.execute(sa.select(Player).where(Player.id == 99))
    player = player_result.scalar_one()
    player.is_active = False
    await db_session.flush()

    # Sell MUST still work
    sell_resp = await api_client.post(
        "/api/trade/sell",
        json={"player_id": 99, "direction": "UP", "shares": float(bought_shares)},
        headers=headers,
    )
    assert sell_resp.status_code == 200
    refund = Decimal(str(sell_resp.json()["refund"]))
    assert refund > 0

    # Verify shares reduced to 0
    pos = await _get_position(db_session, portfolio.id, 99, "UP")
    assert pos is None or pos.shares_owned == 0

    # Verify refund credited
    port_after = await _get_portfolio(db_session, user.id)
    # Started with 1000, spent 50, got refund back
    assert port_after.available_points == Decimal("1000.0000") - Decimal("50") + refund


# ---------------------------------------------------------------------------
# test_buy_zero_shares_from_tiny_budget  (INV-10 — Zero-shares phantom guard)
# ---------------------------------------------------------------------------


async def test_buy_zero_shares_from_tiny_budget(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """
    A budget too small to purchase even one micro-share (0.000001) must
    return HTTP 400, NOT silently deduct the budget and record a 0-share trade.

    In the test environment the b-floor is very high (~200 000) because there
    are very few portfolio rows.  At that b_eff, the threshold budget needed to
    receive at least one micro-share is roughly b_eff * 0.000001 / 0.5 ≈ 0.4
    points.  A budget of 0.0001 is well below that threshold and will yield
    shares_dec = Decimal("0.000000") after _d6() truncation — triggering the
    ZeroSharesError guard (INV-10) before any DB writes occur.
    """
    user, portfolio = await _create_user_portfolio(db_session, "zero_shares_buyer")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}
    budget = "0.0001"

    resp = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "UP", "budget": budget},
        headers=headers,
    )
    assert resp.status_code == 400, (
        f"Expected 400 for zero-shares budget, got {resp.status_code}: {resp.text}"
    )

    # No points deducted — portfolio must be untouched
    port_after = await _get_portfolio(db_session, user.id)
    assert port_after.available_points == Decimal("1000.0000"), (
        f"Portfolio was modified despite zero-shares budget: "
        f"available_points={port_after.available_points}"
    )

    # No position created
    pos = await _get_position(db_session, portfolio.id, 99, "UP")
    assert pos is None, f"Position was created despite zero-shares budget: {pos}"


# ---------------------------------------------------------------------------
# test_buy_normal_budget_unaffected_by_zero_shares_guard
# ---------------------------------------------------------------------------


async def test_buy_normal_budget_unaffected_by_zero_shares_guard(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """
    Verify the ZeroSharesError guard does not interfere with normal trades.
    A budget of 50 points must still succeed and return shares > 0.
    """
    user, portfolio = await _create_user_portfolio(db_session, "normal_buyer_guard")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}

    resp = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "UP", "budget": 50.0},
        headers=headers,
    )
    assert resp.status_code == 200, (
        f"Normal buy unexpectedly rejected: {resp.status_code}: {resp.text}"
    )
    data = resp.json()
    assert Decimal(str(data["shares"])) > 0, "Shares must be > 0 for a normal budget"


# ---------------------------------------------------------------------------
# test_sell_shares_extra_precision_truncated  (INV-10 — Sell precision guard)
# ---------------------------------------------------------------------------


async def test_sell_shares_extra_precision_truncated(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """
    A sell request with shares expressed to more than 6 decimal places must be
    accepted when the value, after ROUND_DOWN truncation to 6dp, equals the
    user's exact shares_owned.

    Scenario:
      1. Buy UP to acquire shares_owned (exactly 6dp — returned by execute_buy).
      2. Append extra digits to shares_owned to create a >6dp representation.
      3. POST /sell with this padded value.
      4. Expect HTTP 200: the schema truncates to 6dp, matching shares_owned exactly.

    Without the truncation validator, step 3 would succeed in Python Decimal
    comparison (0.123456 < 0.123456001 → True → InsufficientSharesError),
    trapping the user's full position because they sent one extra digit.
    """
    user, portfolio = await _create_user_portfolio(db_session, "precision_seller")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}

    # Buy first
    buy_resp = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "UP", "budget": 50.0},
        headers=headers,
    )
    assert buy_resp.status_code == 200
    shares_owned_str = buy_resp.json()["shares"]  # exact 6dp string from API

    # Append three extra zeros and a 1 (9dp total) — truncation must recover the original
    shares_padded = str(Decimal(shares_owned_str)) + "0001"

    sell_resp = await api_client.post(
        "/api/trade/sell",
        json={"player_id": 99, "direction": "UP", "shares": shares_padded},
        headers=headers,
    )
    assert sell_resp.status_code == 200, (
        f"Sell with extra-precision shares rejected (expected truncation to succeed): "
        f"{sell_resp.status_code}: {sell_resp.text}"
    )
    assert Decimal(str(sell_resp.json()["refund"])) > 0


# ---------------------------------------------------------------------------
# test_sell_shares_extra_precision_exceeds_owned  (INV-10 — negative case)
# ---------------------------------------------------------------------------


async def test_sell_shares_extra_precision_exceeds_owned(
    db_session: AsyncSession,
    sample_player_with_market,
    api_client: AsyncClient,
) -> None:
    """
    A sell request where shares, after truncation to 6dp, still exceed the
    user's shares_owned must return HTTP 400 (InsufficientSharesError).

    Scenario:
      1. Buy UP to acquire shares_owned (e.g. 0.000500).
      2. Send shares = shares_owned + 1 (clearly more than owned).
      3. Expect HTTP 400.

    This confirms the truncation validator does not create a bypass for
    legitimately insufficient-share sells.
    """
    user, portfolio = await _create_user_portfolio(db_session, "overshoot_seller")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}

    buy_resp = await api_client.post(
        "/api/trade/buy",
        json={"player_id": 99, "direction": "UP", "budget": 50.0},
        headers=headers,
    )
    assert buy_resp.status_code == 200
    shares_owned = Decimal(str(buy_resp.json()["shares"]))

    # Attempt to sell more than owned (by 1 full share)
    excess_shares = float(shares_owned + Decimal("1"))

    sell_resp = await api_client.post(
        "/api/trade/sell",
        json={"player_id": 99, "direction": "UP", "shares": excess_shares},
        headers=headers,
    )
    assert sell_resp.status_code == 400, (
        f"Expected 400 for overshooting sell, got {sell_resp.status_code}: {sell_resp.text}"
    )

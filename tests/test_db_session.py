"""
TDD: verify db_session fixture provides a real PostgreSQL session with
proper rollback isolation.

These tests require a live PostgreSQL instance (via docker-compose db).
They are skipped automatically when the DB is unreachable.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.models.player import Player
from app.models.portfolio import Portfolio
from app.models.user import User


async def test_db_session_insert_and_query_user(db_session):
    """Insert a User via db_session then query it back within the same transaction."""
    user = User(
        email="session_test@example.com",
        password_hash="$2b$12$placeholder",
        username="session_test_user",
    )
    db_session.add(user)
    await db_session.flush()

    # UUID should have been populated by Python-side default
    assert user.id is not None

    # Query back using the same session (within the same transaction)
    fetched = await db_session.get(User, user.id)
    assert fetched is not None
    assert fetched.email == "session_test@example.com"
    assert fetched.username == "session_test_user"


async def test_db_session_rollback_isolation(db_session):
    """
    Two independent db_session tests must not see each other's data.
    This test inserts a user with a unique email — if a previous test's
    data leaked, a UNIQUE constraint violation would occur here.
    """
    user = User(
        email="session_test@example.com",  # same email as above — must not conflict
        password_hash="$2b$12$placeholder",
        username="session_test_user",  # same username
    )
    db_session.add(user)
    await db_session.flush()  # would raise IntegrityError if prior test leaked


async def test_db_session_auth_headers_creates_user_with_portfolio(
    db_session, auth_headers
):
    """auth_headers factory creates a user + 1000-point portfolio; returns JWT dict."""
    headers = await auth_headers("alice")

    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Bearer ")

    # Verify both rows exist in the test transaction
    result = await db_session.execute(
        sa.select(User).where(User.username == "alice")
    )
    user = result.scalar_one()
    assert user.email == "alice@test.com"

    result = await db_session.execute(
        sa.select(Portfolio).where(Portfolio.user_id == user.id)
    )
    portfolio = result.scalar_one()
    assert portfolio.available_points == Decimal("1000.0000")


async def test_db_session_sample_player_with_market(sample_player_with_market):
    """sample_player_with_market fixture creates all 4 rows correctly."""
    d = sample_player_with_market

    assert d["player"].id == 99
    assert d["player"].name == "Test Player"
    assert d["fixture"].id == 1001
    assert d["fixture"].status == "finished"
    assert d["pmr"].sportmonks_rating == Decimal("7.00")
    assert d["market"].current_rating == Decimal("7.0000")
    assert d["market"].oracle_rating == Decimal("7.0000")
    assert d["market"].oracle_source == "layer_1"


async def test_db_session_liquidity_config(liquidity_config):
    """liquidity_config fixture creates the global row with correct defaults."""
    cfg = liquidity_config

    assert cfg.base_liquidity == Decimal("100.0000")
    assert cfg.n_reference == 50
    assert cfg.alpha_default == Decimal("0.050000")
    assert cfg.id is not None

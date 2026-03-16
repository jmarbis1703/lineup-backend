"""
Global pytest fixtures for the LineUp backend test suite.

Fixture hierarchy (PostgreSQL required for DB fixtures):

    apply_migrations  (session-scoped, sync)
         └── db_engine  (session-scoped, sync wrapper around AsyncEngine)
                  └── db_session  (function-scoped, async, rolls back after each test)
                           ├── auth_headers      (function-scoped, factory)
                           ├── sample_player_with_market  (function-scoped)
                           └── liquidity_config  (function-scoped)

    client  (function-scoped, async) — independent of DB; used by health tests etc.

Tests that don't request any db_* fixture never touch PostgreSQL.
"""
from __future__ import annotations

import base64
import os
from collections.abc import AsyncGenerator
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from httpx import AsyncClient, ASGITransport
from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.main import app
from app.models.fixture import Fixture
from app.models.liquidity_config import LiquidityConfig
from app.models.market_state import LmsrMarketState
from app.models.player import Player
from app.models.player_match_rating import PlayerMatchRating
from app.models.portfolio import Portfolio
from app.models.user import User

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

_FALLBACK_SYNC = "postgresql+psycopg2://lineup:lineup_secret@localhost:5432/lineup"
_FALLBACK_ASYNC = "postgresql+asyncpg://lineup:lineup_secret@localhost:5432/lineup"


def _sync_url() -> str:
    raw = os.getenv("TEST_DATABASE_URL", os.getenv("DATABASE_URL", _FALLBACK_SYNC))
    return raw.replace("+asyncpg", "+psycopg2")


def _async_url() -> str:
    raw = os.getenv("TEST_DATABASE_URL", os.getenv("DATABASE_URL", _FALLBACK_ASYNC))
    return raw.replace("+psycopg2", "+asyncpg")


# ---------------------------------------------------------------------------
# RSA key fixtures for Clerk JWT testing
# ---------------------------------------------------------------------------


def _int_to_base64url(n: int) -> str:
    length = (n.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()


@pytest.fixture(scope="session")
def rsa_test_keys() -> dict:
    """
    Session-scoped RSA-2048 key pair for signing/verifying test JWTs.

    Returns a dict with:
        ``private_pem``: bytes — PEM-encoded private key for jwt.encode
        ``jwks``: dict — JWKS payload to feed to verify_clerk_jwt
    """
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    nums = private_key.public_key().public_numbers()
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": "test-key-1",
                "n": _int_to_base64url(nums.n),
                "e": _int_to_base64url(nums.e),
            }
        ]
    }
    return {"private_pem": private_pem, "jwks": jwks}


@pytest.fixture(autouse=True)
def patch_clerk_jwks(monkeypatch, rsa_test_keys: dict) -> None:
    """
    Patch ``_fetch_jwks`` in app.core.auth so every test uses the local RSA
    test key instead of making a real network request to Clerk.
    """
    import app.core.auth as auth_module

    async def _mock_fetch_jwks() -> dict:
        return rsa_test_keys["jwks"]

    monkeypatch.setattr(auth_module, "_fetch_jwks", _mock_fetch_jwks)


# ---------------------------------------------------------------------------
# DB infrastructure fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def apply_migrations() -> None:
    """
    Run ``alembic upgrade head`` once per test session.
    Skips all dependant tests gracefully if PostgreSQL is unreachable.
    """
    url = _sync_url()
    try:
        eng = sa.create_engine(url, poolclass=sa.pool.NullPool)
        with eng.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        eng.dispose()
    except Exception as exc:
        pytest.skip(f"PostgreSQL not reachable — skipping DB fixtures: {exc}")

    ini_path = os.path.join(os.path.dirname(__file__), "..", "alembic.ini")
    cfg = Config(ini_path)
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


@pytest.fixture
async def db_engine(apply_migrations):
    """
    Function-scoped AsyncEngine.

    A fresh engine (and connection pool) is created for every test and bound
    to that test's event loop.  ``await engine.dispose()`` at teardown closes
    all asyncpg connections cleanly before the event loop shuts down, avoiding
    "Event loop is closed" errors that occur when a session-scoped engine's
    pool holds asyncpg connections from a previous event loop.
    """
    engine = create_async_engine(_async_url(), echo=False, pool_pre_ping=True)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    """
    Per-test AsyncSession.

    Wraps the test in one outer transaction that is **always rolled back**
    at teardown, leaving the DB in the same state it was before the test.

    ``join_transaction_mode="create_savepoint"`` means that any
    ``session.commit()`` inside application code releases a SAVEPOINT
    rather than committing to the real DB — so the outer rollback still
    catches everything.
    """
    async with db_engine.connect() as conn:
        await conn.begin()
        session = AsyncSession(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            await conn.rollback()


# ---------------------------------------------------------------------------
# HTTP client fixture (no DB dependency — health tests work without PostgreSQL)
# ---------------------------------------------------------------------------


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client wired directly to the FastAPI ASGI app."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Auth helper fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_headers(db_session: AsyncSession, rsa_test_keys: dict):
    """
    Factory fixture.  Call ``await auth_headers("alice")`` inside a test to
    get ``{"Authorization": "Bearer <token>"}`` for a freshly created user
    (with a 1000-point portfolio).  The user is rolled back with the test.

    JWTs are RS256-signed with the session-scoped test RSA key; ``_fetch_jwks``
    is patched (via ``patch_clerk_jwks``) to return the matching public key.
    """

    async def _make(username: str) -> dict[str, str]:
        clerk_id = f"user_{username}_test"
        user = User(
            email=f"{username}@test.com",
            clerk_id=clerk_id,
            username=username,
        )
        db_session.add(user)
        await db_session.flush()

        portfolio = Portfolio(
            user_id=user.id,
            available_points=Decimal("1000.0000"),
        )
        db_session.add(portfolio)
        await db_session.flush()

        exp = datetime.now(timezone.utc) + timedelta(hours=1)
        token = jwt.encode(
            {"sub": clerk_id, "exp": exp},
            rsa_test_keys["private_pem"],
            algorithm="RS256",
            headers={"kid": "test-key-1"},
        )
        return {"Authorization": f"Bearer {token}"}

    return _make


# ---------------------------------------------------------------------------
# Domain data fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def sample_player_with_market(db_session: AsyncSession) -> dict[str, Any]:
    """
    Inserts a complete player data set into the test transaction:

    - Player            id=99, name="Test Player", position_group="MF"
    - Fixture           id=1001, status="finished"
    - PlayerMatchRating player_id=99, fixture_id=1001, sportmonks_rating=7.0
    - LmsrMarketState   player_id=99, current_rating=7.0, oracle_rating=7.0

    All rows are rolled back when the test ends.
    Returns a dict with keys: player, fixture, pmr, market.
    """
    now = datetime.now(timezone.utc)

    player = Player(
        id=99,
        name="Test Player",
        team="Test FC",
        position="Midfielder",
        position_group="MF",
        league="Test League",
        league_id=999,
        is_active=True,
        last_synced_at=now,
    )
    db_session.add(player)

    fixture = Fixture(
        id=1001,
        home_team="Home FC",
        away_team="Away FC",
        kickoff_time=now,
        status="finished",
        league_id=999,
        matchday=28,
        last_synced_at=now,
    )
    db_session.add(fixture)
    await db_session.flush()

    pmr = PlayerMatchRating(
        player_id=99,
        fixture_id=1001,
        sportmonks_rating=Decimal("7.00"),
        minutes_played=90,
        goals=1,
        assists=0,
        shots_on_target=3,
        key_passes=2,
        tackles=1,
        recorded_at=now,
    )
    db_session.add(pmr)

    market = LmsrMarketState(
        player_id=99,
        alpha=Decimal("0.050000"),
        b_min=Decimal("100.0000"),
        q_up=Decimal("0.000000"),
        q_down=Decimal("0.000000"),
        current_rating=Decimal("7.0000"),
        oracle_rating=Decimal("7.0000"),
        oracle_source="layer_1",
        updated_at=now,
    )
    db_session.add(market)
    await db_session.flush()

    return {"player": player, "fixture": fixture, "pmr": pmr, "market": market}


@pytest.fixture
async def liquidity_config(db_session: AsyncSession) -> LiquidityConfig:
    """
    Inserts the single global LiquidityConfig row:
        base_liquidity=100, n_reference=50, alpha_default=0.05

    Rolled back with the test.
    """
    config = LiquidityConfig(
        base_liquidity=Decimal("100.0000"),
        n_reference=50,
        alpha_default=Decimal("0.050000"),
        last_calibrated_at=datetime.now(timezone.utc),
    )
    db_session.add(config)
    await db_session.flush()
    return config

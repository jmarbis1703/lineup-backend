"""
Schema + constraint tests for all 13 SQLAlchemy models.
Uses an in-memory SQLite engine — no live PostgreSQL required.
Each test gets a fresh Session that rolls back at teardown, so
failed-constraint tests cannot pollute subsequent tests.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.fixture import Fixture
from app.models.liquidity_config import LiquidityConfig
from app.models.market_state import LmsrMarketState
from app.models.player import Player
from app.models.player_match_rating import PlayerMatchRating
from app.models.portfolio import Portfolio
from app.models.position import Position
from app.models.rating_history import RatingHistory
from app.models.tournament import Tournament
from app.models.tournament_member import TournamentMember
from app.models.tournament_snapshot import TournamentSnapshot
from app.models.trade import Trade
from app.models.user import User

NOW = datetime.now(timezone.utc)
LATER = NOW + timedelta(days=1)


# ─────────────────────────────── Fixtures ────────────────────────────────────

@pytest.fixture(scope="module")
def engine():
    """Single in-memory SQLite engine shared across the module."""
    e = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(e)
    yield e
    Base.metadata.drop_all(e)


@pytest.fixture
def session(engine):
    """Per-test Session. Always rolls back so tests are fully isolated."""
    with Session(engine) as s:
        yield s
        s.rollback()


# ─────────────────────────── Object factories ────────────────────────────────

def _user(tag: str) -> User:
    return User(email=f"{tag}@test.com", username=tag)


def _player(pid: int) -> Player:
    return Player(id=pid, name=f"Player {pid}")


def _fixture(fid: int) -> Fixture:
    return Fixture(id=fid, home_team="Home FC", away_team="Away FC", kickoff_time=NOW)


def _market(player_id: int, **kwargs) -> LmsrMarketState:
    kwargs.setdefault("current_rating", Decimal("6.5"))
    return LmsrMarketState(player_id=player_id, **kwargs)


# ─────────────────────────── 1. All 13 tables exist ──────────────────────────

def test_all_15_tables_created(engine):
    expected = {
        "users",
        "portfolios",
        "players",
        "fixtures",
        "player_match_ratings",
        "positions",
        "trades",
        "lmsr_market_state",
        "rating_history",
        "tournaments",
        "tournament_members",
        "tournament_snapshots",
        "liquidity_config",
        "watchlists",
        "portfolio_snapshots",
    }
    assert set(Base.metadata.tables.keys()) == expected


# ─────────────────────────── 2. UNIQUE constraints ───────────────────────────

def test_portfolio_unique_user_id_duplicate_fails(session):
    """INV-08: only one portfolio per user."""
    user = _user("pf_dup"); session.add(user); session.flush()
    session.add(Portfolio(user_id=user.id)); session.flush()
    session.add(Portfolio(user_id=user.id))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_market_state_unique_player_id_duplicate_fails(session):
    """INV-07: only one market per player."""
    session.add(_player(1)); session.flush()
    session.add(_market(1)); session.flush()
    session.add(_market(1, current_rating=Decimal("7.0")))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_positions_two_up_same_player_fails(session):
    """UNIQUE(portfolio_id, player_id, direction): two UP rows must be rejected."""
    user = _user("pos_dup"); session.add(user); session.flush()
    pf = Portfolio(user_id=user.id); session.add(pf); session.flush()
    session.add(_player(2)); session.flush()

    session.add(Position(portfolio_id=pf.id, player_id=2, direction="UP")); session.flush()
    session.add(Position(portfolio_id=pf.id, player_id=2, direction="UP"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_positions_up_and_down_same_player_succeeds(session):
    """UNIQUE(portfolio_id, player_id, direction): one UP + one DOWN must succeed."""
    user = _user("pos_ok"); session.add(user); session.flush()
    pf = Portfolio(user_id=user.id); session.add(pf); session.flush()
    session.add(_player(3)); session.flush()

    session.add_all([
        Position(portfolio_id=pf.id, player_id=3, direction="UP"),
        Position(portfolio_id=pf.id, player_id=3, direction="DOWN"),
    ])
    session.flush()  # must not raise


def test_player_match_rating_unique_player_fixture_fails(session):
    """UNIQUE(player_id, fixture_id): same player+fixture twice must be rejected."""
    session.add(_player(4)); session.flush()
    session.add(_fixture(1)); session.flush()

    session.add(PlayerMatchRating(player_id=4, fixture_id=1)); session.flush()
    session.add(PlayerMatchRating(player_id=4, fixture_id=1))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# ─────────────────────────── 3. CHECK constraints ────────────────────────────

def test_check_available_points_negative_fails(session):
    """INV-02: available_points < 0 must be rejected."""
    user = _user("ck_pts"); session.add(user); session.flush()
    session.add(Portfolio(user_id=user.id, available_points=Decimal("-1")))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_check_b_min_zero_fails(session):
    """INV-04: b_min = 0 must be rejected (must be strictly positive)."""
    session.add(_player(5)); session.flush()
    session.add(_market(5, b_min=Decimal("0")))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_check_current_rating_above_10_fails(session):
    """INV-01: current_rating = 11.0 must be rejected."""
    session.add(_player(6)); session.flush()
    session.add(_market(6, current_rating=Decimal("11.0")))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# ─────────────────────── 4. Dust Buffer (INV-06) ─────────────────────────────

def test_q_up_below_dust_buffer_fails(session):
    """q_up = -0.02 is below the -0.01 floor — must be rejected."""
    session.add(_player(7)); session.flush()
    session.add(_market(7, q_up=Decimal("-0.02")))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_q_down_below_dust_buffer_fails(session):
    """q_down = -0.02 is below the -0.01 floor — must be rejected."""
    session.add(_player(8)); session.flush()
    session.add(_market(8, q_down=Decimal("-0.02")))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_q_within_dust_buffer_succeeds(session):
    """q_up = q_down = -0.005 is within [-0.01, 0) — must be accepted."""
    session.add(_player(9)); session.flush()
    session.add(_market(9, q_up=Decimal("-0.005"), q_down=Decimal("-0.005")))
    session.flush()  # must not raise

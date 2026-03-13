"""
Tests for tournament endpoints and lifecycle workers:
  POST /api/tournaments
  POST /api/tournaments/{id}/join
  GET  /api/tournaments/{id}/leaderboard
  GET  /api/tournaments/mine
  activate_pending_tournaments()  §7.5
  freeze_expired_tournaments()    §7.6
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.main import app as fastapi_app
from app.models.market_state import LmsrMarketState
from app.models.player import Player
from app.models.portfolio import Portfolio
from app.models.position import Position
from app.models.tournament import Tournament
from app.models.tournament_member import TournamentMember
from app.models.tournament_snapshot import TournamentSnapshot
from app.models.user import User
from app.workers.tournaments import activate_pending_tournaments, freeze_expired_tournaments


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token(user_id) -> str:
    from jose import jwt

    from app.config import settings

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
):
    user = User(
        email=f"{username}@tournament.test",
        password_hash="$2b$12$testhashplaceholderfortestingonly",
        username=username,
    )
    db.add(user)
    await db.flush()
    portfolio = Portfolio(user_id=user.id, available_points=points)
    db.add(portfolio)
    await db.flush()
    return user, portfolio


def _pending_payload() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "name": "Test Tournament",
        "start_time": (now + timedelta(days=1)).isoformat(),
        "end_time": (now + timedelta(days=8)).isoformat(),
    }


def _active_payload(name: str = "Active Tournament") -> dict:
    now = datetime.now(timezone.utc)
    return {
        "name": name,
        "start_time": (now - timedelta(days=1)).isoformat(),
        "end_time": (now + timedelta(days=7)).isoformat(),
    }


async def _activate(db: AsyncSession, tournament_id: str) -> None:
    """Directly flip a tournament's status to 'active'."""
    t = (
        await db.execute(
            sa.select(Tournament).where(
                Tournament.id == tournament_id  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    t.status = "active"
    await db.flush()


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    fastapi_app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app), base_url="http://test"
    ) as client:
        yield client
    fastapi_app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# test_create_tournament_pending
# ---------------------------------------------------------------------------


async def test_create_tournament_pending(
    db_session: AsyncSession,
    api_client: AsyncClient,
) -> None:
    """A newly created tournament starts with status='pending'."""
    user, _ = await _create_user_portfolio(db_session, "tourney_creator")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}

    resp = await api_client.post(
        "/api/tournaments",
        json=_pending_payload(),
        headers=headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "pending"
    assert data["name"] == "Test Tournament"
    assert str(data["created_by"]) == str(user.id)


# ---------------------------------------------------------------------------
# test_join_tournament
# ---------------------------------------------------------------------------


async def test_join_tournament(
    db_session: AsyncSession,
    api_client: AsyncClient,
) -> None:
    """A user can join an existing tournament."""
    creator, _ = await _create_user_portfolio(db_session, "tj_creator")
    joiner, _ = await _create_user_portfolio(db_session, "tj_joiner")
    creator_hdr = {"Authorization": f"Bearer {_make_token(creator.id)}"}
    joiner_hdr = {"Authorization": f"Bearer {_make_token(joiner.id)}"}

    create_resp = await api_client.post(
        "/api/tournaments", json=_pending_payload(), headers=creator_hdr
    )
    assert create_resp.status_code == 201
    tournament_id = create_resp.json()["id"]

    join_resp = await api_client.post(
        f"/api/tournaments/{tournament_id}/join", headers=joiner_hdr
    )
    assert join_resp.status_code == 200
    data = join_resp.json()
    assert data["tournament_id"] == tournament_id
    assert str(data["user_id"]) == str(joiner.id)


# ---------------------------------------------------------------------------
# test_join_twice_409
# ---------------------------------------------------------------------------


async def test_join_twice_409(
    db_session: AsyncSession,
    api_client: AsyncClient,
) -> None:
    """Joining the same tournament twice → 409 Conflict."""
    user, _ = await _create_user_portfolio(db_session, "tj_twice")
    headers = {"Authorization": f"Bearer {_make_token(user.id)}"}

    create_resp = await api_client.post(
        "/api/tournaments", json=_pending_payload(), headers=headers
    )
    tournament_id = create_resp.json()["id"]

    r1 = await api_client.post(
        f"/api/tournaments/{tournament_id}/join", headers=headers
    )
    assert r1.status_code == 200

    r2 = await api_client.post(
        f"/api/tournaments/{tournament_id}/join", headers=headers
    )
    assert r2.status_code == 409


# ---------------------------------------------------------------------------
# test_join_active_tournament_locks_starting_value
# ---------------------------------------------------------------------------


async def test_join_active_tournament_locks_starting_value(
    db_session: AsyncSession,
    api_client: AsyncClient,
) -> None:
    """
    Joining an active tournament immediately records starting_value equal
    to the user's current total_value.
    """
    creator, _ = await _create_user_portfolio(db_session, "tj_active_creator")
    joiner, _ = await _create_user_portfolio(db_session, "tj_active_joiner")
    creator_hdr = {"Authorization": f"Bearer {_make_token(creator.id)}"}
    joiner_hdr = {"Authorization": f"Bearer {_make_token(joiner.id)}"}

    create_resp = await api_client.post(
        "/api/tournaments", json=_active_payload(), headers=creator_hdr
    )
    tournament_id = create_resp.json()["id"]
    await _activate(db_session, tournament_id)

    # Joiner has 1000 points and no open positions → total_value = 1000
    join_resp = await api_client.post(
        f"/api/tournaments/{tournament_id}/join", headers=joiner_hdr
    )
    assert join_resp.status_code == 200
    data = join_resp.json()

    # starting_value should match total_value (1000 with no positions)
    assert abs(float(data["starting_value"]) - 1000.0) < 0.01


# ---------------------------------------------------------------------------
# test_join_pending_tournament_placeholder
# ---------------------------------------------------------------------------


async def test_join_pending_tournament_placeholder(
    db_session: AsyncSession,
    api_client: AsyncClient,
) -> None:
    """Joining a pending tournament sets starting_value = 0 (placeholder)."""
    creator, _ = await _create_user_portfolio(db_session, "tj_pend_creator")
    joiner, _ = await _create_user_portfolio(db_session, "tj_pend_joiner")
    creator_hdr = {"Authorization": f"Bearer {_make_token(creator.id)}"}
    joiner_hdr = {"Authorization": f"Bearer {_make_token(joiner.id)}"}

    create_resp = await api_client.post(
        "/api/tournaments", json=_pending_payload(), headers=creator_hdr
    )
    tournament_id = create_resp.json()["id"]

    join_resp = await api_client.post(
        f"/api/tournaments/{tournament_id}/join", headers=joiner_hdr
    )
    assert join_resp.status_code == 200
    data = join_resp.json()
    assert Decimal(str(data["starting_value"])) == Decimal("0")


# ---------------------------------------------------------------------------
# test_tournament_leaderboard_filters_members
# ---------------------------------------------------------------------------


async def test_tournament_leaderboard_filters_members(
    db_session: AsyncSession,
    api_client: AsyncClient,
) -> None:
    """Leaderboard only shows users who joined the tournament."""
    creator, _ = await _create_user_portfolio(db_session, "tlb_creator")
    member, _ = await _create_user_portfolio(db_session, "tlb_member")
    outsider, _ = await _create_user_portfolio(db_session, "tlb_outsider")
    creator_hdr = {"Authorization": f"Bearer {_make_token(creator.id)}"}
    member_hdr = {"Authorization": f"Bearer {_make_token(member.id)}"}

    create_resp = await api_client.post(
        "/api/tournaments", json=_active_payload(), headers=creator_hdr
    )
    tournament_id = create_resp.json()["id"]
    await _activate(db_session, tournament_id)

    await api_client.post(
        f"/api/tournaments/{tournament_id}/join", headers=creator_hdr
    )
    await api_client.post(
        f"/api/tournaments/{tournament_id}/join", headers=member_hdr
    )
    # outsider does NOT join

    resp = await api_client.get(
        f"/api/tournaments/{tournament_id}/leaderboard", headers=creator_hdr
    )
    assert resp.status_code == 200
    data = resp.json()
    usernames = [e["username"] for e in data]

    assert "tlb_creator" in usernames
    assert "tlb_member" in usernames
    assert "tlb_outsider" not in usernames


# ---------------------------------------------------------------------------
# test_tournament_leaderboard_ranks_by_delta  (CRITICAL)
# ---------------------------------------------------------------------------


async def test_tournament_leaderboard_ranks_by_delta(
    db_session: AsyncSession,
    api_client: AsyncClient,
) -> None:
    """
    CRITICAL: Ranking uses Δ total_value (profit_loss), NOT absolute total_value.

    Setup:
      user_a: starting_value=1000, then gains 500 pts → profit_loss=500
      user_b: starting_value=2000, no change → profit_loss=0

    user_b has higher absolute value (2000 > 1500), but user_a ranks first
    because profit_loss 500 > 0.
    """
    user_a, _ = await _create_user_portfolio(
        db_session, "tlb_delta_a", Decimal("1000.0000")
    )
    user_b, _ = await _create_user_portfolio(
        db_session, "tlb_delta_b", Decimal("2000.0000")
    )
    headers_a = {"Authorization": f"Bearer {_make_token(user_a.id)}"}
    headers_b = {"Authorization": f"Bearer {_make_token(user_b.id)}"}

    create_resp = await api_client.post(
        "/api/tournaments",
        json=_active_payload("Delta Test"),
        headers=headers_b,
    )
    tournament_id = create_resp.json()["id"]
    await _activate(db_session, tournament_id)

    # Both join: user_a locks starting_value=1000, user_b locks starting_value=2000
    await api_client.post(
        f"/api/tournaments/{tournament_id}/join", headers=headers_a
    )
    await api_client.post(
        f"/api/tournaments/{tournament_id}/join", headers=headers_b
    )

    # Simulate user_a gaining 500 pts (available_points 1000→1500)
    port_a = (
        await db_session.execute(
            sa.select(Portfolio).where(Portfolio.user_id == user_a.id)
        )
    ).scalar_one()
    port_a.available_points = Decimal("1500.0000")
    await db_session.flush()

    # user_b unchanged: total_value=2000, profit_loss=0

    resp = await api_client.get(
        f"/api/tournaments/{tournament_id}/leaderboard", headers=headers_a
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data[0]["username"] == "tlb_delta_a"  # higher profit_loss → rank 1
    assert data[1]["username"] == "tlb_delta_b"
    assert data[0]["profit_loss"] == pytest.approx(500.0, abs=0.01)
    assert data[1]["profit_loss"] == pytest.approx(0.0, abs=0.01)


# ---------------------------------------------------------------------------
# test_tournament_veteran_no_advantage
# ---------------------------------------------------------------------------


async def test_tournament_veteran_no_advantage(
    db_session: AsyncSession,
    api_client: AsyncClient,
) -> None:
    """
    A veteran with a large portfolio and a new user both have profit_loss ≈ 0
    when neither trades after joining an active tournament.

    This validates that ranking by Δ value (not absolute) prevents veterans
    from dominating by virtue of existing wealth.
    """
    veteran, _ = await _create_user_portfolio(
        db_session, "veteran_vet", Decimal("5000.0000")
    )
    new_user, _ = await _create_user_portfolio(
        db_session, "veteran_new", Decimal("1000.0000")
    )
    vet_hdr = {"Authorization": f"Bearer {_make_token(veteran.id)}"}
    new_hdr = {"Authorization": f"Bearer {_make_token(new_user.id)}"}

    create_resp = await api_client.post(
        "/api/tournaments",
        json=_active_payload("Veteran Test"),
        headers=vet_hdr,
    )
    tournament_id = create_resp.json()["id"]
    await _activate(db_session, tournament_id)

    # Both join — starting_values locked at current total_value
    r1 = await api_client.post(
        f"/api/tournaments/{tournament_id}/join", headers=vet_hdr
    )
    r2 = await api_client.post(
        f"/api/tournaments/{tournament_id}/join", headers=new_hdr
    )
    assert r1.status_code == 200
    assert r2.status_code == 200

    # Neither trades after joining

    resp = await api_client.get(
        f"/api/tournaments/{tournament_id}/leaderboard", headers=vet_hdr
    )
    assert resp.status_code == 200
    data = resp.json()

    for entry in data:
        assert abs(entry["profit_loss"]) < 0.01, (
            f"profit_loss should be ≈ 0 for {entry['username']}, "
            f"got {entry['profit_loss']}"
        )


# ---------------------------------------------------------------------------
# test_my_tournaments
# ---------------------------------------------------------------------------


async def test_my_tournaments(
    db_session: AsyncSession,
    api_client: AsyncClient,
) -> None:
    """GET /api/tournaments/mine returns tournaments the user has joined."""
    user, _ = await _create_user_portfolio(db_session, "mine_user")
    other, _ = await _create_user_portfolio(db_session, "mine_other")
    user_hdr = {"Authorization": f"Bearer {_make_token(user.id)}"}
    other_hdr = {"Authorization": f"Bearer {_make_token(other.id)}"}

    # User creates and joins a tournament
    create_resp = await api_client.post(
        "/api/tournaments", json=_pending_payload(), headers=user_hdr
    )
    assert create_resp.status_code == 201
    tournament_id = create_resp.json()["id"]

    join_resp = await api_client.post(
        f"/api/tournaments/{tournament_id}/join", headers=user_hdr
    )
    assert join_resp.status_code == 200

    # Other user creates their own tournament (user does NOT join it)
    other_create = await api_client.post(
        "/api/tournaments",
        json={**_pending_payload(), "name": "Other Tournament"},
        headers=other_hdr,
    )
    assert other_create.status_code == 201

    resp = await api_client.get("/api/tournaments/mine", headers=user_hdr)
    assert resp.status_code == 200
    data = resp.json()

    ids = [t["id"] for t in data]
    assert tournament_id in ids
    # User should NOT see the other user's tournament (they didn't join it)
    other_id = other_create.json()["id"]
    assert other_id not in ids


# ===========================================================================
# Worker tests — activate_pending_tournaments (§7.5)
# ===========================================================================


async def _make_player_market(
    db: AsyncSession,
    player_id: int,
    q_up: float = 500.0,
    q_down: float = 0.0,
    b_min: float = 100.0,
    alpha: float = 0.0,
) -> LmsrMarketState:
    """Insert a Player + LmsrMarketState and return the market row."""
    from app.core.lmsr import lmsr_rating

    now = datetime.now(timezone.utc)
    db.add(
        Player(
            id=player_id,
            name=f"T-Worker Player {player_id}",
            team="Test FC",
            position_group="FW",
            league_id=999,
            is_active=True,
            last_synced_at=now,
        )
    )
    await db.flush()

    rating = lmsr_rating(q_up, q_down, b_min)
    market = LmsrMarketState(
        player_id=player_id,
        alpha=Decimal(str(alpha)),
        b_min=Decimal(str(b_min)).quantize(Decimal("0.0001")),
        q_up=Decimal(str(q_up)).quantize(Decimal("0.000001")),
        q_down=Decimal(str(q_down)).quantize(Decimal("0.000001")),
        current_rating=Decimal(str(rating)).quantize(Decimal("0.0001")),
        oracle_rating=Decimal(str(rating)).quantize(Decimal("0.0001")),
        oracle_source="layer_1",
        updated_at=now,
    )
    db.add(market)
    await db.flush()
    return market


async def _make_pending_tournament_with_members(
    db: AsyncSession,
    n_members: int,
    start_offset: timedelta = timedelta(minutes=-5),
    base_points: Decimal = Decimal("1000.0000"),
    prefix: str = "tworker",
) -> tuple[Tournament, list[User], list[Portfolio]]:
    """Create a pending tournament + n members with portfolios."""
    now = datetime.now(timezone.utc)
    creator = User(
        email=f"{prefix}_creator@worker.test",
        password_hash="$2b$12$testhashplaceholderfortestingonly",
        username=f"{prefix}_creator",
    )
    db.add(creator)
    await db.flush()

    tournament = Tournament(
        name=f"{prefix} Tournament",
        status="pending",
        created_by=creator.id,
        start_time=now + start_offset,
        end_time=now + timedelta(days=7),
    )
    db.add(tournament)
    await db.flush()

    users: list[User] = []
    portfolios: list[Portfolio] = []
    for i in range(n_members):
        user = User(
            email=f"{prefix}_m{i}@worker.test",
            password_hash="$2b$12$testhashplaceholderfortestingonly",
            username=f"{prefix}_m{i}",
        )
        db.add(user)
        await db.flush()
        port = Portfolio(user_id=user.id, available_points=base_points)
        db.add(port)
        await db.flush()
        db.add(TournamentMember(
            tournament_id=tournament.id,
            user_id=user.id,
            starting_value=Decimal("0"),
        ))
        users.append(user)
        portfolios.append(port)
    await db.flush()

    return tournament, users, portfolios


# ---------------------------------------------------------------------------
# test_activate_locks_starting_values
# ---------------------------------------------------------------------------


async def test_activate_locks_starting_values(db_session: AsyncSession) -> None:
    """Both members get starting_value > 0 and tournament becomes 'active'."""
    tournament, users, portfolios = await _make_pending_tournament_with_members(
        db_session,
        n_members=2,
        start_offset=timedelta(minutes=-5),
        base_points=Decimal("1000.0000"),
        prefix="act_lock",
    )

    count = await activate_pending_tournaments(db_session)

    assert count == 1

    # Reload tournament
    t = (
        await db_session.execute(
            sa.select(Tournament).where(Tournament.id == tournament.id)
        )
    ).scalar_one()
    assert t.status == "active"

    # Both members should have starting_value > 0 (locked from their cash balance)
    for user in users:
        member = (
            await db_session.execute(
                sa.select(TournamentMember).where(
                    TournamentMember.tournament_id == tournament.id,
                    TournamentMember.user_id == user.id,
                )
            )
        ).scalar_one()
        assert float(member.starting_value) > 0, (
            f"starting_value for {user.username} should be > 0"
        )
        assert abs(float(member.starting_value) - 1000.0) < 0.01


# ---------------------------------------------------------------------------
# test_activate_does_not_re_lock_active
# ---------------------------------------------------------------------------


async def test_activate_does_not_re_lock_active(db_session: AsyncSession) -> None:
    """Already-active tournaments are NOT re-processed."""
    now = datetime.now(timezone.utc)
    creator, _ = await _create_user_portfolio(db_session, "re_lock_creator")

    tournament = Tournament(
        name="Already Active",
        status="active",  # <-- already active
        created_by=creator.id,
        start_time=now - timedelta(hours=1),
        end_time=now + timedelta(days=7),
    )
    db_session.add(tournament)
    await db_session.flush()

    count = await activate_pending_tournaments(db_session)

    assert count == 0  # no pending tournaments found → 0 activated


# ---------------------------------------------------------------------------
# test_activate_uses_batch_queries
# ---------------------------------------------------------------------------


async def test_activate_uses_batch_queries(db_session: AsyncSession) -> None:
    """activate_pending_tournaments issues O(1) queries regardless of member count."""
    tournament, _, _ = await _make_pending_tournament_with_members(
        db_session,
        n_members=3,
        start_offset=timedelta(minutes=-1),
        prefix="batch_act",
    )

    execute_count = 0
    original_execute = db_session.execute

    async def spy_execute(stmt, *args, **kwargs):
        nonlocal execute_count
        execute_count += 1
        return await original_execute(stmt, *args, **kwargs)

    db_session.execute = spy_execute
    try:
        await activate_pending_tournaments(db_session)
    finally:
        db_session.execute = original_execute

    # Batch approach: ~5-6 queries total for any N members.
    # If N+1, 3 members → at least 9 queries for portfolios + positions alone.
    assert execute_count <= 10, (
        f"Expected O(1) batch queries, got {execute_count}. "
        "Each member must NOT trigger its own individual portfolio/position queries."
    )


# ---------------------------------------------------------------------------
# test_activate_uses_repeatable_read_isolation
# ---------------------------------------------------------------------------


async def test_activate_uses_repeatable_read_isolation(
    db_session: AsyncSession,
) -> None:
    """activate_pending_tournaments requests REPEATABLE READ isolation."""
    isolation_levels_requested: list[str] = []

    async def spy_connection(**kwargs):
        eo = kwargs.get("execution_options", {})
        if "isolation_level" in eo:
            isolation_levels_requested.append(eo["isolation_level"])
        # Do NOT call through — avoids PostgreSQL error in nested txn context
        return None

    original_connection = db_session.connection
    db_session.connection = spy_connection
    try:
        await activate_pending_tournaments(db_session)
    finally:
        db_session.connection = original_connection

    assert any(
        level in ("REPEATABLE READ", "SERIALIZABLE")
        for level in isolation_levels_requested
    ), (
        f"Expected REPEATABLE READ or SERIALIZABLE isolation, got: "
        f"{isolation_levels_requested}"
    )


# ---------------------------------------------------------------------------
# test_activate_total_value_includes_cash_and_positions
# ---------------------------------------------------------------------------


async def test_activate_total_value_includes_cash_and_positions(
    db_session: AsyncSession,
) -> None:
    """starting_value must include both available_points AND open position sell_refund."""
    # Market: q_up=500, q_down=0, b_min=100, alpha=0
    # Position: 250 UP shares → refund ≈ 243 (verified mathematically)
    # Cash: 750 → total ≈ 993 >= 950
    market = await _make_player_market(
        db_session,
        player_id=4001,
        q_up=500.0,
        q_down=0.0,
        b_min=100.0,
        alpha=0.0,
    )

    now = datetime.now(timezone.utc)
    creator = User(
        email="act_pos_creator@worker.test",
        password_hash="$2b$12$testhashplaceholderfortestingonly",
        username="act_pos_creator",
    )
    db_session.add(creator)
    await db_session.flush()

    member_user = User(
        email="act_pos_member@worker.test",
        password_hash="$2b$12$testhashplaceholderfortestingonly",
        username="act_pos_member",
    )
    db_session.add(member_user)
    await db_session.flush()

    portfolio = Portfolio(user_id=member_user.id, available_points=Decimal("750.0000"))
    db_session.add(portfolio)
    await db_session.flush()

    position = Position(
        portfolio_id=portfolio.id,
        player_id=4001,
        direction="UP",
        shares_owned=Decimal("250.000000"),
        average_entry_price=Decimal("0.500000"),
    )
    db_session.add(position)
    await db_session.flush()

    tournament = Tournament(
        name="Pos Value Test",
        status="pending",
        created_by=creator.id,
        start_time=now - timedelta(minutes=5),
        end_time=now + timedelta(days=7),
    )
    db_session.add(tournament)
    await db_session.flush()

    db_session.add(TournamentMember(
        tournament_id=tournament.id,
        user_id=member_user.id,
        starting_value=Decimal("0"),
    ))
    await db_session.flush()

    await activate_pending_tournaments(db_session)

    member = (
        await db_session.execute(
            sa.select(TournamentMember).where(
                TournamentMember.tournament_id == tournament.id,
                TournamentMember.user_id == member_user.id,
            )
        )
    ).scalar_one()

    # Cash alone = 750; with position sell_refund ≈ 243 → total ≈ 993 >= 950
    assert float(member.starting_value) >= 950, (
        f"starting_value {member.starting_value} should be >= 950 "
        "(cash 750 + position refund ~243). "
        "Portfolios batch query may not be included in valuation."
    )


# ---------------------------------------------------------------------------
# test_activate_starting_value_consistent_across_members
# ---------------------------------------------------------------------------


async def test_activate_starting_value_consistent_across_members(
    db_session: AsyncSession,
) -> None:
    """All 3 members' starting_values are computed from the same market snapshot."""
    market = await _make_player_market(
        db_session,
        player_id=4002,
        q_up=300.0,
        q_down=0.0,
        b_min=100.0,
        alpha=0.0,
    )

    now = datetime.now(timezone.utc)
    creator = User(
        email="cons_creator@worker.test",
        password_hash="$2b$12$testhashplaceholderfortestingonly",
        username="cons_creator",
    )
    db_session.add(creator)
    await db_session.flush()

    tournament = Tournament(
        name="Consistent Snapshot Test",
        status="pending",
        created_by=creator.id,
        start_time=now - timedelta(minutes=1),
        end_time=now + timedelta(days=7),
    )
    db_session.add(tournament)
    await db_session.flush()

    # Create 3 members, each with 1000 points and identical UP positions (100 shares)
    member_starting_values = []
    for i in range(3):
        user = User(
            email=f"cons_m{i}@worker.test",
            password_hash="$2b$12$testhashplaceholderfortestingonly",
            username=f"cons_m{i}",
        )
        db_session.add(user)
        await db_session.flush()

        port = Portfolio(user_id=user.id, available_points=Decimal("1000.0000"))
        db_session.add(port)
        await db_session.flush()

        db_session.add(Position(
            portfolio_id=port.id,
            player_id=4002,
            direction="UP",
            shares_owned=Decimal("100.000000"),
            average_entry_price=Decimal("1.000000"),
        ))
        db_session.add(TournamentMember(
            tournament_id=tournament.id,
            user_id=user.id,
            starting_value=Decimal("0"),
        ))
        await db_session.flush()

        member_starting_values.append((user.id,))

    await activate_pending_tournaments(db_session)

    # All 3 members have identical portfolios → starting_values must be identical
    values = []
    for (user_id,) in member_starting_values:
        member = (
            await db_session.execute(
                sa.select(TournamentMember).where(
                    TournamentMember.tournament_id == tournament.id,
                    TournamentMember.user_id == user_id,
                )
            )
        ).scalar_one()
        values.append(float(member.starting_value))

    assert len(set(round(v, 4) for v in values)) == 1, (
        f"All 3 members have identical portfolios but got different starting_values: "
        f"{values}. Snapshot tearing detected."
    )
    # All values should be > 1000 (cash + position refund)
    for v in values:
        assert v > 1000.0


# ===========================================================================
# Worker tests — freeze_expired_tournaments (§7.6)
# ===========================================================================


async def _make_expired_active_tournament(
    db: AsyncSession,
    n_members: int,
    base_points: Decimal = Decimal("1000.0000"),
    starting_value: Decimal = Decimal("1000.0000"),
    prefix: str = "freeze",
) -> tuple[Tournament, list[User], list[Portfolio]]:
    """Create an 'active' tournament already past end_time, with n members."""
    now = datetime.now(timezone.utc)
    creator = User(
        email=f"{prefix}_fc@freeze.test",
        password_hash="$2b$12$testhashplaceholderfortestingonly",
        username=f"{prefix}_fc",
    )
    db.add(creator)
    await db.flush()

    tournament = Tournament(
        name=f"{prefix} Tournament",
        status="active",
        created_by=creator.id,
        start_time=now - timedelta(days=8),
        end_time=now - timedelta(minutes=10),  # already expired
    )
    db.add(tournament)
    await db.flush()

    users: list[User] = []
    portfolios: list[Portfolio] = []
    for i in range(n_members):
        user = User(
            email=f"{prefix}_fm{i}@freeze.test",
            password_hash="$2b$12$testhashplaceholderfortestingonly",
            username=f"{prefix}_fm{i}",
        )
        db.add(user)
        await db.flush()
        port = Portfolio(user_id=user.id, available_points=base_points)
        db.add(port)
        await db.flush()
        db.add(TournamentMember(
            tournament_id=tournament.id,
            user_id=user.id,
            starting_value=starting_value,
        ))
        users.append(user)
        portfolios.append(port)
    await db.flush()

    return tournament, users, portfolios


# ---------------------------------------------------------------------------
# test_freeze_snapshots_values
# ---------------------------------------------------------------------------


async def test_freeze_snapshots_values(db_session: AsyncSession) -> None:
    """Snapshot includes starting_value, total_value, profit_loss; ranked by profit_loss DESC."""
    tournament, users, portfolios = await _make_expired_active_tournament(
        db_session,
        n_members=2,
        base_points=Decimal("1200.0000"),
        starting_value=Decimal("1000.0000"),
        prefix="snap",
    )
    # user[0] unchanged: profit_loss = 200 (1200 - 1000)
    # user[1] unchanged: profit_loss = 200 (1200 - 1000)

    count = await freeze_expired_tournaments(db_session)

    assert count == 1

    # Tournament should be 'completed'
    t = (
        await db_session.execute(
            sa.select(Tournament).where(Tournament.id == tournament.id)
        )
    ).scalar_one()
    assert t.status == "completed"

    # Snapshots exist, ranked, include all fields
    snaps = (
        await db_session.execute(
            sa.select(TournamentSnapshot)
            .where(TournamentSnapshot.tournament_id == tournament.id)
            .order_by(TournamentSnapshot.rank.asc())
        )
    ).scalars().all()

    assert len(snaps) == 2
    for snap in snaps:
        assert snap.total_value is not None
        assert snap.starting_value == Decimal("1000.0000")
        assert snap.profit_loss is not None
        assert snap.rank >= 1
        # profit_loss = total_value - starting_value
        assert abs(float(snap.profit_loss) - (float(snap.total_value) - 1000.0)) < 0.01


# ---------------------------------------------------------------------------
# test_freeze_does_not_modify_positions
# ---------------------------------------------------------------------------


async def test_freeze_does_not_modify_positions(db_session: AsyncSession) -> None:
    """freeze_expired_tournaments must not touch positions.shares_owned."""
    market = await _make_player_market(
        db_session, player_id=5001, q_up=200.0, q_down=0.0, b_min=100.0
    )

    tournament, users, portfolios = await _make_expired_active_tournament(
        db_session,
        n_members=1,
        base_points=Decimal("800.0000"),
        starting_value=Decimal("800.0000"),
        prefix="nomod",
    )

    # Add a position to the member's portfolio
    position = Position(
        portfolio_id=portfolios[0].id,
        player_id=5001,
        direction="UP",
        shares_owned=Decimal("50.000000"),
        average_entry_price=Decimal("2.000000"),
    )
    db_session.add(position)
    await db_session.flush()

    await freeze_expired_tournaments(db_session)

    await db_session.refresh(position)
    assert position.shares_owned == Decimal("50.000000"), (
        "freeze must NOT modify positions.shares_owned"
    )
    assert position.average_entry_price == Decimal("2.000000"), (
        "freeze must NOT modify average_entry_price"
    )


# ---------------------------------------------------------------------------
# test_completed_returns_snapshot
# ---------------------------------------------------------------------------


async def test_completed_returns_snapshot(
    db_session: AsyncSession,
    api_client: AsyncClient,
) -> None:
    """GET leaderboard on a completed tournament returns frozen snapshot data."""
    now = datetime.now(timezone.utc)

    creator, _ = await _create_user_portfolio(db_session, "snap_creator")
    member1, _ = await _create_user_portfolio(db_session, "snap_m1", Decimal("1500.0000"))
    member2, _ = await _create_user_portfolio(db_session, "snap_m2", Decimal("900.0000"))

    tournament = Tournament(
        name="Completed Snap Test",
        status="completed",
        created_by=creator.id,
        start_time=now - timedelta(days=8),
        end_time=now - timedelta(minutes=10),
    )
    db_session.add(tournament)
    await db_session.flush()

    # Insert frozen snapshots directly (simulating what freeze_expired_tournaments writes)
    db_session.add(TournamentSnapshot(
        tournament_id=tournament.id,
        user_id=member1.id,
        rank=1,
        total_value=Decimal("1500.0000"),
        starting_value=Decimal("1000.0000"),
        profit_loss=Decimal("500.0000"),
    ))
    db_session.add(TournamentSnapshot(
        tournament_id=tournament.id,
        user_id=member2.id,
        rank=2,
        total_value=Decimal("900.0000"),
        starting_value=Decimal("1000.0000"),
        profit_loss=Decimal("-100.0000"),
    ))
    await db_session.flush()

    headers = {"Authorization": f"Bearer {_make_token(creator.id)}"}
    resp = await api_client.get(
        f"/api/tournaments/{tournament.id}/leaderboard", headers=headers
    )
    assert resp.status_code == 200
    data = resp.json()

    assert len(data) == 2
    assert data[0]["rank"] == 1
    assert data[0]["profit_loss"] == pytest.approx(500.0, abs=0.01)
    assert data[1]["rank"] == 2
    assert data[1]["profit_loss"] == pytest.approx(-100.0, abs=0.01)


# ---------------------------------------------------------------------------
# test_freeze_uses_batch_queries
# ---------------------------------------------------------------------------


async def test_freeze_uses_batch_queries(db_session: AsyncSession) -> None:
    """freeze_expired_tournaments issues O(1) queries regardless of member count."""
    tournament, _, _ = await _make_expired_active_tournament(
        db_session,
        n_members=3,
        base_points=Decimal("1000.0000"),
        starting_value=Decimal("1000.0000"),
        prefix="freeze_batch",
    )

    execute_count = 0
    original_execute = db_session.execute

    async def spy_execute(stmt, *args, **kwargs):
        nonlocal execute_count
        execute_count += 1
        return await original_execute(stmt, *args, **kwargs)

    db_session.execute = spy_execute
    try:
        await freeze_expired_tournaments(db_session)
    finally:
        db_session.execute = original_execute

    # Batch approach: ~6-7 queries total for any N members.
    assert execute_count <= 12, (
        f"Expected O(1) batch queries, got {execute_count}. "
        "Each member must NOT trigger individual portfolio/position queries."
    )


# ---------------------------------------------------------------------------
# test_freeze_includes_available_points
# ---------------------------------------------------------------------------


async def test_freeze_includes_available_points(db_session: AsyncSession) -> None:
    """Member with 1000 points and 0 positions → total_value in snapshot == 1000."""
    tournament, users, _ = await _make_expired_active_tournament(
        db_session,
        n_members=1,
        base_points=Decimal("1000.0000"),
        starting_value=Decimal("1000.0000"),
        prefix="cash_only",
    )

    await freeze_expired_tournaments(db_session)

    snap = (
        await db_session.execute(
            sa.select(TournamentSnapshot).where(
                TournamentSnapshot.tournament_id == tournament.id,
                TournamentSnapshot.user_id == users[0].id,
            )
        )
    ).scalar_one()

    assert abs(float(snap.total_value) - 1000.0) < 0.01, (
        f"total_value should be 1000.0 (cash only), got {snap.total_value}. "
        "Portfolios batch query may not be included in valuation."
    )


# ---------------------------------------------------------------------------
# test_freeze_total_value_formula
# ---------------------------------------------------------------------------


async def test_freeze_total_value_formula(db_session: AsyncSession) -> None:
    """total_value = available_points + sell_refund(position) — not just cash."""
    # Market: q_up=600, q_down=0, b_min=100, alpha=0
    # Position: 350 UP shares → sell_refund ≈ 342
    # Cash: 500 → total ≈ 842 >= 800
    market = await _make_player_market(
        db_session,
        player_id=5002,
        q_up=600.0,
        q_down=0.0,
        b_min=100.0,
        alpha=0.0,
    )

    tournament, users, portfolios = await _make_expired_active_tournament(
        db_session,
        n_members=1,
        base_points=Decimal("500.0000"),
        starting_value=Decimal("500.0000"),
        prefix="tvform",
    )

    position = Position(
        portfolio_id=portfolios[0].id,
        player_id=5002,
        direction="UP",
        shares_owned=Decimal("350.000000"),
        average_entry_price=Decimal("1.500000"),
    )
    db_session.add(position)
    await db_session.flush()

    await freeze_expired_tournaments(db_session)

    snap = (
        await db_session.execute(
            sa.select(TournamentSnapshot).where(
                TournamentSnapshot.tournament_id == tournament.id,
                TournamentSnapshot.user_id == users[0].id,
            )
        )
    ).scalar_one()

    assert float(snap.total_value) >= 800, (
        f"total_value {snap.total_value} should be >= 800 "
        "(cash 500 + position sell_refund ~342). "
        "Position sell_refund may not be included in freeze valuation."
    )

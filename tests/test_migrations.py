"""
Migration round-trip test: upgrade → downgrade → upgrade.

Requires a live PostgreSQL instance. Set TEST_DATABASE_URL (psycopg2 sync DSN)
or relies on the docker-compose default (lineup:lineup_secret@localhost:5432/lineup).
Tests are skipped gracefully if PostgreSQL is unreachable.

Run order matters: tests execute sequentially via the shared module-scoped fixtures.
"""
from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

EXPECTED_TABLES = {
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
}

_DEFAULT_SYNC_URL = "postgresql+psycopg2://lineup:lineup_secret@localhost:5432/lineup"


def _sync_url() -> str:
    raw = os.getenv(
        "TEST_DATABASE_URL",
        os.getenv("DATABASE_URL", _DEFAULT_SYNC_URL),
    )
    return raw.replace("+asyncpg", "+psycopg2")


# ─────────────────────────────── Fixtures ────────────────────────────────────


@pytest.fixture(scope="module")
def pg_engine():
    """Synchronous engine pointing at the test DB. Skip if DB unreachable."""
    url = _sync_url()
    try:
        engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"PostgreSQL not reachable — skipping migration tests: {exc}")
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def alembic_cfg(pg_engine):
    """Alembic Config wired to the test DB URL."""
    ini_path = os.path.join(os.path.dirname(__file__), "..", "alembic.ini")
    cfg = Config(ini_path)
    cfg.set_main_option(
        "sqlalchemy.url",
        pg_engine.url.render_as_string(hide_password=False),
    )
    return cfg


# ─────────────────────────────── Helpers ─────────────────────────────────────


def _tables_in_db(engine: sa.Engine) -> set[str]:
    """Return names of all BASE TABLE rows in the public schema."""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT table_name "
                "FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        )
        return {row[0] for row in rows}


def _extension_exists(engine: sa.Engine, name: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT 1 FROM pg_extension WHERE extname = :n"),
            {"n": name},
        ).fetchone()
        return row is not None


# ──────────────────────────────── Tests ──────────────────────────────────────


def test_upgrade_creates_all_13_tables(pg_engine, alembic_cfg):
    """upgrade head → all 13 tables present + uuid-ossp extension installed."""
    # Start from a clean baseline (idempotent if already at base)
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")

    tables = _tables_in_db(pg_engine)
    missing = EXPECTED_TABLES - tables
    assert missing == set(), f"Tables missing after upgrade: {missing}"

    assert _extension_exists(pg_engine, "uuid-ossp"), "uuid-ossp extension not found"


def test_all_expected_constraints_exist(pg_engine):
    """Spot-check key constraints survived the upgrade via information_schema."""
    with pg_engine.connect() as conn:
        # CHECK constraints
        check_names = {
            row[0]
            for row in conn.execute(
                sa.text(
                    "SELECT constraint_name "
                    "FROM information_schema.table_constraints "
                    "WHERE constraint_type = 'CHECK' AND table_schema = 'public'"
                )
            )
        }
        expected_checks = {
            "ck_portfolios_available_points_nneg",
            "ck_lmsr_b_min_positive",
            "ck_lmsr_q_up_dust_buffer",
            "ck_lmsr_q_down_dust_buffer",
            "ck_lmsr_current_rating_range",
            "ck_positions_shares_owned_nneg",
            "ck_trades_shares_positive",
            "ck_tournaments_end_after_start",
        }
        missing_checks = expected_checks - check_names
        assert missing_checks == set(), f"CHECK constraints missing: {missing_checks}"

        # UNIQUE constraints
        unique_names = {
            row[0]
            for row in conn.execute(
                sa.text(
                    "SELECT constraint_name "
                    "FROM information_schema.table_constraints "
                    "WHERE constraint_type = 'UNIQUE' AND table_schema = 'public'"
                )
            )
        }
        expected_uniques = {
            "uq_users_email",
            "uq_users_username",
            "uq_portfolios_user_id",
            "uq_lmsr_market_state_player_id",
            "uq_positions_portfolio_player_direction",
            "uq_pmr_player_fixture",
            "uq_tournament_members_tour_user",
            "uq_tournament_snapshots_tour_user",
        }
        missing_uniques = expected_uniques - unique_names
        assert missing_uniques == set(), f"UNIQUE constraints missing: {missing_uniques}"


def test_all_expected_indexes_exist(pg_engine):
    """Verify all named indexes exist in pg_indexes."""
    with pg_engine.connect() as conn:
        idx_names = {
            row[0]
            for row in conn.execute(
                sa.text(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
                )
            )
        }
        expected_indexes = {
            "ix_players_league_id",
            "ix_players_is_active",
            "ix_fixtures_kickoff_time",
            "ix_fixtures_status",
            "ix_fixtures_league_id",
            "ix_trades_portfolio_ts",
            "ix_trades_player_ts",
            "ix_pmr_player_recorded",
            "ix_rating_history_player_recorded",
        }
        missing_indexes = expected_indexes - idx_names
        assert missing_indexes == set(), f"Indexes missing: {missing_indexes}"


def test_downgrade_removes_all_tables(pg_engine, alembic_cfg):
    """downgrade base → none of the 13 application tables remain."""
    command.downgrade(alembic_cfg, "base")

    tables = _tables_in_db(pg_engine)
    still_present = EXPECTED_TABLES & tables
    assert still_present == set(), f"Tables still present after downgrade: {still_present}"


def test_upgrade_again_round_trip(pg_engine, alembic_cfg):
    """Second upgrade head → all 13 tables recreated (round-trip complete)."""
    command.upgrade(alembic_cfg, "head")

    tables = _tables_in_db(pg_engine)
    missing = EXPECTED_TABLES - tables
    assert missing == set(), f"Tables missing after second upgrade: {missing}"

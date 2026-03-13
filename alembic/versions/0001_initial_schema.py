"""Initial schema — all 13 tables

Revision ID: 0001
Revises:
Create Date: 2026-03-07
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── PostgreSQL extension ──────────────────────────────────────────────────
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')

    # ── users ─────────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("username", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )

    # ── players ───────────────────────────────────────────────────────────────
    op.create_table(
        "players",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("team", sa.String(255), nullable=True),
        sa.Column("position", sa.String(50), nullable=True),
        sa.Column("position_group", sa.String(2), nullable=False),
        sa.Column("league", sa.String(100), nullable=True),
        sa.Column("league_id", sa.Integer(), nullable=True),
        sa.Column("photo_url", sa.String(500), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_players"),
    )
    op.create_index("ix_players_league_id", "players", ["league_id"])
    op.create_index("ix_players_is_active", "players", ["is_active"])

    # ── fixtures ──────────────────────────────────────────────────────────────
    op.create_table(
        "fixtures",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("home_team", sa.String(255), nullable=False),
        sa.Column("away_team", sa.String(255), nullable=False),
        sa.Column("kickoff_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("league_id", sa.Integer(), nullable=True),
        sa.Column("matchday", sa.Integer(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_fixtures"),
    )
    op.create_index("ix_fixtures_kickoff_time", "fixtures", ["kickoff_time"])
    op.create_index("ix_fixtures_status", "fixtures", ["status"])
    op.create_index("ix_fixtures_league_id", "fixtures", ["league_id"])

    # ── liquidity_config ──────────────────────────────────────────────────────
    op.create_table(
        "liquidity_config",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("base_liquidity", sa.Numeric(10, 4), nullable=False),
        sa.Column("n_reference", sa.Integer(), nullable=False),
        sa.Column("alpha_default", sa.Numeric(10, 6), nullable=False),
        sa.Column("last_calibrated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_liquidity_config"),
    )

    # ── portfolios ────────────────────────────────────────────────────────────
    op.create_table(
        "portfolios",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("available_points", sa.Numeric(12, 4), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_portfolios_user_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_portfolios"),
        sa.UniqueConstraint("user_id", name="uq_portfolios_user_id"),
        sa.CheckConstraint(
            "available_points >= 0",
            name="ck_portfolios_available_points_nneg",
        ),
    )

    # ── lmsr_market_state ─────────────────────────────────────────────────────
    op.create_table(
        "lmsr_market_state",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("alpha", sa.Numeric(10, 6), nullable=False),
        sa.Column("b_min", sa.Numeric(10, 4), nullable=False),
        sa.Column("q_up", sa.Numeric(14, 6), nullable=False),
        sa.Column("q_down", sa.Numeric(14, 6), nullable=False),
        sa.Column("current_rating", sa.Numeric(6, 4), nullable=False),
        sa.Column("oracle_rating", sa.Numeric(6, 4), nullable=True),
        sa.Column("oracle_source", sa.String(10), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["player_id"], ["players.id"],
            name="fk_lmsr_player_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_lmsr_market_state"),
        sa.UniqueConstraint("player_id", name="uq_lmsr_market_state_player_id"),
        sa.CheckConstraint("b_min > 0", name="ck_lmsr_b_min_positive"),
        sa.CheckConstraint("q_up >= -0.01", name="ck_lmsr_q_up_dust_buffer"),
        sa.CheckConstraint("q_down >= -0.01", name="ck_lmsr_q_down_dust_buffer"),
        sa.CheckConstraint(
            "current_rating >= 0.0 AND current_rating <= 10.0",
            name="ck_lmsr_current_rating_range",
        ),
    )

    # ── positions ─────────────────────────────────────────────────────────────
    op.create_table(
        "positions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("portfolio_id", sa.Uuid(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(4), nullable=False),
        sa.Column("shares_owned", sa.Numeric(14, 6), nullable=False),
        sa.Column("average_entry_price", sa.Numeric(10, 6), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["portfolio_id"], ["portfolios.id"],
            name="fk_positions_portfolio_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["player_id"], ["players.id"],
            name="fk_positions_player_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_positions"),
        sa.UniqueConstraint(
            "portfolio_id", "player_id", "direction",
            name="uq_positions_portfolio_player_direction",
        ),
        sa.CheckConstraint(
            "shares_owned >= 0",
            name="ck_positions_shares_owned_nneg",
        ),
    )

    # ── trades ────────────────────────────────────────────────────────────────
    # Note: no FK on player_id — trades remain readable even if player deleted (§2.7)
    op.create_table(
        "trades",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("portfolio_id", sa.Uuid(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(10), nullable=False),
        sa.Column("shares", sa.Numeric(14, 6), nullable=False),
        sa.Column("cost_or_refund", sa.Numeric(14, 6), nullable=False),
        sa.Column("price_per_share", sa.Numeric(14, 6), nullable=False),
        sa.Column("rating_before", sa.Numeric(6, 4), nullable=False),
        sa.Column("rating_after", sa.Numeric(6, 4), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["portfolio_id"], ["portfolios.id"],
            name="fk_trades_portfolio_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_trades"),
        sa.CheckConstraint("shares > 0", name="ck_trades_shares_positive"),
    )
    op.create_index("ix_trades_portfolio_ts", "trades", ["portfolio_id", "timestamp"])
    op.create_index("ix_trades_player_ts", "trades", ["player_id", "timestamp"])

    # ── player_match_ratings ──────────────────────────────────────────────────
    op.create_table(
        "player_match_ratings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("fixture_id", sa.Integer(), nullable=False),
        sa.Column("sportmonks_rating", sa.Numeric(4, 2), nullable=True),
        sa.Column("minutes_played", sa.Integer(), nullable=True),
        sa.Column("goals", sa.Integer(), nullable=True),
        sa.Column("assists", sa.Integer(), nullable=True),
        sa.Column("shots_on_target", sa.Integer(), nullable=True),
        sa.Column("pass_accuracy", sa.Numeric(5, 2), nullable=True),
        sa.Column("key_passes", sa.Integer(), nullable=True),
        sa.Column("tackles", sa.Integer(), nullable=True),
        sa.Column("interceptions", sa.Integer(), nullable=True),
        sa.Column("clearances", sa.Integer(), nullable=True),
        sa.Column("dribbles_won", sa.Integer(), nullable=True),
        sa.Column("duels_won", sa.Integer(), nullable=True),
        sa.Column("saves", sa.Integer(), nullable=True),
        sa.Column("goals_conceded", sa.Integer(), nullable=True),
        sa.Column("clean_sheet", sa.Boolean(), nullable=True),
        sa.Column("xg", sa.Numeric(6, 4), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["player_id"], ["players.id"],
            name="fk_pmr_player_id",
        ),
        sa.ForeignKeyConstraint(
            ["fixture_id"], ["fixtures.id"],
            name="fk_pmr_fixture_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_player_match_ratings"),
        sa.UniqueConstraint(
            "player_id", "fixture_id",
            name="uq_pmr_player_fixture",
        ),
    )
    op.create_index(
        "ix_pmr_player_recorded",
        "player_match_ratings",
        ["player_id", "recorded_at"],
    )

    # ── rating_history ────────────────────────────────────────────────────────
    # Note: no FK on player_id — history survives player deletion
    op.create_table(
        "rating_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("rating", sa.Numeric(6, 4), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_rating_history"),
    )
    op.create_index(
        "ix_rating_history_player_recorded",
        "rating_history",
        ["player_id", "recorded_at"],
    )

    # ── tournaments ───────────────────────────────────────────────────────────
    op.create_table(
        "tournaments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"],
            name="fk_tournaments_created_by",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tournaments"),
        sa.CheckConstraint(
            "end_time > start_time",
            name="ck_tournaments_end_after_start",
        ),
    )

    # ── tournament_members ────────────────────────────────────────────────────
    op.create_table(
        "tournament_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tournament_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("starting_value", sa.Numeric(12, 4), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tournament_id"], ["tournaments.id"],
            name="fk_tournament_members_tournament_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_tournament_members_user_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tournament_members"),
        sa.UniqueConstraint(
            "tournament_id", "user_id",
            name="uq_tournament_members_tour_user",
        ),
    )

    # ── tournament_snapshots ──────────────────────────────────────────────────
    op.create_table(
        "tournament_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tournament_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("total_value", sa.Numeric(12, 4), nullable=False),
        sa.Column("starting_value", sa.Numeric(12, 4), nullable=False),
        sa.Column("profit_loss", sa.Numeric(12, 4), nullable=False),
        sa.Column("snapshotted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tournament_id"], ["tournaments.id"],
            name="fk_tournament_snapshots_tournament_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_tournament_snapshots_user_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tournament_snapshots"),
        sa.UniqueConstraint(
            "tournament_id", "user_id",
            name="uq_tournament_snapshots_tour_user",
        ),
    )


def downgrade() -> None:
    op.drop_table("tournament_snapshots")
    op.drop_table("tournament_members")
    op.drop_table("tournaments")
    op.drop_index("ix_rating_history_player_recorded", table_name="rating_history")
    op.drop_table("rating_history")
    op.drop_index("ix_pmr_player_recorded", table_name="player_match_ratings")
    op.drop_table("player_match_ratings")
    op.drop_index("ix_trades_player_ts", table_name="trades")
    op.drop_index("ix_trades_portfolio_ts", table_name="trades")
    op.drop_table("trades")
    op.drop_table("positions")
    op.drop_table("lmsr_market_state")
    op.drop_table("portfolios")
    op.drop_table("liquidity_config")
    op.drop_index("ix_fixtures_league_id", table_name="fixtures")
    op.drop_index("ix_fixtures_status", table_name="fixtures")
    op.drop_index("ix_fixtures_kickoff_time", table_name="fixtures")
    op.drop_table("fixtures")
    op.drop_index("ix_players_is_active", table_name="players")
    op.drop_index("ix_players_league_id", table_name="players")
    op.drop_table("players")
    op.drop_table("users")
    op.execute('DROP EXTENSION IF EXISTS "uuid-ossp"')

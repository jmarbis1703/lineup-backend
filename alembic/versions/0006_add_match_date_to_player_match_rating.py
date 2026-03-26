"""Add match_date to player_match_ratings

Revision ID: 0006
Revises: 0005
Create Date: 2026-03-25
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: existing rows have no kickoff date and must not be deleted or broken.
    # Backfill real kickoff dates by running:
    #   docker compose exec api python scripts/refresh_match_ratings.py --league 8 --days 90
    op.add_column(
        "player_match_ratings",
        sa.Column("match_date", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("player_match_ratings", "match_date")

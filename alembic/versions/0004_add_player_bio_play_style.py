"""Add bio and play_style columns to players table

Revision ID: 0004
Revises: 0003
Create Date: 2026-03-20
"""
from alembic import op
import sqlalchemy as sa


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("players", sa.Column("bio", sa.String(), nullable=True))
    op.add_column("players", sa.Column("play_style", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column("players", "play_style")
    op.drop_column("players", "bio")

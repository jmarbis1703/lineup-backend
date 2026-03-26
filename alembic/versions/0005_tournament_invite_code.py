"""Add invite_code to tournaments

Revision ID: 0005
Revises: 0004
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tournaments", sa.Column("invite_code", sa.String(8), nullable=True))
    op.create_unique_constraint("uq_tournaments_invite_code", "tournaments", ["invite_code"])
    op.create_index("ix_tournaments_invite_code", "tournaments", ["invite_code"])


def downgrade() -> None:
    op.drop_index("ix_tournaments_invite_code", table_name="tournaments")
    op.drop_constraint("uq_tournaments_invite_code", "tournaments", type_="unique")
    op.drop_column("tournaments", "invite_code")

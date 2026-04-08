"""Create waitlist_signups table

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-08

New table: waitlist_signups
Tracks email sign-ups for the pre-launch waitlist. Each row holds:
  - email (unique) — the applicant's address
  - referral_code (UUID v4, unique) — shareable referral link token
  - referred_by (nullable) — referral_code of the user who invited them
  - position — queue rank (assigned at insert, decremented via referrals)
  - referral_count — how many successful referrals this user has made
  - created_at — UTC timestamp of sign-up

No auth required to insert or read from this table.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "waitlist_signups",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("referral_code", sa.String(36), nullable=False),
        sa.Column("referred_by", sa.String(36), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("referral_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("(now() AT TIME ZONE 'utc')"),
        ),
    )
    op.create_index("ix_waitlist_email", "waitlist_signups", ["email"], unique=True)
    op.create_index("ix_waitlist_referral_code", "waitlist_signups", ["referral_code"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_waitlist_referral_code", table_name="waitlist_signups")
    op.drop_index("ix_waitlist_email", table_name="waitlist_signups")
    op.drop_table("waitlist_signups")

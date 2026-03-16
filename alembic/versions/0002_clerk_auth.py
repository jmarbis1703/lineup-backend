"""Clerk auth: add clerk_id, drop password_hash

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-16
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("clerk_id", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_users_clerk_id", "users", ["clerk_id"])
    op.drop_column("users", "password_hash")


def downgrade() -> None:
    op.add_column(
        "users",
        sa.Column("password_hash", sa.String(255), nullable=False, server_default=""),
    )
    op.drop_constraint("uq_users_clerk_id", "users", type_="unique")
    op.drop_column("users", "clerk_id")

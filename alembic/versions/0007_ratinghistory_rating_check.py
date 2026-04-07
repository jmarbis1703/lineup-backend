"""Add CHECK constraint on rating_history.rating

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-07

Adds ck_rating_history_rating_range: rating >= 0.0 AND rating <= 10.0

All three write paths already enforce this range before insertion:
  - market_init / trade: lmsr_rating() hard-clamps to [0.0, 10.0]
  - oracle_update: compute_oracle_rating() Layer 2/3 clamp to [3.0, 10.0];
    Layer 1 is a weighted average of Sportmonks 1–10 scale ratings
The constraint is added as VALID (immediate scan) because no existing row
can plausibly fall outside [0.0, 10.0].
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_rating_history_rating_range",
        "rating_history",
        sa.text("rating >= 0.0 AND rating <= 10.0"),
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_rating_history_rating_range",
        "rating_history",
        type_="check",
    )

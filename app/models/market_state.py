import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LmsrMarketState(Base):
    """One row per player. The single global LS-LMSR market (INV-07)."""

    __tablename__ = "lmsr_market_state"
    __table_args__ = (
        # INV-04: b_min must be strictly positive
        CheckConstraint("b_min > 0", name="ck_lmsr_b_min_positive"),
        # INV-06: Dust Buffer — q values may drift to -0.01 from NUMERIC rounding
        CheckConstraint("q_up >= -0.01", name="ck_lmsr_q_up_dust_buffer"),
        CheckConstraint("q_down >= -0.01", name="ck_lmsr_q_down_dust_buffer"),
        # INV-01: displayed rating clamped [0.0, 10.0]
        CheckConstraint(
            "current_rating >= 0.0 AND current_rating <= 10.0",
            name="ck_lmsr_current_rating_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # INV-07: UNIQUE(player_id) — exactly one market per player
    player_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("players.id"), unique=True, nullable=False
    )
    alpha: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False, default=Decimal("0.05"))
    b_min: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, default=Decimal("100.0"))
    q_up: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False, default=Decimal("0"))
    q_down: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False, default=Decimal("0"))
    current_rating: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    # INV-09: oracle_rating is DISPLAY ONLY — never written by the trading engine
    oracle_rating: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4))
    oracle_source: Mapped[Optional[str]] = mapped_column(String(10), default="layer_3")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

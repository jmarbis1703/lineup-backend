import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RatingHistory(Base):
    __tablename__ = "rating_history"
    __table_args__ = (
        Index("ix_rating_history_player_recorded", "player_id", "recorded_at"),
        CheckConstraint(
            "rating >= 0.0 AND rating <= 10.0",
            name="ck_rating_history_rating_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # No FK — history rows survive player deletion
    player_id: Mapped[int] = mapped_column(Integer, nullable=False)
    rating: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    # 'trade' | 'oracle_update' | 'market_init'
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

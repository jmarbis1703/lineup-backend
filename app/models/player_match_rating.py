import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PlayerMatchRating(Base):
    """Per-match Sportmonks data cache. Used by the 3-layer oracle."""

    __tablename__ = "player_match_ratings"
    __table_args__ = (
        UniqueConstraint("player_id", "fixture_id", name="uq_pmr_player_fixture"),
        Index("ix_pmr_player_recorded", "player_id", "recorded_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    player_id: Mapped[int] = mapped_column(Integer, ForeignKey("players.id"), nullable=False)
    fixture_id: Mapped[int] = mapped_column(Integer, ForeignKey("fixtures.id"), nullable=False)

    # Layer 1 oracle input — NULL if Sportmonks did not provide a rating
    sportmonks_rating: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 2))

    # Layer 2 oracle inputs
    minutes_played: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    goals: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    assists: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    shots_on_target: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    pass_accuracy: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    key_passes: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    tackles: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    interceptions: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    clearances: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    dribbles_won: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    duels_won: Mapped[Optional[int]] = mapped_column(Integer, default=0)

    # GK-only stats
    saves: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    goals_conceded: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    clean_sheet: Mapped[Optional[bool]] = mapped_column(Boolean, default=False)

    # NULL if Sportmonks did not provide xG
    xg: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4))

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

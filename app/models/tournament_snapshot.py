import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TournamentSnapshot(Base):
    """Frozen leaderboard written by freeze_expired_tournaments task (§7.6)."""

    __tablename__ = "tournament_snapshots"
    __table_args__ = (
        UniqueConstraint("tournament_id", "user_id", name="uq_tournament_snapshots_tour_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tournament_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id"), nullable=False
    )
    # Rank by profit_loss DESC
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    total_value: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    # Copied from tournament_members.starting_value at freeze time
    starting_value: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    # total_value − starting_value
    profit_loss: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    snapshotted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

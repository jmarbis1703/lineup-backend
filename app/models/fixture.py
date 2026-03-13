from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Fixture(Base):
    __tablename__ = "fixtures"
    __table_args__ = (
        Index("ix_fixtures_kickoff_time", "kickoff_time"),
        Index("ix_fixtures_status", "status"),
        Index("ix_fixtures_league_id", "league_id"),
    )

    # Sportmonks fixture ID — NOT auto-generated
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    home_team: Mapped[str] = mapped_column(String(255), nullable=False)
    away_team: Mapped[str] = mapped_column(String(255), nullable=False)
    kickoff_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="scheduled")
    league_id: Mapped[Optional[int]] = mapped_column(Integer)
    matchday: Mapped[Optional[int]] = mapped_column(Integer)
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

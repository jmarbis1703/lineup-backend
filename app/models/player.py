from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Player(Base):
    __tablename__ = "players"
    __table_args__ = (
        Index("ix_players_league_id", "league_id"),
        Index("ix_players_is_active", "is_active"),
    )

    # Sportmonks player ID — NOT auto-generated
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    team: Mapped[Optional[str]] = mapped_column(String(255))
    # Raw position string from Sportmonks
    position: Mapped[Optional[str]] = mapped_column(String(50))
    # Normalised group: FW, MF, DF, GK — used by Layer 2 oracle
    position_group: Mapped[str] = mapped_column(String(2), nullable=False, default="MF")
    league: Mapped[Optional[str]] = mapped_column(String(100))
    league_id: Mapped[Optional[int]] = mapped_column(Integer)
    photo_url: Mapped[Optional[str]] = mapped_column(String(500))
    bio: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    play_style: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # Granular position label: GK, CB, RB, LB, RWB, LWB, CDM, CM, CAM, RM, LM, RW, LW, CF, ST
    # Null until player import re-runs after migration 0005
    position_specific: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

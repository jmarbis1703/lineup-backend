import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        # One row per (portfolio, player, direction) — prevents duplicate UP/DOWN entries
        UniqueConstraint(
            "portfolio_id", "player_id", "direction",
            name="uq_positions_portfolio_player_direction",
        ),
        # INV-03: shares_owned must never go negative
        CheckConstraint("shares_owned >= 0", name="ck_positions_shares_owned_nneg"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    player_id: Mapped[int] = mapped_column(Integer, ForeignKey("players.id"), nullable=False)
    direction: Mapped[str] = mapped_column(String(4), nullable=False)  # 'UP' or 'DOWN'
    # INV-03
    shares_owned: Mapped[Decimal] = mapped_column(
        Numeric(14, 6), nullable=False, default=Decimal("0")
    )
    # Cost basis — updated on BUY, frozen on SELL (see PRD §2.6)
    average_entry_price: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

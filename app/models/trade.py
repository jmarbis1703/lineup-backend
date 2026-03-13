import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        CheckConstraint("shares > 0", name="ck_trades_shares_positive"),
        Index("ix_trades_portfolio_ts", "portfolio_id", "timestamp"),
        Index("ix_trades_player_ts", "player_id", "timestamp"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    # No FK on player_id — trades remain readable even if player is deleted (§2.7)
    player_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # BUY_UP | BUY_DOWN | SELL_UP | SELL_DOWN
    type: Mapped[str] = mapped_column(String(10), nullable=False)
    shares: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    cost_or_refund: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    price_per_share: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    rating_before: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    rating_after: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

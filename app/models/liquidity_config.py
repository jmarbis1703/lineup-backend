import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, Integer, Numeric, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LiquidityConfig(Base):
    """Single global row — calibration parameters for the LS-LMSR b_min."""

    __tablename__ = "liquidity_config"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    base_liquidity: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=Decimal("100.0")
    )
    n_reference: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    alpha_default: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0.05")
    )
    last_calibrated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

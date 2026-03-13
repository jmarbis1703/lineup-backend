import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Portfolio(Base):
    __tablename__ = "portfolios"
    __table_args__ = (
        # INV-02: available_points must never go negative
        CheckConstraint("available_points >= 0", name="ck_portfolios_available_points_nneg"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # INV-08: UNIQUE(user_id) — exactly one portfolio per user
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    available_points: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False, default=Decimal("1000.0000")
    )

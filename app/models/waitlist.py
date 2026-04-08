from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class WaitlistSignup(Base):
    __tablename__ = "waitlist_signups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    referral_code: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    referred_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    referral_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

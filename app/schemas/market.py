from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class PlayerMarketResponse(BaseModel):
    id: int
    name: str
    team: Optional[str] = None
    position_group: str
    league: Optional[str] = None
    league_id: Optional[int] = None
    photo_url: Optional[str] = None
    current_rating: Decimal
    oracle_rating: Optional[Decimal] = None
    oracle_source: Optional[str] = None
    q_up: Decimal
    q_down: Decimal
    b_effective: float
    total_shares: Decimal
    is_active: bool


class ChartPoint(BaseModel):
    rating: Decimal
    source: str
    recorded_at: datetime

from decimal import Decimal

from pydantic import BaseModel


class PositionOut(BaseModel):
    player_id: int
    direction: str
    shares_owned: Decimal
    average_entry_price: Decimal
    unrealized_pnl: float


class PortfolioResponse(BaseModel):
    available_points: Decimal
    total_value: float
    positions: list[PositionOut]

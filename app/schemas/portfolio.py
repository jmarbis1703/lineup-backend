from decimal import Decimal

from pydantic import BaseModel


class PositionOut(BaseModel):
    player_id: int
    direction: str
    shares_owned: Decimal
    average_entry_price: Decimal
    unrealized_pnl: float


class PortfolioResponse(BaseModel):
    available_points: float
    total_value: float
    positions: list[PositionOut]


class TradeHistoryEntry(BaseModel):
    id: str                # UUID as string
    player_id: int
    player_name: str
    type: str              # 'BUY_UP' | 'BUY_DOWN' | 'SELL_UP' | 'SELL_DOWN'
    shares: float
    cost_or_refund: float
    price_per_share: float
    timestamp: str         # ISO datetime


class TradeHistoryResponse(BaseModel):
    trades: list[TradeHistoryEntry]
    total: int             # total count for pagination


class PortfolioPoint(BaseModel):
    time: str              # ISO datetime string
    value: float


class PortfolioHistoryResponse(BaseModel):
    timeframe: str
    data: list[PortfolioPoint]

"""Pydantic schemas for trade endpoints (buy, sell, preview)."""
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class BuyRequest(BaseModel):
    player_id: int
    direction: Literal["UP", "DOWN"]
    budget: Decimal = Field(gt=Decimal("0"), description="Virtual points to spend (must be > 0)")


class SellRequest(BaseModel):
    player_id: int
    direction: Literal["UP", "DOWN"]
    shares: Decimal = Field(gt=Decimal("0"), description="Number of shares to sell (must be > 0)")


class PreviewRequest(BaseModel):
    player_id: int
    direction: Literal["UP", "DOWN"]
    budget: Decimal = Field(gt=Decimal("0"), description="Virtual points to spend (must be > 0)")


class BuyResponse(BaseModel):
    shares: Decimal
    cost: Decimal
    rating_before: Decimal
    rating_after: Decimal


class SellResponse(BaseModel):
    refund: Decimal
    rating_before: Decimal
    rating_after: Decimal


class PreviewResponse(BaseModel):
    shares: Decimal
    cost: Decimal
    rating_after: Decimal

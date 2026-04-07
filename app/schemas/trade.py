"""Pydantic schemas for trade endpoints (buy, sell, preview)."""
from decimal import ROUND_DOWN, Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class BuyRequest(BaseModel):
    player_id: int
    direction: Literal["UP", "DOWN"]
    budget: Decimal = Field(gt=Decimal("0"), description="Virtual points to spend (must be > 0)")


class SellRequest(BaseModel):
    player_id: int
    direction: Literal["UP", "DOWN"]
    shares: Decimal = Field(gt=Decimal("0"), description="Number of shares to sell (must be > 0)")

    @field_validator("shares")
    @classmethod
    def quantize_shares_to_six_dp(cls, v: Decimal) -> Decimal:
        """Truncate incoming shares to 6 decimal places (NUMERIC(14,6) precision).

        Positions store shares_owned at NUMERIC(14,6). Accepting higher precision
        from the client would cause silent DB rounding on the q_up/q_down write
        and could make the comparison (shares_owned < shares) fail unexpectedly
        when the user intends to sell their full position.

        ROUND_DOWN matches the _d6() truncation used when shares are created in
        execute_buy, so a full-position sell always passes the ownership check.
        """
        quantized = v.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        if quantized <= Decimal("0"):
            raise ValueError(
                "shares must be greater than 0 after rounding to 6 decimal places"
            )
        return quantized


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

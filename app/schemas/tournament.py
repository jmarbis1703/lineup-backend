import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class TournamentCreate(BaseModel):
    name: str = Field(..., min_length=3, max_length=100, strip_whitespace=True)
    start_time: datetime
    end_time: datetime


class TournamentResponse(BaseModel):
    """Full tournament response — includes invite_code. Use only for the authenticated creator."""

    id: uuid.UUID
    name: str
    status: str
    created_by: uuid.UUID
    start_time: datetime
    end_time: datetime
    created_at: datetime
    participant_count: int = 0
    invite_code: str | None = None

    model_config = {"from_attributes": True}


class TournamentPublicResponse(BaseModel):
    """Public tournament response — invite_code intentionally excluded."""

    id: uuid.UUID
    name: str
    status: str
    created_by: uuid.UUID
    start_time: datetime
    end_time: datetime
    created_at: datetime
    participant_count: int = 0

    model_config = {"from_attributes": True}


class JoinResponse(BaseModel):
    tournament_id: uuid.UUID
    user_id: uuid.UUID
    starting_value: Decimal
    message: str


class JoinByIdRequest(BaseModel):
    """Request body for POST /tournaments/{id}/join — invite code required."""

    invite_code: str


class TournamentLeaderboardEntry(BaseModel):
    rank: int
    display_name: str  # SEC-17: first 8 chars of clerk_id — no internal UUID exposed
    username: str
    starting_value: Decimal
    current_total_value: float
    profit_loss: float

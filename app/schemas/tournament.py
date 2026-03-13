import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class TournamentCreate(BaseModel):
    name: str
    start_time: datetime
    end_time: datetime


class TournamentResponse(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    created_by: uuid.UUID
    start_time: datetime
    end_time: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class JoinResponse(BaseModel):
    tournament_id: uuid.UUID
    user_id: uuid.UUID
    starting_value: Decimal
    message: str


class TournamentLeaderboardEntry(BaseModel):
    rank: int
    user_id: uuid.UUID
    username: str
    starting_value: Decimal
    current_total_value: float
    profit_loss: float

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class HotPlayer(BaseModel):
    name: str
    club: str
    position: str
    rating: float
    xg: float
    key_passes: float
    form: str          # '↑' | '↓' | '→' derived from change_24h
    prediction: str    # 'Goal scorer' | 'Key creator' | 'Clean sheet' | 'Defensive rock'
    epm_pts: int       # predicted EPM points


class FixtureResponse(BaseModel):
    id: int
    home_team: str
    away_team: str
    kickoff_time: datetime
    status: str
    league_id: Optional[int] = None
    matchday: Optional[int] = None
    hot_players: Optional[list[HotPlayer]] = None
    analysis: Optional[str] = None

    model_config = {"from_attributes": True}

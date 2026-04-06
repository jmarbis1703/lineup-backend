from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class MatchFormEntry(BaseModel):
    match_date: str        # "YYYY-MM-DD"
    opponent: str          # the team that is not the player's team
    home_away: str         # "H" or "A"
    rating: float = 0.0   # sportmonks_rating, 0–10 scale
    goals: int = 0
    assists: int = 0
    minutes: int = 0
    shots_on_target: int = 0
    xg: float = 0.0
    saves: int = 0         # GK only — always included, frontend filters by position
    clean_sheet: bool = False  # GK only — always included, frontend filters


class PlayerMatchFormEntry(BaseModel):
    match_date: str  # "YYYY-MM-DD" — fixture kickoff date from Sportmonks starting_at (§9B); falls back to recorded_at for pre-0006 rows
    goals: int = 0
    assists: int = 0
    minutes: int = 0
    rating: float = 0.0
    # opponent and result omitted — columns not yet in model (future step)


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
    change_24h: Optional[float] = None  # percent, positive = up
    bio: Optional[str] = None
    play_style: Optional[str] = None
    volatility_tier: str = "medium"  # "low" | "medium" | "high" — percentile rank of b_effective
    # Stats fields — populated from last 5 PlayerMatchRating rows.
    # All default to zero / empty so a brand-new player always renders correctly.
    recent_form: list[PlayerMatchFormEntry] = []  # up to 5 entries, newest-last
    goals: int = 0
    assists: int = 0
    minutes_per_game: Optional[int] = None # None when no match data exists
    tackles: int = 0
    goals_conceded: int = 0                # GK-relevant; 0 for outfield
    rating: float = 0.0                    # Layer 1 recency-weighted Sportmonks match rating avg


class ChartPoint(BaseModel):
    rating: Decimal
    source: str
    recorded_at: datetime


class PlayerStatsResponse(BaseModel):
    player_id: int
    goals: int
    assists: int
    xg: float
    key_passes: int
    rating: float          # recency-weighted average of last 5 sportmonks_ratings (Layer 1)
    form: list[float]      # last 5 sportmonks_ratings, oldest first
    minutes_played: int    # sum of last 5 matches
    minutes_per_game: Optional[int] = None  # average minutes per match (None when no match data)
    vaep: float            # simplified composite: (goals*6 + assists*4 + key_passes*2) / 10 clamped [0,10]
    matches_available: int # how many matches the stats are based on (0-5)
    goals_conceded: int = 0  # GK: sum of goals conceded in last 5 matches
    tackles: int = 0         # sum of tackles in last 5 matches (all positions)
    recent_matches: list[MatchFormEntry] = []  # last 5 matches, newest-first; [] until match data exists


class PublicPlayerResponse(BaseModel):
    name: str
    team: Optional[str] = None
    rating: float          # current_rating
    change_24h: Optional[float] = None

# Import Base and all models so that:
# 1. Alembic's env.py can do `from app.models import Base` and see every table.
# 2. Any module can do `from app.models import User, Portfolio, ...` cleanly.

from app.models.base import Base  # noqa: F401
from app.models.fixture import Fixture  # noqa: F401
from app.models.liquidity_config import LiquidityConfig  # noqa: F401
from app.models.market_state import LmsrMarketState  # noqa: F401
from app.models.player import Player  # noqa: F401
from app.models.player_match_rating import PlayerMatchRating  # noqa: F401
from app.models.portfolio import Portfolio  # noqa: F401
from app.models.portfolio_snapshot import PortfolioSnapshot  # noqa: F401
from app.models.position import Position  # noqa: F401
from app.models.rating_history import RatingHistory  # noqa: F401
from app.models.tournament import Tournament  # noqa: F401
from app.models.tournament_member import TournamentMember  # noqa: F401
from app.models.tournament_snapshot import TournamentSnapshot  # noqa: F401
from app.models.trade import Trade  # noqa: F401
from app.models.user import User  # noqa: F401
from app.models.watchlist import Watchlist  # noqa: F401

__all__ = [
    "Base",
    "Fixture",
    "LiquidityConfig",
    "LmsrMarketState",
    "Player",
    "PlayerMatchRating",
    "Portfolio",
    "PortfolioSnapshot",
    "Position",
    "RatingHistory",
    "Tournament",
    "TournamentMember",
    "TournamentSnapshot",
    "Trade",
    "User",
    "Watchlist",
]

"""Sportmonks v3 Football API client."""
import asyncio
import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Position mapping — PRD §7.1
# Sportmonks integer position_id → oracle position group (FW / MF / DF / GK)
# 2 GK + 6 DF + 7 MF + 8 FW = 23 total entries
# ---------------------------------------------------------------------------

POSITION_MAP: dict[int, str] = {
    # Goalkeeper (2)
    1: "GK",
    26: "GK",
    # Defender (6)
    2: "DF",
    3: "DF",
    4: "DF",
    5: "DF",
    6: "DF",
    7: "DF",
    # Midfielder (7)
    8: "MF",
    9: "MF",
    10: "MF",
    11: "MF",
    12: "MF",
    24: "MF",  # Central Midfielder (e.g. Bellingham)
    25: "MF",  # Box-to-Box Midfielder (e.g. Valverde)
    # Forward (8)
    13: "FW",
    14: "FW",
    15: "FW",
    16: "FW",
    17: "FW",
    18: "FW",
    19: "FW",
    27: "FW",  # Centre Forward (e.g. Benzema)
}


class SportmonksError(Exception):
    """Raised when the Sportmonks API returns an error or unexpected response."""


def map_position_to_group(position_str: str) -> str:
    """Map a Sportmonks position_id (as string) or position name to FW/MF/DF/GK.

    Tries integer ID lookup in POSITION_MAP first; falls back to keyword
    matching on the string.  Returns "MF" if no match is found.
    """
    try:
        return POSITION_MAP.get(int(position_str), "MF")
    except (ValueError, TypeError):
        pass

    s = (position_str or "").lower()
    if any(k in s for k in ("goalkeeper", "keeper")):
        return "GK"
    if any(k in s for k in ("defender", "back", "stopper", "sweeper")):
        return "DF"
    if any(k in s for k in ("forward", "winger", "striker", "attacker")):
        return "FW"
    return "MF"


# Stat type_ids from Sportmonks v3 details endpoint
_TYPE_GOALS = 52
_TYPE_ASSISTS = 79
_TYPE_MINUTES = 78
_TYPE_SHOTS = 80
_TYPE_PASSES = 99
_TYPE_DRIBBLES = 83
_TYPE_DUELS = 84


class SportmonksClient:
    """Async HTTP client for the Sportmonks v3 Football API."""

    def __init__(
        self,
        api_token: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self._api_token = api_token or settings.sportmonks_api_token
        self._base_url = (base_url or settings.sportmonks_base_url).rstrip("/")
        self._client = httpx.AsyncClient(
            headers={"Authorization": self._api_token},
            timeout=30.0,
        )

    async def _get(self, path: str, params: dict | None = None) -> dict:
        """Shared GET with 1.2 s inter-request delay and 429 retry backoff."""
        await asyncio.sleep(1.2)
        url = f"{self._base_url}/{path.lstrip('/')}"
        for attempt in range(3):
            resp = await self._client.get(url, params=params or {})
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                logger.warning(
                    "Rate-limited by Sportmonks; waiting %ss (attempt %d)",
                    retry_after,
                    attempt + 1,
                )
                await asyncio.sleep(retry_after)
                continue
            if resp.status_code != 200:
                raise SportmonksError(
                    f"GET {url} returned HTTP {resp.status_code}: {resp.text[:200]}"
                )
            return resp.json()
        raise SportmonksError(f"GET {url} failed after 3 rate-limit retries")

    async def get_teams_by_league(self, league_id: int, season_id: int) -> list[dict]:
        """Return all teams (with embedded squads) for a league.

        Starter-tier plan approach (teams/seasons and teamLeagues filter both
        return 404/400 on Starter plans):
        1. GET /standings/seasons/{season_id} → unique team IDs (participant_id)
        2. GET /squads/teams/{team_id}?include=player per team → squad entries
        """
        standings_data = await self._get(
            f"standings/seasons/{season_id}",
            params={"per_page": 100},
        )
        standings = standings_data.get("data", [])
        team_ids = list({s["participant_id"] for s in standings if s.get("participant_id")})

        teams: list[dict] = []
        for team_id in team_ids:
            squad_data = await self._get(
                f"squads/teams/{team_id}",
                params={"include": "player"},
            )
            teams.append({"id": team_id, "squads": squad_data.get("data", [])})

        return teams

    async def get_squad(self, team_id: int) -> list[dict]:
        """Return current squad entries for a team (with embedded player objects)."""
        data = await self._get(
            f"squads/teams/{team_id}",
            params={"include": "player"},
        )
        return data.get("data", [])

    async def get_player_statistics(
        self, player_id: int, season_id: int
    ) -> dict[str, Any]:
        """Fetch season statistics for a player; return normalised stat dict.

        Parses Sportmonks ``details`` type_ids into oracle-ready stat keys.
        Missing type_ids are returned as ``None``.
        """
        data = await self._get(
            f"players/{player_id}",
            params={
                "include": "statistics.details",
                "filters": f"playerStatisticSeasons:{season_id}",
            },
        )
        player = data.get("data", {})
        stats_list = player.get("statistics", [])

        result: dict[str, Any] = {
            "sportmonks_rating": None,
            "minutes_played": None,
            "goals": None,
            "assists": None,
            "shots_on_target": None,
            "pass_accuracy": None,
            "key_passes": None,
            "tackles": None,
            "dribbles_won": None,
            "duels_won": None,
            "saves": None,
            "goals_conceded": None,
            "clean_sheet": None,
            "xg": None,
        }

        if not stats_list:
            return result

        stat_entry = stats_list[0]
        rating_str = stat_entry.get("rating")
        if rating_str:
            try:
                result["sportmonks_rating"] = float(rating_str)
            except (ValueError, TypeError):
                pass

        for detail in stat_entry.get("details", []):
            type_id = detail.get("type_id")
            value = detail.get("value") or {}

            if type_id == _TYPE_GOALS:
                result["goals"] = value.get("goals") or value.get("total")
            elif type_id == _TYPE_ASSISTS:
                result["assists"] = value.get("assists") or value.get("total")
            elif type_id == _TYPE_MINUTES:
                result["minutes_played"] = value.get("minutes")
            elif type_id == _TYPE_SHOTS:
                result["shots_on_target"] = value.get("on_target")
            elif type_id == _TYPE_PASSES:
                pct = value.get("percentage")
                if pct is not None:
                    try:
                        result["pass_accuracy"] = float(str(pct))
                    except (ValueError, TypeError):
                        pass
            elif type_id == _TYPE_DRIBBLES:
                result["dribbles_won"] = value.get("success")
            elif type_id == _TYPE_DUELS:
                result["duels_won"] = value.get("won")

        return result

    async def get_fixture_with_lineups(self, fixture_id: int) -> dict:
        """Return fixture data including lineups and player ratings."""
        data = await self._get(
            f"fixtures/{fixture_id}",
            params={"include": "lineups.player;events"},
        )
        return data.get("data", {})

    async def get_fixtures_by_date_range(
        self,
        league_id: int,
        date_from: str,
        date_to: str,
    ) -> list[dict]:
        """Return fixtures for a league between two ISO dates (YYYY-MM-DD)."""
        data = await self._get(
            "fixtures",
            params={
                "filters": f"fixtureLeagues:{league_id};between:{date_from},{date_to}",
                "include": "participants;state",
            },
        )
        return data.get("data", [])

    async def get_current_season_id(self, league_id: int) -> int:
        """Return the current season ID for a league."""
        data = await self._get(
            f"leagues/{league_id}",
            params={"include": "currentseason"},
        )
        league = data.get("data", {})
        season = league.get("currentseason") or {}
        season_id = season.get("id")
        if season_id is None:
            raise SportmonksError(f"No current season found for league {league_id}")
        return int(season_id)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

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
    # Section type IDs returned by /squads/teams/{id}
    24: "GK",  # Goalkeeper section
    25: "DF",  # Defender section
    26: "MF",  # Midfielder section
    27: "FW",  # Attacker section
    # Legacy detailed position IDs
    1: "GK",
    2: "DF",
    3: "DF",
    4: "DF",
    5: "DF",
    6: "DF",
    7: "DF",
    8: "MF",
    9: "MF",
    10: "MF",
    11: "MF",
    12: "MF",
    13: "FW",
    14: "FW",
    15: "FW",
    16: "FW",
    17: "FW",
    18: "FW",
    19: "FW",
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
_TYPE_TACKLES = 44
_TYPE_INTERCEPTIONS = 45
_TYPE_CLEARANCES = 47
_TYPE_KEY_PASSES = 119
_TYPE_SAVES = 58
_TYPE_CLEAN_SHEET = 56
_TYPE_GOALS_CONCEDED = 57
_TYPE_XG = 117
_TYPE_MATCH_RATING = 118
# Lineup-context type_ids differ from statistics-context type_ids.
# Do NOT reuse season-stat constants in _parse_lineup_stats.
_TYPE_LINEUP_MINUTES = 1584  # minutes played in fixture lineup details (verified: fixture 19427186)


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
        """Shared GET with 3.5s pre-request delay and 429 retry backoff.
        Sleep fires before every request so no two requests fire within 3.5s
        regardless of phase transitions (squads → stats → fixtures).
        Retry-After is floor'd to at least 30 * 2**attempt to prevent rapid retries.
        """
        url = f"{self._base_url}/{path.lstrip('/')}"
        await asyncio.sleep(3.5)
        for attempt in range(3):
            resp = await self._client.get(url, params=params or {})
            if resp.status_code == 429:
                _raw = int(resp.headers.get("Retry-After", 0) or 0)
                retry_after = max(_raw, 30 * (2 ** attempt))
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
            params={"per_page": 100, "include": "participant"},
        )
        standings = standings_data.get("data", [])
        team_name_map: dict[int, str] = {}
        team_position_map: dict[int, int] = {}
        for s in standings:
            tid = s.get("participant_id")
            if tid:
                participant = s.get("participant") or {}
                team_name_map[tid] = participant.get("name") or f"Team {tid}"
                pos = s.get("position")
                if pos is not None:
                    team_position_map[tid] = int(pos)
        team_ids = list({s["participant_id"] for s in standings if s.get("participant_id")})

        teams: list[dict] = []
        for team_id in team_ids:
            squad_data = await self._get(
                f"squads/teams/{team_id}",
                params={"include": "player"},
            )
            teams.append({
                "id": team_id,
                "name": team_name_map.get(team_id, f"Team {team_id}"),
                "squads": squad_data.get("data", []),
                "position": team_position_map.get(team_id),  # int (1-based) or None
            })

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
            "interceptions": None,
            "clearances": None,
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
        # Handle both plain string "7.23" and nested dict {"average": "7.23"}
        rating_raw = stat_entry.get("rating")
        if isinstance(rating_raw, dict):
            rating_raw = rating_raw.get("average") or rating_raw.get("total")
        if rating_raw:
            try:
                result["sportmonks_rating"] = float(rating_raw)
            except (ValueError, TypeError):
                pass

        for detail in stat_entry.get("details", []):
            type_id = detail.get("type_id")
            value = detail.get("value") or {}

            if type_id == _TYPE_GOALS:
                g = value.get("goals")
                result["goals"] = g if g is not None else value.get("total")
            elif type_id == _TYPE_ASSISTS:
                a = value.get("assists")
                result["assists"] = a if a is not None else value.get("total")
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
            elif type_id == _TYPE_TACKLES:
                result["tackles"] = value.get("total") or value.get("tackles")
            elif type_id == _TYPE_INTERCEPTIONS:
                result["interceptions"] = value.get("total") or value.get("interceptions")
            elif type_id == _TYPE_CLEARANCES:
                result["clearances"] = value.get("total") or value.get("clearances")
            elif type_id == _TYPE_KEY_PASSES:
                result["key_passes"] = value.get("total") or value.get("key_passes")
            elif type_id == _TYPE_SAVES:
                result["saves"] = value.get("total") or value.get("saves")
            elif type_id == _TYPE_CLEAN_SHEET:
                result["clean_sheet"] = value.get("total") or value.get("clean_sheet")
            elif type_id == _TYPE_GOALS_CONCEDED:
                result["goals_conceded"] = value.get("total") or value.get("goals_conceded")
            elif type_id == _TYPE_XG:
                xg = value.get("total") or value.get("xg")
                if xg is not None:
                    try:
                        result["xg"] = float(xg)
                    except (ValueError, TypeError):
                        pass
            else:
                logger.debug("unknown type_id %s value %s", type_id, value)

        return result

    async def get_team_fixtures_with_ratings(
        self,
        team_id: int,
        date_from: str,
        date_to: str,
    ) -> list[dict]:
        """Fixtures for a team in date range, including lineups.details for match ratings.

        GET /fixtures/between/{date_from}/{date_to}/{team_id}?include=lineups.details
        Returns list of fixture dicts, each containing a 'lineups' array.
        """
        data = await self._get(
            f"fixtures/between/{date_from}/{date_to}/{team_id}",
            params={"include": "lineups.details"},
        )
        return data.get("data", [])

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

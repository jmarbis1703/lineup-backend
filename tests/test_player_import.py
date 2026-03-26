"""Tests for Sportmonks client parsing and the initial player import pipeline."""
from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lmsr import lmsr_rating
from app.models.market_state import LmsrMarketState
from app.models.player import Player
from app.services.sportmonks import SportmonksClient, map_position_to_group
from app.workers.player_import import initial_player_import, sync_new_players

# ---------------------------------------------------------------------------
# Fixture data helpers
# ---------------------------------------------------------------------------

_FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name: str) -> dict:
    with open(os.path.join(_FIXTURES_DIR, name)) as fh:
        return json.load(fh)


_TEAMS_DATA = _load("sportmonks_teams.json")
_STANDINGS_DATA = _load("sportmonks_standings.json")

# Parsed stats expected from sportmonks_player_stats.json (Bellingham, id=200100)
_BELLINGHAM_STATS: dict = {
    "sportmonks_rating": 8.20,
    "minutes_played": 2530,
    "goals": 14,
    "assists": 9,
    "shots_on_target": 41,
    "pass_accuracy": 85.0,
    "key_passes": None,
    "tackles": None,
    "interceptions": None,
    "clearances": None,
    "dribbles_won": 28,
    "duels_won": 61,
    "saves": None,
    "goals_conceded": None,
    "clean_sheet": None,
    "xg": None,
}

_EMPTY_STATS: dict = {
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

_FIXTURES_WITH_RATINGS = [
    {
        "id": 9001,
        "lineups": [
            {
                "player_id": 200100,
                "details": [
                    {"type_id": 118, "data": {"value": 8.5}},
                ],
            },
            {
                "player_id": 300200,
                "details": [],  # no match rating for this player
            },
        ],
    },
    {
        "id": 9002,
        "lineups": [
            {
                "player_id": 200100,
                "details": [
                    {"type_id": 118, "data": {"value": 7.8}},
                ],
            },
        ],
    },
]

_MINIMAL_ACTIVE_STATS: dict = {
    "sportmonks_rating": None,
    "minutes_played": 500,   # > 0 → passes activity filter
    "goals": None, "assists": None, "shots_on_target": None,
    "pass_accuracy": None, "key_passes": None, "tackles": None,
    "interceptions": None, "clearances": None,
    "dribbles_won": None, "duels_won": None, "saves": None,
    "goals_conceded": None, "clean_sheet": None, "xg": None,
}


def _make_mock_client(
    teams: list[dict] | None = None,
    player_stats: dict[int, dict] | None = None,
) -> SportmonksClient:
    """Return a SportmonksClient with all API methods replaced by AsyncMocks."""
    client: SportmonksClient = object.__new__(SportmonksClient)
    teams_data = teams if teams is not None else _TEAMS_DATA["data"]
    stats_lookup = player_stats or {}

    async def _get_stats(player_id: int, season_id: int) -> dict:  # noqa: ARG001
        return stats_lookup.get(player_id, _EMPTY_STATS)

    client.get_current_season_id = AsyncMock(return_value=23614)  # type: ignore[method-assign]
    client.get_teams_by_league = AsyncMock(return_value=teams_data)  # type: ignore[method-assign]
    client.get_squad = AsyncMock(  # type: ignore[method-assign]
        return_value=teams_data[0].get("squads", []) if teams_data else []
    )
    client.get_player_statistics = AsyncMock(side_effect=_get_stats)  # type: ignore[method-assign]
    client.get_team_fixtures_with_ratings = AsyncMock(return_value=[])  # type: ignore[method-assign]
    return client


# ---------------------------------------------------------------------------
# Test 1: client.get_teams_by_league parses the API response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_parses_teams() -> None:
    """get_teams_by_league fetches standings then squads per team."""
    _squad_response = {"data": _TEAMS_DATA["data"][0]["squads"]}

    async def _mock_get(path: str, params: dict | None = None) -> dict:
        if path.startswith("standings/"):
            return _STANDINGS_DATA
        return _squad_response

    client: SportmonksClient = object.__new__(SportmonksClient)
    client._api_token = "tok"  # type: ignore[attr-defined]
    client._base_url = "https://api.sportmonks.com/v3/football"  # type: ignore[attr-defined]
    client._get = AsyncMock(side_effect=_mock_get)  # type: ignore[method-assign]

    teams = await client.get_teams_by_league(564, 23614)

    assert isinstance(teams, list)
    assert len(teams) == 1
    assert teams[0]["id"] == 86
    assert teams[0]["name"] == "Real Madrid"
    assert len(teams[0]["squads"]) == 3
    # Verify the correct Starter-plan endpoints are used
    client._get.assert_any_call(
        "standings/seasons/23614", params={"per_page": 100, "include": "participant"}
    )
    client._get.assert_any_call("squads/teams/86", params={"include": "player"})


# ---------------------------------------------------------------------------
# Test 2: client.get_squad parses the API response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_parses_squad() -> None:
    """get_squad returns squad entries from the squads/teams/{id} endpoint."""
    # squads/teams/{id}?include=player returns {"data": [<squad entries>]}
    squad_response = {"data": _TEAMS_DATA["data"][0]["squads"]}

    client: SportmonksClient = object.__new__(SportmonksClient)
    client._api_token = "tok"  # type: ignore[attr-defined]
    client._base_url = "https://api.sportmonks.com/v3/football"  # type: ignore[attr-defined]
    client._get = AsyncMock(return_value=squad_response)  # type: ignore[method-assign]

    squad = await client.get_squad(86)

    assert len(squad) == 3
    player_ids = {entry["player_id"] for entry in squad}
    assert player_ids == {200100, 300200, 400300}
    client._get.assert_called_once_with("squads/teams/86", params={"include": "player"})


# ---------------------------------------------------------------------------
# Test 2b: zero-stat parse — regression for falsy `or` bug
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_parses_zero_goals_as_zero_not_none() -> None:
    """goals=0 and assists=0 must be stored as 0, not discarded as None."""
    from app.services.sportmonks import _TYPE_GOALS, _TYPE_ASSISTS, _TYPE_MINUTES

    stats_response = {
        "data": {
            "statistics": [
                {
                    "rating": None,
                    "details": [
                        {"type_id": _TYPE_GOALS, "value": {"goals": 0, "total": 0}},
                        {"type_id": _TYPE_ASSISTS, "value": {"assists": 0, "total": 0}},
                        {"type_id": _TYPE_MINUTES, "value": {"minutes": 900}},
                    ],
                }
            ]
        }
    }

    client: SportmonksClient = object.__new__(SportmonksClient)
    client._api_token = "tok"  # type: ignore[attr-defined]
    client._base_url = "https://api.sportmonks.com/v3/football"  # type: ignore[attr-defined]
    client._get = AsyncMock(return_value=stats_response)  # type: ignore[method-assign]

    result = await client.get_player_statistics(200100, 23614)

    assert result["goals"] == 0, "goals=0 must not be coerced to None"
    assert result["assists"] == 0, "assists=0 must not be coerced to None"
    assert result["minutes_played"] == 900


# ---------------------------------------------------------------------------
# Test 3: position mapping (no DB required)
# ---------------------------------------------------------------------------


def test_position_mapping() -> None:
    """map_position_to_group handles integer IDs and string fallback correctly."""
    # Section type IDs from /squads/teams/{id}
    assert map_position_to_group("24") == "GK"   # Goalkeeper section
    assert map_position_to_group("25") == "DF"   # Defender section
    assert map_position_to_group("26") == "MF"   # Midfielder section
    assert map_position_to_group("27") == "FW"   # Attacker section
    # Legacy detailed position IDs
    assert map_position_to_group("1") == "GK"
    assert map_position_to_group("2") == "DF"
    # String keyword fallback
    assert map_position_to_group("Goalkeeper") == "GK"
    assert map_position_to_group("Centre Forward") == "FW"
    assert map_position_to_group("Defender") == "DF"
    # Default fallback
    assert map_position_to_group("unknown") == "MF"
    assert map_position_to_group("") == "MF"


# ---------------------------------------------------------------------------
# Tests 4–9 — require a live PostgreSQL DB via db_session + liquidity_config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_creates_players(
    db_session: AsyncSession,
    liquidity_config,  # noqa: ANN001 — from conftest
) -> None:
    """initial_player_import inserts one Player row per squad entry."""
    client = _make_mock_client(player_stats={
        200100: _BELLINGHAM_STATS,
        300200: _MINIMAL_ACTIVE_STATS,
        400300: _MINIMAL_ACTIVE_STATS,
    })

    count = await initial_player_import(db_session, client, [564])

    assert count == 3
    result = await db_session.execute(
        select(Player).where(Player.league_id == 564)
    )
    players = result.scalars().all()
    assert len(players) == 3
    names = {p.name for p in players}
    assert "Jude Bellingham" in names
    assert "Karim Benzema" in names
    assert "Federico Valverde" in names


@pytest.mark.asyncio
async def test_import_creates_markets(
    db_session: AsyncSession,
    liquidity_config,  # noqa: ANN001
) -> None:
    """initial_player_import creates one LmsrMarketState row per player."""
    client = _make_mock_client(player_stats={
        200100: _BELLINGHAM_STATS,
        300200: _MINIMAL_ACTIVE_STATS,
        400300: _MINIMAL_ACTIVE_STATS,
    })

    await initial_player_import(db_session, client, [564])

    result = await db_session.execute(select(LmsrMarketState))
    markets = result.scalars().all()
    assert len(markets) == 3
    player_ids = {m.player_id for m in markets}
    assert player_ids == {200100, 300200, 400300}


@pytest.mark.asyncio
async def test_oracle_layer1(
    db_session: AsyncSession,
    liquidity_config,  # noqa: ANN001
) -> None:
    """A player with a Sportmonks rating gets oracle_source='layer_1'."""
    client = _make_mock_client(player_stats={200100: _BELLINGHAM_STATS})

    await initial_player_import(db_session, client, [564])

    result = await db_session.execute(
        select(LmsrMarketState).where(LmsrMarketState.player_id == 200100)
    )
    market = result.scalar_one()
    assert market.oracle_source == "layer_1"
    # Layer 1 rating = weighted avg of [8.20] ≈ 8.20
    assert abs(float(market.oracle_rating) - 8.20) < 0.01


@pytest.mark.asyncio
async def test_oracle_layer3(
    db_session: AsyncSession,
    liquidity_config,  # noqa: ANN001
) -> None:
    """A player with no stats at all gets oracle_source='layer_3' and rating=6.5."""
    # All three players get empty stats → Benzema and Valverde land on layer_3
    client = _make_mock_client()  # all players get _EMPTY_STATS

    await initial_player_import(db_session, client, [564])

    result = await db_session.execute(
        select(LmsrMarketState).where(LmsrMarketState.player_id == 300200)
    )
    market = result.scalar_one()
    assert market.oracle_source == "layer_3"
    assert abs(float(market.oracle_rating) - 6.5) < 1e-9


@pytest.mark.asyncio
async def test_market_rating_matches_oracle(
    db_session: AsyncSession,
    liquidity_config,  # noqa: ANN001
) -> None:
    """lmsr_rating(q_up, q_down, b_min) ≈ oracle_rating after market init."""
    client = _make_mock_client(player_stats={200100: _BELLINGHAM_STATS})

    await initial_player_import(db_session, client, [564])

    result = await db_session.execute(
        select(LmsrMarketState).where(LmsrMarketState.player_id == 200100)
    )
    market = result.scalar_one()
    computed = lmsr_rating(
        float(market.q_up),
        float(market.q_down),
        float(market.b_min),
    )
    # initialize_market guarantees |lmsr_rating - oracle_rating| < 1e-9;
    # NUMERIC(14,6) rounding may add small error — allow 1e-4.
    assert abs(computed - float(market.oracle_rating)) < 1e-4


@pytest.mark.asyncio
async def test_sync_marks_inactive(
    db_session: AsyncSession,
    liquidity_config,  # noqa: ANN001
) -> None:
    """sync_new_players sets is_active=False for players no longer in the squad."""
    # 1. Import all 3 players
    import_client = _make_mock_client(player_stats={
        200100: _BELLINGHAM_STATS,
        300200: _MINIMAL_ACTIVE_STATS,
        400300: _MINIMAL_ACTIVE_STATS,
    })
    await initial_player_import(db_session, import_client, [564])

    # Verify all 3 are active
    result = await db_session.execute(
        select(Player).where(Player.league_id == 564)
    )
    assert all(p.is_active for p in result.scalars().all())

    # 2. Sync with an empty squad (Real Madrid transferred everyone out)
    empty_teams = [{"id": 86, "name": "Real Madrid", "squads": []}]
    sync_client = _make_mock_client(teams=empty_teams)
    outcome = await sync_new_players(db_session, sync_client, [564])

    assert outcome["deactivated"] == 3
    assert outcome["new"] == 0

    # All 3 players should now be inactive
    result2 = await db_session.execute(
        select(Player).where(Player.league_id == 564)
    )
    assert all(not p.is_active for p in result2.scalars().all())


@pytest.mark.asyncio
async def test_import_filters_by_activity(
    db_session: AsyncSession,
    liquidity_config,  # noqa: ANN001
) -> None:
    """Activity filter: only players with minutes_played > 0 are imported.

    4-player squad — 2 active (200100, 300200), 2 inactive (400300=0 mins, 500400=None).
    """
    four_player_teams = [
        {
            "id": 86,
            "name": "Real Madrid",
            "squads": [
                {"player_id": 200100, "position_id": 26,
                 "player": {"id": 200100, "name": "Jude Bellingham", "image_path": None}},
                {"player_id": 300200, "position_id": 27,
                 "player": {"id": 300200, "name": "Karim Benzema", "image_path": None}},
                {"player_id": 400300, "position_id": 26,
                 "player": {"id": 400300, "name": "Federico Valverde", "image_path": None}},
                {"player_id": 500400, "position_id": 2,
                 "player": {"id": 500400, "name": "Antonio Rudiger", "image_path": None}},
            ],
        }
    ]
    player_stats = {
        200100: {**_EMPTY_STATS, "minutes_played": 2530, "sportmonks_rating": 8.20},
        300200: {**_EMPTY_STATS, "minutes_played": 900},
        400300: {**_EMPTY_STATS, "minutes_played": 0},    # filtered out
        500400: _EMPTY_STATS,                              # filtered out (None)
    }

    client = _make_mock_client(teams=four_player_teams, player_stats=player_stats)
    count = await initial_player_import(db_session, client, [564])

    assert count == 2
    result = await db_session.execute(select(Player).where(Player.league_id == 564))
    imported_ids = {p.id for p in result.scalars().all()}
    assert imported_ids == {200100, 300200}
    assert 400300 not in imported_ids
    assert 500400 not in imported_ids


# ---------------------------------------------------------------------------
# Test: nested-dict rating format (Sportmonks v3 may return {"average": "7.23"})
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nested_dict_rating_parsed() -> None:
    """get_player_statistics handles rating as {'average': '8.20'} (not plain string)."""
    from app.services.sportmonks import _TYPE_MINUTES

    stats_response = {
        "data": {
            "statistics": [
                {
                    "rating": {"average": "8.20", "count": 30},
                    "details": [
                        {"type_id": _TYPE_MINUTES, "value": {"minutes": 2530}},
                    ],
                }
            ]
        }
    }

    client: SportmonksClient = object.__new__(SportmonksClient)
    client._api_token = "tok"  # type: ignore[attr-defined]
    client._base_url = "https://api.sportmonks.com/v3/football"  # type: ignore[attr-defined]
    client._get = AsyncMock(return_value=stats_response)  # type: ignore[method-assign]

    result = await client.get_player_statistics(200100, 23614)

    assert result["sportmonks_rating"] == pytest.approx(8.20), (
        "nested-dict rating {'average': '8.20'} must be parsed as 8.20"
    )
    assert result["minutes_played"] == 2530


# ---------------------------------------------------------------------------
# Test: cross-league peer pool — both leagues' players in oracle context
# ---------------------------------------------------------------------------

_LEAGUE_B_TEAMS = [
    {
        "id": 999,
        "name": "PSG",
        "squads": [
            {"player_id": 600100, "position_id": 13,
             "player": {"id": 600100, "name": "Kylian Mbappe", "image_path": None}},
            {"player_id": 700200, "position_id": 8,
             "player": {"id": 700200, "name": "Marco Verratti", "image_path": None}},
        ],
    }
]


@pytest.mark.asyncio
async def test_cross_league_peer_pool(
    db_session: AsyncSession,
    liquidity_config,  # noqa: ANN001
) -> None:
    """Two leagues → all players imported, not just the first league's top-N."""
    league_b_stats = {
        600100: {**_MINIMAL_ACTIVE_STATS, "minutes_played": 2400},
        700200: {**_MINIMAL_ACTIVE_STATS, "minutes_played": 2100},
    }
    combined_stats = {
        200100: _BELLINGHAM_STATS,
        300200: _MINIMAL_ACTIVE_STATS,
        400300: _MINIMAL_ACTIVE_STATS,
        **league_b_stats,
    }

    # Client must handle two league_ids — mock returns different teams per call
    client: SportmonksClient = object.__new__(SportmonksClient)
    call_count = 0

    async def _get_teams(league_id: int, season_id: int) -> list[dict]:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        return _TEAMS_DATA["data"] if league_id == 564 else _LEAGUE_B_TEAMS

    async def _get_stats(player_id: int, season_id: int) -> dict:  # noqa: ARG001
        return combined_stats.get(player_id, _EMPTY_STATS)

    client.get_current_season_id = AsyncMock(return_value=23614)  # type: ignore[method-assign]
    client.get_teams_by_league = AsyncMock(side_effect=_get_teams)  # type: ignore[method-assign]
    client.get_player_statistics = AsyncMock(side_effect=_get_stats)  # type: ignore[method-assign]
    client.get_team_fixtures_with_ratings = AsyncMock(return_value=[])  # type: ignore[method-assign]

    count = await initial_player_import(db_session, client, [564, 888])

    # 3 from league 564 + 2 from league 888 = 5 total
    assert count == 5
    result = await db_session.execute(select(Player))
    all_ids = {p.id for p in result.scalars().all()}
    assert {200100, 300200, 400300} <= all_ids, "league 564 players must be present"
    assert {600100, 700200} <= all_ids, "league 888 players must be present"
    # Bellingham (layer_1) should still use layer_1 despite cross-league pool
    mkt_result = await db_session.execute(
        select(LmsrMarketState).where(LmsrMarketState.player_id == 200100)
    )
    bellingham_market = mkt_result.scalar_one()
    assert bellingham_market.oracle_source == "layer_1"


# ---------------------------------------------------------------------------
# Test: Layer 1 fires from fixture lineups (type_id 118) when no season rating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_layer1_from_fixture_lineups(
    db_session: AsyncSession,
    liquidity_config,  # noqa: ANN001
) -> None:
    """Player without a season rating gets layer_1 from fixture lineup ratings."""
    # Player 200100 has no sportmonks_rating (season-level) but appears in fixtures
    # Player 300200 has no ratings at all → should fall to layer_3
    client = _make_mock_client(player_stats={
        200100: {**_MINIMAL_ACTIVE_STATS, "minutes_played": 2530},
        300200: _MINIMAL_ACTIVE_STATS,
        400300: _MINIMAL_ACTIVE_STATS,
    })
    # Return fixture data only for team 86 (the Real Madrid team in the test fixture)
    client.get_team_fixtures_with_ratings = AsyncMock(  # type: ignore[method-assign]
        return_value=_FIXTURES_WITH_RATINGS
    )

    await initial_player_import(db_session, client, [564])

    # Player 200100: fixture ratings [8.5, 7.8] → Layer 1 recency-weighted avg
    # weights [0.35, 0.25], total 0.60 → (0.35*8.5 + 0.25*7.8) / 0.60
    expected_rating = (0.35 * 8.5 + 0.25 * 7.8) / 0.60
    result_200100 = await db_session.execute(
        select(LmsrMarketState).where(LmsrMarketState.player_id == 200100)
    )
    market_200100 = result_200100.scalar_one()
    assert market_200100.oracle_source == "layer_1"
    assert abs(float(market_200100.oracle_rating) - expected_rating) < 0.01

    # Player 300200: no fixture rating, no season rating → layer_3
    result_300200 = await db_session.execute(
        select(LmsrMarketState).where(LmsrMarketState.player_id == 300200)
    )
    market_300200 = result_300200.scalar_one()
    assert market_300200.oracle_source == "layer_3"
    assert abs(float(market_300200.oracle_rating) - 6.5) < 1e-9

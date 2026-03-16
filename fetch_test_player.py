#!/usr/bin/env python3
"""
One-shot recon script — dumps raw Sportmonks API responses for a known player.
Run via: docker compose -f docker-compose.prod.yml exec api python fetch_test_player.py
Output:  sportmonks_payload_dump.json  (written to container WORKDIR, i.e. /app/)
"""
import asyncio
import json
import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select, or_
from app.config import settings
from app.models.player import Player

BASE_URL = settings.sportmonks_base_url.rstrip("/")
HEADERS  = {"Authorization": settings.sportmonks_api_token}


async def fetch(path: str, params: dict) -> dict:
    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0) as client:
        resp = await client.get(f"{BASE_URL}/{path}", params=params)
        print(f"  {resp.status_code}  GET {resp.url}")
        return resp.json()


async def main():
    # ── Step 1: Get a real player from the DB ────────────────────────────────
    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as db:
        stmt = (
            select(Player)
            .where(
                or_(
                    Player.name.ilike("%Bowen%"),
                    Player.is_active == True,
                )
            )
            .order_by(
                Player.name.ilike("%Bowen%").desc(),
                Player.id,
            )
            .limit(1)
        )
        result = await db.execute(stmt)
        player = result.scalar_one_or_none()

    if player is None:
        print("ERROR: No players in DB. Run the initial import first.")
        return

    PLAYER_ID = player.id
    LEAGUE_ID = player.league_id
    print(f"Using player: {player.name!r}  id={PLAYER_ID}  league_id={LEAGUE_ID}")

    # ── Step 2: Resolve current season_id from Sportmonks API ────────────────
    season_data = await fetch(
        f"leagues/{LEAGUE_ID}",
        {"include": "currentseason"},
    )
    season = (season_data.get("data") or {}).get("currentseason") or {}
    SEASON_ID = season.get("id")
    if not SEASON_ID:
        print("ERROR: Could not resolve season_id for league", LEAGUE_ID)
        return
    print(f"Resolved season_id={SEASON_ID}")

    results = {}

    # ── Probe 1: EXACT call the app currently makes ──────────────────────────
    print("Probe 1 — current app call (include=statistics.details + season filter)")
    results["probe1_current"] = await fetch(
        f"players/{PLAYER_ID}",
        {"include": "statistics.details", "filters": f"playerStatisticSeasons:{SEASON_ID}"}
    )

    # ── Probe 2: Remove season filter (see if filter is killing results) ─────
    print("Probe 2 — same include, NO season filter")
    results["probe2_no_filter"] = await fetch(
        f"players/{PLAYER_ID}",
        {"include": "statistics.details"}
    )

    # ── Probe 3: Include statistics top-level only (check for rating field) ──
    print("Probe 3 — include=statistics only (no .details)")
    results["probe3_stats_only"] = await fetch(
        f"players/{PLAYER_ID}",
        {"include": "statistics", "filters": f"playerStatisticSeasons:{SEASON_ID}"}
    )

    # ── Probe 4: Include both statistics AND statistics.details ───────────────
    print("Probe 4 — include=statistics;statistics.details")
    results["probe4_both"] = await fetch(
        f"players/{PLAYER_ID}",
        {"include": "statistics;statistics.details", "filters": f"playerStatisticSeasons:{SEASON_ID}"}
    )

    output_path = "sportmonks_payload_dump.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nDone. Written to {output_path}")

    # ── Quick summary ─────────────────────────────────────────────────────────
    for probe_name, payload in results.items():
        stats = payload.get("data", {}).get("statistics", [])
        if stats:
            s0 = stats[0]
            rating_val = s0.get("rating")
            details_count = len(s0.get("details", []))
            print(f"  {probe_name}: statistics[0].rating={rating_val!r}  details_count={details_count}")
        else:
            print(f"  {probe_name}: statistics=[] (empty — check filter or season ID)")


asyncio.run(main())

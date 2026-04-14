"""Read-only diagnostic: probe live Sportmonks API for Layer 1 match ratings.

Usage:
    python scripts/debug_layer1.py

No database access. No writes. Pure network probe.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

from app.services.sportmonks import SportmonksClient
from app.workers.player_import import _extract_match_ratings

ARSENAL_TEAM_ID = 8


async def main() -> None:
    client = SportmonksClient()
    try:
        date_to = datetime.now(timezone.utc)
        date_from = date_to - timedelta(days=45)

        date_from_str = date_from.strftime("%Y-%m-%d")
        date_to_str = date_to.strftime("%Y-%m-%d")

        print(f"Fetching fixtures for Arsenal (team_id={ARSENAL_TEAM_ID})")
        print(f"Date range: {date_from_str} → {date_to_str}\n")

        fixtures = await client.get_team_fixtures_with_ratings(
            team_id=ARSENAL_TEAM_ID,
            date_from=date_from_str,
            date_to=date_to_str,
        )

        print(f"Fixtures returned: {len(fixtures)}\n")

        # Show the raw structure of the first player's details in the first fixture
        if fixtures:
            first_fixture = fixtures[0]
            lineups = first_fixture.get("lineups", [])
            print(f"First fixture ID: {first_fixture.get('id')}")
            print(f"Lineups count in first fixture: {len(lineups)}\n")
            if lineups:
                print("--- First lineup entry (raw JSON) ---")
                print(json.dumps(lineups[0], indent=2))
                print()
            else:
                print("WARNING: 'lineups' array is empty in the first fixture.\n")
        else:
            print("WARNING: No fixtures returned. Check date range or team ID.\n")

        # Run our exact extraction logic
        ratings_map = _extract_match_ratings(fixtures)

        print(f"--- ratings_map ({len(ratings_map)} players) ---")
        print(json.dumps(ratings_map, indent=2))

        if not ratings_map:
            print(
                "\nDIAGNOSIS: ratings_map is empty.\n"
                "Possible causes:\n"
                "  1. 'details' array missing or empty in lineup entries\n"
                "  2. type_id 118 not present in details\n"
                "  3. 'value' key structure differs from {'rating': ...} or {'total': ...}\n"
                "Check the raw lineup entry above for the actual structure."
            )

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())

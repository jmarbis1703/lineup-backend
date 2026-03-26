"""
One-off probe: print raw name fields for 3 specific players from Sportmonks squad API.
Run from lineup-backend/: python scripts/probe_name_fields.py
"""
import asyncio
import httpx
import json
import os

API_TOKEN = os.environ.get("SPORTMONKS_API_TOKEN", "2efMbNiMM2B6Kp4I9GYInmK1cEodzq51ZMM7Ga4CjTeWw3r4VWJLyIvPh26J")
BASE_URL = "https://api.sportmonks.com/v3/football"

# Team IDs: Barcelona=9, Real Madrid=86
# We'll fetch squad for both and look for our 3 targets by known player IDs
# Lamine Yamal (Spanish) = 1027656, Jude Bellingham (English) = 1093452, Vinicius Jr (Brazilian) = 1080960
TARGET_IDS = {
    1027656: "Lamine Yamal (Spanish)",
    1093452: "Jude Bellingham (English)",
    1080960: "Vinicius Jr (Brazilian)",
}

TEAM_IDS = [9, 86]  # Barcelona, Real Madrid

async def main():
    async with httpx.AsyncClient(timeout=30) as client:
        for team_id in TEAM_IDS:
            url = f"{BASE_URL}/squads/teams/{team_id}"
            params = {
                "api_token": API_TOKEN,
                "include": "player",
            }
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

            entries = data.get("data", [])
            for entry in entries:
                player = entry.get("player", {})
                pid = player.get("id")
                if pid in TARGET_IDS:
                    label = TARGET_IDS[pid]
                    print(f"\n{'='*60}")
                    print(f"PLAYER: {label}  (id={pid}, team_id={team_id})")
                    print(f"{'='*60}")
                    print(f"  name         = {player.get('name')!r}")
                    print(f"  common_name  = {player.get('common_name')!r}")
                    print(f"  display_name = {player.get('display_name')!r}")
                    print(f"  firstname    = {player.get('firstname')!r}")
                    print(f"  lastname     = {player.get('lastname')!r}")
                    print(f"\n  RAW player object keys: {sorted(player.keys())}")

if __name__ == "__main__":
    asyncio.run(main())

# LineUp — Vibe Coding Implementation Plan
## Bridging the 10 Backend Gaps

This document tracks the implementation of 10 backend features that bridge mock data in `lineup-nextjs/lib/mock-data.ts` with real API endpoints on `lineup-backend`.

## Status: IMPLEMENTED ✓

All 10 items have been implemented. See commit history for details.

---

## What Was Built

| Item | Feature | Files Changed | Status |
|---|---|---|---|
| 1 | `change_24h` on PlayerMarketResponse | `schemas/market.py`, `api/market.py` | ✓ Done |
| 2 | `vaep` Score | Delivered via Item 3 stats endpoint | ✓ Done |
| 3 | Player Stats Endpoint | `schemas/market.py`, `api/market.py` | ✓ Done |
| 4 | Watchlist (Saved Players) | `models/watchlist.py`, `schemas/watchlist.py`, `api/watchlist.py`, `main.py`, migration 0003 | ✓ Done |
| 5 | Portfolio History | `models/portfolio_snapshot.py`, `schemas/portfolio.py`, `api/portfolio.py`, `workers/snapshot.py`, migration 0003 | ✓ Done |
| 6 | Trade History Endpoint | `schemas/portfolio.py`, `api/portfolio.py` | ✓ Done |
| 7 | Trending Players Endpoint | `api/market.py` | ✓ Done |
| 8 | Fixture Hot Players | `schemas/fixtures.py`, `api/fixtures.py` | ✓ Done |
| 9 | Tournament Participant Count | `schemas/tournament.py`, `api/tournament.py` | ✓ Done |
| 10 | Public Player Endpoint | `schemas/market.py`, `api/market.py` | ✓ Done |

## New Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/market/players/public?limit=10` | No | Landing ticker |
| GET | `/api/market/trending?limit=8` | No | Top movers |
| GET | `/api/market/players/{id}/stats` | No | Player match stats |
| GET | `/api/portfolio/trades?limit=50&offset=0` | Yes | Trade history |
| GET | `/api/portfolio/history?timeframe=1D\|7D\|30D` | Yes | Portfolio value chart |
| GET | `/api/portfolio/watchlist` | Yes | Saved players |
| POST | `/api/portfolio/watchlist` | Yes | Add to watchlist |
| DELETE | `/api/portfolio/watchlist/{player_id}` | Yes | Remove from watchlist |

## Modified Endpoints

| Endpoint | Change |
|---|---|
| `GET /api/market/players` | Added `change_24h` field |
| `GET /api/market/players/{id}` | Added `change_24h` field |
| `GET /api/tournaments/mine` | Added `participant_count` field |
| `GET /api/fixtures/upcoming` | Added `include_hot_players` query param |

## New DB Tables (migration 0003)

- `watchlists` — user saved players (user_id + player_id, unique)
- `portfolio_snapshots` — hourly portfolio value snapshots

## New Celery Beat Task

- `snapshot-portfolios-hourly` — runs `app.workers.snapshot.snapshot_all_portfolios_task` every hour on the hour

## Architecture Notes

- **Prime Directive honored**: Zero modifications to `app/core/` files
- `_get_change_24h_map()` is a shared helper in `api/market.py` used by Items 1, 7, and 10
- Hot players enrichment is batch-queried (no N+1 per fixture)
- Portfolio snapshots use same batch-query pattern as tournament workers
- Route ordering: `/players/public` registered before `/players/{player_id}` to avoid path collision

## Run Migration

```bash
alembic upgrade head
```

## Verification Checklist

1. `alembic upgrade head` — no errors, 2 new tables in DB
2. `GET /api/market/players` — each player has `change_24h` field (null if new)
3. `GET /api/market/players/{id}/stats` — returns goals/assists/xg/form/vaep
4. `GET /api/portfolio/trades` — returns existing trade ledger entries
5. `GET /api/tournaments/mine` — each tournament has `participant_count`
6. `GET /api/market/players/public` — works without auth header
7. `GET /api/market/trending` — returns top 8 by change_24h
8. `GET /api/portfolio/watchlist` — returns `{ player_ids: [] }` for new user
9. `POST /api/portfolio/watchlist` `{"player_id": 1}` — 201
10. `DELETE /api/portfolio/watchlist/1` — 204
11. `GET /api/portfolio/history?timeframe=7D` — returns `[]` until Celery runs
12. `GET /api/fixtures/upcoming?include_hot_players=true` — returns `hot_players` array
13. **Regression**: `POST /api/trade/buy` still works
14. **Regression**: `GET /api/portfolio/me` unchanged response shape
15. **Regression**: WebSocket `/ws/market` still broadcasts

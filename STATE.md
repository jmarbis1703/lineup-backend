# LineUp Backend — STATE.md

> Agent MUST update this file after every completed step.

---

## Current Phase

**Phase**: Phase 11 — End-to-End Integration Tests
**Status**: Step 11.2 COMPLETE. Full regression gate: **208/208 PASSED** (all DB tests live against Postgres). Zero failures.

---

## Completed Steps

| # | Step | Agent | Status | Notes |
|---|------|-------|--------|-------|
| 1 | Create project directory structure per PRD §8 | Scaffolding | DONE | All dirs + empty files created |
| 2 | Create empty `__init__.py` files | Scaffolding | DONE | app, models, schemas, api, core, workers, services, tests |
| 3 | Create STATE.md | Scaffolding | DONE | This file |
| 4 | Write `requirements.txt` | Scaffolding / DevOps | DONE | pip dry-run resolves all 19 packages |
| 5 | Write `Dockerfile` | Scaffolding / DevOps | DONE | python:3.11-slim, gcc + libpq-dev |
| 6 | Write `docker-compose.yml` | Scaffolding / DevOps | DONE | `docker compose config` validates OK |
| 7 | Write `.env.example` | Scaffolding / DevOps | DONE | Includes SPORTMONKS_API_TOKEN, all DSNs |
| 8 | Implement `app/config.py` | Scaffolding / DevOps | DONE | pydantic-settings; DATABASE_URL, REDIS_URL, JWT_*, SPORTMONKS_* |
| 9 | Implement `app/main.py` | Scaffolding / DevOps | DONE | create_app factory, CORS, 6 empty routers, GET /health |
| 10 | Write `tests/test_health.py` + `conftest.py` | Scaffolding / DevOps | DONE | 2/2 PASSED (`pytest tests/test_health.py -v`) |
| 11 | Write `pytest.ini` | Scaffolding / DevOps | DONE | asyncio_mode=auto, fixture loop scope pinned |
| 12 | Implement `app/models/base.py` | DB | DONE | DeclarativeBase |
| 13 | Implement all 13 SQLAlchemy models in `app/models/` | DB | DONE | SA 2.0 Mapped annotations; all constraints per PRD §2 |
| 14 | Update `app/models/__init__.py` | DB | DONE | Exports Base + all 13 model classes |
| 15 | Write `tests/test_models.py` | DB | DONE | 12/12 PASSED — tables, UNIQUE, CHECK, Dust Buffer |
| 16 | Initialize Alembic + `alembic.ini` + `alembic/env.py` | DB | DONE | env.py imports all 13 models; DATABASE_URL asyncpg→psycopg2 auto-convert |
| 17 | Write `alembic/versions/0001_initial_schema.py` | DB | DONE | uuid-ossp extension, all 13 tables, all constraints, all indexes |
| 18 | Write `tests/test_migrations.py` | DB | DONE | 5/5 PASSED — upgrade→downgrade→upgrade round-trip; constraints + indexes verified via information_schema |
| 19 | Implement `app/dependencies.py` — `get_db()` async generator | DB | DONE | AsyncSession per request; module-level engine + async_sessionmaker |
| 20 | Rewrite `tests/conftest.py` — full fixture set | DB | DONE | apply_migrations, db_engine(async fn-scope), db_session(SAVEPOINT rollback), client, auth_headers factory, sample_player_with_market, liquidity_config |
| 21 | Create `tests/fixtures/` JSON files | DB | DONE | sportmonks_teams.json (3 players), sportmonks_player_stats.json, sportmonks_fixture.json (lineups+ratings, FT) |
| 22 | Write `tests/test_db_session.py` | DB | DONE | 5/5 PASSED — insert/query user, rollback isolation, auth_headers, sample_player_with_market, liquidity_config |
| 23 | Implement `app/core/lmsr.py` (§3 math) | Math | DONE | 7 pure functions; max-shift LSE; log1p budget-to-shares; asymmetric init |
| 24 | Implement `app/core/calibration.py` (§3.8) | Math | DONE | calibrate_b_min; sqrt scaling; ratchet enforced at call site |
| 25 | Write `tests/test_lmsr.py` | Math | DONE | 64/64 PASSED — effective_b, cost known-values, round-trip 1e-9, slippage, sell, rating, init above/below 5, calibration, LS-LMSR dynamics, stress (10k/alternating/random), overflow (q=100k), micro-budget lopsided clamp |
| 26 | Implement `app/core/oracle.py` (§4 — 3-layer oracle) | Oracle | DONE | 4 pure functions; only `math` import; Layer1 recency-weighted avg; Layer2 percentile→3–10 composite with zero-weight guard; Layer3 6.5 default; fallback chain |
| 27 | Write `tests/test_oracle.py` | Oracle | DONE | 22/22 PASSED — L1 5/3/1-match, empty, range; percentile midpoint/top/bottom/no-variation/inverse/inverse-high; L2 FW/MF/DF/GK weights, missing stats, zero-weight guard, range; full oracle L1→L2→L3 chain |
| 28 | Implement `app/core/auth.py` (JWT + bcrypt) | Auth | DONE | hash_password, verify_password, create_jwt, decode_jwt; get_current_user in dependencies.py; schemas/auth.py; api/auth.py; 10/10 PASSED |
| 29 | Implement `app/core/portfolio.py` (portfolio valuation) | Core | DONE | calculate_total_value: available_points + Σ sell_refund(position) via LS-LMSR math (no linear multiply) |
| 30 | Implement `app/core/trading.py` (buy/sell execution, INV-02–06) | Core | DONE | execute_buy, execute_sell, preview_buy; FOR UPDATE lock order: portfolios→market; 6dp share quantisation; close-only mode enforced |
| 31 | Implement `app/schemas/trade.py` | Core | DONE | BuyRequest/Response, SellRequest/Response, PreviewRequest/Response |
| 32 | Implement `app/api/trade.py` (buy, sell, preview endpoints) | API | DONE | 403 inactive player (buy only); 400 insufficient points/shares; no tournament queries (PRD §5.2 prohibition) |
| 33 | Write `tests/test_trading.py` (21 tests) | QA | DONE | 21/21 PASSED — all cases including concurrent race, close-only buy/sell, avg entry price, 6dp quantisation, tournament prohibition |
| 34 | Implement `app/schemas/market.py`, `portfolio.py`, `leaderboard.py`, `tournament.py` | API | DONE | PlayerMarketResponse, ChartPoint, PositionOut, PortfolioResponse, LeaderboardEntry, TournamentCreate/Response/JoinResponse/LeaderboardEntry |
| 35 | Implement `app/api/market.py` | API | DONE | GET /api/market/players (active, filters), GET /api/market/players/{id} (incl. inactive — close-only), GET /api/market/players/{id}/chart |
| 36 | Implement `app/api/portfolio.py` | API | DONE | GET /api/portfolio/me — LS-LMSR sell_refund PnL (no linear multiply); positions, total_value, unrealized_pnl |
| 37 | Implement `app/api/leaderboard.py` | API | DONE | GET /api/leaderboard — top-50 by live total_value; batch 3-query fetch (no N+1) |
| 38 | Implement `app/api/tournament.py` | API | DONE | POST /api/tournaments; GET /mine; POST /{id}/join (locks starting_value if active); GET /{id}/leaderboard (Δ profit_loss ranking) |
| 39 | Write `tests/test_market.py` (8 tests) | QA | DONE | 8/8 PASSED — all active/inactive/filter/chart/close-only cases |
| 40 | Write `tests/test_portfolio.py` (4 tests) | QA | DONE | 4/4 PASSED — initial, buy UP/DOWN, auth guard |
| 41 | Write `tests/test_leaderboard.py` (3 tests) | QA | DONE | 3/3 PASSED — ordering, max-50, rank-field |
| 42 | Write `tests/test_tournaments.py` (9 tests) | QA | DONE | 9/9 PASSED — create/join/409/active-lock/pending-placeholder/leaderboard-filter/delta-rank/veteran-no-advantage/mine |
| 43 | Fix bcrypt==4.0.1 pin in requirements.txt; update test_auth.py for /api/portfolio/me | Fixes | DONE | Restored 8 failing auth tests; 174/174 total |
| 44 | Implement app/services/redis_pubsub.py (publish_rating_update, subscribe) | WebSocket | DONE | Channel: lineup:rating_updates; JSON schema: type/player_id/rating_before/rating_after/direction/timestamp |
| 45 | Implement app/api/websocket.py (WS /ws/market, JWT ?token= auth, close 4401) | WebSocket | DONE | Forward task + disconnect loop; forward.cancel() on client disconnect |
| 46 | Integrate publish_rating_update into app/api/trade.py (buy + sell, fire-and-forget) | WebSocket | DONE | asyncio.create_task after db.commit(); failures never abort trade |
| 47 | Write tests/test_websocket.py (5 tests) | QA | DONE | test_ws_connect_valid_token, test_ws_no_token_rejected, test_ws_invalid_token_rejected, test_ws_receives_trade_update, test_ws_message_format; 5/5 PASSED |
| 48 | Implement app/workers/celery_app.py | Celery | DONE | Beat schedule: sync_players/fixtures daily 06:00; check_finished_fixtures 10min; activate_pending_tournaments 1min; freeze_expired_tournaments 5min; liquidity_recalibration 6h |
| 49 | Implement app/workers/fixture_sync.py | Celery | DONE | sync_fixtures(): upserts 5 leagues' upcoming fixtures; state→status map; participants→home/away teams |
| 50 | Implement app/workers/oracle_update.py | Celery | DONE | check_finished_fixtures(): INV-09 strictly enforced; WS event fire-and-forget; Layer 1→2→3 oracle; lookback_minutes=20 window |
| 51 | Implement app/workers/tournaments.py | Celery | DONE | activate_pending_tournaments(): batch-only O(1) queries + REPEATABLE READ; freeze_expired_tournaments(): same pattern + tournament_snapshots ranked by profit_loss DESC |
| 52 | Implement app/workers/calibration.py | Celery | DONE | liquidity_recalibration(): one-way ratchet + proportional q scaling; positions NEVER touched; hyper-inflation exploit blocked |
| 53 | Write tests/test_oracle_update.py (4 tests) | QA | DONE | oracle_rating set; INV-09 q_up/q_down/current_rating unchanged; rating_history row; WS event published |
| 54 | Write tests/test_calibration.py (6 tests) | QA | DONE | b_min updates+q scales; thick market unaffected; rating invariant; proportional scaling; one-way ratchet; positions not modified |
| 55 | Extend tests/test_tournaments.py (+12 tests) | QA | DONE | activate locks starting_values; no re-lock; O(1) batch queries; REPEATABLE READ spy; cash+position valuation; snapshot consistency; freeze snapshots; no position modify; completed→snapshot API; freeze batch; cash-only freeze; cash+position freeze |
| 56 | Write tests/test_integration_e2e.py (7 tests, 28 assertions) | Integration/QA | DONE | Player import+market init (oracle layers 1/3, rating_history); trading Alice/Bob/Charlie; portfolio+leaderboard; oracle update INV-09; tournament lifecycle pending→active→completed; cost basis VWAP; LS-LMSR b_eff growth+diminishing impact |
| 57 | Fix "Zero Import" bug in initial_player_import() — 3 root causes | Fixes | DONE | Fix 1: unwrap Sportmonks v3 nested squads envelope (dict→list); Fix 2: fetch stats for ALL squad members before slicing, sort by minutes_played DESC then slice; Fix 3: activity filter minutes_played > 0 with new-season fallback. Tests: updated 3 existing + added test_import_filters_by_activity (10 total). |
| 60 | Oracle misalignment fix: Layer 1 skip + Layer 2 inflation | Fixes | DONE | Fix 1: sportmonks_rating handles nested dict `{"average": "7.23"}`. Fix 2: two-pass import for cross-league peer pool. Fix 3: 8 missing stat type_ids parsed (tackles/interceptions/clearances/key_passes/saves/clean_sheet/goals_conceded/xg). Tests: 2 new + updated stat dicts. |
| 62 | Fix oracle math (Layer 2 formula) + Layer 1 fixture-based data ingestion | Fixes | DONE | **Phase 1 — Layer 2 formula**: `percentile_to_rating` changed from `3.0 + rank*7.0` (range 3–10) to `4.0 + rank*5.0` (range 4–9). Median rank=0.5 still maps to 6.5 (Layer 3 parity preserved). Prevents players being capped at 10.0 on high percentile. Updated 14 test assertions in test_oracle.py. **Phase 2 — Layer 1 fixture ingestion**: Added `_TYPE_MATCH_RATING = 118` constant and `get_team_fixtures_with_ratings()` method to SportmonksClient (GET fixtures/between/{from}/{to}/{team_id}?include=lineups.details). Added `_extract_match_ratings()` helper in player_import.py to parse lineup details. `initial_player_import` now fetches last 45 days of fixtures per team and injects per-match ratings (up to 5, most-recent-first) into stats_cache. `_upsert_player` uses fixture ratings as Layer 1 input when available, falling back to season-level sportmonks_rating. Added test_layer1_from_fixture_lineups + updated all mock clients. Economic effect: fewer players at inflated prices; Layer 1 now fires whenever a player appeared in recent fixtures. |

---

## Architecture Decisions Locked

- ONE global LMSR market per player (no tournament-scoped markets).
- ONE portfolio per user (no tournament economies).
- Custom tournaments = filtered leaderboard views only.
- Trading is budget-based (points → system calculates shares).
- Both UP and DOWN share directions supported.
- All players imported from Sportmonks API — 0 hardcoded players.
- 3-Layer Oracle: Layer 1 (Sportmonks rating avg) → Layer 2 (stat composite) → Layer 3 (6.5 default).
- Oracle is informational only — NEVER modifies q_up, q_down, current_rating (INV-09).
- Prices use NUMERIC (never float) throughout SQL layer.
- All trade writes inside transactions with SELECT ... FOR UPDATE on portfolios FIRST, then lmsr_market_state (INV-05).
- b_min one-way ratchet: can only increase (§3.8).
- Sell endpoint NEVER checks is_active — close-only mode applies to buys only (§5.2).
- No explicit db.rollback() in route handlers — raises before flush(), session auto-cleans on close.
- Concurrent test uses db_engine directly (real committed sessions) for genuine cross-session lock testing.

---

## Key Invariants (Quick Ref)

| ID | Rule |
|----|------|
| INV-01 | Rating clamped [0.0, 10.0] |
| INV-02 | available_points >= 0 always (check before buy) |
| INV-03 | shares_owned >= 0 always (check before sell) |
| INV-04 | b_eff > 0 always |
| INV-05 | All writes in txn; lock portfolios first, then lmsr_market_state |
| INV-06 | q_up, q_down >= -0.01 (Dust Buffer); sell guard: q - shares >= -0.01 |
| INV-07 | Exactly one lmsr_market_state row per player (UNIQUE player_id) |
| INV-08 | Exactly one portfolio row per user (UNIQUE user_id) |
| INV-09 | Oracle NEVER writes q_up, q_down, current_rating — only oracle_rating, oracle_source |

---

_Last updated: 2026-03-13 — Step 58: Fixed get_teams_by_league for Starter-tier Sportmonks plan. Both teams/seasons/{id} (404) and teamLeagues filter (400 "not applicable") fail on Starter. Replaced with 2-step: (1) GET standings/seasons/{season_id}?per_page=100 → unique participant_ids; (2) GET squads/teams/{team_id}?include=player per team. Same fix applied to get_squad. Added sportmonks_standings.json fixture; updated test_client_parses_teams to use side_effect mock routing by path; updated test_client_parses_squad. Live import result: 100 players imported across La Liga + EPL._

### Step 60 — Oracle Misalignment Fix: Layer 1 Skip + Layer 2 Inflation (2026-03-14)

**Issue**: Initial import of 250 players produced inflated ratings 8.5–9.6+ for all players.

**Root causes:**
1. **Layer 1 silently skipped** — Sportmonks v3 returns `rating` as `{"average": "7.23"}` (nested dict), not the plain string `"7.23"` the code expected. `float({"average": "7.23"})` raises `TypeError`, caught and swallowed, leaving `sportmonks_rating=None` for all players → Layer 2 fires for everyone.
2. **Per-league peer pool too small** — `all_players_stats` was built inside the per-league loop, giving each league's 50 players as the peer pool. Among ~10-12 elite forwards ranked only against each other, top scorers hit 90th+ percentile → compounded ratings of 9.0+.
3. **Eight stat type_ids never parsed** — `tackles`, `interceptions`, `clearances`, `key_passes`, `saves`, `clean_sheet`, `goals_conceded`, `xg` were declared in oracle weights but not extracted from the Sportmonks `details` array, so Layer 2 computed composites from an incomplete stat profile.

**Fixes applied:**
- `app/services/sportmonks.py`: Rating extraction handles both `str` and `dict` formats (`rating_raw.get("average") or rating_raw.get("total")`). Added constants `_TYPE_TACKLES`, `_TYPE_INTERCEPTIONS`, `_TYPE_CLEARANCES`, `_TYPE_KEY_PASSES`, `_TYPE_SAVES`, `_TYPE_CLEAN_SHEET`, `_TYPE_GOALS_CONCEDED`, `_TYPE_XG` (type_ids 44/45/47/119/58/56/57/117). Parsed all 8 new stats in the `details` loop. Added `debug` fallthrough logging for unknown type_ids. Added `interceptions`/`clearances` to result dict.
- `app/workers/player_import.py`: Refactored `initial_player_import()` into a two-pass approach — Pass 1 collects all data across all leagues without upserting; Pass 2 upserts all players against the full cross-league peer pool (250 players). Extracted `_collect_league_entries()` helper.
- `tests/test_player_import.py`: Updated `_BELLINGHAM_STATS`, `_EMPTY_STATS`, `_MINIMAL_ACTIVE_STATS` to include `interceptions`/`clearances`. Added `test_nested_dict_rating_parsed` (verifies `{"average": "8.20"}` → `8.20`). Added `test_cross_league_peer_pool` (verifies both leagues' players imported + Bellingham still layer_1).

**Post-fix action (human)**:
```bash
docker compose -f docker-compose.prod.yml down -v
docker compose -f docker-compose.prod.yml build --no-cache
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml exec api alembic upgrade head
docker compose -f docker-compose.prod.yml exec api python scripts/run_initial_import.py
# Verify: layer_1 should be dominant source, avg rating 7.0–7.8
docker compose -f docker-compose.prod.yml exec db psql -U $POSTGRES_USER -d $POSTGRES_DB \
  -c "SELECT oracle_source, COUNT(*), ROUND(AVG(oracle_rating)::numeric,2) avg FROM lmsr_market_state GROUP BY oracle_source;"
```

---

## Phase 12 — Production Deployment

### Phase 12.1 — Core deployment files (pre-existing, validated)

| File | Status |
|------|--------|
| `docker-compose.prod.yml` | EXISTS — no exposed db/redis ports; Caddy on 80/443; `.env.prod` env_file |
| `Caddyfile` | EXISTS — auto-TLS, WebSocket upgrade, security headers |
| `deploy.sh` | EXISTS — git pull, build, migrate, conditional seed, health loop |
| `.env.prod.example` | EXISTS — full secrets template |

Validation: `bash -n deploy.sh` → OK. `docker compose config` → OK (env warnings expected without `.env.prod`).

### Phase 12.2 — Documentation & health check (COMPLETE 2026-03-13)

| File | Status |
|------|--------|
| `docs/vps_setup.md` | CREATED — UFW rules, Docker CE install, first-time deploy steps, cron health check, verify commands |
| `scripts/check_system.py` | CREATED — 7-check health script; exit 0 all pass / exit 1 any fail; --base-url, --sportmonks-token, --timeout flags |

Validation: `python -m py_compile scripts/check_system.py` → syntax OK.

### Step 59 — Production Data Bug Fixes (2026-03-14)

**Issue**: Initial import (150 players) produced `oracle_rating=10.0` for all players
and `team="Unknown"` for every player.

**Root causes identified:**
1. `percentile_to_rating` used ceiling-rank — tied zeros ranked at top of tie group, inflating every player with 0 in a popular stat to the 90th+ percentile.
2. `goals`/`assists` zero values stored as `None` due to Python `or` falsy behaviour (`0 or None` → `None`), thinning the peer pool so real scorers reached rank=1.0.
3. `get_teams_by_league` returned no `name` key; importer always fell back to `"Unknown"`.

**Fixes applied:**
- `app/core/oracle.py`: ceiling-rank → mid-rank `(lower + 0.5 * equal) / n` (both forward and inverse modes).
- `app/services/sportmonks.py`: explicit `None` check replaces `or` for goals/assists parsing.
- `app/services/sportmonks.py`: `include=participant` on standings fetch populates `team_name_map`; `name` key added to every team dict returned by `get_teams_by_league`.
- `tests/test_oracle.py`: 8 existing assertions recalculated for mid-rank + 7 new tie-regression tests added.
- `tests/test_player_import.py`: standings params assertion updated; `sportmonks_standings.json` fixture extended with `participant` sub-object; zero-stat parse regression test added.

**Post-fix action (human)**: `docker compose -f docker-compose.prod.yml down -v` then re-run import.

### Pending human actions

1. Copy repo to VPS: `git clone <repo> /opt/lineup`
2. Fill secrets: `cp .env.prod.example .env.prod && nano .env.prod`
3. First deploy: `./deploy.sh --first`
4. Verify: `python scripts/check_system.py --base-url https://<domain> --sportmonks-token <token>`
   → Expected: `7/7 checks PASSED`


---

## Step 61 (2026-03-14): Production resilience hardening — initial import
- Fixed God Transaction: `initial_player_import` now commits setup queries before Pass 1 API calls begin, then commits per-league in Pass 2 (5 short transactions ≤8 min each) instead of one 40-min transaction (`app/workers/player_import.py`)
- Added exponential backoff to `SportmonksClient._get` when `Retry-After` header absent: 30s/60s/120s vs flat 60s default (`app/services/sportmonks.py`)
- Added `--league-id` CLI arg to `scripts/run_initial_import.py` for single-league test runs (`python scripts/run_initial_import.py --league-id 8`)

## Step 62 (2026-03-16): Fix `_extract_match_ratings` for live API payload structure
Updated `_extract_match_ratings` to handle live API payload structure `{'data': {'value': float}}` with fallback to legacy `{'value': {'rating': ...}}`. Live probe revealed type_id 118 is returned as `{"type_id": 118, "data": {"value": 6.85}}` not `{"type_id": 118, "value": {"rating": 8.5}}`; the old code always returned an empty `ratings_map`, causing all players to fall back to Layer 2. Mock fixtures in `tests/test_player_import.py` updated to match live structure.

---

## Session 15 (2026-04-06): Price Anomaly Fixes — Phantom % Changes + Chart Dual-Line Rendering

### Completed

**S15-1 — Phantom price change fix (`change_24h` source filter)**
- **Root cause:** `_get_change_24h_map()` in `app/api/market.py` queried `rating_history` without filtering on `source`. Oracle rows (`source="oracle_update"`) containing match-performance scores (e.g. 4.57) were compared against `LmsrMarketState.current_rating` (LMSR market price, e.g. 7.05), producing spurious % changes of 30–70% with no user trades.
- **Fix:** Added `RatingHistory.source.in_(["trade", "market_init"])` to the WHERE clause in `_get_change_24h_map()`. Oracle rows are now excluded from the 24h baseline entirely. Players with no trade/market_init history older than 24h correctly return `change_24h = None`.
- **Files:** `app/api/market.py` (lines ~128–132)
- **No DB migration. No breaking change.**

**S15-2 — Chart dual-line rendering (Market Price solid / Match Rating dashed)**
- **Root cause:** `Sparkline.tsx` mapped all `RatingHistory` rows to a single recharts `Line`, including `source="oracle_update"` rows with oracle scores. Oracle scores diverge from the LMSR market price (both 0–10 but semantically different), producing discrete jumps. The chart's rightmost point (oracle score) diverged from the header's `current_rating` (LMSR price), appearing as a bug.
- **Fix:** Split into two recharts `Line` components sharing one data array: `vMarket` (solid, `source !== "oracle_update"`) and `vOracle` (dashed, `strokeDasharray="4 3"`, `strokeOpacity=0.55`, `source === "oracle_update"`). `connectNulls={false}` on both. Tooltip distinguishes "Market Price" vs "Match Rating" via `formatter` returning `[value, name]` tuple.
- **Files:** `lineup-nextjs/components/Sparkline.tsx`
- **No schema changes. Chart endpoint (`/chart`) already returned `source` field — no backend change needed.**

**S15-3 — Documentation**
- `lineup-backend/README.md`: Added "Rating History Sources" section documenting the two data series, query rules, and what not to touch.
- `lineup-nextjs/FRONTEND_BACKEND_CONTRACT.md`: Added "Chart Endpoint — Rating History Sources" section.

### Remaining Work

| ID | Item | Status |
|----|------|--------|
| BL-1 | `gt=0` validator on buy/sell request schemas (prevent zero-budget trades) | 🟠 Still unverified — carry forward |

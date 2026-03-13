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

### Pending human actions

1. Copy repo to VPS: `git clone <repo> /opt/lineup`
2. Fill secrets: `cp .env.prod.example .env.prod && nano .env.prod`
3. First deploy: `./deploy.sh --first`
4. Verify: `python scripts/check_system.py --base-url https://<domain> --sportmonks-token <token>`
   → Expected: `7/7 checks PASSED`

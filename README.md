# LineUp Backend

A fantasy football prediction market where users trade on player performance ratings using a **Logarithmic Market Scoring Rule (LS-LMSR)** automated market maker. All players share a single global market state; custom tournaments are leaderboard filters on top of that shared data.

---

## Overview

### Unified Global Market

Every player has one `lmsr_market_state` row shared across all users and all tournaments. There are no per-tournament order books. This means:

- Trades in a tournament affect the global market for everyone
- Tournament leaderboards rank participants by profit/loss (Δ total_value) since they joined
- A completed tournament never blocks further trading

### LS-LMSR Pricing

LineUp uses **Liquidity-Sensitive LMSR** — the standard LMSR augmented with a dynamic liquidity parameter:

```
b_eff = b_min + α × (q_up + q_down)
```

`b_eff` grows with market activity, providing tighter spreads in high-volume markets and protecting thin markets from manipulation. Users trade a fixed **budget** (points) to receive a calculated number of shares, rather than specifying share quantities at trade time.

### 3-Layer Oracle

Oracle ratings seed market initial prices and are updated periodically by Celery workers:

| Layer | Trigger | Method |
|-------|---------|--------|
| **L1** | Sportmonks rating exists | Recency-weighted average of last 5 match ratings (weights: 0.35, 0.25, 0.20, 0.12, 0.08) |
| **L2** | Sportmonks ratings absent, match data exists | Position-weighted percentile composite of stats (goals, xG, tackles, saves, etc.) |
| **L3** | No match data | Default 6.5 |

### Sportmonks Integration

Player data (roster, ratings, stats) is pulled from the [Sportmonks v3 Football API](https://docs.sportmonks.com/football). The initial import covers La Liga (league 564) and Premier League (league 8).

---

## Setup

### Prerequisites

- Docker & Docker Compose
- Python 3.12+ (for running scripts outside Docker)
- A Sportmonks API token

### 1. Configure environment

```bash
cp .env.example .env
# Edit .env and set:
#   SPORTMONKS_API_TOKEN=<your token>
#   JWT_SECRET=<random secret>
#   POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB (optional, defaults provided)
```

### 2. Start services

```bash
docker compose up --build -d
```

This starts:
- `db` — PostgreSQL 15
- `redis` — Redis 7
- `api` — FastAPI on port 8000
- `celery_worker` — background task processor (4 workers)
- `celery_beat` — periodic task scheduler

The API waits for healthy DB and Redis before starting.

### 3. Run database migrations

```bash
docker compose exec api alembic upgrade head
```

> **Container migration note:** `alembic/versions/` is not live-mounted in
> the running container. For new migration files added after the container
> was built, either `docker cp` the file in first or rebuild the image before
> running `alembic upgrade head`:
>
> ```bash
> # Option A — copy without rebuilding
> docker cp alembic/versions/000X_your_migration.py \
>   lineup-api-1:/app/alembic/versions/
> docker compose exec api alembic upgrade head
>
> # Option B — full rebuild (picks up all changes)
> docker compose up --build -d api
> docker compose exec api alembic upgrade head
> ```
>
> **Current migration chain:** `0001 → 0002 → 0003 → 0004 → 0005 → 0006 → 0007`
> (0005 adds `invite_code VARCHAR(8) UNIQUE` to the `tournaments` table;
> 0006 adds `match_date DateTime(timezone=True)` nullable column to `player_match_ratings`;
> 0007 adds `CHECK (rating >= 0.0 AND rating <= 10.0)` to `rating_history`)

### 4. Import players

```bash
docker compose exec api python scripts/run_initial_import.py
```

This fetches all active players for the configured leagues from Sportmonks, upserts them into the `players` table, and initialises their `lmsr_market_state` rows using the oracle rating as the starting price.

**Standings-aware caps (Session 12):** The import applies a per-team player cap based on the team's current league standing. Top-10 teams get 4 players; remaining teams get 2. Players are ranked by existing `PlayerMatchRating` data (avg rating, minutes played). **Always run `refresh_match_ratings` immediately after any league import** to bootstrap rating data for newly added players — otherwise new players sort last regardless of quality.

```bash
docker compose exec api python scripts/refresh_match_ratings.py --league {ID} --days 90
```

**Current active player counts (Session 12 import):**

| League | League ID | Active Players |
|--------|-----------|---------------|
| Premier League | 8 | 60 |
| Bundesliga | 82 | 56 |
| Ligue 1 | 301 | 56 |
| Serie A | 384 | 60 |
| La Liga | 564 | 60 |
| **Total** | | **292** |

Total `PlayerMatchRating` rows: 4,788

> **Note:** `/docs` and `/redoc` are disabled in production (`DEBUG=false`). They are available in development only.

The system is now ready. Visit `http://localhost:8000/docs` for the interactive API explorer (development only).

---

## Running Tests

```bash
# All tests
docker compose exec api pytest

# Specific module
docker compose exec api pytest tests/test_lmsr.py -v

# With coverage
docker compose exec api pytest --cov=app --cov-report=term-missing
```

Key test files:

| File | Coverage |
|------|----------|
| `test_lmsr.py` | LS-LMSR math (cost, shares, refund, rating, init) |
| `test_oracle.py` | 3-layer oracle, percentile normalisation |
| `test_trading.py` | Buy/sell execution, budget checks, dust buffer |
| `test_market.py` | Market API endpoints |
| `test_leaderboard.py` | Global leaderboard ranking |
| `test_tournaments.py` | Tournament lifecycle, leaderboard filter |
| `test_integration_e2e.py` | Full trade → rating update → leaderboard flow |
| `test_calibration.py` | `b_min` auto-calibration |
| `test_oracle_update.py` | Celery oracle update worker |

---

## API Endpoints

Auth is handled entirely by Clerk (RS256 JWT in `Authorization: Bearer <token>`). There are no register/login endpoints — user rows are created by the Clerk webhook at `POST /api/webhooks/clerk`.

### Market (public — no auth)

| Method | Path | Response model | Data status |
|--------|------|---------------|-------------|
| `GET` | `/api/market/players` | `PlayerMarketResponse[]` | **LIVE** — `change_24h` is `null` until the `rating_history` table has rows older than 24 h; supports `?league_id=` and `?search=`. `bio` and `play_style` are `null` until Sportmonks sync; `b_effective` is always a `float`; `volatility_tier` is `"low"\|"medium"\|"high"` (percentile-bucketed from `b_effective` distribution). `league` field populated from `league_id` fallback in `_build_response()`; `players.league` column backfilled for all active PL players (2026-03-23). League ID map: 8=Premier League, 564=La Liga, 384=Serie A, 301=Ligue 1, 82=Bundesliga. |
| `GET` | `/api/market/players/{id}` | `PlayerMarketResponse` | **LIVE** — returns 200 even when `is_active=false` (close-only mode §5.1). Includes `bio: str\|null`, `play_style: str\|null`, `b_effective: float`, `volatility_tier: str` (reads Redis cache `volatility_tier:{id}`, TTL 6h; on miss, queries full active population and warms cache; inactive players fall back to `"medium"`) |
| `GET` | `/api/market/players/{id}/chart` | `ChartPoint[]` | **LIVE** — empty array until `rating_history` rows exist |
| `GET` | `/api/market/players/{id}/stats` | `PlayerStatsResponse` | **RETURNS ZEROS** until `scripts/refresh_match_ratings.py` runs. Seed sentinel (fixture_id=0) is excluded from aggregation. **New fields (2026-03-23):** `goals_conceded: int` (GK: goals allowed in last 5 matches, defaults 0), `tackles: int` (all positions, defaults 0), `minutes_per_game: int\|null` (average mins/match; `null` when no match data). `vaep` is always `0.0` pending event-level data. |
| `GET` | `/api/market/players/public` | `PublicPlayerResponse[]` | **LIVE** — minimal subset for landing page; `change_24h` null until 24 h of history |
| `GET` | `/api/market/trending` | `PlayerMarketResponse[]` | **LIVE** — sorted by `change_24h` desc; players without history appear last; supports `?limit=`; `volatility_tier` percentile-bucketed from full active-player set (not just the returned slice) |

### Fixtures (public — no auth)

| Method | Path | Response model | Data status |
|--------|------|---------------|-------------|
| `GET` | `/api/fixtures/upcoming` | `FixtureResponse[]` | **LIVE** — empty array until `fixtures` table populated by Sportmonks sync; `hot_players` requires `?include_hot_players=true`; `epm_pts` inside hot players uses last-match stats (zeros until Sportmonks sync) |

> **Celery Beat / fixture sync note:** The `sync-fixtures-daily` task is
> scheduled to run at 06:00 UTC via Celery Beat. The fixtures table will
> remain empty until Celery Beat is confirmed running and has completed at
> least one successful sync. Verify with:
>
> ```bash
> docker compose exec celery_beat celery -A app.workers inspect scheduled
> # or check the celery_beat container logs
> docker compose logs celery_beat --tail=50
> ```
>
> The Dashboard "Upcoming Hot Fixtures" section shows an empty-state message
> (`"No upcoming fixtures scheduled yet."`) until data is present — this is
> correct behaviour, not a bug.

### Trading (auth required)

| Method | Path | Response model | Data status |
|--------|------|---------------|-------------|
| `POST` | `/api/trade/buy` | `BuyResponse` | **LIVE** — 403 if player inactive, 400 if insufficient points or zero shares |
| `POST` | `/api/trade/sell` | `SellResponse` | **LIVE** — sell always permitted even on inactive players (close-only mode) |
| `POST` | `/api/trade/preview` | `PreviewResponse` | **LIVE** — no DB writes |

#### Trading Validation Layer (Session 18)

All three endpoints are rate-limited at **30 requests/minute per authenticated user** (keyed by JWT `sub`). Rate limiting was previously missing from `/api/trade/preview`.

##### Request validation (schema layer — `app/schemas/trade.py`)

| Field | Constraint | Error |
|-------|-----------|-------|
| `budget` (buy/preview) | `> 0` (Decimal) | 422 |
| `shares` (sell) | `> 0` (Decimal), **truncated to 6 decimal places** before further validation | 422 |
| `direction` | `"UP"` or `"DOWN"` (Literal) | 422 |
| `player_id` | integer | 422 |

The `shares` truncation rule: incoming share values are truncated (ROUND_DOWN) to 6 decimal places to match the `NUMERIC(14,6)` precision used in `positions.shares_owned`. A value like `0.1234567` becomes `0.123456`. If the truncated value is `0`, validation fails with 422.

##### Business logic validation (core layer — `app/core/trading.py`)

| Invariant | Guard | HTTP code |
|-----------|-------|----------|
| INV-02: balance sufficient | `budget > available_points` → `InsufficientPointsError` | 400 |
| INV-03: shares owned | `shares > shares_owned` → `InsufficientSharesError` | 400 |
| INV-06: Dust Buffer | `q - shares < -0.01` → `DustBufferError` | 400 |
| INV-10: non-zero shares | quantised shares = 0 → `ZeroSharesError` | 400 |
| Close-only mode | `player.is_active = False` on buy → `InactivePlayerError` | 403 |
| Player exists | no player row → `PlayerNotFoundError` | 404 |
| Market exists | no market row → `MarketNotFoundError` | 404 |

**INV-10 (Zero-shares guard):** If `budget` is so small that the LMSR computation yields fewer than `0.000001` shares (the minimum representable in `NUMERIC(14,6)`), the engine raises `ZeroSharesError` **before any database writes**. The user's balance is never touched. This prevents a phantom deduction where budget is charged but no shares are received.

##### Error response shape

All trading errors return standard FastAPI JSON:

```json
{ "detail": "human-readable message describing the problem" }
```

No internal state, stack traces, or raw exceptions are exposed.

##### Transaction safety

`execute_buy` and `execute_sell` use `SELECT ... FOR UPDATE` with a fixed lock order:
1. `portfolios` row (per-user)
2. `lmsr_market_state` row (per-player)

This order prevents deadlocks under concurrent buys by different users. On exception, the session is rolled back by the `get_db()` dependency's `async with` context manager before any lock is released.

### Portfolio (auth required)

| Method | Path | Response model | Data status |
|--------|------|---------------|-------------|
| `GET` | `/api/portfolio/me` | `PortfolioResponse` | **LIVE** — unrealised PnL computed via LMSR sell-refund (not linear) |
| `GET` | `/api/portfolio/trades` | `TradeHistoryResponse` | **LIVE** — paginated; supports `?limit=&offset=` |
| `GET` | `/api/portfolio/history` | `PortfolioHistoryResponse` | **LIVE** — returns empty `data: []` until the Celery hourly snapshot task has run; supports `?timeframe=1D\|7D\|30D` |
| `GET` | `/api/portfolio/watchlist` | `WatchlistResponse` | **LIVE** |
| `POST` | `/api/portfolio/watchlist` | `{ player_id }` | **LIVE** — idempotent; 404 if player not found |
| `DELETE` | `/api/portfolio/watchlist/{player_id}` | 204 | **LIVE** — idempotent; no error if not in watchlist |

### Leaderboard (public — no auth)

| Method | Path | Response model | Data status |
|--------|------|---------------|-------------|
| `GET` | `/api/leaderboard` | `LeaderboardEntry[]` | **LIVE** — top-50 by live total_value, computed via LMSR sell-refund |

### Tournaments

| Method | Path | Auth | Response model | Data status |
|--------|------|------|----------------|-------------|
| `GET` | `/api/tournaments` | — | `TournamentPublicResponse[]` | **LIVE** — all tournaments, public. **`invite_code` is NOT returned here** (removed in Session 12 via `TournamentPublicResponse` schema — security fix). |
| `POST` | `/api/tournaments` | ✓ | `TournamentResponse` | **LIVE** — created as `pending`; `invite_code` auto-generated (8-char alphanumeric); full response including `invite_code` returned only to creator. |
| `GET` | `/api/tournaments/mine` | ✓ | `TournamentResponse[]` | **LIVE** — tournaments the caller has joined |
| `POST` | `/api/tournaments/join/{code}` | ✓ | `JoinResponse` | **LIVE** — join by invite code; 404 if invalid; 409 if already a member |
| `POST` | `/api/tournaments/{id}/join` | ✓ | `JoinResponse` | **LIVE** — join by UUID; 409 if already a member; locks `starting_value` if `active` |
| `GET` | `/api/tournaments/{id}/leaderboard` | ✓ | `TournamentLeaderboardEntry[]` | **LIVE** — 400 if `pending`; frozen snapshot when `completed` |

### System (public — no auth)

| Method | Path | Response model | Data status |
|--------|------|---------------|-------------|
| `GET` | `/health` | `{ status: "ok" }` | **LIVE** |
| `WS` | `/ws/market` | rating update events | **LIVE** — Redis pub/sub; publishes on every trade |

---

## Architecture

```mermaid
graph TD
    SM[Sportmonks API] -->|REST| PI[Player Import Worker]
    PI -->|upsert players + init market| DB[(PostgreSQL)]

    subgraph Oracle["Oracle Update Worker (Celery Beat)"]
        L1[Layer 1: Recency-Weighted\nSportmonks Ratings]
        L2[Layer 2: Position-Weighted\nStat Percentiles]
        L3[Layer 3: Default 6.5]
        L1 -->|fallback| L2
        L2 -->|fallback| L3
    end

    SM -->|ratings + stats| Oracle
    Oracle -->|write oracle_rating| DB

    DB -->|read market state| MI[Market Init\ninitialize_market\nr_base → q_up, q_down]
    MI --> DB

    U[Users] -->|POST /api/trade/buy\nPOST /api/trade/sell| TA[Trading API]
    TA -->|execute_buy / execute_sell| LMSR[LS-LMSR Engine\nb_eff = b_min + α·Q\ncost function, refunds]
    LMSR -->|update q_up, q_down,\ncurrent_rating| DB
    LMSR -->|publish| RD[(Redis)]
    RD -->|WebSocket push| U

    DB -->|read all positions\n+ market states| LB[Global Leaderboard\ntop-50 by total_value]
    LB --> U

    subgraph Tournaments["Custom Tournaments"]
        TC[Create / Join] -->|snapshot starting_value| DB
        TL[Tournament Leaderboard] -->|filter by member list\nrank by Δ total_value| DB
    end

    U -->|POST /api/tournaments| Tournaments
    Tournaments -.->|same global market data| DB
```

---

## Parameter Tuning Guide

All parameters live in the `liquidity_config` table (single row) or per-player `lmsr_market_state`.

### `alpha` — Liquidity Sensitivity

```
b_eff = b_min + alpha * (q_up + q_down)
```

| Value | Effect |
|-------|--------|
| `0.0` | Fixed-`b` LMSR — slippage constant regardless of volume |
| `0.05` (default) | Moderate growth — spreads widen ~5% per share unit |
| `0.10+` | Aggressive — large trades incur significant slippage |

Increase `alpha` to protect the market from large single trades. Decrease it to improve liquidity for low-volume players.

### `b_min` — Baseline Liquidity

Minimum liquidity parameter. Controls the cost to move the market from 50/50 when no shares exist.

```
b_min = base_liquidity / n_reference
```

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `base_liquidity` | 100.0 | Total points budgeted to seed all markets |
| `n_reference` | 50 | Reference number of active players |

**Effect:** Larger `b_min` → cheaper to buy shares, wider price range supported, more resistant to small trades. Smaller `b_min` → more volatile, fast-moving markets.

The `calibration` Celery task auto-updates `b_min` based on observed trading volume. Manual override: update `liquidity_config.base_liquidity` directly in the DB.

### `base_liquidity` — Market Seed Budget

Total virtual points used to initialise all markets. Increasing this spreads more liquidity across all players, dampening initial volatility. Applies at market init time; changing it after import only affects newly created markets.

### Oracle Weights (Layer 1)

Recency weights are hardcoded in `app/core/oracle.py`:

```python
_LAYER1_WEIGHTS = [0.35, 0.25, 0.20, 0.12, 0.08]  # most-recent → oldest
```

Edit to preference and redeploy. Increase the first weight to make ratings more reactive to the latest match; flatten the array for a pure rolling average.

### Oracle Position Weights (Layer 2)

Per-position stat weights are in `_POSITION_WEIGHTS` in `app/core/oracle.py`. Each entry is `(stat_key, weight, inverse)`. Weights within a position group are renormalised if a stat is missing, so they do not need to sum to exactly 1.0.

---

## LMSR Dynamic b Floor

### What it is

The b floor is a dynamic liquidity protection mechanism that prevents extreme price movements when the user base is small. It sets a minimum value for the LMSR b parameter, computed from the current number of active users.

### Why it exists

The LMSR b parameter was calibrated for a market with ~100 active users. At beta scale (2–10 users), a single 100-point trade would move a player's price by 60% or more. The b floor raises the effective b so that a 100-point trade moves the price by less than 1% until the user base grows organically.

### Formula

```
B_FLOOR = LMSR_B_BASE × max(1, LMSR_N_TARGET / max(LMSR_N_MIN, n_users))

b_effective = max(B_FLOOR, b_min + alpha × volume)
```

Config values (env vars):

| Env var | Default | Meaning |
|---------|---------|---------|
| `LMSR_B_BASE` | `10000` | Target b at full scale (~100 users); < 1% impact per 100pt trade |
| `LMSR_N_TARGET` | `100` | User count at which B_FLOOR equals LMSR_B_BASE (anchor point) |
| `LMSR_N_MIN` | `5` | Safety floor on user count; prevents division explosion |

### Price impact by user count (100-point trade, fresh market)

| Users | b_effective | Price impact |
|-------|-------------|--------------|
| 5 | 200,000 | ~0.05% |
| 10 | 100,000 | ~0.10% |
| 50 | 20,000 | ~0.50% |
| 100 | 10,000 | ~1.00% |
| 500+ | 10,000 | ~1.00% (B_FLOOR plateaus; alpha takes over at high volume) |

### Where it lives

| Concern | Location |
|---------|----------|
| Formula | `app/core/lmsr.py` → `b_floor_for_users()` |
| Application | `app/core/trading.py` → `execute_buy()`, `execute_sell()`, `preview_buy()` |
| Cache | Redis key `lmsr_b_floor`, TTL 600 s (10 min) |
| User count cache | Redis key `user_count_cache`, TTL 600 s |
| Refresh task | `app/workers/player_import.py` → `refresh_b_floor_task` |
| Beat schedule | `celery_app.py` → `refresh-b-floor-hourly` (every hour on the hour) |
| Fallback chain | Redis miss → `COUNT(DISTINCT user_id) FROM portfolios` → `LMSR_N_MIN=5` |

### Removal / Supersession Criteria

The b floor does **not** need to be removed — it is designed to become a no-op automatically as the market grows.

However, explicit review is required at these thresholds:

**THRESHOLD 1 — 100 active users**
At this point `B_FLOOR = LMSR_B_BASE = 10,000`. The floor is at its minimum design value.
Action: Verify in production that price movements feel correct. Consider increasing `LMSR_B_BASE` if markets still feel too reactive.

**THRESHOLD 2 — Average market volume exceeds 200,000 points per player**
At this point the natural `b_effective = b_min + alpha × volume` exceeds B_FLOOR on its own. The floor becomes mathematically irrelevant.
Action: confirm via:
```sql
SELECT AVG(q_up + q_down) FROM lmsr_market_state;
```
If result > 2,000,000 (shares), the floor is superseded. The `refresh_b_floor_task` Celery task can be disabled but does no harm if left running.

**THRESHOLD 3 — Platform leaves beta**
Action: Review `LMSR_B_BASE`, `LMSR_N_TARGET` values with actual trading data. Adjust via env vars without a redeploy. Do **not** remove the code — it is a permanent safety mechanism.

### What NOT to do

- **Do NOT** hardcode `LMSR_B_BASE=0` to "disable" it. Set `LMSR_B_BASE=100` (same as `b_min`) to make the floor trivially small instead.
- **Do NOT** delete `b_floor_for_users()` — it may be needed again if user count drops (e.g. after a platform reset).
- **Do NOT** set `LMSR_N_TARGET` to a very large number without recalculating the price impact table above.

### Monitoring

```bash
# Check current b floor
docker compose exec redis redis-cli get lmsr_b_floor

# Check current user count cache
docker compose exec redis redis-cli get user_count_cache

# Force a refresh
docker compose exec celery_worker celery -A app.workers.celery_app:celery_app \
  call app.workers.player_import.refresh_b_floor_task

# Flush b floor cache (next trade will recompute)
docker compose exec redis redis-cli del lmsr_b_floor user_count_cache
```

---

## Production Deployment

### First-time deploy

```bash
# 1. Copy and fill in production secrets
cp .env.prod.example .env.prod
# Set POSTGRES_PASSWORD, REDIS_PASSWORD, JWT_SECRET (openssl rand -hex 32)
# Set DOMAIN, TLS_EMAIL, SPORTMONKS_API_TOKEN

# 2. Make scripts executable
chmod +x deploy.sh scripts/healthcheck_cron.sh

# 3. Deploy (builds image, migrates DB, imports players, starts Caddy)
./deploy.sh --first
```

### Subsequent deploys

```bash
./deploy.sh
```

`deploy.sh` steps: `git pull` → build image tagged with git SHA → start DB/Redis → `alembic upgrade head` → restart services → health check → prune old images.

The initial import is guarded by a `.initial_import_done` sentinel file and skipped automatically after the first run. Pass `--first` to force re-import.

### Health check cron

```bash
# Install — runs every 5 minutes, appends to log
echo "*/5 * * * * /opt/lineup-backend/scripts/healthcheck_cron.sh >> /var/log/lineup-health.log 2>&1" \
  | crontab -

# Configure alerts (add to .env.prod or export before cron runs)
ALERT_WEBHOOK=https://hooks.slack.com/services/...   # Slack/Discord
ALERT_EMAIL=ops@yourdomain.com                        # fallback email
```

Checks performed every 5 minutes:

| Check | Threshold |
|-------|-----------|
| Docker healthcheck status for `db`, `redis`, `api` | Not `unhealthy` |
| `GET /health` HTTP response | 200 |
| `pg_isready` inside DB container | Accepting connections |
| Redis `PING` | PONG |
| Celery worker `inspect ping` | Responds within 5 s |
| Disk usage | < 85% |

Script is silent on success (cron-friendly). Fires webhook and/or email on any failure.

### Production files

| File | Purpose |
|------|---------|
| `docker-compose.prod.yml` | Production compose: Caddy, no exposed DB/Redis ports, log rotation, `restart: always` |
| `Caddyfile` | Reverse proxy config: auto-TLS, HSTS, WebSocket upgrade, security headers |
| `.env.prod.example` | Production env template (copy → `.env.prod`) |
| `deploy.sh` | Deployment script with sentinel-guarded initial import |
| `scripts/healthcheck_cron.sh` | Cron health monitor with webhook/email alerts |

---

## Incident Log

### Incident: position_specific + 429 errors (Session 2–3)

**What was attempted:**
Adding granular position_specific field (CB, ST, LW etc.) sourced from
lineups.detailedposition in fixture includes.

**What broke and why:**

1. 429 rate limit errors mid-import
   - Root cause: lineups.detailedposition added to fixture include string
     increased per-fixture response size significantly, causing the
     players entity to hit Sportmonks rate limit (3,000 req/hour per entity)
     faster than before when combined with the 3.5s sleep cadence.
   - Contributing factor: Celery workers running concurrently during import
     create independent SportmonksClient instances that consume from the same
     rate limit bucket.
   - Fix: removed lineups.detailedposition from fixture include entirely.
     Reduced fixture date window from 45 to 21 days as additional headroom.

2. Wrong position_group — 4 midfielders classified as GK
   - Root cause: rows were written during a prior import run when POSITION_MAP
     had a bug. After POSITION_MAP was corrected (26→MF), the upsert did not
     re-run so stale rows persisted.
   - Fix: direct DB UPDATE on the 4 affected rows (Xavi Simons, Florian Wirtz,
     Douglas Luiz, Morgan Gibbs-White).

3. position_specific removed entirely (Session 2–3, completed Session 14)
   - Root cause: detailedposition.developer_name is not available on the
     Sportmonks Starter plan — returns empty object from squad, player,
     and fixture endpoints.
   - Dead code removed: DEVELOPER_NAME_TO_POSITION dict,
     map_position_specific_from_developer_name(),
     _extract_detailed_positions().
   - Column was mistakenly left in the ORM model without a migration,
     causing UndefinedColumnError on every import. Session 14 completed
     the removal from model, schema, API handler, and worker.
   - UI falls back to position_group ?? '—'.

**Rules added to prevent recurrence:**
- Never add nested includes to fixture fetches without auditing rate limit
  impact first. Each nested include multiplies response size and counts
  toward the per-entity hourly quota.
- Always stop Celery before running any import.
- After any POSITION_MAP change, run a targeted DB UPDATE on existing rows —
  do not assume the next import will correct stale data quickly enough.
- Do not guess at 429 root causes. Use the diagnostic script in
  scripts/diagnose_rate_limit.py to instrument headers before drawing
  conclusions.

### Incident: Wrong type_ids in _parse_lineup_stats (Fix A, Session 7)

Root cause 1: _TYPE_MINUTES = 78 is correct for statistics.details (season
  stats) but returns a small integer (1–4) in lineups.details context.
  Correct type_id for minutes played in fixture lineup details: 1584.
  Verified by cross-referencing 40 players across fixture 19427186.
Root cause 2: _TYPE_KEY_PASSES = 119 returns pass accuracy % in lineup
  context, not key passes. Commented out pending correct type_id verification.
Fix: Added _TYPE_LINEUP_MINUTES = 1584 in sportmonks.py. Added warning comment
  block above _parse_lineup_stats. Backfill re-run: --league 8 --days 90.
  Result: 0 fixtures synced (expected — already in DB), 1062 ratings upserted
  with corrected minutes_played values (confirmed range 50–95).

### Fix: Total Assets double-counted available_points (2026-03-23)

Root cause: `mylineup/page.tsx` computed "Total Assets" as `available_points + total_value`. The backend's `PortfolioResponse.total_value` returns position value only (Σ LS-LMSR sell-refund across open positions, no cash). Adding `available_points` on top inflated the figure by the full cash balance — a user with no positions and 2000 pts would see "Total Assets: 2000" instead of 0. Fix: removed `available_points` from the formula; Total Assets now renders `total_value` directly. The separate "Wallet" card continues to show `available_points` unchanged.

### Fix: Sell UX — amount bleed, missing position context, no input guard (2026-03-25)

Root cause: `TradePanel.tsx` sell mode never read `portfolio.positions`. The `usePortfolio()` hook was called but only `available_points` was consumed. This produced four bugs: (1) amount typed in buy mode persisted when switching to sell — could exceed actual position size by orders of magnitude; (2) user had no visibility into how many shares they owned or in which direction; (3) Confirm button allowed submitting any share count, causing backend 400 `InsufficientSharesError` for over-sells; (4) no fraction shortcuts existed for sell. Fix: all changes are in `TradePanel.tsx` (frontend only) — mode toggle now calls `setAmount(''); previewMutation.reset()`; direction toggle clears amount in sell mode; `sharesOwned` derived from `portfolio.positions.find(player_id + direction)`; "Owned: N" label, 25%/50%/100% shortcuts, unrealized P&L display, and "No {direction} position held" empty state added; Confirm disabled when `mode === 'sell' && (sharesOwned === 0 || Number(amount) > sharesOwned)`. Backend (`execute_sell`, LMSR math, all Pydantic schemas) confirmed correct — no backend changes.

### Fix B: recent_form now returns array of objects (Session 7)

Added PlayerMatchFormEntry schema to schemas/market.py.
recent_form shape: { match_date, goals, assists, minutes, rating }
opponent and result omitted — columns not in PlayerMatchRating model (deferred).
Breaking change vs prior list[float] shape — frontend must consume objects.
Known gap: match_date reflects upsert timestamp (recorded_at), not fixture
  kickoff date. Will be corrected in Step 9 when match_date column is added
  to PlayerMatchRating and populated from Sportmonks fixture starting_at.

---

## Operational Notes

14. type_id schemas differ between endpoints. lineups.details and
    statistics.details use different type_ids for the same concepts.
    Do NOT reuse constants across contexts without verifying against
    the live API. Confirmed lineup type_ids:
      1584 → minutes played (lineups.details only)
      78   → minutes played (statistics.details only — season stats)
      119  → pass accuracy % in lineup context (NOT key passes)
    See _parse_lineup_stats warning comment block for full notes.

15. volatility_tier Redis cache (Session 10, 2026-03-25)
    - Key schema  : `volatility_tier:{player_id}`  (value: "low" | "medium" | "high")
    - TTL         : 21600 s (6 hours) — matches liquidity recalibration Celery task cadence
    - Written by  : GET /api/market/players and GET /api/market/trending after every bulk
                    tier_map computation (asyncio.create_task fire-and-forget)
    - Read by     : GET /api/market/players/{id} — cache-hit serves accurate population-
                    context tier; cache-miss triggers full active-player DB query +
                    compute_volatility_tiers() + cache warm for all players
    - Inspect     : docker compose exec redis redis-cli KEYS "volatility_tier:*"
                    docker compose exec redis redis-cli TTL "volatility_tier:129602"
    - Flush cache : docker compose exec redis redis-cli --scan --pattern "volatility_tier:*" | xargs docker compose exec -T redis redis-cli DEL
    - Redis down  : All three endpoints degrade gracefully (no 500 errors)
    - No post-trade invalidation — TTL-based expiry only (see backend-requirements-spec §20)

16. Celery Beat — refresh_match_ratings_task (Step 9A, 2026-03-25)
    - Task name   : app.workers.player_import.refresh_match_ratings_task
    - Schedule    : crontab(hour=7, minute=0) — 07:00 UTC daily
    - League list : MATCH_RATING_LEAGUES in app/workers/player_import.py (currently [8])
                    To add a league for Step 7 (multi-league), append its Sportmonks league ID here.
    - Window      : MATCH_RATING_DAYS_BACK = 3 (beat task); CLI uses --days independently
    - Trigger manually: docker compose exec celery_worker celery -A app.workers.celery_app:celery_app call app.workers.player_import.refresh_match_ratings_task

17. match_date column on PlayerMatchRating (Step 9B, 2026-03-25)
    - Migration   : 0006_add_match_date_to_player_match_rating.py — nullable DateTime(timezone=True)
    - Source      : fixture["starting_at"] parsed to UTC datetime in refresh_match_ratings()
    - Fallback    : _get_stats_map() uses (r.match_date or r.recorded_at) — safe for pre-0006 rows
    - Backfill    : docker compose exec api python scripts/refresh_match_ratings.py --league 8 --days 90
                    (run after applying migration 0006; 1,062 rows updated on 2026-03-25)

18. Post-import rating bootstrap (2026-03-26)
    After any league re-import, always run refresh_match_ratings immediately to bootstrap rating
    data for newly imported players:
      docker compose exec api python scripts/refresh_match_ratings.py --league {ID} --days 90
    Without this step, new players will have sort key (0.0, 0) on the next import and may be
    excluded in favour of players with existing rating history.
    Current workaround until two-pass import is implemented (see Remaining Work — Ranking Fix).

19. Standings-aware per-team cap (Session 12, 2026-03-26)
    - Function     : _team_cap(standing_position: int) -> int in app/workers/player_import.py
    - Logic        : Top-10 teams in standings → 4 players; remaining teams → 2 players
    - Ranking signal: avg(sportmonks_rating) + sum(minutes_played) from existing PlayerMatchRating
                     rows. Players with no rating rows sort last (see Operational Note 18).
    - team_position_map built from standings fetch in sportmonks.py at import time
    - Post-import deactivation: after each league import, players in that league NOT in the
      selected set are set is_active=False (scoped to that league_id only; no rows deleted)
    - Fallback log when no rating data: "WARNING: No match rating data for league {league_id}
      — player selection is arbitrary. Run refresh_match_ratings after import."

20. Player name fix — display_name field (Session 12, 2026-03-26)
    - _upsert_player() now uses display_name (popular short name) with .strip() as the
      primary name source. Falls back to name field, then existing DB value.
    - Applies on both insert and update branches.
    - Purpose: ensures players are stored under their well-known name (e.g. "Vini Jr."
      rather than "Vinícius José de Oliveira Junior Morais").

21. refresh_match_ratings.py DB connection fix (Session 12, 2026-03-26)
    - Script previously used hardcoded os.environ.get("DATABASE_URL", "postgresql+asyncpg://lineup:lineup_secret@localhost:5432/lineup")
    - Now uses from app.config import settings — identical pattern to run_initial_import.py
    - Also fixed sessionmaker → async_sessionmaker for SQLAlchemy 2 compatibility
    - If DATABASE_URL password contains special characters (e.g. ! → %21), URL-encode them
      or use an alphanumeric-only password to avoid asyncpg connection failures.

23. Rate limits and WebSocket auth (Session 13, 2026-03-26)
    - Rate limits (Redis-backed via slowapi):
        200 req/min  — global, per IP
         30 req/min  — per user: POST /api/trade/buy, POST /api/trade/sell
         10 req/hour — per user: POST /api/tournaments
         20 req/hour — per user: POST /api/tournaments/{id}/join (both join endpoints)
    - WebSocket auth uses opaque ticket pattern:
        1. POST /api/ws/ticket  (auth required) → returns { ticket: "<token>" }
        2. Connect to WS with ?ticket=<token>
        Ticket TTL: 30 seconds. Single-use. Bearer token no longer accepted in WS query string.

22. Security configuration (Session 12, 2026-03-26)
    - CORS: allow_origins now reads ALLOWED_ORIGINS env var (defaults to http://localhost:3000);
      allow_methods and allow_headers restricted to explicit lists. Wildcard "*" removed.
    - Security headers in next.config.ts: X-Frame-Options, X-Content-Type-Options,
      Referrer-Policy, Permissions-Policy
    - FastAPI /docs and /redoc: disabled in production (DEBUG env var must be "true" to enable)
    - Portfolio auto-provisioning: INSERT ... ON CONFLICT DO NOTHING replaces plain db.add()
      (eliminates race condition on concurrent first-login requests)
    - config.py: hardcoded credential defaults removed; required fields use Pydantic ... and
      fail fast on startup if missing
    - Dead auth schemas in app/schemas/auth.py archived with warning comment
    - Clerk secret key removed from lineup-nextjs/.env.local
    - .env.example files created in both projects with placeholder values only

24. rating_history.rating CHECK constraint (BUG-05, 2026-04-07)
    - Migration   : 0007_ratinghistory_rating_check.py
    - Constraint  : ck_rating_history_rating_range — CHECK (rating >= 0.0 AND rating <= 10.0)
    - Rationale   : rating_history.rating had no CHECK constraint despite all write paths
                    constraining values to [0.0, 10.0]. This mirrors ck_lmsr_current_rating_range
                    on lmsr_market_state.current_rating and catches data-quality violations at
                    the DB layer.
    - Write paths : market_init / trade: lmsr_rating() hard-clamps to [0.0, 10.0]
                    oracle_update: Layer 2/3 clamp to [3.0, 10.0]; Layer 1 = Sportmonks 1–10 scale
    - Model change: app/models/rating_history.py __table_args__ updated to include the constraint
    - Tests added : test_check_rating_history_above_10_fails, test_check_rating_history_below_0_fails,
                    test_check_rating_history_boundaries_succeed in tests/test_models.py

---

## Security

### Session 13 Fixes (2026-03-26)

| ID | Fix |
|----|-----|
| SEC-01–05 | Real credentials scrubbed from all env files |
| SEC-06 | `DEBUG=false` enforced in `.env` |
| SEC-07 | UUID join endpoint now requires `invite_code` |
| SEC-08 | WebSocket migrated to opaque ticket pattern |
| SEC-09 | Redis-backed rate limiting on all mutation endpoints |
| SEC-10 | Dev Redis bound to `127.0.0.1` with password |
| SEC-11 | `TournamentCreate.name` bounded (min 3, max 100) |
| SEC-12 | Content-Security-Policy header added to Next.js |
| SEC-13 | HSTS header added to Next.js |
| SEC-14 | CORS origin list validated at startup |
| SEC-15 | Dockerfile runs as non-root `appuser` |
| SEC-16 | Global 500 handler — tracebacks never reach client |
| SEC-17 | Leaderboard response replaced `user_id` with `display_name` |
| SEC-18 | Orphaned `JWT_SECRET`/`HS256` vars removed from env files |
| SEC-19 | Python dependencies pinned with `==` in `requirements.txt` |
| SEC-20 | npm picomatch ReDoS CVE resolved (`npm audit fix`) |

### Dependency Audit

Run these commands periodically (and before every production deploy) to check for known CVEs.

```bash
# Python CVE scan
pip install pip-audit
pip-audit -r requirements.txt

# Node CVE scan
cd lineup-nextjs && npm audit --audit-level=high
```

---

## Remaining Work — In Priority Order

| Step | Description | Status |
|------|-------------|--------|
| Fix A + Fix B | minutes_per_game fix (type_id 78→1584) + recent_form as PlayerMatchFormEntry objects | ✅ COMPLETE (Session 7) — Fix A: minutes_per_game now 80 (was 2). Fix B: recent_form now array of PlayerMatchFormEntry objects. |
| Step 5 | volatility_tier Redis cache — single-player endpoint accuracy | ✅ COMPLETE (Session 10, 2026-03-25) |
| Step 9A | Celery Beat schedule for refresh_match_ratings — daily 07:00 UTC, PL only | ✅ COMPLETE (Session 11, 2026-03-25) — task: refresh_match_ratings_task; leagues: MATCH_RATING_LEAGUES = [8]; days_back: 3 |
| Step 9B | match_date column on PlayerMatchRating — real fixture kickoff dates in recent_form | ✅ COMPLETE (Session 11, 2026-03-25) — migration 0006; backfill run (1,062 rows); fallback to recorded_at for legacy rows |
| Step 6 | bio/play_style populate from Sportmonks bio data | 🔲 TODO — blocked on ops/Sportmonks plan |
| Step 7 | Multi-league support — daily refresh_match_ratings for all 5 leagues | 🔲 TODO — add league IDs (82, 301, 384, 564) to MATCH_RATING_LEAGUES in player_import.py. Imports done (Session 12); daily beat task currently PL only. |
| Ranking Fix | Two-pass import for new player rating bootstrap | 🔲 TODO — medium complexity, plan as dedicated task |
| Session 13 | Security Audit + Hardening | ✅ COMPLETE (Session 13, 2026-03-26) — SEC-01–20: credentials scrubbed, rate limiting (Redis-backed), WebSocket opaque ticket, CSP/HSTS headers, non-root Docker, CORS lock-down, global 500 handler, leaderboard display_name, dep pinning, picomatch CVE resolved. See Security § Session 13 Fixes. |
| Session 13 — Dynamic b Floor (Market Stability) | Prevents extreme price movements at beta scale. Automatically scales as users grow. | ✅ COMPLETE (2026-03-29) — `b_floor_for_users()` in lmsr.py; applied in execute_buy/sell/preview_buy; Redis-cached with 10-min TTL; hourly Celery Beat refresh; Redis→DB→N_MIN fallback chain. See "LMSR Dynamic b Floor" section for full details and removal criteria. |
| Security BL-1 | Add `gt=0` validator to buy/sell request schemas (`schemas/trade.py`) | 🟠 HIGH — prevents zero/negative quantity requests reaching LMSR engine |
| Security T-1 | Add membership check to `GET /tournaments/{id}/leaderboard` | 🟡 MEDIUM — currently any authenticated user can view any tournament leaderboard |
| Security T-2 | Add self-join guard to both tournament join endpoints | 🟡 MEDIUM — creator can currently join their own tournament twice via different endpoints |
| Security T-3 | Add per-user tournament creation cap (max 10 active) | 🟡 MEDIUM — prevents tournament spam |
| Security WS-1 | Migrate WebSocket auth to opaque ticket pattern | 🟡 MEDIUM — current Bearer token in query string is exposed in server logs |
| Security DEP-1 | Add `npm audit` + `safety check` to CI/CD pipeline | 🟡 MEDIUM — dependency vulnerability scanning not automated |

**Ranking Fix — detail:**
Current issue: newly imported players have no `PlayerMatchRating` rows at import time, so they receive sort
key `(0.0, 0)` and are buried below players with any existing rating — regardless of actual quality.
This is a chicken-and-egg problem: the cap selection runs before ratings exist for the new players.

Workaround in place: run `refresh_match_ratings` immediately after each league import to bootstrap ratings
for new players. The next daily Celery Beat run then has correct data.

Proper fix — implement a two-pass import:
- Pass 1: upsert all squad candidates (no cap)
- Pass 2: run `refresh_match_ratings` for the league
- Pass 3: apply standings-aware cap using now-populated rating data

This is a medium-complexity change. Do not attempt mid-session — plan as a dedicated task.

---

## Project Structure

```
lineup-backend/
├── app/
│   ├── api/          # FastAPI routers (auth, market, trade, portfolio, leaderboard, tournament, websocket)
│   ├── core/         # Pure business logic (lmsr.py, oracle.py, trading.py, portfolio.py, auth.py, calibration.py)
│   ├── models/       # SQLAlchemy ORM models
│   ├── schemas/      # Pydantic request/response schemas
│   ├── services/     # External clients (sportmonks.py, redis_pubsub.py, redis_cache.py)
│   ├── workers/      # Celery tasks (player_import, fixture_sync, oracle_update, tournaments, calibration)
│   ├── config.py     # Pydantic Settings (reads .env)
│   ├── dependencies.py  # FastAPI dependency injection (get_db, get_current_user)
│   └── main.py       # App factory
├── alembic/          # Database migrations
├── scripts/
│   └── run_initial_import.py  # One-shot player import
├── tests/
├── docker-compose.yml
└── README.md
```

---

## Incident Log

### 2026-03-25 — Dust Shares Bug

**Symptom:** After clicking the 100% sell button, fractional dust shares (e.g. 0.3847,
0.2369) remained in positions and could not be sold because the same 100% button
produced the same floored integer on every subsequent click.

**Root cause (frontend only):** `TradePanel.tsx` computed the sell quantity as
`Math.floor(sharesOwned * frac)` for all fraction values including `frac === 1`.
Because `shares_owned` is stored as `NUMERIC(14,6)` (fractional by design — LMSR
budget-based buys return non-integer share counts), flooring the 100% case always
left the fractional remainder stranded.

**Fix:** Changed the 100% branch in `TradePanel.tsx` to send `String(sharesOwned)`
exactly, bypassing Math.floor. Partial sells (25%, 50%) retain Math.floor.

**Files changed:** `lineup-nextjs/components/TradePanel.tsx`

**Migration required:** No. Backend `SellRequest.shares: Decimal` already accepted
fractional quantities. No schema or DB changes needed.

### Fix: InteractiveChart NaN crash + holdings added to overview (2026-03-25)

**Symptom 1 (Bug 1 / Bug 2):** On the MyLineup Overview tab, the portfolio value
chart could throw a React error producing a blank/crashed SVG. Error manifested as
NaN values in SVG attributes (`x`, path `d` strings). Appeared to be player-specific
because the Overview tab was only rendered during certain sell sessions.

**Root cause:** `InteractiveChart.tsx` divides by `points.length - 1` in `toX()`.
When `data` from `usePortfolioHistory` contained exactly **1 snapshot**, `isEmpty`
evaluated to `false` (guard was `data.length === 0`), so the 2-point fallback was
not applied. With a single point, `points.length - 1 = 0` → division by zero → NaN
propagated to every SVG attribute.

**Fix:** Extended the empty-state guard from `data.length === 0` to `data.length <= 1`
in `InteractiveChart.tsx`. Single-snapshot users now see the flat-line fallback + "No
history yet" text. 2+ snapshot users see the real chart unchanged.

**Symptom 3 (Bug 3):** MyLineup Overview tab had no holdings / open positions list.
The Holdings tab had the full table, but the overview showed only aggregate breakdowns
(By Position, By League) with no per-player detail.

**Fix:** Added "Open Positions" compact list to the Overview tab in `mylineup/page.tsx`,
inserted between `InteractiveChart` and the 2-column grid. Shows player name, team,
direction, shares owned, and unrealized P&L. Empty state: "No open positions."

**Files changed:** `lineup-nextjs/components/InteractiveChart.tsx`,
`lineup-nextjs/app/(protected)/mylineup/page.tsx`

**Migration required:** No. All data flows from existing `usePortfolio()` and
`usePortfolioHistory()` hooks — no backend changes.

### 2026-03-25 — Trade Processing Hang Regression

**Symptom:** After recent TradePanel.tsx changes (sell flow, dust shares fix, 25/50/100%
buttons), both buy and sell actions hung indefinitely at "Processing…". Neither a success
state nor an error message was shown. The CORS preflight (OPTIONS) reached the backend but
the POST sometimes never followed.

**Root cause (frontend only):** `handleConfirm` in `TradePanel.tsx` used `mutateAsync`
without a surrounding try-catch. In TanStack Query v5, `mutateAsync` re-throws on error
(unlike `mutate` which swallows rejections internally). When `mutateAsync` rejected — due to
a backend error response OR due to the axios request interceptor failing before dispatch
(e.g. Clerk token refresh failure) — `handleConfirm` propagated the unhandled rejection.
Next.js 14 dev mode catches unhandled async rejections from event handlers and triggers
React's error overlay, freezing the UI. The component's own `isError` state was set by
TanStack Query but hidden behind the overlay, so no error message was visible.

**Fix:** Wrapped the `mutateAsync` calls in `handleConfirm` with try-catch. The catch
block is intentionally empty — TanStack Query populates `mutation.isError` / `mutation.error`
before the rejection propagates, so the existing error paragraph in the JSX renders
automatically. `onClose()` is called only on the success path (inside try), preserving
the correct behavior of keeping the panel open on failure.

**Files changed:** `lineup-nextjs/components/TradePanel.tsx` (handleConfirm, ~8 lines)

**Migration required:** No. Backend contract is correct; no schema or endpoint changes.

---

### 2026-03-25 — Session 10 Bug Fixes: Trade Preview Debounce + MyLineup Value Breakdown

#### Bug 1 — Trade preview not updating while typing (TradePanel.tsx)

**Symptom:** Typing a quantity into the trade input did not trigger the cost
preview. The user had to click outside the field (blur) to see the preview update.

**Root cause:** `handlePreview()` was only called via `onBlur` on the input element.
No `onChange` or timed preview existed — so continuous typing produced no feedback
until focus left the field.

**Fix:** Replaced the `onBlur` trigger with a `useEffect` debounce. The effect
watches `amount`, `mode`, and `direction`. When the user stops typing for 300ms
and the value is a valid positive number in buy mode, `previewMutation.mutate()`
fires automatically. The cleanup (`clearTimeout`) runs on every re-render (new
keystroke) cancelling the pending timer, and on unmount. Switching mode or clearing
the field hits the early-return guard, so no stale preview fires.

**Files changed:** `lineup-nextjs/components/TradePanel.tsx`
— Added `useEffect` to React import
— Removed `handlePreview` function and `onBlur={handlePreview}` from input
— Added debounce `useEffect` (300ms, buy mode only)

**Migration required:** No.

---

#### Bug 2 — MyLineup Portfolio card showing wrong value (mylineup/page.tsx)

**Symptom:** The Portfolio stat card displayed the same number as the Wallet card
for users with no open positions. The Total Assets card was also showing the same
number as Portfolio (both showed `total_value`).

**Root cause:** The backend `PortfolioResponse.total_value` equals
`available_points + total_refund` (i.e., wallet + current market value of all
positions). The Portfolio card was incorrectly rendering `total_value` (the full
total) instead of just the invested portion. For a user with no positions,
`total_refund = 0`, so `total_value = available_points`, making Portfolio = Wallet.

**Correct semantics:**
- Wallet = `available_points` (cash on hand)
- Portfolio = `total_value − available_points` (current market value of open positions)
- Total Assets = `total_value` (wallet + portfolio)

**Fix:** Portfolio card now renders `total_value - available_points`. Total Assets
card already rendered `total_value` and was semantically correct; left unchanged,
added inline comment for clarity.

**Files changed:** `lineup-nextjs/app/(protected)/mylineup/page.tsx`
— Portfolio value expression updated (~line 133)
— Inline comments added to both Portfolio and Total Assets expressions

**Migration required:** No. Backend contract unchanged; no new fields needed.

---

### 2026-03-26 — New Players Ranked Last After League Import

**Symptom:** After a La Liga import, top-squad stars (Lamine Yamal, Thibaut Courtois, etc.) were
absent from the top 4 slots for their respective clubs. Lower-quality players with existing
`PlayerMatchRating` history displaced them.

**Root cause:** `_collect_league_entries()` sorts players within each team by
`(avg_match_rating, minutes_played)` descending. At import time, newly added players have zero
`PlayerMatchRating` rows — and `sportmonks_rating` is also `None` for them — so their sort key
evaluates to `(0.0, 0)`. Any existing player with even a single match rating outranks them,
regardless of quality. The activity filter (`_is_active()`) passes new players correctly via
`minutes_played`, but the sort then buries them.

**Root cause confirmed by:** reading `_collect_league_entries()` sort key logic (lines 145–157 of
`app/workers/player_import.py`).

**Workaround:** Run `refresh_match_ratings` immediately after any league import to bootstrap
`PlayerMatchRating` rows for newly added players. See Operational Note 18.

**Proper fix (not yet implemented):** Two-pass import — upsert all candidates first, run
`refresh_match_ratings`, then apply the standings-aware cap. See Remaining Work — Ranking Fix.

---

## Rating History Sources

The `rating_history` table stores two **semantically distinct** data series in the same `rating` column, distinguished by the `source` field:

| `source` value | Written by | What `rating` represents |
|---------------|-----------|--------------------------|
| `"trade"` | `core/trading.py` — on every user trade | LMSR market price (`lmsr_rating(q_up, q_down, b_eff)`) on 0.0–10.0 scale |
| `"market_init"` | Market initialisation path | LMSR market price at market creation (same scale as `"trade"`) |
| `"oracle_update"` | `workers/oracle_update.py` — every 10 min | Oracle match-performance score derived from Sportmonks stats (same 0.0–10.0 scale, but **not** an LMSR price) |

### Query rules

**Any query that computes a price-derived metric** (e.g. `change_24h` in `_get_change_24h_map()`) **must filter to `source IN ("trade", "market_init")`**. Including `oracle_update` rows will compare a match-performance score against an LMSR market price, producing spurious percentage changes of 30–70% with no user trades.

**The chart endpoint** (`GET /api/market/players/{id}/chart`) intentionally returns all sources without filtering. The frontend renders `oracle_update` rows as dashed line segments at 55% opacity, labelled "Match Rating" in tooltips, distinct from the solid "Market Price" line. This makes the oracle/trade distinction visible to users.

### Do not touch

- Do **not** filter `oracle_update` rows out of the chart endpoint — they are returned deliberately.
- Do **not** add `oracle_update` rows to price-derived metric calculations.
- The `oracle_rating` field on `lmsr_market_state` is display-only (INV-09); oracle writes never modify `current_rating`.

**Migration required:** No.

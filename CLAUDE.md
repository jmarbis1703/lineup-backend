# LineUp Backend — Project Onboarding

## Overview
LineUp Markets is a prediction-market platform for football (soccer) player performance. Users trade "shares" in players using a points currency, with prices determined by an LMSR (Logarithmic Market Scoring Rule) automated market maker.

## Tech Stack
| Layer | Technology |
|-------|-----------|
| Framework | FastAPI (async) |
| Database | PostgreSQL via SQLAlchemy 2 async + asyncpg |
| Migrations | Alembic |
| Auth | Clerk (RS256 JWTs verified via JWKS) |
| Webhooks | Svix (Clerk event sync) |
| Background | Celery + Redis |
| HTTP client | httpx (async) |
| Testing | pytest-asyncio |

## Running Locally

### Prerequisites
- Python 3.11+
- Docker & Docker Compose

### Start infrastructure
```bash
docker compose up -d
```

### Install dependencies
```bash
pip install -r requirements.txt
```

### Environment variables
Copy `.env.example` to `.env` and fill in:
```
DATABASE_URL=postgresql+asyncpg://lineup:lineup_secret@localhost:5432/lineup
REDIS_URL=redis://localhost:6379/0
CLERK_JWKS_URL=https://<your-clerk-domain>/.well-known/jwks.json
CLERK_WEBHOOK_SECRET=whsec_xxxxx
CLERK_AUDIENCE=                   # optional
SPORTMONKS_API_TOKEN=             # optional, for data sync
```

### Run migrations
```bash
alembic upgrade head
```

### Start the API
```bash
uvicorn app.main:app --reload
```

API docs: http://localhost:8000/docs

## Clerk Setup
1. Create a Clerk application at https://clerk.com
2. Copy the **JWKS URL** from: Dashboard → API Keys → Advanced → JWKS URL
3. Create a webhook endpoint pointing to `https://your-domain/api/webhooks/clerk`
4. Subscribe to events: `user.created`, `user.updated`, `user.deleted`
5. Copy the **Signing Secret** (starts with `whsec_`) as `CLERK_WEBHOOK_SECRET`

## Running Tests
```bash
pytest -v                        # all tests (PostgreSQL required for DB tests)
pytest tests/test_auth.py -v     # auth tests only (no PostgreSQL required)
```

## Key Directories
```
app/
  api/           # Route handlers
  core/          # auth.py — JWKS verification
  models/        # SQLAlchemy ORM models
  schemas/       # Pydantic request/response schemas
  config.py      # Settings (pydantic-settings, reads .env)
  dependencies.py # FastAPI dependencies (get_db, get_current_user)
  main.py        # App factory + router registration
alembic/versions/ # DB migrations
tests/           # pytest test suite
```

## Architecture Notes
- All protected endpoints use `Depends(get_current_user)` from `app/dependencies.py`
- `get_current_user` verifies the Bearer JWT via Clerk's JWKS, then looks up the user row by `clerk_id`
- Users are created/updated/deleted by Clerk webhooks hitting `POST /api/webhooks/clerk`
- See `AUTH.md` for the full auth architecture

---

## Completed Work (do not revisit)
- Session 17 — DEP-1: Baseline CI for lineup-backend — COMPLETE
- Session 17 — DEP-1: Baseline CI for lineup-nextjs — COMPLETE

---

## Remaining Work — In Priority Order
<!-- DEP-1 (baseline CI) removed — completed Session 17 -->

---

## Key Files Reference
```
lineup-backend/app/main.py
lineup-backend/app/dependencies.py
lineup-backend/app/config.py
lineup-backend/alembic/versions/
lineup-backend/tests/conftest.py
lineup-backend/.github/workflows/ci.yml
lineup-nextjs/.github/workflows/ci.yml
```

---

## Important Operational Notes
- CI runs on every push and PR to main. Backend: ruff (app/ only),
  mypy (with per-module ignores for pre-existing tech debt in
  app/config.py, app/schemas/tournament.py, app/workers/player_import.py,
  app/workers/snapshot.py, app/api/portfolio.py, app/main.py), pytest
  (SlowAPI limiter initialised in conftest.py test client).
  Frontend: tsc --noEmit, eslint direct invocation, next build.
  NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY must be set as a GitHub Actions
  repository secret in lineup-nextjs repo settings.

---

## Known Active Bugs (as of Session 17 end)
None confirmed.

---

## Incident Log Summary (do not revisit)
- Incident: next lint misinterprets positional args in Next.js 16 —
    "lint" parsed as directory path. Fixed by calling eslint directly
    with eslint-config-next via .eslintrc.json (Session 17)
- Incident: mypy --exclude with file paths unreliable in CI —
    fixed by creating mypy.ini with per-module ignore_errors = True
    (Session 17)
- Incident: effective_b() signature changed from (q_up, q_down, alpha,
    b_min) to (b_min, alpha, q_up, q_down) but tests never updated —
    fixed by correcting call sites in test_lmsr.py and tournaments.py
    (Session 17)
- Incident: SlowAPI middleware reads request.state.view_rate_limit which
    is not initialised in TestClient — fixed by adding limiter state
    setup in conftest.py (Session 17)
- Incident: test_all_13_tables_created hardcoded 13 tables — schema
    now has 15 (watchlists + portfolio_snapshots added since test was
    written) — test updated to reflect current schema (Session 17)
- Incident: GitHub PAT requires both repo and workflow scopes to push
    .github/workflows/ files — plain repo scope is rejected (Session 17)

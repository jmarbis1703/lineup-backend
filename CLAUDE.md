## Graphify — Knowledge Graph (READ THIS FIRST)

Before using Glob, Grep, or Read on more than 2 files: check if 
graphify-out/graph.json exists. If it does, read 
graphify-out/GRAPH_REPORT.md first. Only read raw files for 
specific nodes the graph points to. For "how does X work" or 
"where is Y" questions, check graphify-out/wiki/index.md first.

After completing any task where 5+ files were modified: print 
this reminder at the end of your response:
"⚠️ graphify: run `.venv/Scripts/graphify update .` before 
next session — [N] files changed."

Do NOT run graphify update automatically mid-task.

---

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
- Session 19 — Skills Framework Installation — COMPLETE
  - emilkowalski/skill installed at ~/.claude/skills/
  - pbakaus/impeccable installed at ~/.claude/skills/
  - Leonxlnx/taste-skill installed at ~/.claude/skills/
  - obra/superpowers installed at ~/.claude/skills/
  - AGENT_RULES.md created at ~/.claude/skills/AGENT_RULES.md
  - code-review and security-guidance plugins already present
  - claude-mem (404) replaced by built-in memory system
- Session 19 — startup_landing_page_blueprint.md — COMPLETE
  - Research document created covering waitlist page strategy,
    investor appeal, conversion psychology, case studies
    (Robinhood, Superhuman, Notion, Monzo)
  - Saved to project files for Claude Code reference
  - Includes full wireframe and implementation notes
- Session 19 — Migration 0008 (waitlist_signups) — COMPLETE
  - New table: waitlist_signups
  - Columns: id, email, referral_code, referred_by,
    position, referral_count, created_at
  - Indexes on email and referral_code
  - Applied to production: alembic current shows 0008 (head)
- Session 19 — Waitlist API Endpoints — COMPLETE
  - POST /api/waitlist/join (no auth, 5/min rate limit)
    → validates email, generates UUID referral_code,
      assigns position, increments referrer +10 spots flat
    → returns WaitlistJoinResponse
  - GET /api/waitlist/count (no auth, public)
    → returns WaitlistCountResponse {count: int}
  - Both registered in main.py before auth routes
  - CORS confirmed working from lineupmarkets.com origin
- Session 19 — Waitlist Landing Page — COMPLETE
  - Route: lineup-nextjs/app/(public)/waitlist/page.tsx
  - Confirmed page: app/(public)/waitlist/confirmed/page.tsx
  - Public layout: app/(public)/layout.tsx (no Clerk)
  - Root page.tsx replaced with redirect('/waitlist')
  - Original preserved as app/page.original.tsx
  - Brand tokens added to tailwind.config.ts
  - Logo files added to public/
  - Favicon.svg created
  - Live at: https://lineupmarkets.com/waitlist
  - API_BASE hardcoded to https://api.lineupmarkets.com
    (NEXT_PUBLIC_* vars not available at Docker build time
    for this route — hardcoded is correct pattern)
- Session 19 — Beta Coexistence Confirmed — COMPLETE
  - Clerk Restricted Mode remains ON
  - app/(protected)/ untouched and fully operational
  - Waitlist and beta are completely separate surfaces
  - No links between waitlist and beta in either direction

---

## Remaining Work — In Priority Order
<!-- DEP-1 (baseline CI) removed — completed Session 17 -->

⚪ Session 20 — Waitlist Visual Redesign
  - 3D floating player card in hero (Lamine Yamal)
  - Real PlayerCard visual pattern recreated for waitlist
  - How It Works section: real app UI previews per step
  - Logo size increase (120px → 160px)
  - 3D depth system across all sections
  - Dot grid background on off-white sections
  - Prompt is fully written and ready to paste to Claude Code

⚪ Ranking Fix — Two-pass import (standings-aware)
⚪ Fixture Sync — Backfill finished matches
⚪ BUG-06 — recent_matches not in lib/types.ts
⚪ Mobile hover — touch devices
⚪ Oracle line tooltip rename
⚪ Step 6 — Bio/Play Style sync

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

- Waitlist API endpoints are public (no Clerk JWT required):
  POST /api/waitlist/join and GET /api/waitlist/count.
  Both registered before auth routes in main.py.

- Waitlist frontend API_BASE is hardcoded to
  'https://api.lineupmarkets.com' in
  app/(public)/waitlist/page.tsx — do NOT change to an
  env var without also ensuring the var is available as
  a Docker build ARG in docker-compose.prod.yml.

- Logo files in lineup-nextjs/public/:
  - logo-orange.png: navbar and footer (light sections)
  - logo-orange-on-white.png: dark Section G only
  Note: logo-orange-on-white-bg.png does NOT exist —
  the correct filename is logo-orange-on-white.png

- Waitlist referral position formula:
  referrer.position = max(1, referrer.position - 10)
  Flat 10 spots per successful referral.
  Tier display: 1 ref = +10, 3 refs = +40, 5 refs = priority.

- Root page.tsx redirect chain:
  / → redirect('/waitlist') → app/(public)/waitlist/page.tsx
  Original landing page preserved at app/page.original.tsx

- Migration state:
  Current head: 0008
  Chain: 0001 → 0002 → 0003 → 0004 → 0005 → 0006 → 0007 → 0008

- Waitlist signups in production DB:
  Check count via: curl https://api.lineupmarkets.com/api/waitlist/count
  Or via DB: SELECT COUNT(*) FROM waitlist_signups;

- Skills framework location: ~/.claude/skills/
  AGENT_RULES.md governs automatic skill invocation.
  All four design skills must be read before any frontend task.
  frontend-design skill is highest priority, applied first.

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
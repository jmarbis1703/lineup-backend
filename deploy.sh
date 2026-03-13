#!/usr/bin/env bash
# deploy.sh — Production deployment script for LineUp backend
#
# Usage:
#   ./deploy.sh            # Normal deploy (git pull, build, migrate, restart)
#   ./deploy.sh --first    # First-time deploy (also runs run_initial_import.py)
#
# Requirements:
#   - .env.prod must exist (copy from .env.prod.example and fill in values)
#   - Docker and Docker Compose v2 must be installed
#   - Run as a user with Docker socket access

set -euo pipefail

# ── Config ─────────────────────────────────────────────────────────────────────

COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.prod"
IMAGE_NAME="lineup-api"
FIRST_DEPLOY=false
IMPORT_SENTINEL=".initial_import_done"

# ── Argument parsing ────────────────────────────────────────────────────────────

for arg in "$@"; do
  case $arg in
    --first) FIRST_DEPLOY=true ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

# ── Helpers ─────────────────────────────────────────────────────────────────────

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }
die() { echo "[ERROR] $*" >&2; exit 1; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is not installed"
}

# ── Preflight ───────────────────────────────────────────────────────────────────

require_cmd docker
require_cmd git

[[ -f .env.prod ]] || die ".env.prod not found. Copy .env.prod.example and fill in your values."
[[ -f Caddyfile ]] || die "Caddyfile not found."

# Refuse to deploy if .env.prod still contains placeholder passwords
if grep -qE 'CHANGE_ME|your_sportmonks_api_token_here' .env.prod; then
  die ".env.prod contains placeholder values — fill in real secrets before deploying."
fi

# ── Step 1: Pull latest code ────────────────────────────────────────────────────

log "Pulling latest code..."
git pull --ff-only

# ── Step 2: Determine image tag ─────────────────────────────────────────────────

IMAGE_TAG=$(git rev-parse --short HEAD)
export IMAGE_TAG
log "Building image tag: ${IMAGE_TAG}"

# ── Step 3: Build image ─────────────────────────────────────────────────────────

log "Building Docker image..."
docker build --pull -t "${IMAGE_NAME}:${IMAGE_TAG}" -t "${IMAGE_NAME}:latest" .

# ── Step 4: Start infrastructure (DB + Redis) ───────────────────────────────────

log "Starting database and Redis..."
$COMPOSE up -d db redis

log "Waiting for database to be healthy..."
until $COMPOSE exec -T db pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" >/dev/null 2>&1; do
  sleep 2
done
log "Database is ready."

# ── Step 5: Run migrations ──────────────────────────────────────────────────────

log "Running database migrations..."
$COMPOSE run --rm \
  -e DATABASE_URL \
  api alembic upgrade head

log "Migrations complete."

# ── Step 6: Initial import (first deploy only) ──────────────────────────────────

if [[ "$FIRST_DEPLOY" == true ]] || [[ ! -f "$IMPORT_SENTINEL" ]]; then
  log "Running initial player import (this may take a minute)..."
  $COMPOSE run --rm api python scripts/run_initial_import.py
  touch "$IMPORT_SENTINEL"
  log "Initial import complete."
else
  log "Skipping initial import (${IMPORT_SENTINEL} exists). Pass --first to force."
fi

# ── Step 7: Restart services with new image ─────────────────────────────────────

log "Deploying new image and restarting services..."
$COMPOSE up -d --no-build --remove-orphans

# ── Step 8: Verify API health ────────────────────────────────────────────────────

log "Waiting for API to become healthy..."
RETRIES=20
until $COMPOSE exec -T api curl -fsS http://localhost:8000/health >/dev/null 2>&1; do
  RETRIES=$((RETRIES - 1))
  [[ $RETRIES -le 0 ]] && die "API did not become healthy after restart. Check logs: docker compose -f docker-compose.prod.yml logs api"
  sleep 3
done
log "API is healthy."

# ── Step 9: Prune old images ─────────────────────────────────────────────────────

log "Pruning dangling images..."
docker image prune -f --filter "label=com.docker.compose.project=lineup-backend" 2>/dev/null || true

# ── Done ─────────────────────────────────────────────────────────────────────────

log "Deploy complete. Image: ${IMAGE_NAME}:${IMAGE_TAG}"
log "To tail logs: docker compose -f docker-compose.prod.yml logs -f"

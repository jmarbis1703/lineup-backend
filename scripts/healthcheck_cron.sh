#!/usr/bin/env bash
# healthcheck_cron.sh — Checks all LineUp services and alerts on failure.
#
# Designed to run every 5 minutes via cron:
#   */5 * * * * /path/to/lineup-backend/scripts/healthcheck_cron.sh >> /var/log/lineup-health.log 2>&1
#
# Alerts via:
#   - ALERT_WEBHOOK: POST to a Slack/Discord/PagerDuty webhook URL (optional)
#   - ALERT_EMAIL: sends mail via local sendmail (optional)
#
# Configure by exporting vars or editing the defaults below.
# On healthy runs this script is silent (cron-friendly).

set -euo pipefail

# ── Config ─────────────────────────────────────────────────────────────────────

COMPOSE_FILE="${COMPOSE_FILE:-/opt/lineup-backend/docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-/opt/lineup-backend/.env.prod}"
API_INTERNAL_URL="${API_INTERNAL_URL:-http://localhost:8000/health}"
ALERT_WEBHOOK="${ALERT_WEBHOOK:-}"   # Slack/Discord webhook URL, or empty to skip
ALERT_EMAIL="${ALERT_EMAIL:-}"       # Email address, or empty to skip
HOSTNAME="${HOSTNAME:-$(hostname)}"
LOGFILE="${LOGFILE:-/var/log/lineup-health.log}"

# ── State ──────────────────────────────────────────────────────────────────────

FAILURES=()
COMPOSE="docker compose -f ${COMPOSE_FILE} --env-file ${ENV_FILE}"

# ── Helpers ─────────────────────────────────────────────────────────────────────

log()  { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }
fail() { FAILURES+=("$*"); log "FAIL: $*"; }

send_alert() {
  local message="$1"
  local full_msg="[${HOSTNAME}] LineUp health check FAILED:\n${message}"

  if [[ -n "$ALERT_WEBHOOK" ]]; then
    curl -fsS -X POST "${ALERT_WEBHOOK}" \
      -H "Content-Type: application/json" \
      -d "{\"text\": \"${full_msg}\"}" >/dev/null 2>&1 || true
  fi

  if [[ -n "$ALERT_EMAIL" ]]; then
    echo -e "Subject: [LineUp] Health check failed on ${HOSTNAME}\n\n${full_msg}" \
      | sendmail "$ALERT_EMAIL" 2>/dev/null || true
  fi
}

# ── Checks ──────────────────────────────────────────────────────────────────────

check_container() {
  local name="$1"
  local state
  state=$(docker inspect --format '{{.State.Status}}' "${name}" 2>/dev/null || echo "missing")
  if [[ "$state" != "running" ]]; then
    fail "Container '${name}' is ${state}"
  fi
}

check_api_http() {
  local http_code
  http_code=$(curl -fsS -o /dev/null -w "%{http_code}" "${API_INTERNAL_URL}" 2>/dev/null || echo "000")
  if [[ "$http_code" != "200" ]]; then
    fail "API /health returned HTTP ${http_code} (expected 200)"
  fi
}

check_db() {
  # Load POSTGRES_USER and POSTGRES_DB from env file to build the pg_isready check
  local pg_user pg_db
  pg_user=$(grep -m1 '^POSTGRES_USER=' "${ENV_FILE}" | cut -d= -f2)
  pg_db=$(grep -m1 '^POSTGRES_DB=' "${ENV_FILE}" | cut -d= -f2)

  if ! $COMPOSE exec -T db pg_isready -U "${pg_user}" -d "${pg_db}" >/dev/null 2>&1; then
    fail "PostgreSQL is not accepting connections"
  fi
}

check_redis() {
  local redis_password
  redis_password=$(grep -m1 '^REDIS_PASSWORD=' "${ENV_FILE}" | cut -d= -f2)
  if ! $COMPOSE exec -T redis redis-cli -a "${redis_password}" ping 2>/dev/null | grep -q PONG; then
    fail "Redis did not respond to PING"
  fi
}

check_celery_worker() {
  # Verify at least one celery_worker container is running and responsive
  local container
  container=$(docker ps --filter "name=lineup.*celery_worker" --format "{{.Names}}" | head -1)
  if [[ -z "$container" ]]; then
    fail "No celery_worker container is running"
    return
  fi
  if ! docker exec "${container}" celery -A app.workers.celery_app:celery_app inspect ping --timeout=5 >/dev/null 2>&1; then
    fail "celery_worker is not responding to inspect ping"
  fi
}

check_disk() {
  local usage
  usage=$(df / --output=pcent | tail -1 | tr -d ' %')
  if [[ "$usage" -ge 85 ]]; then
    fail "Disk usage is at ${usage}% (threshold: 85%)"
  fi
}

check_docker_health() {
  # Check Docker-native health status for services that have HEALTHCHECK
  for service in db redis api; do
    local container status
    container=$(docker ps --filter "name=lineup.*${service}" --format "{{.Names}}" | head -1)
    if [[ -z "$container" ]]; then
      fail "Service '${service}': no running container found"
      continue
    fi
    status=$(docker inspect --format '{{.State.Health.Status}}' "${container}" 2>/dev/null || echo "none")
    if [[ "$status" == "unhealthy" ]]; then
      fail "Service '${service}' Docker healthcheck is unhealthy"
    fi
  done
}

# ── Run all checks ───────────────────────────────────────────────────────────────

check_docker_health
check_api_http
check_db
check_redis
check_celery_worker
check_disk

# ── Report ───────────────────────────────────────────────────────────────────────

if [[ ${#FAILURES[@]} -gt 0 ]]; then
  FAILURE_MSG=$(printf '%s\n' "${FAILURES[@]}")
  log "Health check FAILED (${#FAILURES[@]} issue(s)):"
  printf '%s\n' "${FAILURES[@]}"
  send_alert "$FAILURE_MSG"
  exit 1
fi

# All healthy — silent exit (no cron noise)
exit 0

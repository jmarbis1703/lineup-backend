#!/usr/bin/env python3
"""
LineUp production system health checker.

Usage:
    python scripts/check_system.py --base-url https://api.yourdomain.com
    python scripts/check_system.py --base-url http://localhost:8000
    python scripts/check_system.py --base-url https://api.yourdomain.com \
        --sportmonks-token <token> --timeout 15
"""

import argparse
import sys
from typing import Optional

import httpx

# ANSI colour codes
GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"
BOLD = "\033[1m"


def _pass(label: str) -> bool:
    print(f"  {GREEN}PASS{RESET}  {label}")
    return True


def _fail(label: str, reason: str) -> bool:
    print(f"  {RED}FAIL{RESET}  {label}  —  {reason}")
    return False


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_api_reachable(client: httpx.Client, base_url: str) -> bool:
    label = "API reachable (GET /health → 200 + {\"status\": \"ok\"})"
    try:
        r = client.get(f"{base_url}/health")
        if r.status_code != 200:
            return _fail(label, f"HTTP {r.status_code}")
        body = r.json()
        if body.get("status") != "ok":
            return _fail(label, f"unexpected body: {body}")
        return _pass(label)
    except Exception as exc:
        return _fail(label, str(exc))


def check_auth_endpoint(client: httpx.Client, base_url: str) -> bool:
    label = "Auth endpoint live (POST /api/auth/register → 422 validation error)"
    try:
        r = client.post(f"{base_url}/api/auth/register", json={"bad": "payload"})
        # 422 = endpoint is up and validating; any 5xx = bad
        if r.status_code == 422:
            return _pass(label)
        if r.status_code >= 500:
            return _fail(label, f"HTTP {r.status_code} (server error)")
        # 400 also acceptable — endpoint is alive
        if r.status_code in (400, 401, 403):
            return _pass(label)
        return _fail(label, f"unexpected HTTP {r.status_code}")
    except Exception as exc:
        return _fail(label, str(exc))


def check_market_endpoint(client: httpx.Client, base_url: str) -> bool:
    label = "Market endpoint live (GET /api/market/players → 200 + JSON list)"
    try:
        r = client.get(f"{base_url}/api/market/players")
        if r.status_code != 200:
            return _fail(label, f"HTTP {r.status_code}")
        body = r.json()
        if not isinstance(body, list):
            return _fail(label, f"expected JSON list, got {type(body).__name__}")
        return _pass(label)
    except Exception as exc:
        return _fail(label, str(exc))


def check_lmsr_fields(client: httpx.Client, base_url: str) -> bool:
    label = "LS-LMSR fields present (current_rating, q_up, q_down in player response)"
    try:
        r = client.get(f"{base_url}/api/market/players")
        if r.status_code != 200:
            return _fail(label, f"HTTP {r.status_code} on /api/market/players")
        players = r.json()
        if not players:
            # No players yet — can't verify fields, but not a deployment failure
            return _pass(f"{label}  [skipped — 0 players in market]")
        first = players[0]
        missing = [f for f in ("current_rating", "q_up", "q_down") if f not in first]
        if missing:
            return _fail(label, f"missing fields: {missing}")
        return _pass(label)
    except Exception as exc:
        return _fail(label, str(exc))


def check_sportmonks(token: str, timeout: float) -> bool:
    label = "Sportmonks API reachable (GET /v3/football/leagues → 200)"
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(
                "https://api.sportmonks.com/v3/football/leagues",
                headers={"Authorization": token},
            )
        if r.status_code == 200:
            return _pass(label)
        return _fail(label, f"HTTP {r.status_code}")
    except Exception as exc:
        return _fail(label, str(exc))


def check_db_connectivity(api_ok: bool, market_ok: bool) -> bool:
    label = (
        "DB connectivity (inferred: /health=ok AND /api/market/players=200 "
        "⟹ DB is connected via depends_on: db healthy)"
    )
    if api_ok and market_ok:
        return _pass(label)
    return _fail(label, "upstream checks failed — DB may be down")


def check_redis_celery(market_ok: bool) -> bool:
    label = (
        "Redis/Celery live (inferred: /api/market/players returns 200 "
        "⟹ no 500s from worker startup)"
    )
    if market_ok:
        return _pass(label)
    return _fail(label, "upstream market check failed — Redis/Celery may be down")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="LineUp production system health check")
    parser.add_argument(
        "--base-url",
        required=True,
        help="Base URL of the LineUp API, e.g. https://api.yourdomain.com",
    )
    parser.add_argument(
        "--sportmonks-token",
        default=None,
        help="Sportmonks API token (check #5 skipped if omitted)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP timeout in seconds (default: 10)",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    timeout: float = args.timeout
    sportmonks_token: Optional[str] = args.sportmonks_token

    print(f"\n{BOLD}LineUp System Health Check{RESET}")
    print(f"  Target : {base_url}")
    print(f"  Timeout: {timeout}s\n")

    results: list[bool] = []

    with httpx.Client(timeout=timeout) as client:
        # Check 1
        api_ok = check_api_reachable(client, base_url)
        results.append(api_ok)

        # Check 2
        results.append(check_auth_endpoint(client, base_url))

        # Check 3
        market_ok = check_market_endpoint(client, base_url)
        results.append(market_ok)

        # Check 4
        results.append(check_lmsr_fields(client, base_url))

    # Check 5 — Sportmonks (separate client, optional)
    if sportmonks_token:
        results.append(check_sportmonks(sportmonks_token, timeout))
    else:
        print(f"  {'--':4}  Sportmonks reachable  [skipped — no --sportmonks-token provided]")
        results.append(True)  # don't penalise when token not supplied

    # Checks 6 & 7 — inferred
    results.append(check_db_connectivity(api_ok, market_ok))
    results.append(check_redis_celery(market_ok))

    passed = sum(results)
    total = len(results)

    print()
    if passed == total:
        print(f"{BOLD}{GREEN}{passed}/{total} checks PASSED{RESET}")
        return 0
    else:
        failed = total - passed
        print(f"{BOLD}{RED}{failed}/{total} checks FAILED{RESET}  ({passed} passed)")
        return 1


if __name__ == "__main__":
    sys.exit(main())

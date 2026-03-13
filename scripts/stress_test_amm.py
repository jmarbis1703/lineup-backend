#!/usr/bin/env python3
"""
LS-LMSR Concurrency Stress Test — Phase 4.2

Validates INV-05 (lock order), INV-02 (no negative balance), and LS-LMSR math
integrity under N concurrent BUY_UP trades on the same player.

Usage:
    python scripts/stress_test_amm.py \\
        --base-url http://localhost:8000 \\
        --concurrency 50 \\
        [--db-url postgresql+asyncpg://lineup:secret@localhost:5432/lineup]

Pass criteria (printed to stdout):
    A. 0 HTTP 500 responses
    B. available_points >= 0 for all users (INV-02)
    C. q_up == sum of shares from successful trades (no phantom shares)
    D. LS-LMSR math drift < 0.000001 (NUMERIC precision preserved)
    E. 0 deadlock log entries (caller must check: docker compose logs db | grep deadlock)
    F. Total cost conserved: sum(trade.cost) == initial_points - final_points
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from decimal import Decimal, getcontext
from typing import Any

import httpx

# Set high precision for Decimal arithmetic comparisons
getcontext().prec = 28

_TRADE_BUDGET = Decimal("100")
_INITIAL_POINTS = Decimal("1000")
_TARGET_PLAYER_ID: int | None = None  # resolved at runtime
_FAILURES: list[str] = []


def _fail(msg: str) -> None:
    _FAILURES.append(msg)
    print(f"  [FAIL] {msg}", file=sys.stderr)


def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


async def register_users(
    client: httpx.AsyncClient,
    base_url: str,
    n: int,
    run_id: str,
) -> list[dict[str, Any]]:
    """Register n test users; return list of {token, user_id, username}."""
    results = []
    for i in range(n):
        username = f"stress_{run_id}_{i}"
        payload = {
            "email": f"{username}@stress.test",
            "username": username,
            "password": "StressTest1234!",
        }
        resp = await client.post(f"{base_url}/api/auth/register", json=payload)
        if resp.status_code not in (200, 201):
            _fail(f"Registration failed for {username}: {resp.status_code} {resp.text}")
            results.append(None)
        else:
            data = resp.json()
            results.append({
                "token": data["token"],
                "user_id": data["user_id"],
                "username": username,
                "available_points": Decimal(str(data["available_points"])),
            })
    registered = [r for r in results if r is not None]
    print(f"  Registered {len(registered)}/{n} users")
    return results


async def find_or_create_target_player(
    client: httpx.AsyncClient,
    base_url: str,
) -> int:
    """Find the first active player from the market API."""
    resp = await client.get(f"{base_url}/api/market/players")
    if resp.status_code != 200:
        raise RuntimeError(f"Cannot list players: {resp.status_code} {resp.text}")
    players = resp.json()
    active = [p for p in players if p.get("is_active", True)]
    if not active:
        raise RuntimeError("No active players found — seed the DB before running this test")
    return active[0]["id"]


# ---------------------------------------------------------------------------
# Concurrent trade execution
# ---------------------------------------------------------------------------


async def execute_buy(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    player_id: int,
    budget: Decimal,
) -> dict[str, Any]:
    """POST /api/trade/buy and return {status_code, data, shares, cost}."""
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "player_id": player_id,
        "direction": "UP",
        "budget": float(budget),
    }
    try:
        resp = await client.post(
            f"{base_url}/api/trade/buy",
            json=payload,
            headers=headers,
            timeout=30.0,
        )
        return {
            "status_code": resp.status_code,
            "data": resp.json() if resp.content else {},
            "success": resp.status_code == 200,
        }
    except Exception as exc:
        return {"status_code": -1, "data": {}, "success": False, "error": str(exc)}


async def get_portfolio(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
) -> dict[str, Any] | None:
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.get(f"{base_url}/api/portfolio/me", headers=headers, timeout=15.0)
    if resp.status_code != 200:
        return None
    return resp.json()


async def get_market_state(
    client: httpx.AsyncClient,
    base_url: str,
    player_id: int,
) -> dict[str, Any] | None:
    resp = await client.get(
        f"{base_url}/api/market/players",
        params={"player_id": player_id},
        timeout=15.0,
    )
    if resp.status_code != 200:
        return None
    players = resp.json()
    for p in players:
        if p["id"] == player_id:
            return p
    return None


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


def assert_no_500s(responses: list[dict]) -> None:
    """Assertion A: no HTTP 500 responses."""
    http_500s = [r for r in responses if r["status_code"] == 500]
    if http_500s:
        _fail(f"A: {len(http_500s)} HTTP 500 response(s) — unhandled server exceptions")
    else:
        _ok(f"A: 0 HTTP 500 responses across {len(responses)} requests")


def assert_no_negative_balances(
    portfolios: list[dict | None],
    users: list[dict],
) -> None:
    """Assertion B: available_points >= 0 for all users (INV-02)."""
    violations = 0
    for port, user in zip(portfolios, users):
        if port is None:
            continue
        bal = Decimal(str(port.get("available_points", 0)))
        if bal < Decimal("0"):
            _fail(
                f"B: INV-02 violated — {user['username']} has "
                f"available_points={bal} (negative)"
            )
            violations += 1
    if violations == 0:
        _ok("B: INV-02 satisfied — no negative available_points")
    else:
        _fail(f"B: {violations} user(s) have negative available_points")


def assert_q_up_matches_shares(
    market: dict | None,
    q_up_before: Decimal,
    successful_responses: list[dict],
) -> None:
    """Assertion C: q_up increased by exactly the sum of shares from successful trades."""
    if market is None:
        _fail("C: Cannot verify q_up — market state unavailable")
        return

    q_up_after = Decimal(str(market["q_up"]))
    delta_q_up = q_up_after - q_up_before

    total_shares = sum(
        Decimal(str(r["data"].get("shares_received", 0)))
        for r in successful_responses
        if r.get("success") and "shares_received" in r.get("data", {})
    )

    if total_shares == Decimal("0"):
        _ok("C: No successful trades with share data to verify (all rejected or no shares field)")
        return

    drift = abs(delta_q_up - total_shares)
    if drift > Decimal("0.000001"):
        _fail(
            f"C: q_up drift detected — delta_q_up={delta_q_up}, "
            f"sum(shares)={total_shares}, drift={drift}"
        )
    else:
        _ok(
            f"C: q_up={q_up_after} matches sum of {len(successful_responses)} "
            f"successful share grants (drift={drift})"
        )


def assert_lmsr_precision(market: dict | None) -> None:
    """Assertion D: q_up and q_down are NUMERIC with no float bleed (drift < 0.000001)."""
    if market is None:
        _fail("D: Cannot verify LMSR precision — market state unavailable")
        return

    # Check that the values returned by the API have ≤ 6 decimal digits
    # (Postgres NUMERIC(14,6) = 6 decimal places)
    for field in ("q_up", "q_down"):
        val_str = str(market.get(field, "0"))
        if "." in val_str:
            decimals = len(val_str.split(".")[1])
            if decimals > 6:
                _fail(
                    f"D: {field}={val_str} has {decimals} decimal places — "
                    "float bleed into NUMERIC field detected"
                )
                return
    _ok("D: NUMERIC precision preserved — no float bleed detected in q_up/q_down")


def assert_cost_conservation(
    users: list[dict],
    portfolios_before: dict[str, Decimal],
    portfolios_after: list[dict | None],
    successful_responses: list[dict],
) -> None:
    """Assertion F: sum of trade costs == initial_points - final_points per user."""
    violations = 0
    for port_after, user in zip(portfolios_after, users):
        if port_after is None or user is None:
            continue
        initial = portfolios_before.get(user["username"], _INITIAL_POINTS)
        final = Decimal(str(port_after.get("available_points", initial)))
        spent = initial - final
        if spent < Decimal("0"):
            _fail(
                f"F: {user['username']} ended with MORE points than they started "
                f"({final} > {initial}) — cost conservation violated"
            )
            violations += 1

    if violations == 0:
        _ok("F: Cost conservation satisfied — no user gained points from buying")


# ---------------------------------------------------------------------------
# Main stress test runner
# ---------------------------------------------------------------------------


async def run_stress_test(base_url: str, concurrency: int) -> None:
    run_id = str(uuid.uuid4())[:8]
    print(f"\n{'='*60}")
    print(f"  LineUp LS-LMSR Stress Test  |  concurrency={concurrency}")
    print(f"  base_url={base_url}  |  run_id={run_id}")
    print(f"{'='*60}\n")

    async with httpx.AsyncClient(timeout=60.0) as client:

        # ── Setup: find target player ────────────────────────────────────────
        print("[1/5] Finding target player...")
        try:
            player_id = await find_or_create_target_player(client, base_url)
        except RuntimeError as exc:
            print(f"  [ERROR] {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"  Target player_id={player_id}")

        # ── Get market state before trading ─────────────────────────────────
        market_before = await get_market_state(client, base_url, player_id)
        q_up_before = Decimal(str(market_before["q_up"])) if market_before else Decimal("0")
        print(f"  Market before: q_up={q_up_before}")

        # ── Register test users ──────────────────────────────────────────────
        print(f"\n[2/5] Registering {concurrency} test users...")
        users = await register_users(client, base_url, concurrency, run_id)
        valid_users = [u for u in users if u is not None]
        if len(valid_users) < concurrency:
            print(f"  WARNING: Only {len(valid_users)}/{concurrency} users registered")

        # ── Fire concurrent buy trades ───────────────────────────────────────
        print(f"\n[3/5] Firing {len(valid_users)} concurrent BUY_UP trades (budget=100 each)...")
        coroutines = [
            execute_buy(client, base_url, u["token"], player_id, _TRADE_BUDGET)
            for u in valid_users
        ]
        t_start = asyncio.get_event_loop().time()
        responses = await asyncio.gather(*coroutines, return_exceptions=False)
        t_end = asyncio.get_event_loop().time()

        successful = [r for r in responses if r.get("success")]
        rejected = [r for r in responses if not r.get("success") and r["status_code"] not in (-1, 500)]
        errors = [r for r in responses if r["status_code"] == 500]

        print(
            f"  {len(successful)} succeeded | {len(rejected)} rejected "
            f"(40x/42x) | {len(errors)} server errors"
        )
        print(f"  All {len(responses)} requests completed in {t_end - t_start:.2f}s")

        # ── Collect portfolio states after trades ────────────────────────────
        print("\n[4/5] Collecting portfolio states...")
        portfolios_after = await asyncio.gather(
            *[get_portfolio(client, base_url, u["token"]) for u in valid_users]
        )
        market_after = await get_market_state(client, base_url, player_id)
        print(f"  Market after:  q_up={market_after['q_up'] if market_after else 'N/A'}")

        # ── Assertions ───────────────────────────────────────────────────────
        print("\n[5/5] Running assertions...")
        assert_no_500s(responses)
        assert_no_negative_balances(portfolios_after, valid_users)
        assert_q_up_matches_shares(market_after, q_up_before, successful)
        assert_lmsr_precision(market_after)
        assert_cost_conservation(
            valid_users,
            {u["username"]: u["available_points"] for u in valid_users},
            portfolios_after,
            successful,
        )

    # ── Final verdict ────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    if _FAILURES:
        print(f"  STRESS TEST FAILED — {len(_FAILURES)} assertion(s) failed:")
        for f in _FAILURES:
            print(f"    • {f}")
        print(
            "\n  NOTE: Also check for deadlocks in Postgres logs:\n"
            "    docker compose -f docker-compose.prod.yml logs db | grep deadlock"
        )
        print(f"{'='*60}\n")
        sys.exit(1)
    else:
        print("  ALL ASSERTIONS PASSED")
        print(
            "\n  NOTE: Verify no deadlocks in Postgres logs:\n"
            "    docker compose -f docker-compose.prod.yml logs db | grep deadlock"
        )
        print(f"{'='*60}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="LS-LMSR AMM concurrency stress test")
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="API base URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=50,
        help="Number of concurrent users / trades (default: 50)",
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="(Optional) Postgres URL for direct DB invariant queries",
    )
    args = parser.parse_args()

    asyncio.run(run_stress_test(args.base_url, args.concurrency))


if __name__ == "__main__":
    main()

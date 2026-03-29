"""
LS-LMSR pure math functions — no database, HTTP, or Redis imports.

All functions that evaluate exp(q/b) use the max-shift log-sum-exp trick
(§3.0) to prevent OverflowError at high share volumes.

Functions tolerate micro-negative q in [-0.01, 0.0) (Dust Buffer §3.8).
Guards of the form `assert q >= 0` are intentionally absent.
"""
import math


# ---------------------------------------------------------------------------
# Internal helper — used by every function that needs log-sum-exp
# ---------------------------------------------------------------------------

def _log_sum_exp(q_up: float, q_down: float, b: float) -> float:
    """Max-shift log-sum-exp: b * log(exp(q_up/b) + exp(q_down/b)).

    Both exponents are shifted by m = max(q_up/b, q_down/b) so the
    largest term is exp(0) = 1.0 — overflow is mathematically impossible.
    """
    x_up = q_up / b
    x_down = q_down / b
    m = max(x_up, x_down)
    return b * (m + math.log(math.exp(x_up - m) + math.exp(x_down - m)))


# ---------------------------------------------------------------------------
# §3.2  Effective liquidity parameter
# ---------------------------------------------------------------------------

# ─────────────────────────────────────────────────────────────────────────────
# DYNAMIC b FLOOR — Market Stability Mechanism
#
# PURPOSE: Prevents extreme price movements when user count is low.
# At beta (2–10 users), a 100-point trade would otherwise move a player's
# price by ~60%.  The floor raises b_effective so a 100-point trade
# causes < 1% price movement until the market grows organically.
#
# AUTOMATIC NO-OP: As the user base grows and trading volume accumulates,
# the natural b = b_min + alpha × (q_up + q_down) overtakes B_FLOOR and
# the floor becomes mathematically irrelevant — no code change required.
#
# REMOVAL CRITERIA: Do not remove this function.  It becomes irrelevant
# once average market volume exceeds ~200,000 pts per player.
# Monitor via:  SELECT AVG(q_up + q_down) FROM lmsr_market_state;
# If result > 2,000,000 shares, the floor is superseded.
#
# CONFIG: LMSR_B_BASE, LMSR_N_TARGET, LMSR_N_MIN in app/config.py —
# tunable via env vars without a redeploy.
# ─────────────────────────────────────────────────────────────────────────────
def b_floor_for_users(
    n_users: int,
    b_base: float,
    n_target: int,
    n_min: int,
) -> float:
    """Compute the minimum b value for the current user count.

    Returns a high floor when the user base is small, decreasing toward
    b_base as the platform grows.  At n_users >= n_target the floor equals
    b_base.  Beyond n_target it stays at b_base (max() clamp).

    Formula:
        B_FLOOR = b_base * max(1, n_target / max(n_min, n_users))

    Examples (b_base=10000, n_target=100, n_min=5):
        n=5   → 200000  (< 0.05% per 100pt trade)
        n=20  → 50000
        n=50  → 20000
        n=100 → 10000   (anchor — < 1% per 100pt trade)
        n=500 → 10000   (floor stops scaling; alpha takes over)
    """
    safe_n = max(n_min, n_users)
    multiplier = max(1.0, n_target / safe_n)
    return b_base * multiplier


def effective_b(
    b_min: float,
    alpha: float,
    q_up: float,
    q_down: float,
    b_floor: float = 0.0,
) -> float:
    """Compute the effective liquidity parameter b_eff.

    b_eff = max(b_floor, b_min + alpha * (q_up + q_down))

    The natural component grows as total outstanding shares increase,
    providing liquidity-sensitive slippage (INV-04: always > 0 because
    b_min > 0 and alpha, q_up, q_down >= -0.01).

    b_floor is an optional lower bound supplied by the dynamic b floor
    mechanism (§market-stability).  When omitted (default 0.0) the
    function is equivalent to the original formula with no floor.
    """
    natural_b = b_min + alpha * (q_up + q_down)
    return max(natural_b, b_floor)


# ---------------------------------------------------------------------------
# §3.3  LMSR cost function
# ---------------------------------------------------------------------------

def lmsr_cost(q_up: float, q_down: float, b: float) -> float:
    """LS-LMSR cost (log-partition) at state (q_up, q_down) with parameter b.

    C(q_up, q_down) = b * log(exp(q_up/b) + exp(q_down/b))

    Uses max-shift log-sum-exp (§3.0) — overflow-safe for any realistic
    share volume.
    """
    return _log_sum_exp(q_up, q_down, b)


# ---------------------------------------------------------------------------
# §3.4  Budget-to-shares calculation (log1p form)
# ---------------------------------------------------------------------------

def calculate_shares_for_budget(
    q_up: float,
    q_down: float,
    b: float,
    budget: float,
    is_buying_up: bool,
) -> float:
    """Return the number of shares received for `budget` points at current state.

    Uses the log1p algebraic inverse to avoid overflow on large budgets.
    The micro-clamp to -1e-15 guards the float64 precision edge-case where
    a tiny budget falls below the ULP of a very large q_other (§3.4).

    Returns 0.0 if budget is too small to move the market by even 1 ULP.
    """
    C_new = lmsr_cost(q_up, q_down, b) + budget

    if is_buying_up:
        exponent = min(-1e-15, (q_down - C_new) / b)
        new_q_up = b * ((C_new / b) + math.log1p(-math.exp(exponent)))
        return max(0.0, new_q_up - q_up)
    else:
        exponent = min(-1e-15, (q_up - C_new) / b)
        new_q_down = b * ((C_new / b) + math.log1p(-math.exp(exponent)))
        return max(0.0, new_q_down - q_down)


# ---------------------------------------------------------------------------
# §3.5  Sell refund functions
# ---------------------------------------------------------------------------

def sell_refund_up(q_up: float, q_down: float, b: float, shares: float) -> float:
    """Points refunded for selling `shares` UP shares at current market state.

    Refund = C(q_up, q_down) - C(q_up - shares, q_down)

    Both costs use max-shift log-sum-exp (§3.0). Result is always >= 0
    in normal market conditions (cost function is monotone in q_up).
    """
    cost_before = lmsr_cost(q_up, q_down, b)
    cost_after = lmsr_cost(q_up - shares, q_down, b)
    return max(0.0, cost_before - cost_after)


def sell_refund_down(q_up: float, q_down: float, b: float, shares: float) -> float:
    """Points refunded for selling `shares` DOWN shares at current market state.

    Refund = C(q_up, q_down) - C(q_up, q_down - shares)

    Both costs use max-shift log-sum-exp (§3.0).
    """
    cost_before = lmsr_cost(q_up, q_down, b)
    cost_after = lmsr_cost(q_up, q_down - shares, b)
    return max(0.0, cost_before - cost_after)


# ---------------------------------------------------------------------------
# §3.6  Market rating (sigmoid)
# ---------------------------------------------------------------------------

def lmsr_rating(q_up: float, q_down: float, b: float) -> float:
    """Compute the displayed market rating on a 0.0–10.0 scale.

    P_up = exp(q_up/b) / (exp(q_up/b) + exp(q_down/b))
           = softmax of q_up

    rating = P_up * 10.0, clamped to [0.0, 10.0] (INV-01).

    Uses max-shift to evaluate P_up without overflow:
      P_up = 1 / (1 + exp((q_down - q_up) / b))
    but we compute via the stable log-sum-exp difference form below to
    avoid any intermediate overflow.
    """
    x_up = q_up / b
    x_down = q_down / b
    m = max(x_up, x_down)
    exp_up = math.exp(x_up - m)
    exp_down = math.exp(x_down - m)
    p_up = exp_up / (exp_up + exp_down)
    rating = p_up * 10.0
    return max(0.0, min(10.0, rating))


# ---------------------------------------------------------------------------
# §3.7  Market initialisation (asymmetric form)
# ---------------------------------------------------------------------------

def initialize_market(r_base: float, b_min: float) -> tuple[float, float]:
    """Initialise (q_up, q_down) so that lmsr_rating == r_base.

    Uses the asymmetric form to guarantee non-negative shares (INV-06):
    - P >= 0.5: q_down = 0, q_up = b_min * ln(P / (1-P))
    - P <  0.5: q_up  = 0, q_down = b_min * ln((1-P) / P)

    Post-condition:
        q_up >= 0.0 and q_down >= 0.0
        abs(lmsr_rating(q_up, q_down, b_min) - r_base) < 1e-9
    """
    P = r_base / 10.0
    P = max(0.001, min(0.999, P))   # safety clamp

    if P >= 0.5:
        q_down = 0.0
        q_up = b_min * math.log(P / (1.0 - P))
    else:
        q_up = 0.0
        q_down = b_min * math.log((1.0 - P) / P)

    return (q_up, q_down)


# ---------------------------------------------------------------------------
# §3.8  Volatility tier bucketing
# ---------------------------------------------------------------------------

def compute_volatility_tiers(b_effective_map: dict[int, float]) -> dict[int, str]:
    """Assign a volatility tier to each player based on their b_effective rank.

    Higher b_effective = more liquidity = less price movement = LOWER volatility.
    Tiers are relative to the current population — percentile-based.

    Bucketing (ascending sort by b_effective):
      Bottom 33rd percentile → "high"   (lowest liquidity, most volatile)
      33rd–66th percentile   → "medium"
      Top 33rd percentile    → "low"    (highest liquidity, least volatile)

    Edge cases:
      Empty map            → {}
      Fewer than 3 players → all "medium"
      All identical values → all "medium"
    """
    if not b_effective_map:
        return {}

    n = len(b_effective_map)
    if n < 3:
        return {pid: "medium" for pid in b_effective_map}

    sorted_ids = sorted(b_effective_map, key=lambda pid: b_effective_map[pid])

    if b_effective_map[sorted_ids[0]] == b_effective_map[sorted_ids[-1]]:
        return {pid: "medium" for pid in b_effective_map}

    low_cutoff = n // 3          # first index NOT in "high" band
    high_cutoff = (2 * n) // 3  # first index in "low" band

    result: dict[int, str] = {}
    for rank, pid in enumerate(sorted_ids):
        if rank < low_cutoff:
            result[pid] = "high"
        elif rank < high_cutoff:
            result[pid] = "medium"
        else:
            result[pid] = "low"
    return result

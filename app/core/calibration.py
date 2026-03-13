"""
LS-LMSR liquidity calibration — no database, HTTP, or Redis imports.

The one-way b-min ratchet (b_min can only increase) is enforced at the
call site in the Celery worker (§7.7), NOT inside this function.
This keeps calibrate_b_min a pure, testable math function.
"""
import math


def calibrate_b_min(
    base_liquidity: float,
    n_reference: int,
    active_users: int,
) -> float:
    """Compute target b_min from global platform state.

    Formula (§3.8):
        b_min = base_liquidity * sqrt(active_users / n_reference)

    Rationale: liquidity should scale with the square-root of active
    users relative to the reference population.  This gives diminishing
    returns as the user base grows — each new user adds less marginal
    liquidity — while still ensuring the market deepens over time.

    Args:
        base_liquidity: Baseline b_min at n_reference users (from
                        liquidity_config.base_liquidity, default 100.0).
        n_reference:    Reference user count (from liquidity_config.n_reference,
                        default 50).
        active_users:   Current number of active users on the platform.

    Returns:
        Target b_min (always > 0 because base_liquidity > 0).

    Note:
        The caller (calibration Celery task §7.7) is responsible for
        applying the one-way ratchet:
            b_min_new = max(b_min_current, calibrate_b_min(...))
        This function MUST NOT be modified to implement the ratchet itself
        — it is a pure math function and must remain easily testable.
    """
    if n_reference <= 0:
        raise ValueError(f"n_reference must be > 0, got {n_reference}")
    if active_users < 0:
        raise ValueError(f"active_users must be >= 0, got {active_users}")

    ratio = max(0.0, active_users) / n_reference
    return base_liquidity * math.sqrt(ratio)

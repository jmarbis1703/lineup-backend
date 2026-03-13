"""
Comprehensive tests for app/core/lmsr.py and app/core/calibration.py.

Sections:
    - EFFECTIVE B
    - COST (known values)
    - BUDGET BUY (critical round-trip, slippage, extremes)
    - SELL
    - RATING
    - INIT (above 5 and below 5)
    - CALIBRATION
    - LS-LMSR (early-trade price impact, b growth)
    - STRESS (10 k buys, alternating, random)
    - OVERFLOW (CRITICAL — all three functions at q=100 000)
    - MICRO-BUDGET LOPSIDED MARKET (CRITICAL — §3.4 micro-clamp)

All tests are pure math — no database, HTTP, or async fixtures needed.
"""
import math
import random

import pytest

from app.core.lmsr import (
    effective_b,
    lmsr_cost,
    calculate_shares_for_budget,
    sell_refund_up,
    sell_refund_down,
    lmsr_rating,
    initialize_market,
)
from app.core.calibration import calibrate_b_min


# ---------------------------------------------------------------------------
# EFFECTIVE B
# ---------------------------------------------------------------------------

class TestEffectiveB:
    def test_b_min_dominates_when_shares_low(self):
        """At zero shares, b_eff equals b_min exactly."""
        assert effective_b(0.0, 0.0, alpha=0.05, b_min=100.0) == 100.0

    def test_b_min_still_visible_at_moderate_volume(self):
        """b_min component should be non-negligible at q_up=10, q_down=10."""
        b = effective_b(10.0, 10.0, alpha=0.05, b_min=100.0)
        # alpha*(10+10) = 1.0  <<  b_min = 100.0
        assert b == pytest.approx(101.0, rel=1e-9)
        assert b / 100.0 < 1.02   # b_min still dominates

    def test_alpha_dominates_when_shares_high(self):
        """At very high volume, alpha*q >> b_min."""
        q = 100_000.0
        b = effective_b(q, 0.0, alpha=0.05, b_min=100.0)
        alpha_component = 0.05 * q
        assert b == pytest.approx(100.0 + alpha_component, rel=1e-9)
        assert alpha_component > 100.0 * 40   # alpha clearly dominates (ratio = 50×)

    def test_always_positive_with_typical_inputs(self):
        b = effective_b(500.0, 300.0, alpha=0.05, b_min=100.0)
        assert b > 0

    def test_always_positive_with_dust_buffer_q(self):
        """Micro-negative q (Dust Buffer) must not drive b to zero."""
        b = effective_b(-0.01, 50.0, alpha=0.05, b_min=100.0)
        assert b > 0

    def test_alpha_zero_gives_constant_b(self):
        b1 = effective_b(0.0, 0.0, alpha=0.0, b_min=50.0)
        b2 = effective_b(1_000_000.0, 2_000_000.0, alpha=0.0, b_min=50.0)
        assert b1 == b2 == 50.0

    def test_scales_linearly_with_total_shares(self):
        b = effective_b(100.0, 200.0, alpha=0.05, b_min=100.0)
        assert b == pytest.approx(100.0 + 0.05 * 300.0, rel=1e-9)


# ---------------------------------------------------------------------------
# COST — known values
# ---------------------------------------------------------------------------

class TestLmsrCost:
    def test_symmetric_state_equals_b_log2(self):
        """C(0, 0, b) = b * log(2) — exact closed form."""
        assert lmsr_cost(0.0, 0.0, 100.0) == pytest.approx(100.0 * math.log(2), rel=1e-9)

    def test_known_value_small_q(self):
        """C(50, 30, 100) matches manual computation b*log(exp(0.5)+exp(0.3))."""
        q_up, q_down, b = 50.0, 30.0, 100.0
        # max-shift: m=0.5; 100*(0.5 + log(1 + exp(-0.2)))
        expected = b * math.log(math.exp(q_up / b) + math.exp(q_down / b))
        assert lmsr_cost(q_up, q_down, b) == pytest.approx(expected, rel=1e-6)

    def test_known_value_asymmetric(self):
        """C(200, 0, 100) = 100*(2 + log(1+exp(-2))) verified manually."""
        q_up, q_down, b = 200.0, 0.0, 100.0
        expected = b * math.log(math.exp(q_up / b) + math.exp(q_down / b))
        assert lmsr_cost(q_up, q_down, b) == pytest.approx(expected, rel=1e-6)

    def test_cost_increases_with_q_up(self):
        assert lmsr_cost(50.0, 0.0, 100.0) > lmsr_cost(0.0, 0.0, 100.0)

    def test_cost_symmetric_in_q_up_q_down(self):
        """log-sum-exp is order-independent."""
        assert lmsr_cost(30.0, 70.0, 100.0) == pytest.approx(
            lmsr_cost(70.0, 30.0, 100.0), rel=1e-12
        )

    def test_dust_buffer_micro_negative_q_does_not_crash(self):
        c = lmsr_cost(-0.005, 100.0, 100.0)
        assert math.isfinite(c)


# ---------------------------------------------------------------------------
# BUDGET BUY — CRITICAL
# ---------------------------------------------------------------------------

class TestCalculateSharesForBudget:
    # --- basic positive-shares checks ---

    def test_buy_up_returns_positive_shares(self):
        shares = calculate_shares_for_budget(0.0, 0.0, 100.0, 50.0, is_buying_up=True)
        assert shares > 0

    def test_buy_down_returns_positive_shares(self):
        shares = calculate_shares_for_budget(0.0, 0.0, 100.0, 50.0, is_buying_up=False)
        assert shares > 0

    def test_symmetric_state_equal_shares_both_directions(self):
        s_up = calculate_shares_for_budget(0.0, 0.0, 100.0, 50.0, True)
        s_dn = calculate_shares_for_budget(0.0, 0.0, 100.0, 50.0, False)
        assert s_up == pytest.approx(s_dn, rel=1e-9)

    # --- round-trip: cost delta must equal budget, within 1e-9 ---

    def test_round_trip_cost_delta_equals_budget_up(self):
        q_up, q_down, b, budget = 0.0, 0.0, 100.0, 75.0
        shares = calculate_shares_for_budget(q_up, q_down, b, budget, True)
        delta = lmsr_cost(q_up + shares, q_down, b) - lmsr_cost(q_up, q_down, b)
        assert delta == pytest.approx(budget, rel=1e-9)

    def test_round_trip_cost_delta_equals_budget_down(self):
        q_up, q_down, b, budget = 0.0, 0.0, 100.0, 75.0
        shares = calculate_shares_for_budget(q_up, q_down, b, budget, False)
        delta = lmsr_cost(q_up, q_down + shares, b) - lmsr_cost(q_up, q_down, b)
        assert delta == pytest.approx(budget, rel=1e-9)

    def test_round_trip_at_non_zero_state(self):
        """Round-trip must also hold when starting from a non-zero market state."""
        q_up, q_down, b, budget = 120.0, 40.0, 100.0, 30.0
        shares = calculate_shares_for_budget(q_up, q_down, b, budget, True)
        delta = lmsr_cost(q_up + shares, q_down, b) - lmsr_cost(q_up, q_down, b)
        assert delta == pytest.approx(budget, rel=1e-9)

    # --- small and large budgets ---

    def test_small_budget_001_works(self):
        shares = calculate_shares_for_budget(0.0, 0.0, 100.0, 0.01, True)
        assert shares > 0
        assert math.isfinite(shares)

    def test_large_budget_999_works(self):
        shares = calculate_shares_for_budget(0.0, 0.0, 100.0, 999.0, True)
        assert shares > 0
        assert math.isfinite(shares)

    # --- slippage: split budget is more efficient in LS-LMSR ---

    def test_slippage_one_large_trade_worse_than_two_smaller(self):
        """In LS-LMSR (alpha > 0), buying in two tranches yields more total
        shares than one big trade.  After the first tranche b has grown, making
        the second tranche cheaper-per-share (deeper market)."""
        q_up, q_down, alpha, b_min = 0.0, 0.0, 0.05, 100.0

        # One big trade (b does not update mid-trade)
        b_initial = effective_b(q_up, q_down, alpha, b_min)
        shares_100 = calculate_shares_for_budget(q_up, q_down, b_initial, 100.0, True)

        # First tranche of 50
        b1 = effective_b(q_up, q_down, alpha, b_min)
        shares_a = calculate_shares_for_budget(q_up, q_down, b1, 50.0, True)
        # b has grown — second tranche is in a deeper market
        b2 = effective_b(q_up + shares_a, q_down, alpha, b_min)
        shares_b = calculate_shares_for_budget(q_up + shares_a, q_down, b2, 50.0, True)

        total_split = shares_a + shares_b
        # Split trades are strictly more efficient
        assert total_split > shares_100

    def test_zero_budget_returns_zero(self):
        shares = calculate_shares_for_budget(50.0, 50.0, 100.0, 0.0, True)
        assert shares == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# SELL
# ---------------------------------------------------------------------------

class TestSellRefund:
    def test_refund_up_is_positive(self):
        q_up, q_down, b = 0.0, 0.0, 100.0
        shares = calculate_shares_for_budget(q_up, q_down, b, 100.0, True)
        refund = sell_refund_up(q_up + shares, q_down, b, shares)
        assert refund > 0

    def test_refund_down_is_positive(self):
        q_up, q_down, b = 0.0, 0.0, 100.0
        shares = calculate_shares_for_budget(q_up, q_down, b, 100.0, False)
        refund = sell_refund_down(q_up, q_down + shares, b, shares)
        assert refund > 0

    def test_buy_then_sell_immediately_recovers_budget(self):
        """Buying then immediately selling (same market state) is lossless."""
        q_up, q_down, b, budget = 0.0, 0.0, 100.0, 50.0
        shares = calculate_shares_for_budget(q_up, q_down, b, budget, True)
        refund = sell_refund_up(q_up + shares, q_down, b, shares)
        assert refund == pytest.approx(budget, rel=1e-6)

    def test_buy_then_sell_refund_leq_budget(self):
        """After other trades have moved the market, selling back returns ≤ budget
        (no risk-free profit)."""
        q_up, q_down, b = 0.0, 0.0, 100.0
        budget = 80.0
        shares = calculate_shares_for_budget(q_up, q_down, b, budget, True)
        # Simulate someone else buying DOWN heavily, moving market against Alice
        q_up_alice = q_up + shares
        q_down_adversarial = q_down + 500.0
        refund = sell_refund_up(q_up_alice, q_down_adversarial, b, shares)
        assert refund <= budget + 1e-9   # may be much less, never more

    def test_sell_down_symmetric_at_equal_state(self):
        q_up, q_down, b = 200.0, 200.0, 100.0
        refund_up = sell_refund_up(q_up, q_down, b, 30.0)
        refund_down = sell_refund_down(q_up, q_down, b, 30.0)
        assert refund_up == pytest.approx(refund_down, rel=1e-9)


# ---------------------------------------------------------------------------
# RATING
# ---------------------------------------------------------------------------

class TestLmsrRating:
    def test_midpoint_is_5(self):
        assert lmsr_rating(0.0, 0.0, 100.0) == pytest.approx(5.0, rel=1e-9)

    def test_never_above_10(self):
        assert lmsr_rating(1_000_000.0, 0.0, 100.0) <= 10.0

    def test_never_below_0(self):
        assert lmsr_rating(0.0, 1_000_000.0, 100.0) >= 0.0

    def test_increases_with_q_up(self):
        r1 = lmsr_rating(100.0, 50.0, 100.0)
        r2 = lmsr_rating(150.0, 50.0, 100.0)
        assert r2 > r1

    def test_decreases_with_q_down(self):
        r1 = lmsr_rating(50.0, 100.0, 100.0)
        r2 = lmsr_rating(50.0, 150.0, 100.0)
        assert r2 < r1

    def test_high_q_up_gives_rating_above_5(self):
        assert lmsr_rating(200.0, 0.0, 100.0) > 5.0

    def test_high_q_down_gives_rating_below_5(self):
        assert lmsr_rating(0.0, 200.0, 100.0) < 5.0

    def test_dust_buffer_values_do_not_crash(self):
        r = lmsr_rating(-0.005, 100.0, 100.0)
        assert math.isfinite(r)


# ---------------------------------------------------------------------------
# INIT — above 5 (§3.7)
# ---------------------------------------------------------------------------

class TestInitializeMarketAbove5:
    def test_75_round_trips(self):
        """initialize_market(7.5, 100) → lmsr_rating recovers 7.5."""
        q_up, q_down = initialize_market(7.5, 100.0)
        assert lmsr_rating(q_up, q_down, 100.0) == pytest.approx(7.5, abs=1e-9)

    def test_neutral_gives_both_zero(self):
        q_up, q_down = initialize_market(5.0, 100.0)
        assert q_up == pytest.approx(0.0, abs=1e-9)
        assert q_down == pytest.approx(0.0, abs=1e-9)

    def test_high_rating_q_down_is_zero(self):
        q_up, q_down = initialize_market(7.5, 100.0)
        assert q_down == 0.0
        assert q_up > 0

    def test_no_negative_q_across_range(self):
        for r in [3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]:
            q_up, q_down = initialize_market(r, 100.0)
            assert q_up >= 0.0
            assert q_down >= 0.0

    def test_round_trips_for_multiple_ratings(self):
        b_min = 100.0
        # Excludes 0.0 / 10.0: P clamped to 0.001/0.999, so round-trip gives 0.01/9.99.
        for r_base in [3.0, 4.0, 5.0, 6.5, 7.2, 8.9]:
            q_up, q_down = initialize_market(r_base, b_min)
            r_recovered = lmsr_rating(q_up, q_down, b_min)
            assert r_recovered == pytest.approx(r_base, abs=1e-6), (
                f"Round-trip failed for r_base={r_base}: got {r_recovered}"
            )


# ---------------------------------------------------------------------------
# INIT — BELOW 5, asymmetric form (§3.7 — prevents negative q)
# ---------------------------------------------------------------------------

class TestInitializeMarketBelow5:
    def test_initialize_market_below_5_structure(self):
        """initialize_market(3.0, 100) must set q_up=0 and q_down>0.

        This verifies the asymmetric form from §3.7: when P < 0.5,
        q_up is pinned to 0.0 and q_down carries the full imbalance.
        Without asymmetric init, the naive formula would produce a negative
        q_up, violating INV-06.
        """
        q_up, q_down = initialize_market(3.0, 100.0)
        assert q_up == 0.0, f"Expected q_up==0.0 for r<5, got {q_up}"
        assert q_down > 0.0, f"Expected q_down>0 for r<5, got {q_down}"

    def test_initialize_market_below_5_round_trips(self):
        """lmsr_rating(q_up, q_down, 100) must recover 3.0 within 1e-9."""
        q_up, q_down = initialize_market(3.0, 100.0)
        r_recovered = lmsr_rating(q_up, q_down, 100.0)
        assert r_recovered == pytest.approx(3.0, abs=1e-9), (
            f"Round-trip failed: expected 3.0, got {r_recovered}"
        )

    def test_initialize_market_40_structure(self):
        """Verify asymmetric form also holds at 4.0."""
        q_up, q_down = initialize_market(4.0, 100.0)
        assert q_up == 0.0
        assert q_down > 0.0

    def test_initialize_market_40_round_trips(self):
        q_up, q_down = initialize_market(4.0, 100.0)
        assert lmsr_rating(q_up, q_down, 100.0) == pytest.approx(4.0, abs=1e-9)


# ---------------------------------------------------------------------------
# CALIBRATION (§3.8)
# ---------------------------------------------------------------------------

class TestCalibrateBMin:
    def test_baseline_correct_at_reference_users(self):
        result = calibrate_b_min(base_liquidity=100.0, n_reference=50, active_users=50)
        assert result == pytest.approx(100.0, rel=1e-9)

    def test_sqrt_scaling_4x_users_gives_2x_b(self):
        b1 = calibrate_b_min(100.0, 50, 50)
        b4 = calibrate_b_min(100.0, 50, 200)   # 4× users
        assert b4 == pytest.approx(b1 * 2.0, rel=1e-9)

    def test_zero_users_gives_zero(self):
        assert calibrate_b_min(100.0, 50, 0) == pytest.approx(0.0, abs=1e-9)

    def test_more_users_monotonically_increases_b(self):
        b50 = calibrate_b_min(100.0, 50, 50)
        b100 = calibrate_b_min(100.0, 50, 100)
        b200 = calibrate_b_min(100.0, 50, 200)
        assert b50 < b100 < b200

    def test_base_liquidity_scales_linearly(self):
        b1 = calibrate_b_min(100.0, 50, 50)
        b2 = calibrate_b_min(200.0, 50, 50)
        assert b2 == pytest.approx(b1 * 2.0, rel=1e-9)

    def test_invalid_n_reference_raises(self):
        with pytest.raises(ValueError):
            calibrate_b_min(100.0, 0, 50)

    def test_negative_users_raises(self):
        with pytest.raises(ValueError):
            calibrate_b_min(100.0, 50, -1)


# ---------------------------------------------------------------------------
# LS-LMSR — early-trade price impact and b growth
# ---------------------------------------------------------------------------

class TestLSLMSRDynamics:
    def test_b_grows_with_volume(self):
        b_initial = effective_b(0.0, 0.0, 0.05, 100.0)
        b_after = effective_b(500.0, 200.0, 0.05, 100.0)
        assert b_after > b_initial

    def test_early_trades_move_price_more_than_late_trades(self):
        """Buying N shares at low volume moves the rating more than buying
        the same N shares at high volume (larger b means less price impact)."""
        b_min, alpha, shares = 100.0, 0.05, 20.0

        # Early trade: market starts at (0, 0)
        b_early = effective_b(0.0, 0.0, alpha, b_min)
        delta_early = (
            lmsr_rating(shares, 0.0, b_early)
            - lmsr_rating(0.0, 0.0, b_early)
        )

        # Late trade: market already has high volume
        q_up_high = 1_000.0
        b_late = effective_b(q_up_high, 0.0, alpha, b_min)
        delta_late = (
            lmsr_rating(q_up_high + shares, 0.0, b_late)
            - lmsr_rating(q_up_high, 0.0, b_late)
        )

        assert delta_early > delta_late, (
            f"Early delta={delta_early:.6f} should exceed late delta={delta_late:.6f}"
        )

    def test_effective_b_grows_across_successive_buys(self):
        """Simulate 10 sequential purchases and confirm b always grows."""
        q_up, q_down, alpha, b_min = 0.0, 0.0, 0.05, 100.0
        b_prev = effective_b(q_up, q_down, alpha, b_min)
        for _ in range(10):
            shares = calculate_shares_for_budget(q_up, q_down, b_prev, 50.0, True)
            q_up += shares
            b_new = effective_b(q_up, q_down, alpha, b_min)
            assert b_new > b_prev
            b_prev = b_new


# ---------------------------------------------------------------------------
# STRESS
# ---------------------------------------------------------------------------

class TestStress:
    def test_10k_sequential_buys_no_overflow(self):
        """10 000 sequential UP buys at fixed b must never overflow or NaN."""
        q_up, q_down, b = 0.0, 0.0, 100.0
        for _ in range(10_000):
            shares = calculate_shares_for_budget(q_up, q_down, b, 10.0, True)
            assert math.isfinite(shares), f"Non-finite shares at q_up={q_up}"
            assert shares >= 0.0
            q_up += shares

    def test_alternating_buys_and_sells_stay_non_negative(self):
        """Alternating buy-UP / sell-half should keep q_up non-negative."""
        q_up, q_down, b = 0.0, 0.0, 100.0
        for i in range(1_000):
            if i % 2 == 0:
                shares = calculate_shares_for_budget(q_up, q_down, b, 50.0, True)
                q_up += shares
            else:
                to_sell = q_up / 2.0
                if to_sell > 0:
                    refund = sell_refund_up(q_up, q_down, b, to_sell)
                    assert math.isfinite(refund)
                    q_up -= to_sell
        assert q_up >= 0.0

    def test_no_nan_on_random_valid_inputs(self):
        """1 000 random (q_up, q_down, b, budget) tuples must not produce NaN."""
        rng = random.Random(42)
        for _ in range(1_000):
            q_up = rng.uniform(0.0, 10_000.0)
            q_down = rng.uniform(0.0, 10_000.0)
            b = rng.uniform(10.0, 1_000.0)
            budget = rng.uniform(0.01, 500.0)

            assert not math.isnan(lmsr_cost(q_up, q_down, b))
            assert not math.isnan(lmsr_rating(q_up, q_down, b))
            shares = calculate_shares_for_budget(q_up, q_down, b, budget, True)
            assert not math.isnan(shares)


# ---------------------------------------------------------------------------
# OVERFLOW — CRITICAL (validates §3.0 max-shift)
# ---------------------------------------------------------------------------

class TestOverflow:
    """With naive exp(q/b), q=100 000 and b=100 gives exp(1000) → OverflowError.
    The max-shift trick must prevent this in all three functions."""

    def test_lmsr_cost_extreme_q_no_overflow(self):
        """lmsr_cost(100_000, 0, 100) must return a valid finite float."""
        c = lmsr_cost(100_000.0, 0.0, 100.0)
        assert math.isfinite(c), "lmsr_cost raised OverflowError or returned inf/nan"
        # C ≈ q_up when q_up >> q_down
        assert c == pytest.approx(100_000.0, rel=1e-4)

    def test_lmsr_rating_extreme_q_no_overflow(self):
        """lmsr_rating(100_000, 0, 100) must succeed and approach 10.0."""
        r = lmsr_rating(100_000.0, 0.0, 100.0)
        assert math.isfinite(r), "lmsr_rating raised OverflowError or returned inf/nan"
        assert r == pytest.approx(10.0, abs=0.01)

    def test_sell_refund_up_extreme_q_no_overflow(self):
        """sell_refund_up(100_000, 1, 100, 1) must succeed."""
        refund = sell_refund_up(100_000.0, 1.0, 100.0, 1.0)
        assert math.isfinite(refund), "sell_refund_up raised OverflowError"
        assert refund > 0

    def test_budget_1_million_no_overflow(self):
        """calculate_shares_for_budget with budget=1_000_000 must not overflow
        (validates §3.4 log1p form)."""
        shares = calculate_shares_for_budget(0.0, 0.0, 100.0, 1_000_000.0, True)
        assert math.isfinite(shares), (
            "calculate_shares_for_budget raised OverflowError or returned inf/nan"
        )
        assert shares > 0


# ---------------------------------------------------------------------------
# MICRO-BUDGET LOPSIDED MARKET — CRITICAL (validates §3.4 micro-clamp)
# ---------------------------------------------------------------------------

class TestMicroBudgetLopsidedMarket:
    """These tests reproduce the float64 precision-loss scenario described in §3.4:

    When q_other is ~1e13, C_new = lmsr_cost(...) + 0.001 rounds back to the
    same float as lmsr_cost(...) because 0.001 < ULP(1e13).  The exponent
    (q_other - C_new) / b collapses to 0.0, driving exp(0.0)=1.0 and
    log1p(-1.0) = log(0) → ValueError without the min(-1e-15, exponent) guard.
    """

    def test_micro_budget_lopsided_down(self):
        """Heavily DOWN-sided market; tiny UP buy must not raise ValueError.

        q_down=1e13, q_up=0, b=100, budget=0.001, is_buying_up=True.
        The micro-clamp min(-1e-15, exponent) must engage and return a
        valid positive float instead of crashing.
        """
        shares = calculate_shares_for_budget(
            q_up=0.0,
            q_down=1e13,
            b=100.0,
            budget=0.001,
            is_buying_up=True,
        )
        assert math.isfinite(shares), (
            "calculate_shares_for_budget raised ValueError (math domain error) "
            "in the lopsided-down scenario — micro-clamp may be broken"
        )
        assert shares >= 0.0

    def test_micro_budget_lopsided_up(self):
        """Symmetric: heavily UP-sided market; tiny DOWN buy must not crash.

        q_up=1e13, q_down=0, b=100, budget=0.001, is_buying_up=False.
        """
        shares = calculate_shares_for_budget(
            q_up=1e13,
            q_down=0.0,
            b=100.0,
            budget=0.001,
            is_buying_up=False,
        )
        assert math.isfinite(shares), (
            "calculate_shares_for_budget raised ValueError (math domain error) "
            "in the lopsided-up scenario — micro-clamp may be broken"
        )
        assert shares >= 0.0

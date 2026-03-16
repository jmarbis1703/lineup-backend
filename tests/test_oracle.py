"""
Tests for app/core/oracle.py — 3-Layer Oracle Rating System.

Pure-function tests: no database, no HTTP, no fixtures required.
"""
import pytest

from app.core.oracle import (
    compute_layer1_rating,
    compute_layer2_composite,
    compute_oracle_rating,
    percentile_to_rating,
)


# ===========================================================================
# LAYER 1 — recency-weighted average
# ===========================================================================

def test_layer1_5_matches():
    """All 5 weights are used; no renormalisation needed (sum = 1.0)."""
    ratings = [8.0, 7.5, 7.0, 6.5, 6.0]
    # weights = [0.35, 0.25, 0.20, 0.12, 0.08], total = 1.0
    expected = (
        0.35 * 8.0
        + 0.25 * 7.5
        + 0.20 * 7.0
        + 0.12 * 6.5
        + 0.08 * 6.0
    )  # 7.335
    assert compute_layer1_rating(ratings) == pytest.approx(expected)


def test_layer1_3_matches():
    """Fewer than 5 ratings → weights truncated and renormalised."""
    ratings = [8.0, 7.5, 7.0]
    # weights = [0.35, 0.25, 0.20], total = 0.80
    expected = (0.35 * 8.0 + 0.25 * 7.5 + 0.20 * 7.0) / 0.80  # 7.59375
    assert compute_layer1_rating(ratings) == pytest.approx(expected)


def test_layer1_1_match():
    """Single rating is returned as-is (renormalised weight = 1.0)."""
    assert compute_layer1_rating([8.5]) == pytest.approx(8.5)


def test_layer1_empty():
    """Empty list → None (no data)."""
    assert compute_layer1_rating([]) is None


def test_layer1_result_in_range():
    """Result always in [0.0, 10.0] for typical football rating inputs."""
    cases = [
        [10.0],
        [0.0],
        [10.0, 10.0, 10.0, 10.0, 10.0],
        [0.0, 0.0, 0.0],
        [5.5, 6.0, 7.2, 8.1],
    ]
    for ratings in cases:
        result = compute_layer1_rating(ratings)
        assert result is not None
        assert 0.0 <= result <= 10.0


# ===========================================================================
# LAYER 2 — percentile_to_rating helper
# ===========================================================================

def test_percentile_midpoint():
    """Value at the 50th percentile maps to ~6.25 under mid-rank."""
    # [1..10]: lower=count(v<5)=4, equal=1, rank=(4+0.5)/10=0.45 → 6.25
    all_values = list(range(1, 11))
    assert percentile_to_rating(5, all_values) == pytest.approx(6.25)


def test_percentile_top():
    """Max value in a pool gets mid-rank 0.9 (not 1.0) — it ties with itself."""
    # [1..5]: lower=4, equal=1, rank=(4+0.5)/5=0.9 → 8.5
    all_values = [1, 2, 3, 4, 5]
    assert percentile_to_rating(5, all_values) == pytest.approx(8.5)


def test_percentile_bottom():
    """Min value in a large pool → rank ≈ 0 → rating close to 4.0."""
    all_values = list(range(1, 101))  # 1 .. 100
    # rank = (0+0.5)/100 = 0.005 → 4.0 + 0.025 = 4.025
    result = percentile_to_rating(1, all_values)
    assert 4.0 <= result <= 4.5


def test_percentile_no_variation():
    """All values identical → no percentile rank possible → 6.5 default."""
    assert percentile_to_rating(5.0, [5.0, 5.0, 5.0]) == pytest.approx(6.5)


def test_percentile_inverse():
    """inverse=True: the LOWEST raw value earns a high (but not 10.0) rating."""
    # goals_conceded=0 in pool [0,1,2,3,4]:
    # lower=count(v>0)=4, equal=1, rank=(4+0.5)/5=0.9 → 8.5
    all_values = [0, 1, 2, 3, 4]
    assert percentile_to_rating(0, all_values, inverse=True) == pytest.approx(8.5)


def test_percentile_inverse_high_is_bad():
    """inverse=True: the HIGHEST raw value earns a rating close to 4.0."""
    # goals_conceded=100 in pool [0..100]:
    # rank = (0+0.5)/101 ≈ 0.00495 → 4.0 + 0.0247 ≈ 4.025
    all_values = list(range(0, 101))
    result = percentile_to_rating(100, all_values, inverse=True)
    assert 4.0 <= result <= 4.1


# ===========================================================================
# LAYER 2 — compute_layer2_composite (per-position weight tables)
# ===========================================================================

def test_layer2_forward():
    """FW: goals (0.25) and shots_on_target (0.20) weighted correctly."""
    pool = [
        {"position_group": "FW", "goals": 0, "shots_on_target": 10},
        {"position_group": "FW", "goals": 10, "shots_on_target": 0},
    ]
    player_stats = {"goals": 10, "shots_on_target": 0}
    # goals: peer_values=[0,10], player=10 → lower=1,equal=1,rank=0.75 → 7.75
    # shots_on_target: peer_values=[10,0], player=0 → lower=0,equal=1,rank=0.25 → 5.25
    # composite = (0.25*7.75 + 0.20*5.25) / (0.25 + 0.20)
    expected = (0.25 * 7.75 + 0.20 * 5.25) / (0.25 + 0.20)
    result = compute_layer2_composite(player_stats, "FW", pool)
    assert result == pytest.approx(expected)


def test_layer2_midfielder():
    """MF: pass_accuracy (0.20) outweighs goals (0.15) — weights verified."""
    pool = [
        {"position_group": "MF", "pass_accuracy": 0, "goals": 10},
        {"position_group": "MF", "pass_accuracy": 10, "goals": 0},
    ]
    player_stats = {"pass_accuracy": 10, "goals": 0}
    # pass_accuracy: peer_values=[0,10], player=10 → lower=1,equal=1,rank=0.75 → 7.75
    # goals: peer_values=[10,0], player=0 → lower=0,equal=1,rank=0.25 → 5.25
    # composite = (0.20*7.75 + 0.15*5.25) / (0.20 + 0.15)
    expected = (0.20 * 7.75 + 0.15 * 5.25) / (0.20 + 0.15)
    result = compute_layer2_composite(player_stats, "MF", pool)
    assert result == pytest.approx(expected)


def test_layer2_defender():
    """DF: tackles (0.25) and interceptions (0.20) weighted correctly."""
    pool = [
        {"position_group": "DF", "tackles": 0, "interceptions": 10},
        {"position_group": "DF", "tackles": 10, "interceptions": 0},
    ]
    player_stats = {"tackles": 10, "interceptions": 0}
    # tackles: peer_values=[0,10], player=10 → lower=1,equal=1,rank=0.75 → 7.75
    # interceptions: peer_values=[10,0], player=0 → lower=0,equal=1,rank=0.25 → 5.25
    # composite = (0.25*7.75 + 0.20*5.25) / (0.25 + 0.20)
    expected = (0.25 * 7.75 + 0.20 * 5.25) / (0.25 + 0.20)
    result = compute_layer2_composite(player_stats, "DF", pool)
    assert result == pytest.approx(expected)


def test_layer2_goalkeeper():
    """GK: saves (0.30) weighted correctly; goals_conceded uses inverse=True."""
    pool = [
        {"position_group": "GK", "saves": 0, "goals_conceded": 10},  # worst
        {"position_group": "GK", "saves": 5, "goals_conceded": 5},   # mid
        {"position_group": "GK", "saves": 10, "goals_conceded": 0},  # best
    ]
    # Best GK: saves=10 in [0,5,10] → lower=2,equal=1,rank=2.5/3≈0.833 → 8.167
    #          goals_conceded=0 inverse in [10,5,0] → lower=2,equal=1,rank=0.833 → 8.167
    # composite = (0.30*8.167 + 0.20*8.167) / (0.30 + 0.20) ≈ 8.167
    best = compute_layer2_composite(
        {"saves": 10, "goals_conceded": 0}, "GK", pool
    )
    assert best == pytest.approx(4.0 + (2.5 / 3) * 5.0)

    # Worst GK must score lower than best GK
    worst = compute_layer2_composite(
        {"saves": 0, "goals_conceded": 10}, "GK", pool
    )
    assert worst is not None
    assert best > worst

    # Explicit inverse check: lower goals_conceded → HIGHER composite rating
    gc_low = compute_layer2_composite({"goals_conceded": 0}, "GK", pool)
    gc_high = compute_layer2_composite({"goals_conceded": 10}, "GK", pool)
    assert gc_low is not None and gc_high is not None
    assert gc_low > gc_high, (
        "goals_conceded uses inverse=True: lower conceded → higher rating"
    )


def test_layer2_missing_stats():
    """Stats absent from player_stats are skipped; only used weights in denominator."""
    pool = [
        {"position_group": "FW", "goals": 0},
        {"position_group": "FW", "goals": 10},
    ]
    # Only 'goals' provided; all other FW stats (shots, xg, …) are absent.
    player_stats = {"goals": 10}
    # goals: peer_values=[0,10], lower=1,equal=1,rank=0.75 → 7.75
    # used_weight = 0.25 (goals only); composite = 0.25*7.75 / 0.25 = 7.75
    result = compute_layer2_composite(player_stats, "FW", pool)
    assert result is not None
    assert result == pytest.approx(7.75)


def test_layer2_all_missing_stats_division_by_zero():
    """All weighted stats missing → used_weight=0.0 → must return None, not raise."""
    pool = [{"position_group": "FW", "goals": 5}]
    # Empty player dict: every FW stat is absent → used_weight stays 0.0
    result = compute_layer2_composite({}, "FW", pool)
    assert result is None, (
        "Zero-weight guard must return None to trigger Layer 3 fallback "
        "instead of raising ZeroDivisionError"
    )


def test_layer2_result_in_range():
    """Layer 2 composite is always in [3.0, 10.0]."""
    pool = [
        {"position_group": "MF", "goals": i, "pass_accuracy": 10 - i}
        for i in range(11)
    ]
    for goals_val in range(11):
        result = compute_layer2_composite(
            {"goals": goals_val, "pass_accuracy": 10 - goals_val}, "MF", pool
        )
        assert result is not None
        assert 3.0 <= result <= 10.0


# ===========================================================================
# LAYER 2 — tie-regression tests  (ceiling-rank prevention)
# ===========================================================================

def test_percentile_massive_tie_at_zero_not_inflated():
    """90 zeros + 10 non-zeros: player with 0 goals must NOT score near 10.0."""
    all_values = [0] * 90 + list(range(1, 11))
    result = percentile_to_rating(0, all_values)
    # mid-rank: lower=0, equal=90, rank=45/100=0.45 → 6.25
    assert result == pytest.approx(6.25)


def test_percentile_tie_all_zeros_returns_default():
    """All zeros → no-variation guard fires, returns 6.5."""
    assert percentile_to_rating(0, [0] * 100) == pytest.approx(6.5)


def test_percentile_tie_unique_top_and_zero_pool():
    """Single unique scorer above a pool of zeros: both get correct mid-rank."""
    all_values = [0] * 9 + [1]
    # value=1: lower=9, equal=1, rank=(9+0.5)/10=0.95 → 8.75
    assert percentile_to_rating(1, all_values) == pytest.approx(8.75)
    # value=0: lower=0, equal=9, rank=4.5/10=0.45 → 6.25
    assert percentile_to_rating(0, all_values) == pytest.approx(6.25)


def test_percentile_inverse_tie_at_zero_not_inflated():
    """GK with 0 goals_conceded in a mostly-clean-sheet pool: mid-rank, not 10.0."""
    all_values = [0] * 9 + [3]
    # inverse, value=0: lower=count(v>0)=1, equal=9, rank=(1+4.5)/10=0.55 → 6.75
    result = percentile_to_rating(0, all_values, inverse=True)
    assert result == pytest.approx(6.75)


def test_percentile_tied_middle_values_get_midpoint_rank():
    """Three players tied at 2 in [1,2,2,2,3] all receive the midpoint rank → 6.5."""
    all_values = [1, 2, 2, 2, 3]
    # lower=1, equal=3, rank=(1+1.5)/5=0.5 → 6.5
    assert percentile_to_rating(2, all_values) == pytest.approx(6.5)


def test_percentile_two_player_pool_no_ties():
    """Two-player pool (common in Layer 2 small leagues): correct mid-rank split."""
    all_values = [0, 10]
    # value=10: lower=1, equal=1, rank=1.5/2=0.75 → 7.75
    assert percentile_to_rating(10, all_values) == pytest.approx(7.75)
    # value=0: lower=0, equal=1, rank=0.5/2=0.25 → 5.25
    assert percentile_to_rating(0, all_values) == pytest.approx(5.25)


def test_percentile_all_same_nonzero_returns_default():
    """Non-zero constant pool still triggers the no-variation guard → 6.5."""
    assert percentile_to_rating(5.0, [5.0] * 50) == pytest.approx(6.5)


# ===========================================================================
# FULL ORACLE FLOW — Layer 1 → Layer 2 → Layer 3 fallback chain
# ===========================================================================

def _simple_fw_pool() -> list[dict]:
    return [
        {"position_group": "FW", "goals": 1},
        {"position_group": "FW", "goals": 10},
    ]


def test_oracle_uses_layer1_when_ratings_exist():
    """At least one non-None rating → Layer 1 source returned."""
    rating, source = compute_oracle_rating(
        match_ratings=[8.0],
        player_stats=None,
        position_group="FW",
        all_players_stats=_simple_fw_pool(),
    )
    assert source == "layer_1"
    assert rating == pytest.approx(8.0)


def test_oracle_falls_to_layer2_when_no_ratings():
    """match_ratings has entries but all None → Layer 2 composite used."""
    rating, source = compute_oracle_rating(
        match_ratings=[None, None],
        player_stats={"goals": 10},
        position_group="FW",
        all_players_stats=_simple_fw_pool(),
    )
    assert source == "layer_2"
    assert 3.0 <= rating <= 10.0


def test_oracle_falls_to_layer3_when_no_data():
    """No match entries and no stats → Layer 3 default 6.5."""
    rating, source = compute_oracle_rating(
        match_ratings=[],
        player_stats=None,
        position_group="FW",
        all_players_stats=_simple_fw_pool(),
    )
    assert source == "layer_3"
    assert rating == pytest.approx(6.5)


def test_oracle_layer1_takes_priority():
    """Mixed ratings (some None) → Layer 1 wins; Layer 2 stats are ignored."""
    # rated = [8.0, 7.0]; weights [0.35, 0.25], total 0.60
    expected_rating = (0.35 * 8.0 + 0.25 * 7.0) / 0.60  # ≈ 7.5833…

    rating, source = compute_oracle_rating(
        match_ratings=[8.0, None, 7.0],
        player_stats={"goals": 10},
        position_group="FW",
        all_players_stats=_simple_fw_pool(),
    )
    assert source == "layer_1"
    assert rating == pytest.approx(expected_rating)

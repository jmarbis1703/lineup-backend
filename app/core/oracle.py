"""
3-Layer Oracle Rating System — pure functions, no database / HTTP / Redis imports.

Layer 1  Recency-weighted average of Sportmonks match ratings (§4.1)
Layer 2  Position-weighted stat composite via percentile normalisation (§4.2)
Layer 3  Default 6.5 fallback (§4.3)
"""
import math  # noqa: F401 — imported for potential future use; no math calls needed here


# ---------------------------------------------------------------------------
# Position weight tables (§4.2)
# Tuple layout: (stat_key, weight, inverse)
# inverse=True → lower raw value is better (e.g. goals_conceded)
# ---------------------------------------------------------------------------

_POSITION_WEIGHTS: dict[str, list[tuple[str, float, bool]]] = {
    "FW": [
        ("goals",           0.25, False),
        ("shots_on_target", 0.20, False),
        ("xg",              0.20, False),
        ("key_passes",      0.15, False),
        ("dribbles_won",    0.10, False),
        ("minutes_played",  0.10, False),
    ],
    "MF": [
        ("goals",           0.15, False),
        ("shots_on_target", 0.10, False),
        ("xg",              0.15, False),
        ("key_passes",      0.15, False),
        ("dribbles_won",    0.10, False),
        ("pass_accuracy",   0.20, False),
        ("tackles",         0.15, False),
        ("minutes_played",  0.10, False),
    ],
    "DF": [
        ("goals",           0.05, False),
        ("key_passes",      0.05, False),
        ("pass_accuracy",   0.15, False),
        ("tackles",         0.25, False),
        ("interceptions",   0.20, False),
        ("clearances",      0.15, False),
        ("duels_won",       0.15, False),
        ("minutes_played",  0.10, False),
    ],
    "GK": [
        ("pass_accuracy",   0.15, False),
        ("saves",           0.30, False),
        ("clean_sheet",     0.25, False),
        ("goals_conceded",  0.20, True),
        ("minutes_played",  0.10, False),
    ],
}

_LAYER1_WEIGHTS: list[float] = [0.35, 0.25, 0.20, 0.12, 0.08]
_LAYER3_DEFAULT: float = 6.5


# ---------------------------------------------------------------------------
# §4.1  Layer 1 — recency-weighted average
# ---------------------------------------------------------------------------

def compute_layer1_rating(match_ratings: list[float]) -> float | None:
    """Recency-weighted average of up to 5 match ratings (most-recent first).

    Weights [0.35, 0.25, 0.20, 0.12, 0.08] are truncated to len(match_ratings)
    and renormalised so they always sum to 1.0.
    Returns None for an empty list.
    """
    if not match_ratings:
        return None
    weights = _LAYER1_WEIGHTS[: len(match_ratings)]
    total = sum(weights)
    return sum(w * r for w, r in zip(weights, match_ratings)) / total


# ---------------------------------------------------------------------------
# §4.2  Layer 2 helpers
# ---------------------------------------------------------------------------

def percentile_to_rating(
    value: float,
    all_values: list[float],
    inverse: bool = False,
) -> float:
    """Convert a raw stat value to a 4.0–9.0 rating via percentile rank.

    rank = fraction of all_values that are <= value  (or >= value if inverse).
    rating = 4.0 + rank * 5.0, clamped to [3.0, 10.0].

    Returns 6.5 when all_values is empty or contains no variation.
    """
    if not all_values or max(all_values) == min(all_values):
        return _LAYER3_DEFAULT
    n = len(all_values)
    equal = sum(1 for v in all_values if v == value)
    if inverse:
        lower = sum(1 for v in all_values if v > value)
    else:
        lower = sum(1 for v in all_values if v < value)
    rank = (lower + 0.5 * equal) / n
    return max(3.0, min(10.0, 4.0 + rank * 5.0))


def compute_layer2_composite(
    player_stats: dict,
    position_group: str,
    all_players_stats: list[dict],
) -> float | None:
    """Position-weighted stat composite for Layer 2 (§4.2).

    Each stat is normalised to 3.0–10.0 by percentile rank among all players
    with the same position_group in all_players_stats.  Stats that are missing
    or None for this player are skipped; their weight is excluded from the
    denominator.

    Returns None if:
    - position_group is not in the weight table, or
    - the sum of actually-used weights is exactly 0.0  (ZeroDivision guard §4.2)
    """
    stat_weights = _POSITION_WEIGHTS.get(position_group)
    if stat_weights is None:
        return None

    # Peer pool: all players sharing this position_group (for percentile context)
    peers = [
        p for p in all_players_stats if p.get("position_group") == position_group
    ]

    weighted_sum = 0.0
    used_weight = 0.0

    for stat, weight, inverse in stat_weights:
        raw = player_stats.get(stat)
        if raw is None:
            continue  # stat absent — exclude weight from denominator

        peer_values = [p[stat] for p in peers if p.get(stat) is not None]
        normalized = percentile_to_rating(raw, peer_values, inverse=inverse)
        weighted_sum += weight * normalized
        used_weight += weight

    # Zero-weight circuit breaker — must return None, NOT divide (§4.2 CRITICAL)
    if used_weight == 0.0:
        return None

    return weighted_sum / used_weight


# ---------------------------------------------------------------------------
# §4.4  Oracle computation — Layer 1 → Layer 2 → Layer 3 fallback chain
# ---------------------------------------------------------------------------

def compute_oracle_rating(
    match_ratings: list[float | None],
    player_stats: dict | None,
    position_group: str,
    all_players_stats: list[dict],
) -> tuple[float, str]:
    """Compute oracle rating with Layer 1 → Layer 2 → Layer 3 fallback (§4.4).

    Args:
        match_ratings:      Most-recent-first list of Sportmonks ratings.
                            None entries represent matches where no rating exists.
        player_stats:       Aggregated stat dict for this player, or None if
                            no match data exists at all.
        position_group:     One of 'FW', 'MF', 'DF', 'GK'.
        all_players_stats:  Stat dicts for all players (used for percentile rank).

    Returns:
        (rating, source) where source ∈ {'layer_1', 'layer_2', 'layer_3'}.
        rating is clamped to [3.0, 10.0] for layers 1–2; exactly 6.5 for layer 3.
    """
    # Layer 1: use recency-weighted average when at least one rating exists
    rated = [r for r in match_ratings if r is not None]
    if rated:
        return (compute_layer1_rating(rated), "layer_1")

    # Layer 2: match entries exist but all sportmonks_rating are NULL → stat composite
    has_match_entries = bool(match_ratings)
    if has_match_entries and player_stats is not None:
        composite = compute_layer2_composite(
            player_stats, position_group, all_players_stats
        )
        if composite is not None:
            return (composite, "layer_2")

    # Layer 3: no usable data — default midpoint
    return (_LAYER3_DEFAULT, "layer_3")

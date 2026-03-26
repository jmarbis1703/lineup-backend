"""
Redis key-value cache for volatility_tier.

Key schema : volatility_tier:{player_id}   (string → "low" | "medium" | "high")
TTL        : 21600 s (6 hours) — matches liquidity recalibration cadence
Write path : after /players or /trending bulk-computes full tier_map (fire-and-forget)
Read path  : /players/{id} reads cache first; on miss, queries full active population,
             computes tier_map, warms cache, returns tier for requested player

Failures are intentionally swallowed — a Redis outage must never abort a market read.
"""

from __future__ import annotations

import logging

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)

# Redis key prefix for all volatility tier cache entries
VOLATILITY_TIER_KEY_PREFIX = "volatility_tier"
# TTL of 6 hours matches the liquidity recalibration Celery task cadence;
# between recalibrations, relative b_effective rankings are stable.
VOLATILITY_TIER_TTL = 21600  # seconds


def _redis_key(player_id: int) -> str:
    return f"{VOLATILITY_TIER_KEY_PREFIX}:{player_id}"


def _get_redis() -> aioredis.Redis:
    # Mirrors redis_pubsub._get_redis() — per-call connection, no shared pool
    return aioredis.from_url(settings.redis_url, decode_responses=True)


async def write_volatility_tier_cache(
    tier_map: dict[int, str],
    redis_url: str,
) -> None:
    """Write all player volatility tiers to Redis using a pipeline batch.

    Args:
        tier_map: {player_id: tier_string} as returned by compute_volatility_tiers().
        redis_url: Accepted for call-site clarity; connection uses settings.redis_url.

    Failures are swallowed — a Redis write failure must never abort a market read
    that has already completed its DB queries. Called via asyncio.create_task()
    (fire-and-forget) from bulk endpoints after tier_map is computed.
    """
    if not tier_map:
        return
    r = _get_redis()
    try:
        async with r.pipeline(transaction=False) as pipe:
            for player_id, tier in tier_map.items():
                # SET key value EX ttl — pipeline batches all commands in one round-trip
                pipe.set(_redis_key(player_id), tier, ex=VOLATILITY_TIER_TTL)
            await pipe.execute()
    except Exception:
        # Log and continue — Redis outage must not propagate to the caller
        logger.warning(
            "redis_cache: write_volatility_tier_cache failed — continuing without cache",
            exc_info=True,
        )
    finally:
        await r.aclose()


async def get_volatility_tier_from_cache(
    player_id: int,
    redis_url: str,
) -> str | None:
    """Read a single player's volatility tier from Redis.

    Args:
        player_id: The integer player ID to look up.
        redis_url: Accepted for call-site clarity; connection uses settings.redis_url.

    Returns:
        The cached tier string ("low" | "medium" | "high") on hit, or None on
        cache miss or any Redis error. Callers must handle None by falling back
        to full b_eff computation via compute_volatility_tiers().
    """
    r = _get_redis()
    try:
        # Redis GET returns None when the key does not exist — correct miss signal
        return await r.get(_redis_key(player_id))
    except Exception:
        # Redis unavailable — return None so caller falls back to DB computation
        logger.warning(
            "redis_cache: get_volatility_tier_from_cache(%s) failed — treating as cache miss",
            player_id,
            exc_info=True,
        )
        return None
    finally:
        await r.aclose()

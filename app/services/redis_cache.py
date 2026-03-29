"""
Redis key-value cache for volatility tiers and LMSR b floor.

Volatility tier cache:
  Key schema : volatility_tier:{player_id}   (string → "low" | "medium" | "high")
  TTL        : 21600 s (6 hours) — matches liquidity recalibration cadence
  Write path : after /players or /trending bulk-computes full tier_map (fire-and-forget)
  Read path  : /players/{id} reads cache first; on miss, queries full active population,
               computes tier_map, warms cache, returns tier for requested player

LMSR b floor cache (§market-stability):
  Key schema : user_count_cache  (int — total distinct portfolio users)
  Key schema : lmsr_b_floor      (float — current dynamic b floor)
  TTL        : 600 s (10 minutes) for both
  Write path : refresh_b_floor() called by Celery task every hour; also lazily on miss
  Read path  : get_b_floor() at the top of every trade, before any DB locks
  Fallback   : Redis down → DB portfolio count; DB down → settings.lmsr_n_min

Failures are intentionally swallowed — a Redis outage must never abort a trade.
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


# ---------------------------------------------------------------------------
# LMSR b floor cache — §market-stability
# ---------------------------------------------------------------------------

REDIS_KEY_USER_COUNT = "user_count_cache"
REDIS_KEY_B_FLOOR = "lmsr_b_floor"
USER_COUNT_TTL = 600   # 10 minutes
B_FLOOR_TTL = 600      # 10 minutes


async def get_cached_user_count(
    redis_url: str,
    db_session,
) -> int:
    """Return the active user count, reading from Redis cache.

    Falls back to COUNT(DISTINCT user_id) FROM portfolios on cache miss.
    Falls back to settings.lmsr_n_min if DB also fails.

    Args:
        redis_url: Accepted for call-site clarity; connection uses settings.redis_url.
        db_session: AsyncSession — used only on cache miss for the DB fallback query.
    """
    r = _get_redis()
    cached = None
    try:
        val = await r.get(REDIS_KEY_USER_COUNT)
        if val is not None:
            cached = int(val)
    except Exception:
        logger.warning(
            "redis_cache: get_cached_user_count — Redis read failed, querying DB",
            exc_info=True,
        )
    finally:
        await r.aclose()

    if cached is not None:
        return cached

    # Cache miss — query DB
    try:
        import sqlalchemy as sa
        result = await db_session.execute(
            sa.text("SELECT COUNT(DISTINCT user_id) FROM portfolios")
        )
        count = int(result.scalar() or 0)
        # Rehydrate cache
        r2 = _get_redis()
        try:
            await r2.set(REDIS_KEY_USER_COUNT, str(count), ex=USER_COUNT_TTL)
        except Exception:
            pass
        finally:
            await r2.aclose()
        return count
    except Exception:
        logger.warning(
            "redis_cache: get_cached_user_count — DB fallback failed, using lmsr_n_min",
            exc_info=True,
        )
        return settings.lmsr_n_min


async def get_b_floor(
    redis_url: str,
    db_session,
) -> float:
    """Return the current LMSR b floor value from Redis.

    On cache miss, recomputes B_FLOOR from the current user count and
    rewrites the cache key.  Falls back to settings.lmsr_b_base (the
    full-scale anchor) on all failures — the market remains maximally
    stable rather than maximally volatile.

    Called once at the TOP of execute_buy / execute_sell / preview_buy,
    BEFORE any FOR UPDATE locks, so the b value is fixed for the
    duration of the trade (preserving LMSR budget-balance).

    Args:
        redis_url: Accepted for call-site clarity; connection uses settings.redis_url.
        db_session: AsyncSession — threaded through for the user-count DB fallback.
    """
    from app.core.lmsr import b_floor_for_users

    r = _get_redis()
    cached = None
    try:
        val = await r.get(REDIS_KEY_B_FLOOR)
        if val is not None:
            cached = float(val)
    except Exception:
        logger.warning(
            "redis_cache: get_b_floor — Redis read failed, recomputing",
            exc_info=True,
        )
    finally:
        await r.aclose()

    if cached is not None:
        return cached

    # Cache miss — recompute from user count
    n_users = await get_cached_user_count(redis_url, db_session)
    floor = b_floor_for_users(
        n_users=n_users,
        b_base=settings.lmsr_b_base,
        n_target=settings.lmsr_n_target,
        n_min=settings.lmsr_n_min,
    )
    r2 = _get_redis()
    try:
        await r2.set(REDIS_KEY_B_FLOOR, str(floor), ex=B_FLOOR_TTL)
    except Exception:
        pass
    finally:
        await r2.aclose()
    return floor


async def refresh_b_floor(
    redis_url: str,
    db_session,
) -> float:
    """Force-recompute and recache the LMSR b floor.

    Invalidates the user count cache first so a fresh DB count is used.
    Called by the Celery Beat task every hour.

    Returns the newly computed floor value for logging.
    """
    from app.core.lmsr import b_floor_for_users

    # Invalidate user count so next get_cached_user_count hits the DB
    r = _get_redis()
    try:
        await r.delete(REDIS_KEY_USER_COUNT)
    except Exception:
        pass
    finally:
        await r.aclose()

    n_users = await get_cached_user_count(redis_url, db_session)
    floor = b_floor_for_users(
        n_users=n_users,
        b_base=settings.lmsr_b_base,
        n_target=settings.lmsr_n_target,
        n_min=settings.lmsr_n_min,
    )
    r2 = _get_redis()
    try:
        await r2.set(REDIS_KEY_B_FLOOR, str(floor), ex=B_FLOOR_TTL)
    except Exception:
        pass
    finally:
        await r2.aclose()

    logger.info("redis_cache: B_FLOOR refreshed — n_users=%d, floor=%.2f", n_users, floor)
    return floor


# ---------------------------------------------------------------------------
# Volatility tier cache
# ---------------------------------------------------------------------------

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

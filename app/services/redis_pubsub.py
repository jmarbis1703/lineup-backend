"""
Redis Pub/Sub service for real-time rating updates.

Channel: lineup:rating_updates

Message schema:
{
  "type": "rating_update",
  "player_id": <int>,
  "rating_before": "<Decimal as string>",
  "rating_after": "<Decimal as string>",
  "direction": "UP" | "DOWN",
  "timestamp": "<ISO-8601 UTC>"
}
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import redis.asyncio as aioredis

from app.config import settings

CHANNEL = "lineup:rating_updates"


def _get_redis() -> aioredis.Redis:
    return aioredis.from_url(settings.redis_url, decode_responses=True)


async def publish_rating_update(
    player_id: int,
    rating_before: Decimal,
    rating_after: Decimal,
    direction: str,
) -> None:
    """Publish a rating_update event to the global channel.

    Failures are intentionally swallowed — a Redis outage must never
    abort a completed trade.
    """
    message = json.dumps(
        {
            "type": "rating_update",
            "player_id": player_id,
            "rating_before": str(rating_before),
            "rating_after": str(rating_after),
            "direction": direction,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    r = _get_redis()
    try:
        await r.publish(CHANNEL, message)
    finally:
        await r.aclose()


async def subscribe():
    """Async generator yielding raw JSON strings from lineup:rating_updates.

    Cleans up the Redis connection on generator exit (break / exception /
    task cancellation).
    """
    r = _get_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe(CHANNEL)
    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                yield message["data"]
    finally:
        await pubsub.unsubscribe(CHANNEL)
        await r.aclose()

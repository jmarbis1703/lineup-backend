"""
WebSocket endpoint: /ws/market
Ticket issuance:   POST /api/ws/ticket

SEC-08: JWT is no longer passed as a URL query parameter to avoid credential
logging by proxies. Instead, the client first POSTs to /api/ws/ticket (with
its Bearer JWT) to obtain a short-lived opaque ticket, then opens the WebSocket
with ?ticket=<hex>. The ticket is single-use and expires in 30 seconds.

Close code 4401 = missing, invalid, or expired ticket.
"""
from __future__ import annotations

import asyncio
import secrets

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect

from app.config import settings
from app.core.rate_limit import _get_user_id_key, limiter
from app.dependencies import get_current_user
from app.models.user import User
from app.services.redis_pubsub import subscribe

router = APIRouter()
http_router = APIRouter()

_WS_TICKET_PREFIX = "ws_ticket"
_WS_TICKET_TTL = 30  # seconds — client must connect within this window


def _get_redis() -> aioredis.Redis:
    return aioredis.from_url(settings.redis_url, decode_responses=True)


# ---------------------------------------------------------------------------
# SEC-08 Step A — Ticket issuance endpoint
# SEC-09 — Rate limited: 60 per minute per authenticated user
# ---------------------------------------------------------------------------

@http_router.post("/ticket")
@limiter.limit("60/minute", key_func=_get_user_id_key)
async def issue_ws_ticket(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> dict:
    """
    Issue a one-time WebSocket ticket valid for 30 seconds.

    The client must open the WebSocket connection within this window using
    ?ticket=<hex>. The ticket is deleted from Redis on first use.
    """
    ticket = secrets.token_hex(32)
    r = _get_redis()
    try:
        await r.set(
            f"{_WS_TICKET_PREFIX}:{ticket}",
            str(current_user.id),
            ex=_WS_TICKET_TTL,
        )
    finally:
        await r.aclose()
    return {"ticket": ticket}


# ---------------------------------------------------------------------------
# SEC-08 Step B — WebSocket market stream (ticket-authenticated)
# ---------------------------------------------------------------------------

@router.websocket("/ws/market")
async def ws_market(websocket: WebSocket) -> None:
    # ------------------------------------------------------------------
    # 1. Ticket authentication — replaces ?token= JWT in URL (SEC-08)
    # ------------------------------------------------------------------
    ticket = websocket.query_params.get("ticket")
    if not ticket:
        await websocket.close(code=4401)
        return

    r = _get_redis()
    user_id: str | None = None
    try:
        user_id = await r.get(f"{_WS_TICKET_PREFIX}:{ticket}")
        if user_id:
            # One-time use: delete immediately after lookup
            await r.delete(f"{_WS_TICKET_PREFIX}:{ticket}")
    except Exception:
        user_id = None
    finally:
        await r.aclose()

    if not user_id:
        await websocket.close(code=4401)
        return

    await websocket.accept()

    # ------------------------------------------------------------------
    # 2. Forward Redis messages to this client in a background task
    # ------------------------------------------------------------------
    async def _forward() -> None:
        async for msg in subscribe():
            await websocket.send_text(msg)

    forward = asyncio.create_task(_forward())

    # ------------------------------------------------------------------
    # 3. Block until client disconnects, then cancel the forward task
    # ------------------------------------------------------------------
    try:
        while True:
            frame = await websocket.receive()
            if frame["type"] == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    finally:
        forward.cancel()
        try:
            await forward
        except (asyncio.CancelledError, Exception):
            pass

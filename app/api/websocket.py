"""
WebSocket endpoint: /ws/market

Streams all lineup:rating_updates events to authenticated clients.

Authentication: JWT must be supplied as ?token=<jwt> query parameter.
Close code 4401 = missing or invalid token (4xxx = application-level close).
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from jose import JWTError

from app.core.auth import decode_jwt
from app.services.redis_pubsub import subscribe

router = APIRouter()


@router.websocket("/ws/market")
async def ws_market(websocket: WebSocket) -> None:
    # ------------------------------------------------------------------
    # 1. JWT authentication via query param
    # ------------------------------------------------------------------
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4401)
        return
    try:
        decode_jwt(token)
    except JWTError:
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

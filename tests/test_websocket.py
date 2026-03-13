"""
WebSocket + Redis Pub/Sub tests.

WS tests use starlette.testclient.TestClient (synchronous) so they work
alongside pytest-asyncio auto mode without conflict.

Redis is mocked throughout — no real Redis instance required.
"""
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.auth import create_jwt
from app.main import app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_USER_ID = "00000000-0000-0000-0000-000000000001"


def _valid_token() -> str:
    return create_jwt(_FAKE_USER_ID)


def _make_subscribe(*messages: str):
    """Return a zero-arg callable that yields *messages* as an async generator."""

    def _factory():
        async def _gen():
            for msg in messages:
                yield msg

        return _gen()

    return _factory


# ---------------------------------------------------------------------------
# test_ws_connect_valid_token
# ---------------------------------------------------------------------------


def test_ws_connect_valid_token():
    """A valid JWT results in a successful connection (not rejected with 4401)."""
    token = _valid_token()
    payload = json.dumps({"type": "ping"})

    with patch("app.api.websocket.subscribe", _make_subscribe(payload)):
        client = TestClient(app)
        with client.websocket_connect(f"/ws/market?token={token}") as ws:
            received = ws.receive_text()
        # If we reach here the connection was accepted — verify message came through
        assert json.loads(received)["type"] == "ping"


# ---------------------------------------------------------------------------
# test_ws_no_token_rejected
# ---------------------------------------------------------------------------


def test_ws_no_token_rejected():
    """Missing ?token= causes immediate close with code 4401."""
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/market"):
            pass  # server closes before we can do anything
    assert exc_info.value.code == 4401


def test_ws_invalid_token_rejected():
    """Malformed JWT causes close with code 4401."""
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/market?token=not.a.jwt"):
            pass
    assert exc_info.value.code == 4401


# ---------------------------------------------------------------------------
# test_ws_receives_trade_update
# ---------------------------------------------------------------------------


def test_ws_receives_trade_update():
    """WS endpoint forwards messages from subscribe() verbatim to the client."""
    token = _valid_token()
    update = {
        "type": "rating_update",
        "player_id": 42,
        "rating_before": "7.0000",
        "rating_after": "7.1500",
        "direction": "UP",
        "timestamp": "2026-03-09T12:00:00+00:00",
    }

    with patch("app.api.websocket.subscribe", _make_subscribe(json.dumps(update))):
        client = TestClient(app)
        with client.websocket_connect(f"/ws/market?token={token}") as ws:
            raw = ws.receive_text()

    received = json.loads(raw)
    assert received == update


# ---------------------------------------------------------------------------
# test_ws_message_format
# ---------------------------------------------------------------------------


async def test_ws_message_format():
    """publish_rating_update sends correctly-shaped JSON to Redis."""
    from app.services.redis_pubsub import publish_rating_update

    mock_redis = AsyncMock()
    mock_redis.publish = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with patch("app.services.redis_pubsub._get_redis", return_value=mock_redis):
        await publish_rating_update(
            player_id=99,
            rating_before=Decimal("7.0000"),
            rating_after=Decimal("7.1500"),
            direction="UP",
        )

    mock_redis.publish.assert_called_once()
    channel, raw_msg = mock_redis.publish.call_args[0]

    assert channel == "lineup:rating_updates"

    data = json.loads(raw_msg)
    assert data["type"] == "rating_update"
    assert data["player_id"] == 99
    assert data["rating_before"] == "7.0000"
    assert data["rating_after"] == "7.1500"
    assert data["direction"] == "UP"
    assert "timestamp" in data
    # timestamp must be a non-empty ISO string
    assert len(data["timestamp"]) > 10

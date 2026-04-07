"""
Trade API endpoints: /api/trade/buy, /api/trade/sell, /api/trade/preview.

EXPLICIT PROHIBITION (PRD §5.2): These endpoints MUST NOT query `tournaments`
or `tournament_members`. A completed custom tournament MUST NEVER block or halt
a user from executing trades in the global market.
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rate_limit import _get_user_id_key, limiter
from app.services import redis_pubsub

from app.core.trading import (
    DustBufferError,
    InactivePlayerError,
    InsufficientPointsError,
    InsufficientSharesError,
    MarketNotFoundError,
    PlayerNotFoundError,
    ZeroSharesError,
    execute_buy,
    execute_sell,
    preview_buy,
)
from app.dependencies import get_current_user, get_db
from app.schemas.trade import (
    BuyRequest,
    BuyResponse,
    PreviewRequest,
    PreviewResponse,
    SellRequest,
    SellResponse,
)

router = APIRouter()


@router.post("/buy", response_model=BuyResponse)
@limiter.limit("30/minute", key_func=_get_user_id_key)
async def buy(
    request: Request,
    req: BuyRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Execute a budget-based buy.

    - 403 if player is inactive (close-only mode).
    - 400 if insufficient available_points.
    - 404 if player or market not found.
    """
    try:
        result = await execute_buy(
            db, current_user.id, req.player_id, req.direction, req.budget
        )
        await db.commit()
        asyncio.create_task(
            redis_pubsub.publish_rating_update(
                req.player_id, result["rating_before"], result["rating_after"], req.direction
            )
        )
        return result
    except InactivePlayerError as exc:
        # No DB writes occurred — no rollback needed
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    except InsufficientPointsError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except ZeroSharesError as exc:
        # Budget accepted by Pydantic (> 0) but too small to allocate any shares
        # at the current market price. No DB writes occurred.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except PlayerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except MarketNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/sell", response_model=SellResponse)
@limiter.limit("30/minute", key_func=_get_user_id_key)
async def sell(
    request: Request,
    req: SellRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Execute a share-quantity sell.

    CRITICAL — Close-Only Mode: is_active is NOT checked here.
    Selling shares in an inactive player MUST always be permitted (§5.2).

    - 400 if insufficient shares or Dust Buffer violation.
    """
    try:
        result = await execute_sell(
            db, current_user.id, req.player_id, req.direction, req.shares
        )
        await db.commit()
        asyncio.create_task(
            redis_pubsub.publish_rating_update(
                req.player_id, result["rating_before"], result["rating_after"], req.direction
            )
        )
        return result
    except (InsufficientSharesError, DustBufferError) as exc:
        # No DB writes occurred — no rollback needed
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except MarketNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/preview", response_model=PreviewResponse)
@limiter.limit("30/minute", key_func=_get_user_id_key)
async def preview(
    request: Request,
    req: PreviewRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Preview a buy without executing it (no DB writes, no locks).
    Returns projected shares, cost, and rating_after.

    Rate-limited at 30/minute per user (same as buy/sell) to prevent
    excessive DB queries from repeated preview calls.
    """
    try:
        result = await preview_buy(db, req.player_id, req.direction, req.budget)
        return result
    except MarketNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

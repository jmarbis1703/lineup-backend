"""Clerk webhook handler — Svix-verified event stream."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from svix.webhooks import Webhook, WebhookVerificationError

from app.config import settings
from app.dependencies import get_db
from app.models.portfolio import Portfolio
from app.models.user import User

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@router.post("/clerk", status_code=204)
async def clerk_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> None:
    payload = await request.body()
    headers = dict(request.headers)
    try:
        wh = Webhook(settings.clerk_webhook_secret)
        event = wh.verify(payload, headers)
    except WebhookVerificationError:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event_type = event.get("type")
    data = event.get("data", {})

    if event_type == "user.created":
        await _upsert_user(db, data, create_portfolio=True)
    elif event_type == "user.updated":
        await _upsert_user(db, data, create_portfolio=False)
    elif event_type == "user.deleted":
        await _delete_user(db, data)


async def _upsert_user(db: AsyncSession, data: dict, create_portfolio: bool) -> None:
    clerk_id: str = data["id"]
    email: str = data["email_addresses"][0]["email_address"]
    username: str = data.get("username") or data["id"]

    stmt = (
        pg_insert(User)
        .values(
            id=uuid.uuid4(),
            clerk_id=clerk_id,
            email=email,
            username=username,
            created_at=datetime.now(timezone.utc),
        )
        .on_conflict_do_update(
            index_elements=["clerk_id"],
            set_={"email": email, "username": username},
        )
    )
    await db.execute(stmt)
    await db.flush()

    if create_portfolio:
        result = await db.execute(sa.select(User).where(User.clerk_id == clerk_id))
        user = result.scalar_one()
        existing = await db.execute(
            sa.select(Portfolio).where(Portfolio.user_id == user.id)
        )
        if existing.scalar_one_or_none() is None:
            db.add(
                Portfolio(
                    user_id=user.id,
                    available_points=Decimal("1000.0000"),
                )
            )

    await db.commit()


async def _delete_user(db: AsyncSession, data: dict) -> None:
    clerk_id: str = data["id"]
    await db.execute(sa.delete(User).where(User.clerk_id == clerk_id))
    await db.commit()

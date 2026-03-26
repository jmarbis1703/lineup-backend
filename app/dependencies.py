from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from decimal import Decimal

import sqlalchemy as sa
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt as jose_jwt
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

# Module-level engine + session factory created once at import time.
# pool_pre_ping=True drops stale connections silently on checkout.
_engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(_engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields a transactional AsyncSession per request."""
    async with AsyncSessionLocal() as session:
        yield session


_bearer = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
):
    """
    FastAPI dependency that validates a Clerk Bearer JWT and returns the User row.

    If the JWT is valid but no DB row exists yet (e.g. Clerk webhook missed/delayed),
    the user is auto-provisioned so the first authenticated request always succeeds.
    The webhook handler will upsert email/username when it fires.

    Raises HTTP 401 only for missing or cryptographically invalid tokens.
    """
    from app.core.auth import verify_clerk_jwt  # noqa: PLC0415
    from app.models.portfolio import Portfolio  # noqa: PLC0415
    from app.models.user import User  # noqa: PLC0415

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        clerk_id = await verify_clerk_jwt(credentials.credentials)
    except Exception:
        raise credentials_exception

    result = await db.execute(sa.select(User).where(User.clerk_id == clerk_id))
    user = result.scalar_one_or_none()

    if user is None:
        # JWT is valid but the Clerk webhook hasn't created the row yet.
        # Extract whatever claims are available; webhook will fill in real values later.
        claims = jose_jwt.get_unverified_claims(credentials.credentials)
        email: str = claims.get("email") or f"{clerk_id}@clerk.local"
        username: str = claims.get("username") or clerk_id

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

        result = await db.execute(sa.select(User).where(User.clerk_id == clerk_id))
        user = result.scalar_one()

        # Provision a starting portfolio if none exists.
        # AUTH-2: Use ON CONFLICT DO NOTHING to be idempotent under concurrent first-requests.
        await db.execute(
            pg_insert(Portfolio)
            .values(user_id=user.id, available_points=Decimal("1000.0000"))
            .on_conflict_do_nothing(index_elements=["user_id"])
        )

        await db.commit()

    return user

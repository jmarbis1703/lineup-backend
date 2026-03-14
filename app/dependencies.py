from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import sqlalchemy as sa
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
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
    FastAPI dependency that validates a Bearer JWT and returns the User row.

    Raises HTTP 401 for missing/invalid/expired tokens or unknown users.
    """
    # Import here to avoid circular imports at module load time
    from app.core.auth import decode_jwt  # noqa: PLC0415
    from app.models.user import User  # noqa: PLC0415

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        user_id_str = decode_jwt(credentials.credentials)
    except JWTError:
        raise credentials_exception

    try:
        user_id = uuid.UUID(user_id_str)
    except (ValueError, AttributeError):
        raise credentials_exception

    result = await db.execute(sa.select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise credentials_exception

    return user

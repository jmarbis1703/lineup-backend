"""Auth API: POST /api/auth/register, POST /api/auth/login."""
from __future__ import annotations

from decimal import Decimal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import create_jwt, hash_password, verify_password
from app.dependencies import get_db
from app.models.portfolio import Portfolio
from app.models.user import User
from app.schemas.auth import AuthResponse, LoginRequest, RegisterRequest

router = APIRouter()


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> AuthResponse:
    """Create a new user with a single 1000-point portfolio."""
    # Duplicate e-mail check
    existing = await db.execute(sa.select(User).where(User.email == body.email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Email already registered")

    # Duplicate username check
    existing = await db.execute(sa.select(User).where(User.username == body.username))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Username already taken")

    # Create user
    user = User(
        email=body.email,
        username=body.username,
        password_hash=hash_password(body.password),
    )
    db.add(user)
    await db.flush()  # populate user.id before portfolio FK

    # Create exactly ONE global portfolio with 1000 points
    portfolio = Portfolio(
        user_id=user.id,
        available_points=Decimal("1000.0000"),
    )
    db.add(portfolio)
    await db.commit()

    token = create_jwt(str(user.id))
    return AuthResponse(
        token=token,
        user_id=user.id,
        username=user.username,
        available_points=portfolio.available_points,
    )


@router.post("/login", response_model=AuthResponse)
async def login(
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> AuthResponse:
    """Authenticate with e-mail + password, return JWT."""
    result = await db.execute(sa.select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    port_result = await db.execute(
        sa.select(Portfolio).where(Portfolio.user_id == user.id)
    )
    portfolio = port_result.scalar_one()

    token = create_jwt(str(user.id))
    return AuthResponse(
        token=token,
        user_id=user.id,
        username=user.username,
        available_points=portfolio.available_points,
    )

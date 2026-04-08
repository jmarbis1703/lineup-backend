import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rate_limit import limiter
from app.dependencies import get_db
from app.models.waitlist import WaitlistSignup
from app.schemas.waitlist import WaitlistCountResponse, WaitlistJoinRequest, WaitlistJoinResponse

router = APIRouter()


@router.post("/join", response_model=WaitlistJoinResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def join_waitlist(
    request: Request,
    body: WaitlistJoinRequest,
    db: AsyncSession = Depends(get_db),
) -> WaitlistJoinResponse:
    """Public endpoint — no auth required.

    Adds an email to the waitlist. Returns the assigned queue position,
    a unique referral code, and the current total waitlist count.

    409 is returned if the email is already registered.
    If referred_by is provided and maps to a valid referral_code, the
    referrer moves up 10 spots (flat) and their referral_count increments.
    An invalid referred_by code is silently ignored.
    """
    # Duplicate check
    existing = await db.execute(
        select(WaitlistSignup).where(WaitlistSignup.email == str(body.email))
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Already on the list",
        )

    # Assign position = current count + 1
    count_result = await db.execute(select(func.count()).select_from(WaitlistSignup))
    current_count: int = count_result.scalar_one()
    position = current_count + 1

    # Handle referral
    if body.referred_by:
        referrer_result = await db.execute(
            select(WaitlistSignup).where(WaitlistSignup.referral_code == body.referred_by)
        )
        referrer = referrer_result.scalar_one_or_none()
        if referrer is not None:
            referrer.referral_count += 1
            referrer.position = max(1, referrer.position - 10)

    # Insert new signup
    new_signup = WaitlistSignup(
        email=str(body.email),
        referral_code=str(uuid.uuid4()),
        referred_by=body.referred_by,
        position=position,
        referral_count=0,
    )
    db.add(new_signup)
    await db.commit()
    await db.refresh(new_signup)

    # Return count after insert
    count_after_result = await db.execute(select(func.count()).select_from(WaitlistSignup))
    waitlist_count: int = count_after_result.scalar_one()

    return WaitlistJoinResponse(
        email=new_signup.email,
        position=new_signup.position,
        referral_code=new_signup.referral_code,
        waitlist_count=waitlist_count,
    )


@router.get("/count", response_model=WaitlistCountResponse)
async def get_waitlist_count(
    db: AsyncSession = Depends(get_db),
) -> WaitlistCountResponse:
    """Public endpoint — no auth required. Returns the current waitlist size."""
    result = await db.execute(select(func.count()).select_from(WaitlistSignup))
    count: int = result.scalar_one()
    return WaitlistCountResponse(count=count)

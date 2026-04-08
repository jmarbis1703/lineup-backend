from pydantic import BaseModel, EmailStr


class WaitlistJoinRequest(BaseModel):
    email: EmailStr
    referred_by: str | None = None


class WaitlistJoinResponse(BaseModel):
    email: str
    position: int
    referral_code: str
    waitlist_count: int

    model_config = {"from_attributes": True}


class WaitlistCountResponse(BaseModel):
    count: int

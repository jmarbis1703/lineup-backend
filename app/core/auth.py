"""Authentication helpers: password hashing and JWT creation/decoding."""
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """Return bcrypt hash of *password*."""
    return _pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if *plain* matches *hashed*."""
    return _pwd_context.verify(plain, hashed)


def create_jwt(user_id: str) -> str:
    """Mint a signed JWT with *user_id* as the ``sub`` claim."""
    exp = datetime.now(timezone.utc) + timedelta(
        minutes=settings.jwt_expiration_minutes
    )
    return jwt.encode(
        {"sub": user_id, "exp": exp},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def decode_jwt(token: str) -> str:
    """
    Decode and verify *token*.  Returns the ``sub`` (user_id string).
    Raises ``jose.JWTError`` on any failure (expired, bad signature, etc.).
    """
    payload = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
    )
    sub = payload.get("sub")
    if sub is None:
        raise JWTError("Missing 'sub' claim")
    return sub

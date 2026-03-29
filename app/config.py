from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database — required, no default (CFG-1: fail fast if missing)
    database_url: str

    # Redis — safe local default
    redis_url: str = "redis://localhost:6379/0"

    # Clerk — required fields raise ValidationError at startup if absent (CFG-1)
    clerk_jwks_url: str
    clerk_webhook_secret: str
    clerk_audience: str = ""  # optional — only needed for audience-restricted tokens

    # Sportmonks — required (CFG-1)
    sportmonks_api_token: str
    sportmonks_base_url: str = "https://api.sportmonks.com/v3/football"

    # LMSR dynamic b floor — controls market stability at low user counts.
    # b_effective = max(B_FLOOR(n_users), b_min + alpha * shares)
    # B_FLOOR = lmsr_b_base * max(1, lmsr_n_target / max(lmsr_n_min, n_users))
    # At n_users=lmsr_n_target the floor equals lmsr_b_base (anchor point).
    # Override via env vars LMSR_B_BASE, LMSR_N_TARGET, LMSR_N_MIN.
    lmsr_b_base: float = 10000.0   # target b at full scale (< 1% impact per 100pt trade)
    lmsr_n_target: int = 100       # user count at which floor = b_base
    lmsr_n_min: int = 5            # minimum user count (prevents division explosion)


settings = Settings()

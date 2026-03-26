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


settings = Settings()

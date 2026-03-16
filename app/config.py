from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database
    database_url: str = "postgresql+asyncpg://lineup:lineup_secret@localhost:5432/lineup"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Clerk
    clerk_jwks_url: str = ""
    clerk_webhook_secret: str = ""
    clerk_audience: str = ""

    # Sportmonks
    sportmonks_api_token: str = ""
    sportmonks_base_url: str = "https://api.sportmonks.com/v3/football"


settings = Settings()

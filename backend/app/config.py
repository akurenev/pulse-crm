from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded exclusively from PULSE_* environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="PULSE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Pulse CRM"
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://pulse:pulse@localhost:5432/pulse"
    secret_key: str = "development-only-change-this-secret-key"
    # Disabled unless explicitly enabled for the one-time owner bootstrap.
    # Keeping a development token as a model default would silently re-enable
    # the endpoint after the production variable is removed.
    bootstrap_token: str | None = None
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "testserver"]
    )
    cookie_secure: bool = False
    session_ttl_hours: int = Field(default=24 * 7, ge=1, le=24 * 90)
    job_runner_enabled: bool = True
    job_runner_poll_seconds: float = Field(default=1.0, ge=0.1, le=30)
    job_runner_heartbeat_timeout_seconds: float = Field(default=30.0, ge=5, le=300)
    static_dir: Path = Path("/app/backend/static")
    integration_encryption_key: str | None = None
    integration_encryption_key_id: str = "primary"
    s3_endpoint_url: str | None = None
    s3_region: str = "ru-1"
    s3_bucket: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None

    @model_validator(mode="after")
    def validate_production_secrets(self) -> Settings:
        if self.environment == "production":
            if len(self.secret_key) < 32 or self.secret_key.startswith("development-"):
                raise ValueError("PULSE_SECRET_KEY must be a strong production secret")
            if self.bootstrap_token is not None and (
                len(self.bootstrap_token) < 24 or self.bootstrap_token.startswith("development-")
            ):
                raise ValueError("PULSE_BOOTSTRAP_TOKEN must be a strong production secret")
            if not self.cookie_secure:
                raise ValueError("PULSE_COOKIE_SECURE must be enabled in production")
            if not self.integration_encryption_key:
                raise ValueError(
                    "PULSE_INTEGRATION_ENCRYPTION_KEY must be configured in production"
                )
            required_s3 = {
                "PULSE_S3_BUCKET": self.s3_bucket,
                "PULSE_S3_ACCESS_KEY_ID": self.s3_access_key_id,
                "PULSE_S3_SECRET_ACCESS_KEY": self.s3_secret_access_key,
            }
            missing_s3 = [name for name, value in required_s3.items() if not value]
            if missing_s3:
                raise ValueError("production S3 settings are incomplete: " + ", ".join(missing_s3))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

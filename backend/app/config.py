from __future__ import annotations

import base64
import binascii
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from email_validator import EmailNotValidError, validate_email
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded exclusively from PULSE_* environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="PULSE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
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
    # CRM data export is a privileged, opt-in capability.  Keeping the server
    # switch off by default prevents a future route or UI control from
    # accidentally enabling bulk extraction.
    crm_export_enabled: bool = False
    # Continued cursor traversal is intentionally bounded.  The first page of
    # every list remains free so normal dashboards and polling are unaffected;
    # only follow-up pages consume this per-user, per-resource budget.
    cursor_page_budget: int = Field(default=20, ge=1, le=1_000)
    cursor_page_window_seconds: int = Field(default=15 * 60, ge=60, le=24 * 60 * 60)
    job_runner_enabled: bool = True
    job_runner_poll_seconds: float = Field(default=1.0, ge=0.1, le=30)
    job_runner_heartbeat_timeout_seconds: float = Field(default=30.0, ge=5, le=300)
    static_dir: Path = Path("/app/backend/static")
    integration_encryption_key: str | None = None
    integration_encryption_key_id: str = "primary"
    web_push_vapid_public_key: str | None = None
    web_push_vapid_private_key: str | None = None
    web_push_vapid_subject: str | None = None
    s3_endpoint_url: str | None = None
    s3_region: str = "ru-1"
    s3_bucket: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None

    @model_validator(mode="after")
    def validate_production_secrets(self) -> Settings:
        vapid_values = (
            self.web_push_vapid_public_key,
            self.web_push_vapid_private_key,
            self.web_push_vapid_subject,
        )
        if any(value is not None for value in vapid_values) and not all(
            value is not None and value.strip() for value in vapid_values
        ):
            raise ValueError(
                "PULSE_WEB_PUSH_VAPID_PUBLIC_KEY, "
                "PULSE_WEB_PUSH_VAPID_PRIVATE_KEY and "
                "PULSE_WEB_PUSH_VAPID_SUBJECT must be configured together"
            )
        if self.web_push_vapid_subject:
            self._validate_vapid_subject(self.web_push_vapid_subject)
        if self.web_push_enabled:
            public_key = self._decode_vapid_key(
                self.web_push_vapid_public_key or "",
                setting="PULSE_WEB_PUSH_VAPID_PUBLIC_KEY",
            )
            private_key = self._decode_vapid_key(
                self.web_push_vapid_private_key or "",
                setting="PULSE_WEB_PUSH_VAPID_PRIVATE_KEY",
            )
            if len(public_key) != 65 or public_key[0] != 0x04:
                raise ValueError(
                    "PULSE_WEB_PUSH_VAPID_PUBLIC_KEY must encode a 65-byte "
                    "uncompressed P-256 public key"
                )
            if len(private_key) != 32:
                raise ValueError(
                    "PULSE_WEB_PUSH_VAPID_PRIVATE_KEY must encode a 32-byte P-256 private key"
                )
            try:
                derived_public = ec.derive_private_key(
                    int.from_bytes(private_key, "big"),
                    ec.SECP256R1(),
                ).public_key().public_bytes(
                    serialization.Encoding.X962,
                    serialization.PublicFormat.UncompressedPoint,
                )
            except ValueError as exc:
                raise ValueError(
                    "PULSE_WEB_PUSH_VAPID_PRIVATE_KEY is not a valid P-256 private key"
                ) from exc
            if derived_public != public_key:
                raise ValueError("configured Web Push VAPID public/private keys do not match")
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

    @staticmethod
    def _decode_vapid_key(value: str, *, setting: str) -> bytes:
        raw = value.strip()
        if re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", raw) is None:
            raise ValueError(f"{setting} must be valid base64url")
        try:
            return base64.b64decode(
                raw + "=" * (-len(raw) % 4),
                altchars=b"-_",
                validate=True,
            )
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"{setting} must be valid base64url") from exc

    @staticmethod
    def _validate_vapid_subject(value: str) -> None:
        parsed = urlsplit(value.strip())
        if parsed.scheme == "mailto":
            try:
                validate_email(parsed.path, check_deliverability=False)
            except EmailNotValidError as exc:
                raise ValueError(
                    "PULSE_WEB_PUSH_VAPID_SUBJECT must contain a valid mailto address"
                ) from exc
            return
        if (
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
        ):
            return
        raise ValueError(
            "PULSE_WEB_PUSH_VAPID_SUBJECT must be a valid mailto: or https:// contact URI"
        )

    @property
    def web_push_enabled(self) -> bool:
        return all(
            value is not None and bool(value.strip())
            for value in (
                self.web_push_vapid_public_key,
                self.web_push_vapid_private_key,
                self.web_push_vapid_subject,
            )
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()

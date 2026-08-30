import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import ValidationError

from app.config import Settings


def _vapid_pair() -> tuple[str, str]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_bytes = private_key.private_numbers().private_value.to_bytes(32, "big")
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return (
        base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode(),
        base64.urlsafe_b64encode(private_bytes).rstrip(b"=").decode(),
    )


def test_production_can_disable_bootstrap_after_first_owner() -> None:
    settings = Settings(
        environment="production",
        secret_key="s" * 32,
        bootstrap_token=None,
        cookie_secure=True,
        integration_encryption_key="a" * 43,
        s3_bucket="pulse-private",
        s3_access_key_id="access-key",
        s3_secret_access_key="secret-key",
    )

    assert settings.bootstrap_token is None


def test_web_push_vapid_settings_require_complete_matching_key_pair() -> None:
    public_key, private_key = _vapid_pair()
    settings = Settings(
        web_push_vapid_public_key=public_key,
        web_push_vapid_private_key=private_key,
        web_push_vapid_subject="mailto:notifications@example.com",
    )

    assert settings.web_push_enabled is True

    with pytest.raises(ValidationError, match="must be configured together"):
        Settings(web_push_vapid_public_key=public_key)
    with pytest.raises(ValidationError, match="valid mailto"):
        Settings(
            web_push_vapid_public_key=public_key,
            web_push_vapid_private_key=private_key,
            web_push_vapid_subject="mailto:",
        )
    with pytest.raises(ValidationError, match="must be valid base64url"):
        Settings(
            web_push_vapid_public_key="not+base64",
            web_push_vapid_private_key=private_key,
            web_push_vapid_subject="mailto:notifications@example.com",
        )
    other_public_key, _ = _vapid_pair()
    with pytest.raises(ValidationError, match="public/private keys do not match"):
        Settings(
            web_push_vapid_public_key=other_public_key,
            web_push_vapid_private_key=private_key,
            web_push_vapid_subject="mailto:notifications@example.com",
        )


def test_settings_validation_error_hides_private_key_input() -> None:
    public_key, _private_key = _vapid_pair()
    recognizable_secret = "PRIVATE_KEY_SHOULD_NEVER_APPEAR"

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            web_push_vapid_public_key=public_key,
            web_push_vapid_private_key=recognizable_secret,
            web_push_vapid_subject="mailto:notifications@example.com",
        )

    assert recognizable_secret not in str(exc_info.value)
    assert "input_value" not in str(exc_info.value)

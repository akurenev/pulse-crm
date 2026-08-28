from app.config import Settings


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

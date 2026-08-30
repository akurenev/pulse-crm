from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import ValidationError

from app.api.push import PushSubscriptionWrite
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.integrations.models import NotificationDelivery, WebPushSubscription
from app.integrations.secrets import SecretCipher
from app.integrations.web_push import subscription_aad
from app.main import app
from app.models import DeliveryStatus, Membership, OutboxEvent


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _vapid_settings() -> Settings:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    private_bytes = private_key.private_numbers().private_value.to_bytes(32, "big")
    return Settings(
        job_runner_enabled=True,
        web_push_vapid_public_key=_base64url(public_bytes),
        web_push_vapid_private_key=_base64url(private_bytes),
        web_push_vapid_subject="mailto:notifications@example.com",
    )


def _subscription_payload(endpoint: str, *, marker: int = 1) -> dict[str, object]:
    client_key = ec.derive_private_key(marker, ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return {
        "endpoint": endpoint,
        "expiration_time": None,
        "keys": {
            "p256dh": _base64url(client_key),
            "auth": _base64url(bytes([marker]) * 16),
        },
    }


def _workspace_id(owner_auth: dict[str, object]) -> uuid.UUID:
    workspace = owner_auth["workspace"]
    assert isinstance(workspace, dict)
    return uuid.UUID(str(workspace["id"]))


def _active_push_runtime() -> SimpleNamespace:
    return SimpleNamespace(
        handlers=SimpleNamespace(web_push_sender=object()),
        supervisor=SimpleNamespace(running=True),
        scheduler=SimpleNamespace(running=True),
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1/push",
        "https://10.0.0.2/push",
        "https://localhost/push",
        "https://metadata.internal/push",
        "https://fcm.googleapis.com:8443/push",
        "https://user:password@fcm.googleapis.com/push",
        "https://evil-fcm.googleapis.com/push",
    ],
)
def test_push_subscription_rejects_non_provider_and_unsafe_endpoints(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        PushSubscriptionWrite.model_validate(_subscription_payload(endpoint))


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://fcm.googleapis.com/fcm/send/example",
        "https://updates.push.services.mozilla.com/wpush/v2/example",
        "https://web.push.apple.com/example",
        "https://db5.notify.windows.com/w/?token=example",
    ],
)
def test_push_subscription_accepts_known_provider_endpoints(endpoint: str) -> None:
    parsed = PushSubscriptionWrite.model_validate(_subscription_payload(endpoint))
    assert parsed.endpoint == endpoint


def test_push_subscription_rejects_invalid_p256dh_point_prefix() -> None:
    payload = _subscription_payload("https://fcm.googleapis.com/fcm/send/example")
    keys = payload["keys"]
    assert isinstance(keys, dict)
    keys["p256dh"] = _base64url(b"\x03" + b"x" * 64)
    with pytest.raises(ValidationError, match="65-byte P-256 public key"):
        PushSubscriptionWrite.model_validate(payload)


def test_push_subscription_rejects_point_outside_p256_curve() -> None:
    payload = _subscription_payload("https://fcm.googleapis.com/fcm/send/example")
    keys = payload["keys"]
    assert isinstance(keys, dict)
    keys["p256dh"] = _base64url(b"\x04" + b"\x00" * 64)
    with pytest.raises(ValidationError, match="valid P-256 public key"):
        PushSubscriptionWrite.model_validate(payload)


@pytest.mark.asyncio
async def test_push_config_requires_vapid_and_runtime_cipher(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
) -> None:
    settings = _vapid_settings()
    app.dependency_overrides[get_settings] = lambda: settings
    previous_cipher = getattr(app.state, "integration_secret_cipher", None)
    previous_runtime = getattr(app.state, "integration_runtime", None)
    if hasattr(app.state, "integration_secret_cipher"):
        del app.state.integration_secret_cipher
    try:
        without_cipher = await client.get("/api/v1/push/config")
        app.state.integration_secret_cipher = SecretCipher(key=b"p" * 32, key_id="push-test")
        without_runtime = await client.get("/api/v1/push/config")
        blocked_subscription = await client.post(
            "/api/v1/push/subscriptions",
            headers={"X-CSRF-Token": str(owner_auth["csrf_token"])},
            json=_subscription_payload("https://fcm.googleapis.com/fcm/send/no-runtime"),
        )
        app.state.integration_runtime = _active_push_runtime()
        enabled = await client.get("/api/v1/push/config")
        disabled_settings = settings.model_copy(update={"job_runner_enabled": False})
        app.dependency_overrides[get_settings] = lambda: disabled_settings
        disabled_runner = await client.get("/api/v1/push/config")
        blocked_test = await client.post(
            "/api/v1/push/test",
            headers={"X-CSRF-Token": str(owner_auth["csrf_token"])},
        )
    finally:
        app.dependency_overrides.pop(get_settings, None)
        if previous_cipher is None:
            if hasattr(app.state, "integration_secret_cipher"):
                del app.state.integration_secret_cipher
        else:
            app.state.integration_secret_cipher = previous_cipher
        if previous_runtime is None:
            if hasattr(app.state, "integration_runtime"):
                del app.state.integration_runtime
        else:
            app.state.integration_runtime = previous_runtime

    assert without_cipher.status_code == 200
    assert without_cipher.json() == {"enabled": False, "public_key": None}
    assert without_runtime.json() == {"enabled": False, "public_key": None}
    assert blocked_subscription.status_code == 503
    assert disabled_runner.json() == {"enabled": False, "public_key": None}
    assert blocked_test.status_code == 503
    assert enabled.status_code == 200
    assert enabled.json() == {
        "enabled": True,
        "public_key": settings.web_push_vapid_public_key,
    }


@pytest.mark.asyncio
async def test_push_subscription_is_encrypted_idempotent_capped_and_queues_test(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
) -> None:
    workspace_id = _workspace_id(owner_auth)
    csrf = str(owner_auth["csrf_token"])
    headers = {"X-CSRF-Token": csrf}
    settings = _vapid_settings()
    cipher = SecretCipher(key=b"w" * 32, key_id="push-test")
    previous_cipher = getattr(app.state, "integration_secret_cipher", None)
    previous_runtime = getattr(app.state, "integration_runtime", None)
    app.state.integration_secret_cipher = cipher
    app.state.integration_runtime = _active_push_runtime()
    app.dependency_overrides[get_settings] = lambda: settings
    endpoint = "https://fcm.googleapis.com/fcm/send/device-0"
    try:
        missing_csrf = await client.post(
            "/api/v1/push/subscriptions",
            json=_subscription_payload(endpoint),
        )
        created = await client.post(
            "/api/v1/push/subscriptions",
            headers=headers,
            json=_subscription_payload(endpoint),
        )
        updated = await client.post(
            "/api/v1/push/subscriptions",
            headers=headers,
            json=_subscription_payload(endpoint, marker=2),
        )
        expired_payload = _subscription_payload(
            "https://fcm.googleapis.com/fcm/send/expired-device",
            marker=3,
        )
        expired_payload["expiration_time"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        expired = await client.post(
            "/api/v1/push/subscriptions",
            headers=headers,
            json=expired_payload,
        )
        for index in range(1, 11):
            response = await client.post(
                "/api/v1/push/subscriptions",
                headers=headers,
                json=_subscription_payload(
                    f"https://fcm.googleapis.com/fcm/send/device-{index}",
                    marker=(index % 250) + 1,
                ),
            )
            assert response.status_code == 204, response.text

        queued = await client.post("/api/v1/push/test", headers=headers)
        cooldown = await client.post("/api/v1/push/test", headers=headers)
        removed = await client.request(
            "DELETE",
            "/api/v1/push/subscriptions",
            headers=headers,
            json={"endpoint": "https://fcm.googleapis.com/fcm/send/device-10"},
        )
    finally:
        app.dependency_overrides.pop(get_settings, None)
        if previous_cipher is None:
            del app.state.integration_secret_cipher
        else:
            app.state.integration_secret_cipher = previous_cipher
        if previous_runtime is None:
            del app.state.integration_runtime
        else:
            app.state.integration_runtime = previous_runtime

    assert missing_csrf.status_code == 403
    assert created.status_code == 204
    assert updated.status_code == 204
    assert expired.status_code == 422
    assert queued.status_code == 202, queued.text
    assert cooldown.status_code == 429
    assert cooldown.json()["detail"]["code"] == "web_push_test_cooldown"
    assert int(cooldown.headers["retry-after"]) >= 1
    assert removed.status_code == 204

    async with SessionLocal() as db:
        user_id = await db.scalar(
            sa.select(Membership.user_id).where(Membership.workspace_id == workspace_id)
        )
        assert user_id is not None
        subscriptions = list(
            (
                await db.scalars(
                    sa.select(WebPushSubscription).where(
                        WebPushSubscription.workspace_id == workspace_id,
                        WebPushSubscription.user_id == user_id,
                    )
                )
            ).all()
        )
        # Eleven unique endpoint registrations are hard-capped to ten, then
        # the explicit unsubscribe removes one more without retaining secrets.
        assert len(subscriptions) == 9
        assert all(item.is_active for item in subscriptions)
        assert all(endpoint.encode() not in item.encrypted_subscription for item in subscriptions)

        delivery_id = uuid.UUID(queued.json()["delivery_id"])
        delivery = await db.get(NotificationDelivery, delivery_id)
        assert delivery is not None
        assert delivery.channel == "web_push"
        assert delivery.recipient_id == user_id
        assert delivery.status is DeliveryStatus.pending
        assert (
            await db.scalar(
                sa.select(sa.func.count())
                .select_from(NotificationDelivery)
                .where(
                    NotificationDelivery.workspace_id == workspace_id,
                    NotificationDelivery.dedupe_key.like("web-push:test:%"),
                )
            )
            == 1
        )
        outbox = await db.scalar(
            sa.select(OutboxEvent).where(OutboxEvent.aggregate_id == delivery.id)
        )
        assert outbox is not None
        assert outbox.event_type == "notification.delivery.queued"

        # The retained endpoint payload can only be recovered with its scoped AAD.
        retained = subscriptions[0]
        raw = cipher.decrypt(
            retained.encrypted_subscription,
            associated_data=subscription_aad(
                workspace_id=retained.workspace_id,
                user_id=retained.user_id,
                endpoint_digest=retained.endpoint_hash,
            ),
        )
        decoded = json.loads(raw)
        assert decoded["endpoint"].startswith("https://fcm.googleapis.com/")
        assert decoded["keys"]["p256dh"]

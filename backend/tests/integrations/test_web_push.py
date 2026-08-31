from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.integrations.models import (
    NotificationAudience,
    NotificationDelivery,
    WebPushSubscription,
)
from app.integrations.secrets import SecretCipher
from app.integrations.web_push import (
    MAX_WEB_PUSH_PAYLOAD_BYTES,
    WebPushDeliveryError,
    WebPushSender,
    _NoRedirectSession,
    build_web_push_payload,
    endpoint_hash,
    mirror_delivered_in_app_notification,
    register_subscription,
)
from app.models import DeliveryStatus, User, Workspace

FIXED_NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)


class ProviderFailure(Exception):
    def __init__(self, status_code: int, secret_message: str) -> None:
        super().__init__(secret_message)
        self.response = SimpleNamespace(status=status_code)


@pytest.mark.asyncio
async def test_web_push_sender_is_scoped_bounded_and_handles_partial_results(
    db: AsyncSession,
    integration_domain: dict[str, Any],
) -> None:
    workspace = integration_domain["workspace"]
    user = integration_domain["user"]
    cipher = SecretCipher(key=b"s" * 32, key_id="sender-test")
    endpoints = {
        "success": "https://fcm.googleapis.com/fcm/send/success-device",
        "gone": "https://fcm.googleapis.com/fcm/send/gone-device",
        "transient": "https://fcm.googleapis.com/fcm/send/transient-device",
        "redirect": "https://fcm.googleapis.com/fcm/send/redirect-device",
    }
    for endpoint in endpoints.values():
        await register_subscription(
            db,
            cipher=cipher,
            workspace_id=workspace.id,
            user_id=user.id,
            endpoint=endpoint,
            expiration_time=None,
            p256dh="test-public-key",
            auth="test-auth-secret",
            now=FIXED_NOW,
        )

    other_user = User(
        email="other-push-user@example.com",
        full_name="Other Push User",
        password_hash="not-used",
    )
    other_workspace = Workspace(name="Other Push", slug="other-push")
    db.add_all([other_user, other_workspace])
    await db.flush()
    wrong_user_endpoint = "https://fcm.googleapis.com/fcm/send/wrong-user"
    wrong_workspace_endpoint = "https://fcm.googleapis.com/fcm/send/wrong-workspace"
    await register_subscription(
        db,
        cipher=cipher,
        workspace_id=workspace.id,
        user_id=other_user.id,
        endpoint=wrong_user_endpoint,
        expiration_time=None,
        p256dh="wrong-user-key",
        auth="wrong-user-auth",
        now=FIXED_NOW,
    )
    await register_subscription(
        db,
        cipher=cipher,
        workspace_id=other_workspace.id,
        user_id=user.id,
        endpoint=wrong_workspace_endpoint,
        expiration_time=None,
        p256dh="wrong-workspace-key",
        auth="wrong-workspace-auth",
        now=FIXED_NOW,
    )
    delivery = NotificationDelivery(
        workspace_id=workspace.id,
        audience=NotificationAudience.employee,
        channel="web_push",
        recipient_id=user.id,
        recipient_address=str(user.id),
        subject="Очень длинный заголовок " * 100,
        body='Тело с кавычками " и unicode ' * 500,
        status=DeliveryStatus.pending,
        dedupe_key="web-push-sender-test",
        scheduled_at=FIXED_NOW,
    )
    db.add(delivery)
    await db.commit()

    called_endpoints: list[str] = []
    claim_snapshots: list[dict[str, str]] = []
    claim_objects: list[dict[str, str]] = []
    payloads: list[str] = []

    async def partial_provider(
        *,
        subscription_info: dict[str, Any],
        data: str,
        vapid_private_key: str,
        vapid_claims: dict[str, str],
        ttl: int,
        request_timeout: float,
    ) -> object:
        del vapid_private_key, ttl, request_timeout
        endpoint = str(subscription_info["endpoint"])
        called_endpoints.append(endpoint)
        claim_snapshots.append(dict(vapid_claims))
        claim_objects.append(vapid_claims)
        payloads.append(data)
        vapid_claims["aud"] = "mutated-by-provider"
        if endpoint == endpoints["gone"]:
            raise ProviderFailure(410, f"gone endpoint: {endpoint}")
        if endpoint == endpoints["transient"]:
            raise ProviderFailure(503, f"temporary endpoint: {endpoint}")
        if endpoint == endpoints["redirect"]:
            raise ProviderFailure(302, f"redirect endpoint: {endpoint}")
        return object()

    sender = WebPushSender(
        session_factory=SessionLocal,
        cipher=cipher,
        vapid_private_key="private-test-key",
        vapid_subject="mailto:notifications@example.com",
        webpush_call=partial_provider,
        now=lambda: FIXED_NOW,
    )
    result = await sender(delivery)

    assert result.provider_message_id == f"web-push:{delivery.id}:1"
    assert set(called_endpoints) == set(endpoints.values())
    assert wrong_user_endpoint not in called_endpoints
    assert wrong_workspace_endpoint not in called_endpoints
    assert len({id(item) for item in claim_objects}) == 4
    assert claim_snapshots == [{"sub": "mailto:notifications@example.com"}] * 4
    assert all(len(payload.encode("utf-8")) <= MAX_WEB_PUSH_PAYLOAD_BYTES for payload in payloads)
    assert all(
        set(json.loads(payload)) == {"title", "body", "url", "tag"}
        for payload in payloads
    )
    assert all(
        json.loads(payload)["tag"] == f"notification:{delivery.id}" for payload in payloads
    )

    rows = {
        item.endpoint_hash: item
        for item in (
            await db.scalars(
                sa.select(WebPushSubscription).where(
                    WebPushSubscription.workspace_id == workspace.id,
                    WebPushSubscription.user_id == user.id,
                )
            )
        ).all()
    }
    assert rows[endpoint_hash(endpoints["success"])].is_active is True
    assert rows[endpoint_hash(endpoints["success"])].last_success_at is not None
    assert rows[endpoint_hash(endpoints["transient"])].is_active is True
    assert rows[endpoint_hash(endpoints["gone"])].is_active is False
    assert rows[endpoint_hash(endpoints["redirect"])].is_active is False
    assert all(
        endpoint not in (row.last_error or "")
        for row in rows.values()
        for endpoint in endpoints.values()
    )

    corrupt = WebPushSubscription(
        workspace_id=workspace.id,
        user_id=user.id,
        endpoint_hash="f" * 64,
        encrypted_subscription=b"not-an-aes-envelope",
        encryption_key_id=cipher.key_id,
        is_active=True,
    )
    db.add(corrupt)
    await db.commit()

    async def unavailable_provider(**kwargs: Any) -> object:
        endpoint = str(kwargs["subscription_info"]["endpoint"])
        raise ProviderFailure(503, f"network failed for {endpoint}")

    unavailable_sender = WebPushSender(
        session_factory=SessionLocal,
        cipher=cipher,
        vapid_private_key="private-test-key",
        vapid_subject="mailto:notifications@example.com",
        webpush_call=unavailable_provider,
        now=lambda: FIXED_NOW,
    )
    with pytest.raises(WebPushDeliveryError) as exc_info:
        await unavailable_sender(delivery)
    assert "fcm.googleapis.com" not in str(exc_info.value)
    await db.refresh(corrupt)
    assert corrupt.is_active is False
    assert corrupt.last_error == "encrypted subscription is invalid"


def test_web_push_http_session_forces_redirects_off() -> None:
    calls: list[dict[str, Any]] = []

    class RecordingSession:
        def post(self, _url: str, **kwargs: Any) -> object:
            calls.append(kwargs)
            return object()

    proxy = _NoRedirectSession(RecordingSession())
    proxy.post("https://fcm.googleapis.com/fcm/send/example", allow_redirects=True)

    assert calls == [{"allow_redirects": False}]


@pytest.mark.parametrize(
    ("target_entity_type", "route", "entity_key"),
    [
        ("deal", "/deals?deal=", "deal"),
        ("task", "/tasks?task=", None),
    ],
)
@pytest.mark.asyncio
async def test_web_push_sender_uses_materialized_domain_event_deep_link(
    db: AsyncSession,
    integration_domain: dict[str, Any],
    target_entity_type: str,
    route: str,
    entity_key: str | None,
) -> None:
    workspace = integration_domain["workspace"]
    user = integration_domain["user"]
    entity_id = (
        integration_domain[entity_key].id if entity_key is not None else uuid.uuid4()
    )
    cipher = SecretCipher(key=b"d" * 32, key_id="deep-link-test")
    await register_subscription(
        db,
        cipher=cipher,
        workspace_id=workspace.id,
        user_id=user.id,
        endpoint="https://fcm.googleapis.com/fcm/send/deep-link-device",
        expiration_time=None,
        p256dh="test-public-key",
        auth="test-auth-secret",
        now=FIXED_NOW,
    )
    source = NotificationDelivery(
        workspace_id=workspace.id,
        audience=NotificationAudience.employee,
        channel="in_app",
        recipient_id=user.id,
        recipient_address=str(user.id),
        subject="Domain event",
        body="Open the CRM item",
        target_entity_type=target_entity_type,
        target_entity_id=entity_id,
        status=DeliveryStatus.delivered,
        dedupe_key=f"domain-event-deep-link:{target_entity_type}",
        scheduled_at=FIXED_NOW,
    )
    db.add(source)
    await db.flush()
    mirrored = await mirror_delivered_in_app_notification(
        db,
        delivery=source,
        now=FIXED_NOW,
    )
    assert mirrored is not None
    assert mirrored.target_entity_type == target_entity_type
    assert mirrored.target_entity_id == entity_id
    await db.commit()

    payloads: list[dict[str, Any]] = []

    async def provider(**kwargs: Any) -> object:
        payloads.append(json.loads(str(kwargs["data"])))
        return object()

    sender = WebPushSender(
        session_factory=SessionLocal,
        cipher=cipher,
        vapid_private_key="private-test-key",
        vapid_subject="mailto:notifications@example.com",
        webpush_call=provider,
        now=lambda: FIXED_NOW,
    )
    await sender(mirrored)

    assert payloads == [
        {
            "title": "Domain event",
            "body": "Open the CRM item",
            "url": f"{route}{entity_id}",
            "tag": f"notification:{mirrored.id}",
        }
    ]


@pytest.mark.parametrize(
    ("target_entity_type", "target_entity_id"),
    [
        (None, uuid.uuid4()),
        ("contact", uuid.uuid4()),
        ("deal", None),
    ],
)
def test_web_push_payload_rejects_invalid_materialized_targets(
    target_entity_type: str | None,
    target_entity_id: uuid.UUID | None,
) -> None:
    delivery = NotificationDelivery(
        workspace_id=uuid.uuid4(),
        audience=NotificationAudience.employee,
        channel="web_push",
        recipient_id=uuid.uuid4(),
        recipient_address="recipient",
        body="Test",
        target_entity_type=target_entity_type,
        target_entity_id=target_entity_id,
        dedupe_key="test-untrusted-url",
        scheduled_at=FIXED_NOW,
    )

    assert json.loads(build_web_push_payload(delivery))["url"] == "/"


@pytest.mark.parametrize(
    ("target_entity_type", "target_entity_id"),
    [
        (None, uuid.uuid4()),
        ("deal", None),
        ("contact", uuid.uuid4()),
    ],
)
@pytest.mark.asyncio
async def test_notification_delivery_check_rejects_invalid_target_pairs(
    db: AsyncSession,
    integration_domain: dict[str, Any],
    target_entity_type: str | None,
    target_entity_id: uuid.UUID | None,
) -> None:
    workspace = integration_domain["workspace"]
    user = integration_domain["user"]
    db.add(
        NotificationDelivery(
            workspace_id=workspace.id,
            audience=NotificationAudience.employee,
            channel="web_push",
            recipient_id=user.id,
            recipient_address=str(user.id),
            body="Invalid target",
            target_entity_type=target_entity_type,
            target_entity_id=target_entity_id,
            dedupe_key=f"invalid-target:{target_entity_type}:{target_entity_id}",
            scheduled_at=FIXED_NOW,
        )
    )

    with pytest.raises(sa.exc.IntegrityError):
        await db.flush()


@pytest.mark.asyncio
async def test_web_push_mirror_ignores_client_and_non_in_app_deliveries(
    db: AsyncSession,
    integration_domain: dict[str, Any],
) -> None:
    workspace = integration_domain["workspace"]
    user = integration_domain["user"]
    cipher = SecretCipher(key=b"i" * 32, key_id="ineligible-test")
    await register_subscription(
        db,
        cipher=cipher,
        workspace_id=workspace.id,
        user_id=user.id,
        endpoint="https://fcm.googleapis.com/fcm/send/ineligible-device",
        expiration_time=None,
        p256dh="test-public-key",
        auth="test-auth-secret",
        now=FIXED_NOW,
    )
    client_delivery = NotificationDelivery(
        workspace_id=workspace.id,
        audience=NotificationAudience.client,
        channel="in_app",
        recipient_id=user.id,
        recipient_address=str(user.id),
        body="Client body",
        dedupe_key="ineligible-client",
        scheduled_at=FIXED_NOW,
    )
    other_channel = NotificationDelivery(
        workspace_id=workspace.id,
        audience=NotificationAudience.employee,
        channel="email",
        recipient_id=user.id,
        recipient_address="employee@example.com",
        body="Email body",
        dedupe_key="ineligible-channel",
        scheduled_at=FIXED_NOW,
    )

    assert (
        await mirror_delivered_in_app_notification(
            db,
            delivery=client_delivery,
            now=FIXED_NOW,
        )
        is None
    )
    assert (
        await mirror_delivered_in_app_notification(
            db,
            delivery=other_channel,
            now=FIXED_NOW,
        )
        is None
    )
    assert (
        await db.scalar(
            sa.select(sa.func.count())
            .select_from(NotificationDelivery)
            .where(NotificationDelivery.channel == "web_push")
        )
        == 0
    )

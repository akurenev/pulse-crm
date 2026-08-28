from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.webhooks import (
    IdempotencyConflictError,
    WebhookVerificationError,
    accept_inbound_event,
    verify_generic_webhook,
    webhook_signature,
)


def test_webhook_verification_authenticates_exact_body_and_replay_window() -> None:
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    timestamp = int(now.timestamp())
    body = b'{"deal":{"title":"Coffee"}}'
    verified = verify_generic_webhook(
        secret="test-secret",
        body=body,
        signature=webhook_signature("test-secret", timestamp, body),
        timestamp=timestamp,
        idempotency_key="evt-001",
        now=now,
    )
    assert verified.idempotency_key == "evt-001"

    with pytest.raises(WebhookVerificationError, match="signature"):
        verify_generic_webhook(
            secret="test-secret",
            body=body + b" ",
            signature=webhook_signature("test-secret", timestamp, body),
            timestamp=timestamp,
            idempotency_key="evt-002",
            now=now,
        )
    with pytest.raises(WebhookVerificationError, match="replay"):
        verify_generic_webhook(
            secret="test-secret",
            body=body,
            signature=webhook_signature("test-secret", timestamp, body),
            timestamp=timestamp,
            idempotency_key="evt-003",
            now=now + timedelta(minutes=6),
        )


@pytest.mark.asyncio
async def test_inbound_event_is_idempotent_and_rejects_key_reuse(
    db: AsyncSession, integration_domain: dict[str, object]
) -> None:
    workspace = integration_domain["workspace"]
    now = datetime.now(UTC)
    body = b'{"message":{"body":"Hello"}}'
    verified = verify_generic_webhook(
        secret="test-secret",
        body=body,
        signature=webhook_signature("test-secret", int(now.timestamp()), body),
        timestamp=int(now.timestamp()),
        idempotency_key="same-event",
        now=now,
    )
    first = await accept_inbound_event(
        db,
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        source_key="generic:test",
        verified=verified,
        payload={"message": {"body": "Hello"}},
    )
    second = await accept_inbound_event(
        db,
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        source_key="generic:test",
        verified=verified,
        payload={"message": {"body": "Hello"}},
    )
    assert first.created is True
    assert second.created is False
    assert second.event.id == first.event.id

    changed = verify_generic_webhook(
        secret="test-secret",
        body=b'{"message":{"body":"Changed"}}',
        signature=webhook_signature(
            "test-secret", int(now.timestamp()), b'{"message":{"body":"Changed"}}'
        ),
        timestamp=int(now.timestamp()),
        idempotency_key="same-event",
        now=now,
    )
    with pytest.raises(IdempotencyConflictError):
        await accept_inbound_event(
            db,
            workspace_id=workspace.id,  # type: ignore[attr-defined]
            source_key="generic:test",
            verified=changed,
            payload={"message": {"body": "Changed"}},
        )

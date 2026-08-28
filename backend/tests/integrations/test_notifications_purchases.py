from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.models import (
    ConsentStatus,
    ContactChannelConsent,
    NotificationAudience,
    NotificationDelivery,
    NotificationRule,
    NotificationTemplate,
)
from app.integrations.notifications import NotificationConsentError, queue_notification
from app.integrations.purchases import create_purchase_schedule, ensure_purchase_task
from app.models import OutboxEvent, Task


@pytest.mark.asyncio
async def test_client_notification_requires_consent_and_deduplicates(
    db: AsyncSession, integration_domain: dict[str, object]
) -> None:
    workspace = integration_domain["workspace"]
    contact = integration_domain["contact"]
    template = NotificationTemplate(
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        name="Новая покупка",
        channel="email",
        subject_template="Напоминание для {name}",
        body_template="Здравствуйте, {name}!",
    )
    db.add(template)
    await db.flush()
    rule = NotificationRule(
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        template_id=template.id,
        name="Напомнить клиенту",
        event_type="purchase.reminder",
        audience=NotificationAudience.client,
        channel="email",
        recipients=[],
        filters={},
        is_enabled=True,
    )
    db.add(rule)
    await db.flush()

    kwargs = {
        "workspace_id": workspace.id,  # type: ignore[attr-defined]
        "template": template,
        "rule": rule,
        "audience": NotificationAudience.client,
        "channel": "email",
        "recipient_address": "Anna@Example.com",
        "normalized_address": "anna@example.com",
        "contact_id": contact.id,  # type: ignore[attr-defined]
        "dedupe_key": "purchase:42:client-email",
        "variables": {"name": "Анна"},
    }
    with pytest.raises(NotificationConsentError):
        await queue_notification(db, **kwargs)  # type: ignore[arg-type]

    db.add(
        ContactChannelConsent(
            workspace_id=workspace.id,  # type: ignore[attr-defined]
            contact_id=contact.id,  # type: ignore[attr-defined]
            channel="email",
            address="Anna@Example.com",
            normalized_address="anna@example.com",
            purpose="notifications",
            status=ConsentStatus.granted,
            source="html_form",
            granted_at=datetime.now(UTC),
        )
    )
    await db.flush()
    first = await queue_notification(db, **kwargs)  # type: ignore[arg-type]
    second = await queue_notification(db, **kwargs)  # type: ignore[arg-type]
    assert first.created is True
    assert second.created is False
    assert first.delivery.id == second.delivery.id
    await db.flush()
    assert await db.scalar(sa.select(sa.func.count()).select_from(NotificationDelivery)) == 1
    assert await db.scalar(sa.select(sa.func.count()).select_from(OutboxEvent)) == 1


@pytest.mark.asyncio
async def test_next_purchase_creates_exactly_one_task_and_outbox(
    db: AsyncSession, integration_domain: dict[str, object]
) -> None:
    workspace = integration_domain["workspace"]
    user = integration_domain["user"]
    contact = integration_domain["contact"]
    deal = integration_domain["deal"]
    scheduled_for = datetime.now(UTC) + timedelta(days=30)
    remind_at = scheduled_for - timedelta(days=3)
    schedule, created = await create_purchase_schedule(
        db,
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        deal_id=deal.id,  # type: ignore[attr-defined]
        contact_id=contact.id,  # type: ignore[attr-defined]
        assignee_id=user.id,  # type: ignore[attr-defined]
        scheduled_for=scheduled_for,
        remind_at=remind_at,
    )
    duplicate, duplicate_created = await create_purchase_schedule(
        db,
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        deal_id=deal.id,  # type: ignore[attr-defined]
        contact_id=contact.id,  # type: ignore[attr-defined]
        assignee_id=user.id,  # type: ignore[attr-defined]
        scheduled_for=scheduled_for,
        remind_at=remind_at,
    )
    assert created is True and duplicate_created is False
    assert duplicate.id == schedule.id

    first = await ensure_purchase_task(
        db,
        workspace_id=workspace.id,
        schedule_id=schedule.id,  # type: ignore[attr-defined]
    )
    second = await ensure_purchase_task(
        db,
        workspace_id=workspace.id,
        schedule_id=schedule.id,  # type: ignore[attr-defined]
    )
    assert first.created is True and second.created is False
    assert first.task.id == second.task.id
    await db.flush()
    assert await db.scalar(sa.select(sa.func.count()).select_from(Task)) == 1
    assert await db.scalar(sa.select(sa.func.count()).select_from(OutboxEvent)) == 1

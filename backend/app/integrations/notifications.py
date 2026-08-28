"""Consent-aware notification deliveries written with the domain outbox."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.models import (
    ConsentStatus,
    ContactChannelConsent,
    NotificationAudience,
    NotificationDelivery,
    NotificationRule,
    NotificationTemplate,
)
from app.models import DeliveryStatus, OutboxEvent

PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class NotificationConsentError(ValueError):
    pass


class NotificationTemplateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class QueuedNotification:
    delivery: NotificationDelivery
    created: bool


def render_notification_template(template: str, variables: Mapping[str, Any]) -> str:
    """Render flat placeholders without attribute access or arbitrary expressions."""

    missing: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            missing.add(key)
            return match.group(0)
        value = variables[key]
        return "" if value is None else str(value)

    rendered = PLACEHOLDER.sub(replace, template)
    if missing:
        raise NotificationTemplateError("missing template variables: " + ", ".join(sorted(missing)))
    return rendered


def _insert_for(session: AsyncSession) -> Any:
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        return pg_insert(NotificationDelivery)
    if dialect_name == "sqlite":
        return sqlite_insert(NotificationDelivery)
    raise RuntimeError(f"unsupported database dialect for idempotent insert: {dialect_name}")


async def _ensure_client_consent(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    contact_id: uuid.UUID,
    channel: str,
    normalized_address: str,
    purpose: str,
) -> None:
    consent_id = await session.scalar(
        sa.select(ContactChannelConsent.id).where(
            ContactChannelConsent.workspace_id == workspace_id,
            ContactChannelConsent.contact_id == contact_id,
            ContactChannelConsent.channel == channel,
            ContactChannelConsent.normalized_address == normalized_address,
            ContactChannelConsent.purpose == purpose,
            ContactChannelConsent.status == ConsentStatus.granted,
            ContactChannelConsent.revoked_at.is_(None),
        )
    )
    if consent_id is None:
        raise NotificationConsentError("client notification has no active consent")


async def queue_notification(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    template: NotificationTemplate,
    rule: NotificationRule | None,
    audience: NotificationAudience,
    channel: str,
    recipient_address: str,
    dedupe_key: str,
    variables: Mapping[str, Any],
    scheduled_at: datetime | None = None,
    recipient_id: uuid.UUID | None = None,
    contact_id: uuid.UUID | None = None,
    normalized_address: str | None = None,
    consent_purpose: str = "notifications",
) -> QueuedNotification:
    """Queue one delivery and its outbox record in the caller's transaction."""

    if template.workspace_id != workspace_id or (
        rule is not None and rule.workspace_id != workspace_id
    ):
        raise PermissionError("notification configuration belongs to another workspace")
    if template.channel != channel or (rule is not None and rule.channel != channel):
        raise ValueError("notification channel does not match its template or rule")
    address = recipient_address.strip()
    if not address or len(address) > 512:
        raise ValueError("invalid notification recipient address")
    if not dedupe_key or len(dedupe_key) > 255:
        raise ValueError("invalid notification dedupe key")

    requires_consent = audience is NotificationAudience.client and (
        rule is None or rule.require_client_consent
    )
    if requires_consent:
        if contact_id is None or not normalized_address:
            raise NotificationConsentError("client notification is missing consent identity")
        await _ensure_client_consent(
            session,
            workspace_id=workspace_id,
            contact_id=contact_id,
            channel=channel,
            normalized_address=normalized_address,
            purpose=consent_purpose,
        )

    delivery_id = uuid.uuid4()
    subject = (
        render_notification_template(template.subject_template, variables)
        if template.subject_template
        else None
    )
    body = render_notification_template(template.body_template, variables)
    due_at = scheduled_at or datetime.now(UTC)
    statement = (
        _insert_for(session)
        .values(
            id=delivery_id,
            workspace_id=workspace_id,
            rule_id=rule.id if rule else None,
            template_id=template.id,
            audience=audience,
            channel=channel,
            recipient_id=recipient_id,
            recipient_address=address,
            subject=subject,
            body=body,
            status=DeliveryStatus.pending,
            dedupe_key=dedupe_key,
            scheduled_at=due_at,
            attempts=0,
        )
        .on_conflict_do_nothing(
            index_elements=[NotificationDelivery.workspace_id, NotificationDelivery.dedupe_key]
        )
        .returning(NotificationDelivery.id)
    )
    inserted_id = (await session.execute(statement)).scalar_one_or_none()
    if inserted_id is None:
        delivery = await session.scalar(
            sa.select(NotificationDelivery).where(
                NotificationDelivery.workspace_id == workspace_id,
                NotificationDelivery.dedupe_key == dedupe_key,
            )
        )
        if delivery is None:  # pragma: no cover
            raise RuntimeError("deduplicated notification delivery could not be loaded")
        return QueuedNotification(delivery=delivery, created=False)

    delivery = await session.get(NotificationDelivery, inserted_id)
    if delivery is None:  # pragma: no cover
        raise RuntimeError("inserted notification delivery could not be loaded")
    session.add(
        OutboxEvent(
            workspace_id=workspace_id,
            event_type="notification.delivery.queued",
            aggregate_type="notification_delivery",
            aggregate_id=delivery.id,
            payload={
                "delivery_id": str(delivery.id),
                "channel": channel,
                "scheduled_at": due_at.isoformat(),
            },
            available_at=due_at,
        )
    )
    return QueuedNotification(delivery=delivery, created=True)

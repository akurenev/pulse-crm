from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ActivityEvent, OutboxEvent, RealtimeEvent


def record_domain_event(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    event_type: str,
    entity_type: str,
    entity_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    payload: dict[str, Any] | None = None,
) -> ActivityEvent:
    """Append audit, outbox and durable realtime records in the caller's transaction."""

    event_payload = payload or {}
    activity = ActivityEvent(
        workspace_id=workspace_id,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=actor_id,
        payload=event_payload,
    )
    db.add(activity)
    db.add(
        OutboxEvent(
            workspace_id=workspace_id,
            event_type=event_type,
            aggregate_type=entity_type,
            aggregate_id=entity_id,
            payload=event_payload,
        )
    )
    db.add(
        RealtimeEvent(
            workspace_id=workspace_id,
            event_type=event_type,
            # Realtime consumers only need an invalidation signal.  Business
            # payloads can contain notes, contact addresses or consent
            # evidence and belong in the access-controlled activity/outbox
            # records, not in the replayable SSE stream.
            payload={"entity_type": entity_type, "entity_id": str(entity_id)},
        )
    )
    return activity


def record_audit_event(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    event_type: str,
    entity_type: str,
    entity_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    payload: dict[str, Any] | None = None,
) -> ActivityEvent:
    """Append a read/security audit record without dispatching a business event."""

    activity = ActivityEvent(
        workspace_id=workspace_id,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=actor_id,
        payload=payload or {},
    )
    db.add(activity)
    return activity


def record_access_change(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    recipient_ids: set[uuid.UUID | None],
    resource: str,
) -> None:
    """Tell affected users to discard cached records without exposing record IDs."""

    for recipient_id in sorted(
        (value for value in recipient_ids if value is not None), key=str
    ):
        db.add(
            RealtimeEvent(
                workspace_id=workspace_id,
                event_type="access.changed",
                payload={
                    "recipient_id": str(recipient_id),
                    "resource": resource,
                },
            )
        )

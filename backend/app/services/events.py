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
            payload={"entity_type": entity_type, "entity_id": str(entity_id), **event_payload},
        )
    )
    return activity

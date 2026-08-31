from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal
from app.integrations.models import Conversation, Message, NotificationDelivery
from app.models import (
    Company,
    Contact,
    Deal,
    Membership,
    RealtimeEvent,
    Session,
    Task,
    User,
    Workspace,
)
from app.security import AuthContext, CurrentUser
from app.services.access import (
    company_access_condition,
    contact_access_condition,
    deal_access_condition,
    is_employee,
    task_access_condition,
)

router = APIRouter(tags=["realtime"])
logger = logging.getLogger(__name__)
NOTIFY_CHANNEL = "pulse_realtime"
MAX_REPLAY_EVENTS = 1_000


def _psycopg_dsn(database_url: str) -> str:
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _public_event_payload(
    event: RealtimeEvent,
    *,
    user_id: object,
) -> dict[str, object] | None:
    """Return a minimal invalidation payload, or hide a private user event."""

    payload = event.payload if isinstance(event.payload, dict) else {}
    if event.event_type == "access.changed":
        recipient_id = payload.get("recipient_id")
        if recipient_id is None or str(recipient_id) != str(user_id):
            return None
        resource = str(payload.get("resource") or "all")
        return {"resource": resource}
    if event.event_type == "notification.delivered":
        recipient_id = payload.get("recipient_id")
        if recipient_id is None or str(recipient_id) != str(user_id):
            return None
        delivery_id = payload.get("delivery_id")
        return {"delivery_id": str(delivery_id)} if delivery_id is not None else {}
    public: dict[str, object] = {}
    for key in ("entity_type", "entity_id"):
        value = payload.get(key)
        if value is not None:
            public[key] = str(value)
    return public


async def _employee_visible_entity_ids(
    db: AsyncSession,
    context: AuthContext,
    entity_type: str,
    entity_ids: set[uuid.UUID],
) -> set[uuid.UUID]:
    if not entity_ids:
        return set()
    if entity_type == "message":
        return set(
            (
                await db.scalars(
                    sa.select(Message.id)
                    .join(Conversation, Conversation.id == Message.conversation_id)
                    .join(Deal, Deal.id == Conversation.deal_id)
                    .where(
                        Message.id.in_(entity_ids),
                        Message.workspace_id == context.workspace_id,
                        Deal.workspace_id == context.workspace_id,
                        Deal.deleted_at.is_(None),
                        deal_access_condition(context),
                    )
                )
            ).all()
        )
    if entity_type == "deal":
        model: Any = Deal
        condition = deal_access_condition(context)
    elif entity_type == "contact":
        model = Contact
        condition = contact_access_condition(context)
    elif entity_type == "task":
        model = Task
        condition = task_access_condition(context)
    elif entity_type == "company":
        model = Company
        condition = company_access_condition(context)
    else:
        return set()
    filters = [
        model.id.in_(entity_ids),
        model.workspace_id == context.workspace_id,
        condition,
    ]
    if model in {Deal, Contact, Company}:
        filters.append(model.deleted_at.is_(None))
    return set((await db.scalars(sa.select(model.id).where(*filters))).all())


async def _employee_visible_event_ids(
    db: AsyncSession,
    events: list[RealtimeEvent],
    context: AuthContext,
) -> set[int]:
    """Authorize a batch with a bounded number of queries, not one query per event."""

    visible_event_ids: set[int] = set()
    entity_events: dict[str, list[tuple[int, uuid.UUID]]] = {}
    delivery_events: dict[int, uuid.UUID] = {}
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.event_type == "access.changed":
            recipient_id = payload.get("recipient_id")
            if recipient_id is not None and str(recipient_id) == str(context.user_id):
                visible_event_ids.add(event.id)
            continue
        if event.event_type == "notification.delivered":
            recipient_id = payload.get("recipient_id")
            if recipient_id is None or str(recipient_id) != str(context.user_id):
                continue
            try:
                delivery_events[event.id] = uuid.UUID(str(payload.get("delivery_id")))
            except (TypeError, ValueError):
                pass
            continue
        entity_type = str(payload.get("entity_type") or "")
        if entity_type not in {"deal", "contact", "task", "company", "message"}:
            continue
        try:
            entity_id = uuid.UUID(str(payload.get("entity_id")))
        except (TypeError, ValueError):
            continue
        entity_events.setdefault(entity_type, []).append((event.id, entity_id))

    for entity_type, pairs in entity_events.items():
        visible_entities = await _employee_visible_entity_ids(
            db,
            context,
            entity_type,
            {entity_id for _, entity_id in pairs},
        )
        visible_event_ids.update(
            event_id for event_id, entity_id in pairs if entity_id in visible_entities
        )

    if delivery_events:
        deliveries = {
            delivery.id: delivery
            for delivery in (
                await db.scalars(
                    sa.select(NotificationDelivery).where(
                        NotificationDelivery.id.in_(set(delivery_events.values())),
                        NotificationDelivery.workspace_id == context.workspace_id,
                        NotificationDelivery.recipient_id == context.user_id,
                    )
                )
            ).all()
        }
        targets_by_type: dict[str, set[uuid.UUID]] = {"deal": set(), "task": set()}
        for delivery in deliveries.values():
            if (
                delivery.target_entity_type in targets_by_type
                and delivery.target_entity_id is not None
            ):
                targets_by_type[delivery.target_entity_type].add(delivery.target_entity_id)
        visible_targets = {
            entity_type: await _employee_visible_entity_ids(
                db, context, entity_type, entity_ids
            )
            for entity_type, entity_ids in targets_by_type.items()
            if entity_ids
        }
        for event_id, delivery_id in delivery_events.items():
            event_delivery = deliveries.get(delivery_id)
            if (
                event_delivery is not None
                and event_delivery.target_entity_type in visible_targets
                and event_delivery.target_entity_id
                in visible_targets[event_delivery.target_entity_type]
            ):
                visible_event_ids.add(event_id)

    return visible_event_ids


async def _initial_event_cursor(
    workspace_id: uuid.UUID,
    *,
    after_id: int | None,
    last_event_id: int | None,
) -> int:
    """Resolve a workspace-scoped SSE cursor without replaying history to new clients."""

    async with SessionLocal() as db:
        tail = await db.scalar(
            sa.select(sa.func.max(RealtimeEvent.id)).where(
                RealtimeEvent.workspace_id == workspace_id
            )
        )
    tail_id = int(tail or 0)
    if after_id is None and last_event_id is None:
        return tail_id
    requested = max(0, after_id or 0, last_event_id or 0)
    return max(requested, tail_id - MAX_REPLAY_EVENTS)


async def _active_stream_context(
    db: AsyncSession,
    context: AuthContext,
) -> AuthContext | None:
    """Reload mutable authentication state for a long-lived SSE connection."""

    row = (
        await db.execute(
            sa.select(Session, User, Workspace, Membership)
            .join(User, User.id == Session.user_id)
            .join(Workspace, Workspace.id == Session.workspace_id)
            .join(
                Membership,
                sa.and_(
                    Membership.workspace_id == Session.workspace_id,
                    Membership.user_id == Session.user_id,
                ),
            )
            .where(
                Session.id == context.session.id,
                Session.workspace_id == context.workspace_id,
                Session.user_id == context.user_id,
                Session.revoked_at.is_(None),
                Session.expires_at > sa.func.now(),
                User.is_active.is_(True),
            )
        )
    ).one_or_none()
    if row is None:
        return None
    session, user, workspace, membership = row
    return AuthContext(
        user=user,
        workspace=workspace,
        membership=membership,
        session=session,
        via_cookie=context.via_cookie,
    )


@router.get("/events", response_class=StreamingResponse)
async def realtime_events(
    request: Request,
    context: CurrentUser,
    after_id: int | None = Query(default=None, ge=0),
    last_event_id: Annotated[int | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Stream new events; explicit cursors let reconnecting clients replay missed events."""

    workspace_id = context.workspace_id
    initial_cursor = await _initial_event_cursor(
        workspace_id,
        after_id=after_id,
        last_event_id=last_event_id,
    )

    async def stream() -> AsyncIterator[str]:
        cursor = initial_cursor
        stream_context = context
        heartbeat = 0
        listener = None
        settings = get_settings()
        if settings.database_url.startswith("postgresql"):
            try:
                import psycopg

                listener = await psycopg.AsyncConnection.connect(
                    _psycopg_dsn(settings.database_url), autocommit=True
                )
                await listener.execute(f"LISTEN {NOTIFY_CHANNEL}")
            except Exception:
                logger.exception("realtime LISTEN unavailable; falling back to polling")
                listener = None
        try:
            while not await request.is_disconnected():
                role_changed = False
                async with SessionLocal() as db:
                    refreshed_context = await _active_stream_context(db, stream_context)
                    if refreshed_context is None:
                        return
                    role_changed = refreshed_context.role is not stream_context.role
                    stream_context = refreshed_context
                    events = list(
                        (
                            await db.scalars(
                                sa.select(RealtimeEvent)
                                .where(
                                    RealtimeEvent.workspace_id == workspace_id,
                                    RealtimeEvent.id > cursor,
                                )
                                .order_by(RealtimeEvent.id)
                                .limit(100)
                            )
                        ).all()
                    )
                    if is_employee(stream_context):
                        visible_event_ids = await _employee_visible_event_ids(
                            db, events, stream_context
                        )
                    else:
                        visible_event_ids = {event.id for event in events}
                if role_changed:
                    data = json.dumps({"resource": "all"}, separators=(",", ":"))
                    yield f"event: access.changed\ndata: {data}\n\n"
                if events:
                    for event in events:
                        cursor = event.id
                        if event.id not in visible_event_ids:
                            continue
                        public_payload = _public_event_payload(
                            event,
                            user_id=stream_context.user_id,
                        )
                        if public_payload is None:
                            continue
                        data = json.dumps(
                            public_payload,
                            ensure_ascii=False,
                            default=str,
                            separators=(",", ":"),
                        )
                        yield f"id: {event.id}\nevent: {event.event_type}\ndata: {data}\n\n"
                    heartbeat = 0
                    continue
                heartbeat += 1
                if heartbeat >= 15:
                    yield ": keep-alive\n\n"
                    heartbeat = 0
                if listener is None:
                    await asyncio.sleep(1)
                else:
                    async for _notification in listener.notifies(timeout=1, stop_after=1):
                        break
        finally:
            if listener is not None:
                await listener.close()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

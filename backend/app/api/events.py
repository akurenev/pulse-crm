from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.db import SessionLocal
from app.models import RealtimeEvent
from app.security import CurrentUser

router = APIRouter(tags=["realtime"])
logger = logging.getLogger(__name__)
NOTIFY_CHANNEL = "pulse_realtime"


def _psycopg_dsn(database_url: str) -> str:
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _public_event_payload(
    event: RealtimeEvent,
    *,
    user_id: object,
) -> dict[str, object] | None:
    """Return a minimal invalidation payload, or hide a private user event."""

    payload = event.payload if isinstance(event.payload, dict) else {}
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


async def _initial_event_cursor(
    workspace_id: uuid.UUID,
    *,
    after_id: int | None,
    last_event_id: int | None,
) -> int:
    """Resolve a workspace-scoped SSE cursor without replaying history to new clients."""

    if after_id is not None or last_event_id is not None:
        return max(0, after_id or 0, last_event_id or 0)

    async with SessionLocal() as db:
        tail = await db.scalar(
            sa.select(sa.func.max(RealtimeEvent.id)).where(
                RealtimeEvent.workspace_id == workspace_id
            )
        )
    return int(tail or 0)


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
                async with SessionLocal() as db:
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
                if events:
                    for event in events:
                        cursor = event.id
                        public_payload = _public_event_payload(
                            event,
                            user_id=context.user_id,
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

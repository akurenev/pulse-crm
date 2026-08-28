from __future__ import annotations

import asyncio
import json
import logging
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


@router.get("/events", response_class=StreamingResponse)
async def realtime_events(
    request: Request,
    context: CurrentUser,
    after_id: int = Query(default=0, ge=0),
    last_event_id: Annotated[int | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Durable SSE feed. Clients reconnect with Last-Event-ID to replay missed events."""

    workspace_id = context.workspace_id
    initial_cursor = max(after_id, last_event_id or 0)

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
                        data = json.dumps(
                            event.payload,
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

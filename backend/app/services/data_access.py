from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from fastapi import HTTPException, status
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import CursorAccessBucket
from app.services.events import record_audit_event


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _fixed_window_start(now: datetime, window_seconds: int) -> datetime:
    timestamp = math.floor(_as_utc(now).timestamp() / window_seconds) * window_seconds
    return datetime.fromtimestamp(timestamp, tz=UTC)


async def consume_cursor_page_budget(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    resource: str,
    window_seconds: int,
    now: datetime | None = None,
) -> tuple[int, datetime]:
    """Atomically increment one fixed-window bucket and return count/start.

    Production uses PostgreSQL ``ON CONFLICT DO UPDATE``.  SQLite follows the
    same statement shape for deterministic repository tests.
    """

    if not resource or len(resource) > 64:
        raise ValueError("resource must contain 1..64 characters")
    if window_seconds < 1:
        raise ValueError("window_seconds must be positive")
    window_started_at = _fixed_window_start(now or datetime.now(UTC), window_seconds)
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        insert: Any = postgresql_insert(CursorAccessBucket)
    elif dialect == "sqlite":
        insert = sqlite_insert(CursorAccessBucket)
    else:  # The supported production and test databases are explicit.
        raise RuntimeError(f"unsupported pagination budget dialect: {dialect}")

    expired = CursorAccessBucket.window_started_at < window_started_at
    statement = (
        insert.values(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            user_id=user_id,
            resource=resource,
            window_started_at=window_started_at,
            request_count=1,
        )
        .on_conflict_do_update(
            index_elements=[
                CursorAccessBucket.workspace_id,
                CursorAccessBucket.user_id,
                CursorAccessBucket.resource,
            ],
            set_={
                "window_started_at": sa.case(
                    (expired, window_started_at),
                    else_=CursorAccessBucket.window_started_at,
                ),
                "request_count": sa.case(
                    (expired, 1),
                    else_=CursorAccessBucket.request_count + 1,
                ),
            },
        )
        .returning(
            CursorAccessBucket.request_count,
            CursorAccessBucket.window_started_at,
        )
    )
    row = (await db.execute(statement)).one()
    return int(row.request_count), _as_utc(row.window_started_at)


async def enforce_cursor_page_budget(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    resource: str,
    cursor: str | None,
    budget_limit: int | None = None,
    window_seconds: int | None = None,
    now: datetime | None = None,
) -> None:
    """Bound continued list traversal without penalising normal first pages."""

    if cursor is None:
        return
    settings = get_settings()
    effective_limit = (
        budget_limit if budget_limit is not None else settings.cursor_page_budget
    )
    effective_window = (
        window_seconds
        if window_seconds is not None
        else settings.cursor_page_window_seconds
    )
    current_time = _as_utc(now or datetime.now(UTC))
    count, started_at = await consume_cursor_page_budget(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        resource=resource,
        window_seconds=effective_window,
        now=current_time,
    )
    if count <= effective_limit:
        await db.commit()
        return

    # Emit once at the crossing, not on every rejected retry.  The bucket itself
    # remains authoritative and is committed together with this audit record.
    if count == effective_limit + 1:
        record_audit_event(
            db,
            workspace_id=workspace_id,
            event_type="data_access.cursor_budget_exceeded",
            entity_type="user",
            entity_id=user_id,
            actor_id=user_id,
            payload={
                "resource": resource,
                "request_count": count,
                "window_seconds": effective_window,
            },
        )
    await db.commit()
    retry_after = max(
        1,
        math.ceil(
            (started_at + timedelta(seconds=effective_window) - current_time).total_seconds()
        ),
    )
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "code": "cursor_page_budget_exceeded",
            "message": "continued pagination limit exceeded; retry after the window resets",
            "resource": resource,
        },
        headers={"Retry-After": str(retry_after)},
    )

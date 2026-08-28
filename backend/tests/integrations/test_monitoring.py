from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.integrations.models import ScheduledEventMarker
from app.integrations.runtime import (
    JOB_DEAL_INACTIVE_EVENT,
    JOB_TASK_DUE_EVENT,
    JOB_TASK_OVERDUE_EVENT,
    RuntimeHandlers,
    RuntimeScheduler,
)
from app.models import ActivityEvent, OutboxEvent, RealtimeEvent, Task, TaskStatus
from app.services.jobs import ClaimedJob

FIXED_NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)


def _claimed(job_type: str, payload: dict[str, Any]) -> ClaimedJob:
    return ClaimedJob(
        id=uuid.uuid4(),
        job_type=job_type,
        payload=payload,
        attempts=1,
        max_attempts=5,
        lease_owner="monitoring-test",
    )


@pytest.mark.asyncio
async def test_scheduled_monitoring_events_use_idempotent_occurrence_markers(
    db: AsyncSession,
    integration_domain: dict[str, Any],
) -> None:
    workspace = integration_domain["workspace"]
    user = integration_domain["user"]
    deal = integration_domain["deal"]
    deal.last_activity_at = FIXED_NOW - timedelta(days=8)
    due_task = Task(
        workspace_id=workspace.id,
        title="Follow up soon",
        status=TaskStatus.open,
        assignee_id=user.id,
        deal_id=deal.id,
        remind_at=FIXED_NOW - timedelta(minutes=5),
        due_at=FIXED_NOW + timedelta(hours=1),
    )
    overdue_task = Task(
        workspace_id=workspace.id,
        title="Already overdue",
        status=TaskStatus.open,
        assignee_id=user.id,
        deal_id=deal.id,
        due_at=FIXED_NOW - timedelta(minutes=30),
    )
    db.add_all([due_task, overdue_task])
    await db.commit()

    jobs = [
        (
            JOB_TASK_DUE_EVENT,
            {
                "task_id": str(due_task.id),
                "workspace_id": str(workspace.id),
                "occurrence_at": due_task.remind_at.isoformat(),
            },
        ),
        (
            JOB_TASK_OVERDUE_EVENT,
            {
                "task_id": str(overdue_task.id),
                "workspace_id": str(workspace.id),
                "occurrence_at": overdue_task.due_at.isoformat(),
            },
        ),
        (
            JOB_DEAL_INACTIVE_EVENT,
            {
                "deal_id": str(deal.id),
                "workspace_id": str(workspace.id),
                "occurrence_at": deal.last_activity_at.isoformat(),
            },
        ),
    ]
    handlers = RuntimeHandlers(session_factory=SessionLocal, now=lambda: FIXED_NOW)
    registry = handlers.registry()

    for job_type, payload in jobs:
        await registry[job_type](_claimed(job_type, payload))
        await registry[job_type](_claimed(job_type, payload))

    expected_types = {"task.due_soon", "task.overdue", "deal.inactive"}
    markers = list(
        (
            await db.scalars(
                sa.select(ScheduledEventMarker).where(
                    ScheduledEventMarker.workspace_id == workspace.id
                )
            )
        ).all()
    )
    activity_types = list(
        (
            await db.scalars(
                sa.select(ActivityEvent.event_type).where(
                    ActivityEvent.workspace_id == workspace.id,
                    ActivityEvent.event_type.in_(expected_types),
                )
            )
        ).all()
    )
    outbox_types = list(
        (
            await db.scalars(
                sa.select(OutboxEvent.event_type).where(
                    OutboxEvent.workspace_id == workspace.id,
                    OutboxEvent.event_type.in_(expected_types),
                )
            )
        ).all()
    )
    realtime_types = list(
        (
            await db.scalars(
                sa.select(RealtimeEvent.event_type).where(
                    RealtimeEvent.workspace_id == workspace.id,
                    RealtimeEvent.event_type.in_(expected_types),
                )
            )
        ).all()
    )

    assert len(markers) == 3
    assert {marker.event_type for marker in markers} == expected_types
    assert set(activity_types) == expected_types and len(activity_types) == 3
    assert set(outbox_types) == expected_types and len(outbox_types) == 3
    assert set(realtime_types) == expected_types and len(realtime_types) == 3

    scheduler = RuntimeScheduler(session_factory=SessionLocal, now=lambda: FIXED_NOW)
    tick = await scheduler.tick()
    assert tick.scheduled.get(JOB_TASK_DUE_EVENT, 0) == 0
    assert tick.scheduled.get(JOB_TASK_OVERDUE_EVENT, 0) == 0
    assert tick.scheduled.get(JOB_DEAL_INACTIVE_EVENT, 0) == 0

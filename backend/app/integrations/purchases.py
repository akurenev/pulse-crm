"""Exactly-once scheduling for the next expected customer purchase."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.models import PurchaseSchedule, PurchaseScheduleStatus
from app.models import OutboxEvent, Task, TaskStatus


@dataclass(frozen=True, slots=True)
class PurchaseTaskResult:
    schedule: PurchaseSchedule
    task: Task
    created: bool


def _insert_for(session: AsyncSession) -> Any:
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        return pg_insert(PurchaseSchedule)
    if dialect_name == "sqlite":
        return sqlite_insert(PurchaseSchedule)
    raise RuntimeError(f"unsupported database dialect for idempotent insert: {dialect_name}")


async def create_purchase_schedule(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    deal_id: uuid.UUID,
    contact_id: uuid.UUID | None,
    assignee_id: uuid.UUID,
    scheduled_for: datetime,
    remind_at: datetime,
) -> tuple[PurchaseSchedule, bool]:
    if scheduled_for.tzinfo is None or remind_at.tzinfo is None:
        raise ValueError("purchase schedule timestamps must be timezone-aware")
    if remind_at > scheduled_for:
        raise ValueError("purchase reminder cannot be after the expected purchase")

    schedule_id = uuid.uuid4()
    statement = (
        _insert_for(session)
        .values(
            id=schedule_id,
            workspace_id=workspace_id,
            deal_id=deal_id,
            contact_id=contact_id,
            assignee_id=assignee_id,
            scheduled_for=scheduled_for,
            remind_at=remind_at,
            status=PurchaseScheduleStatus.active,
            version=1,
        )
        .on_conflict_do_nothing(
            index_elements=[
                PurchaseSchedule.workspace_id,
                PurchaseSchedule.deal_id,
                PurchaseSchedule.scheduled_for,
            ]
        )
        .returning(PurchaseSchedule.id)
    )
    inserted_id = (await session.execute(statement)).scalar_one_or_none()
    if inserted_id is not None:
        schedule = await session.get(PurchaseSchedule, inserted_id)
        if schedule is None:  # pragma: no cover
            raise RuntimeError("inserted purchase schedule could not be loaded")
        return schedule, True

    schedule = await session.scalar(
        sa.select(PurchaseSchedule).where(
            PurchaseSchedule.workspace_id == workspace_id,
            PurchaseSchedule.deal_id == deal_id,
            PurchaseSchedule.scheduled_for == scheduled_for,
        )
    )
    if schedule is None:  # pragma: no cover
        raise RuntimeError("deduplicated purchase schedule could not be loaded")
    return schedule, False


async def ensure_purchase_task(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    schedule_id: uuid.UUID,
    title: str = "Связаться по поводу следующей покупки",
) -> PurchaseTaskResult:
    """Create one task and one reminder outbox event for a schedule.

    The schedule row is locked before checking ``task_id``.  Repeated scheduler
    runs therefore return the same task even when they overlap.
    """

    schedule = await session.scalar(
        sa.select(PurchaseSchedule)
        .where(
            PurchaseSchedule.id == schedule_id,
            PurchaseSchedule.workspace_id == workspace_id,
        )
        .with_for_update()
    )
    if schedule is None:
        raise LookupError("purchase schedule not found")
    if schedule.status is not PurchaseScheduleStatus.active:
        raise ValueError("purchase schedule is not active")
    if schedule.task_id is not None:
        task = await session.scalar(
            sa.select(Task).where(
                Task.id == schedule.task_id,
                Task.workspace_id == workspace_id,
            )
        )
        if task is None:
            raise RuntimeError("purchase schedule references a missing task")
        return PurchaseTaskResult(schedule=schedule, task=task, created=False)

    task = Task(
        workspace_id=workspace_id,
        title=title,
        description="Задача создана из даты следующей сделки.",
        task_type="next_purchase",
        status=TaskStatus.open,
        due_at=schedule.scheduled_for,
        remind_at=schedule.remind_at,
        assignee_id=schedule.assignee_id,
        deal_id=schedule.deal_id,
        contact_id=schedule.contact_id,
    )
    session.add(task)
    await session.flush()
    schedule.task_id = task.id
    schedule.reminder_enqueued_at = datetime.now(UTC)
    schedule.version += 1
    session.add(
        OutboxEvent(
            workspace_id=workspace_id,
            event_type="purchase.due_soon",
            aggregate_type="purchase_schedule",
            aggregate_id=schedule.id,
            payload={
                "schedule_id": str(schedule.id),
                "task_id": str(task.id),
                "dedupe_key": f"purchase-schedule:{schedule.id}:reminder",
            },
            available_at=schedule.remind_at,
        )
    )
    return PurchaseTaskResult(schedule=schedule, task=task, created=True)

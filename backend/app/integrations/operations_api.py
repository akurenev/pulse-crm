"""Dashboard metrics and small operational controls for the single webapp."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.integrations.models import (
    NotificationDelivery,
    PurchaseSchedule,
    PurchaseScheduleStatus,
)
from app.models import (
    ActivityEvent,
    BackgroundJob,
    Deal,
    DeliveryStatus,
    JobStatus,
    Pipeline,
    Stage,
    StageType,
    Task,
    TaskStatus,
)
from app.security import CurrentAdmin, CurrentUser

router = APIRouter(tags=["operations"])


class PipelineConversion(BaseModel):
    pipeline_id: uuid.UUID
    pipeline_name: str
    total_deals: int
    won_deals: int
    conversion_percent: float


class DashboardRead(BaseModel):
    new_leads_24h: int
    overdue_tasks: int
    inactive_deals: int
    upcoming_purchases_30d: int
    pipelines: list[PipelineConversion]


class BackgroundJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_type: str
    status: JobStatus
    run_at: datetime
    attempts: int
    max_attempts: int
    dedupe_key: str | None
    lease_until: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class DeliveryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel: str
    recipient_address: str
    subject: str | None
    status: DeliveryStatus
    scheduled_at: datetime
    delivered_at: datetime | None
    attempts: int
    last_error: str | None


class InAppNotificationRead(BaseModel):
    id: uuid.UUID
    subject: str | None
    body: str
    delivered_at: datetime
    created_at: datetime


@router.get("/dashboard", response_model=DashboardRead)
async def dashboard(
    context: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> DashboardRead:
    now = datetime.now(UTC)
    new_leads = await db.scalar(
        sa.select(sa.func.count())
        .select_from(ActivityEvent)
        .where(
            ActivityEvent.workspace_id == context.workspace_id,
            ActivityEvent.event_type == "lead.created",
            ActivityEvent.occurred_at >= now - timedelta(hours=24),
        )
    )
    overdue = await db.scalar(
        sa.select(sa.func.count())
        .select_from(Task)
        .where(
            Task.workspace_id == context.workspace_id,
            Task.status == TaskStatus.open,
            Task.due_at < now,
        )
    )
    inactive = await db.scalar(
        sa.select(sa.func.count())
        .select_from(Deal)
        .join(Stage, Stage.id == Deal.stage_id)
        .where(
            Deal.workspace_id == context.workspace_id,
            Deal.deleted_at.is_(None),
            Deal.last_activity_at < now - timedelta(days=7),
            Stage.workspace_id == context.workspace_id,
            Stage.stage_type == StageType.open,
        )
    )
    purchases = await db.scalar(
        sa.select(sa.func.count())
        .select_from(PurchaseSchedule)
        .where(
            PurchaseSchedule.workspace_id == context.workspace_id,
            PurchaseSchedule.status == PurchaseScheduleStatus.active,
            PurchaseSchedule.scheduled_for >= now,
            PurchaseSchedule.scheduled_for <= now + timedelta(days=30),
        )
    )
    pipeline_rows = (
        await db.execute(
            sa.select(
                Pipeline.id,
                Pipeline.name,
                sa.func.count(Deal.id),
                sa.func.count(Deal.id).filter(Stage.stage_type == StageType.won),
            )
            .outerjoin(
                Deal,
                sa.and_(
                    Deal.pipeline_id == Pipeline.id,
                    Deal.workspace_id == context.workspace_id,
                    Deal.deleted_at.is_(None),
                ),
            )
            .outerjoin(Stage, Stage.id == Deal.stage_id)
            .where(
                Pipeline.workspace_id == context.workspace_id,
                Pipeline.is_active.is_(True),
            )
            .group_by(Pipeline.id, Pipeline.name, Pipeline.position)
            .order_by(Pipeline.position, Pipeline.name)
        )
    ).all()
    conversions = []
    for pipeline_id, name, total, won in pipeline_rows:
        total_count = int(total or 0)
        won_count = int(won or 0)
        conversions.append(
            PipelineConversion(
                pipeline_id=pipeline_id,
                pipeline_name=name,
                total_deals=total_count,
                won_deals=won_count,
                conversion_percent=round(won_count * 100 / total_count, 1) if total_count else 0.0,
            )
        )
    return DashboardRead(
        new_leads_24h=int(new_leads or 0),
        overdue_tasks=int(overdue or 0),
        inactive_deals=int(inactive or 0),
        upcoming_purchases_30d=int(purchases or 0),
        pipelines=conversions,
    )


@router.get("/admin/jobs", response_model=list[BackgroundJobRead])
async def list_background_jobs(
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
    job_status: JobStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[BackgroundJob]:
    query = sa.select(BackgroundJob).where(BackgroundJob.workspace_id == context.workspace_id)
    if job_status is not None:
        query = query.where(BackgroundJob.status == job_status)
    return list(
        (
            await db.scalars(
                query.order_by(BackgroundJob.updated_at.desc(), BackgroundJob.id).limit(limit)
            )
        ).all()
    )


@router.get("/notifications", response_model=list[InAppNotificationRead])
async def list_in_app_notifications(
    context: CurrentUser,
    db: AsyncSession = Depends(get_session),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[InAppNotificationRead]:
    deliveries = list(
        (
            await db.scalars(
                sa.select(NotificationDelivery)
                .where(
                    NotificationDelivery.workspace_id == context.workspace_id,
                    NotificationDelivery.channel == "in_app",
                    NotificationDelivery.recipient_id == context.user_id,
                    NotificationDelivery.status == DeliveryStatus.delivered,
                    NotificationDelivery.delivered_at.is_not(None),
                )
                .order_by(NotificationDelivery.delivered_at.desc())
                .limit(limit)
            )
        ).all()
    )
    return [
        InAppNotificationRead(
            id=delivery.id,
            subject=delivery.subject,
            body=delivery.body,
            delivered_at=delivery.delivered_at,
            created_at=delivery.created_at,
        )
        for delivery in deliveries
        if delivery.delivered_at is not None
    ]


@router.post("/admin/jobs/{job_id}/retry", response_model=BackgroundJobRead)
async def retry_background_job(
    job_id: uuid.UUID,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> BackgroundJob:
    job = await db.scalar(
        sa.select(BackgroundJob)
        .where(
            BackgroundJob.id == job_id,
            BackgroundJob.workspace_id == context.workspace_id,
        )
        .with_for_update()
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    if job.status is not JobStatus.failed:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="job is not failed")
    job.status = JobStatus.queued
    job.attempts = 0
    job.run_at = datetime.now(UTC)
    job.lease_owner = None
    job.lease_until = None
    job.last_error = None
    await db.commit()
    return job


@router.get("/admin/notification-deliveries", response_model=list[DeliveryRead])
async def list_notification_deliveries(
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
    delivery_status: DeliveryStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[NotificationDelivery]:
    query = sa.select(NotificationDelivery).where(
        NotificationDelivery.workspace_id == context.workspace_id
    )
    if delivery_status is not None:
        query = query.where(NotificationDelivery.status == delivery_status)
    return list(
        (
            await db.scalars(query.order_by(NotificationDelivery.updated_at.desc()).limit(limit))
        ).all()
    )


@router.post(
    "/admin/notification-deliveries/{delivery_id}/retry",
    response_model=DeliveryRead,
)
async def retry_notification_delivery(
    delivery_id: uuid.UUID,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> NotificationDelivery:
    delivery = await db.scalar(
        sa.select(NotificationDelivery)
        .where(
            NotificationDelivery.id == delivery_id,
            NotificationDelivery.workspace_id == context.workspace_id,
        )
        .with_for_update()
    )
    if delivery is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="delivery not found")
    if delivery.status is not DeliveryStatus.failed:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="delivery is not failed")
    delivery.status = DeliveryStatus.pending
    delivery.attempts = 0
    delivery.scheduled_at = datetime.now(UTC)
    delivery.delivered_at = None
    delivery.provider_message_id = None
    delivery.last_error = None
    job = await db.scalar(
        sa.select(BackgroundJob).where(
            BackgroundJob.workspace_id == context.workspace_id,
            BackgroundJob.dedupe_key == f"notification-delivery:{delivery.id}:send",
        )
    )
    if job is not None:
        job.status = JobStatus.queued
        job.attempts = 0
        job.run_at = datetime.now(UTC)
        job.lease_owner = None
        job.lease_until = None
        job.last_error = None
    await db.commit()
    return delivery

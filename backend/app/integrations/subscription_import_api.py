"""Administrative API for subscription renewal spreadsheet imports."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.integrations.models import ImportJob, ImportStatus
from app.integrations.subscription_import import (
    FORMAT_KEY,
    SubscriptionImportError,
    default_call_due_at,
    import_subscriptions,
    parse_frontol_endlic,
)
from app.models import Membership, Pipeline, Stage, StageType, User
from app.security import CurrentAdmin

router = APIRouter(prefix="/admin/integrations/subscription-imports", tags=["admin"])
MAX_IMPORT_BYTES = 10 * 1024 * 1024


class SubscriptionImportGroupRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    organization_name: str
    inn: str
    expires_at: date
    license_count: int
    products: tuple[str, ...]
    contact_count: int
    action: str
    error: str | None


class SubscriptionImportJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: str
    status: ImportStatus
    dry_run: bool
    entity_type: str | None
    counts: dict[str, int]
    started_at: datetime | None
    completed_at: datetime | None
    last_error: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class SubscriptionImportRead(BaseModel):
    job: SubscriptionImportJobRead
    format: str
    counts: dict[str, int]
    groups: list[SubscriptionImportGroupRead]
    warnings: list[str]


@router.post("", response_model=SubscriptionImportRead)
async def upload_subscription_import(
    context: CurrentAdmin,
    file: UploadFile = File(...),
    pipeline_id: uuid.UUID = Form(...),
    assignee_id: uuid.UUID = Form(...),
    stage_id: uuid.UUID | None = Form(default=None),
    call_due_at: datetime | None = Form(default=None),
    dry_run: bool = Form(default=True),
    db: AsyncSession = Depends(get_session),
) -> SubscriptionImportRead:
    content = await file.read(MAX_IMPORT_BYTES + 1)
    if len(content) > MAX_IMPORT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="spreadsheet exceeds the 10 MB limit",
        )
    filename = (file.filename or "").casefold()
    if not filename.endswith(".xlsx"):
        raise HTTPException(status_code=422, detail="only .xlsx files are supported")
    try:
        parsed = parse_frontol_endlic(content)
    except SubscriptionImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    stage = await _validate_route(
        db,
        workspace_id=context.workspace_id,
        pipeline_id=pipeline_id,
        stage_id=stage_id,
        assignee_id=assignee_id,
    )
    due_at = call_due_at or default_call_due_at(timezone_name=context.workspace.timezone)
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=UTC)
    else:
        due_at = due_at.astimezone(UTC)

    started_at = datetime.now(UTC)
    job = ImportJob(
        workspace_id=context.workspace_id,
        provider="subscription_xlsx",
        status=ImportStatus.running,
        dry_run=dry_run,
        entity_type="renewals",
        cursor={
            "format": FORMAT_KEY,
            "pipeline_id": str(pipeline_id),
            "stage_id": str(stage.id),
            "assignee_id": str(assignee_id),
            "filename": file.filename or "subscriptions.xlsx",
            "file_sha256": parsed.file_sha256,
        },
        started_at=started_at,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    try:
        outcome = await import_subscriptions(
            db,
            workspace_id=context.workspace_id,
            actor_id=context.user_id,
            pipeline_id=pipeline_id,
            stage_id=stage.id,
            assignee_id=assignee_id,
            call_due_at=due_at,
            parsed=parsed,
            dry_run=dry_run,
        )
        refreshed_job = await db.get(ImportJob, job.id)
        if refreshed_job is None:  # pragma: no cover - database integrity guard
            raise RuntimeError("subscription import job disappeared")
        refreshed_job.status = ImportStatus.succeeded
        refreshed_job.counts = outcome.counts
        refreshed_job.completed_at = datetime.now(UTC)
        refreshed_job.version += 1
        await db.commit()
        await db.refresh(refreshed_job)
        job = refreshed_job
    except Exception as exc:
        await db.rollback()
        failed_job = await db.get(ImportJob, job.id)
        if failed_job is not None:
            failed_job.status = ImportStatus.failed
            failed_job.last_error = str(exc)[:4_000]
            failed_job.completed_at = datetime.now(UTC)
            failed_job.version += 1
            await db.commit()
        raise

    return SubscriptionImportRead(
        job=SubscriptionImportJobRead.model_validate(job),
        format=FORMAT_KEY,
        counts=outcome.counts,
        groups=[SubscriptionImportGroupRead.model_validate(item) for item in outcome.groups],
        warnings=list(outcome.warnings),
    )


async def _validate_route(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    stage_id: uuid.UUID | None,
    assignee_id: uuid.UUID,
) -> Stage:
    pipeline = await db.scalar(
        sa.select(Pipeline).where(
            Pipeline.id == pipeline_id,
            Pipeline.workspace_id == workspace_id,
            Pipeline.is_active.is_(True),
        )
    )
    if pipeline is None:
        raise HTTPException(status_code=422, detail="pipeline is not active")
    stage_query = sa.select(Stage).where(
        Stage.workspace_id == workspace_id,
        Stage.pipeline_id == pipeline_id,
        Stage.stage_type == StageType.open,
    )
    if stage_id is not None:
        stage_query = stage_query.where(Stage.id == stage_id)
    else:
        stage_query = stage_query.order_by(Stage.position, Stage.created_at).limit(1)
    stage = await db.scalar(stage_query)
    if stage is None:
        raise HTTPException(status_code=422, detail="pipeline has no matching open stage")
    member = await db.scalar(
        sa.select(Membership)
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.workspace_id == workspace_id,
            Membership.user_id == assignee_id,
            User.is_active.is_(True),
        )
    )
    if member is None:
        raise HTTPException(status_code=422, detail="assignee is not an active workspace member")
    return stage


__all__ = ["router"]

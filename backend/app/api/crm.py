from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.integrations.identity import IdentityNormalizationError, sync_contact_points
from app.integrations.models import (
    ChannelConnection,
    ContactPoint,
    ExternalEntityMap,
    ExternalIdentity,
    Form,
    NotificationDelivery,
    NotificationRule,
    PurchaseSchedule,
    PurchaseScheduleStatus,
    WebhookEndpoint,
)
from app.integrations.purchases import create_purchase_schedule
from app.models import (
    ActivityEvent,
    BackgroundJob,
    Company,
    Contact,
    CustomFieldDefinition,
    Deal,
    DealContact,
    DealStageHistory,
    DeliveryStatus,
    FieldEntity,
    JobStatus,
    Membership,
    OutboxEvent,
    Pipeline,
    Source,
    Stage,
    StageRequiredField,
    StageType,
    Task,
    TaskStatus,
    User,
)
from app.pagination import decode_cursor, encode_cursor
from app.schemas import (
    ActivityPage,
    ActivityRead,
    CompanyCreate,
    CompanyPage,
    CompanyRead,
    CompanyUpdate,
    ContactCreate,
    ContactPage,
    ContactRead,
    ContactUpdate,
    CustomFieldCreate,
    CustomFieldRead,
    DealCreate,
    DealPage,
    DealRead,
    DealUpdate,
    NoteCreate,
    PipelineCreate,
    PipelineRead,
    PipelineUpdate,
    RequiredFieldRead,
    RequiredFieldsReplace,
    SourceRead,
    StageAppendCreate,
    StageRead,
    StageTransition,
    StageUpdate,
    TaskCreate,
    TaskPage,
    TaskRead,
    TaskUpdate,
)
from app.security import CurrentAdmin, CurrentMutationUser, CurrentUser
from app.services.data_access import enforce_cursor_page_budget
from app.services.events import record_domain_event

router = APIRouter(tags=["crm"])
BUILT_IN_REQUIRED_FIELDS = {
    "title",
    "company_id",
    "contact_ids",
    "assignee_id",
    "amount",
    "source_id",
    "next_purchase_at",
}


def not_found(entity: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{entity} not found")


def conflict() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "version_conflict", "message": "record was modified by another user"},
    )


def deletion_conflict(
    code: str, message: str, references: list[str] | None = None
) -> HTTPException:
    detail: dict[str, Any] = {"code": code, "message": message}
    if references:
        detail["references"] = references
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _cursor_condition(model: Any, cursor_value: str | None) -> Any | None:
    cursor = decode_cursor(cursor_value)
    if cursor is None:
        return None
    return sa.or_(
        model.created_at < cursor.created_at,
        sa.and_(model.created_at == cursor.created_at, model.id < cursor.entity_id),
    )


def _next_cursor(items: list[Any], limit: int) -> tuple[list[Any], str | None]:
    has_more = len(items) > limit
    visible = items[:limit]
    cursor = encode_cursor(visible[-1].created_at, visible[-1].id) if has_more and visible else None
    return visible, cursor


def _literal_contains_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _tags_search_condition(tags: Any, term: str, dialect_name: str) -> Any:
    if dialect_name == "postgresql":
        tag_values = sa.func.jsonb_array_elements_text(tags).table_valued("value").alias()
    elif dialect_name == "sqlite":
        tag_values = sa.func.json_each(tags).table_valued("value").alias()
    else:
        raise RuntimeError(f"unsupported database dialect for tag search: {dialect_name}")
    return sa.exists(
        sa.select(1)
        .select_from(tag_values)
        .where(
            sa.cast(tag_values.c.value, sa.String).ilike(
                _literal_contains_pattern(term), escape="\\"
            )
        )
    )


def _json_object_values_search_condition(values: Any, term: str, dialect_name: str) -> Any:
    if dialect_name == "postgresql":
        object_values = sa.func.jsonb_each_text(values).table_valued("key", "value").alias()
    elif dialect_name == "sqlite":
        object_values = sa.func.json_each(values).table_valued("key", "value").alias()
    else:
        raise RuntimeError(f"unsupported database dialect for JSON search: {dialect_name}")
    return sa.exists(
        sa.select(1)
        .select_from(object_values)
        .where(
            sa.cast(object_values.c.value, sa.String).ilike(
                _literal_contains_pattern(term), escape="\\"
            )
        )
    )


async def _ensure_company(
    db: AsyncSession, workspace_id: uuid.UUID, entity_id: uuid.UUID
) -> Company:
    entity = await db.scalar(
        sa.select(Company).where(
            Company.id == entity_id,
            Company.workspace_id == workspace_id,
            Company.deleted_at.is_(None),
        )
    )
    if entity is None:
        raise not_found("company")
    return entity


async def _ensure_contact(
    db: AsyncSession, workspace_id: uuid.UUID, entity_id: uuid.UUID
) -> Contact:
    entity = await db.scalar(
        sa.select(Contact).where(
            Contact.id == entity_id,
            Contact.workspace_id == workspace_id,
            Contact.deleted_at.is_(None),
        )
    )
    if entity is None:
        raise not_found("contact")
    return entity


async def _ensure_deal(db: AsyncSession, workspace_id: uuid.UUID, entity_id: uuid.UUID) -> Deal:
    entity = await db.scalar(
        sa.select(Deal).where(
            Deal.id == entity_id,
            Deal.workspace_id == workspace_id,
            Deal.deleted_at.is_(None),
        )
    )
    if entity is None:
        raise not_found("deal")
    return entity


async def _ensure_pipeline(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    entity_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> Pipeline:
    query = sa.select(Pipeline).where(
        Pipeline.id == entity_id,
        Pipeline.workspace_id == workspace_id,
    )
    if for_update:
        query = query.with_for_update()
    entity = await db.scalar(query)
    if entity is None:
        raise not_found("pipeline")
    return entity


async def _ensure_stage(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    entity_id: uuid.UUID,
    *,
    pipeline_id: uuid.UUID | None = None,
    for_update: bool = False,
) -> Stage:
    query = sa.select(Stage).where(
        Stage.id == entity_id,
        Stage.workspace_id == workspace_id,
    )
    if pipeline_id is not None:
        query = query.where(Stage.pipeline_id == pipeline_id)
    if for_update:
        query = query.with_for_update()
    entity = await db.scalar(query)
    if entity is None:
        raise not_found("stage")
    return entity


async def _ensure_member(db: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID) -> None:
    member_id = await db.scalar(
        sa.select(Membership.id).where(
            Membership.workspace_id == workspace_id, Membership.user_id == user_id
        )
    )
    if member_id is None:
        raise not_found("workspace member")


def _clean_resource_name(value: str, entity: str) -> str:
    name = value.strip()
    if not name:
        raise HTTPException(status_code=422, detail=f"{entity} name cannot be blank")
    return name


async def _has_reference(db: AsyncSession, *conditions: Any) -> bool:
    return bool(await db.scalar(sa.select(sa.exists().where(*conditions))))


async def _stage_references(
    db: AsyncSession, workspace_id: uuid.UUID, stage_id: uuid.UUID
) -> list[str]:
    checks = {
        "deals": (
            Deal.workspace_id == workspace_id,
            Deal.stage_id == stage_id,
        ),
        "deal_stage_history": (
            DealStageHistory.workspace_id == workspace_id,
            sa.or_(
                DealStageHistory.from_stage_id == stage_id,
                DealStageHistory.to_stage_id == stage_id,
            ),
        ),
        "channel_connections": (
            ChannelConnection.workspace_id == workspace_id,
            ChannelConnection.default_stage_id == stage_id,
        ),
        "notification_rules": (
            NotificationRule.workspace_id == workspace_id,
            NotificationRule.stage_id == stage_id,
        ),
        "forms": (
            Form.workspace_id == workspace_id,
            Form.stage_id == stage_id,
        ),
        "webhook_endpoints": (
            WebhookEndpoint.workspace_id == workspace_id,
            WebhookEndpoint.stage_id == stage_id,
        ),
    }
    return [name for name, conditions in checks.items() if await _has_reference(db, *conditions)]


async def _pipeline_references(
    db: AsyncSession, workspace_id: uuid.UUID, pipeline_id: uuid.UUID
) -> list[str]:
    stage_ids = sa.select(Stage.id).where(
        Stage.workspace_id == workspace_id,
        Stage.pipeline_id == pipeline_id,
    )
    checks = {
        "deals": (
            Deal.workspace_id == workspace_id,
            sa.or_(Deal.pipeline_id == pipeline_id, Deal.stage_id.in_(stage_ids)),
        ),
        "deal_stage_history": (
            DealStageHistory.workspace_id == workspace_id,
            sa.or_(
                DealStageHistory.from_stage_id.in_(stage_ids),
                DealStageHistory.to_stage_id.in_(stage_ids),
            ),
        ),
        "channel_connections": (
            ChannelConnection.workspace_id == workspace_id,
            sa.or_(
                ChannelConnection.default_pipeline_id == pipeline_id,
                ChannelConnection.default_stage_id.in_(stage_ids),
            ),
        ),
        "notification_rules": (
            NotificationRule.workspace_id == workspace_id,
            sa.or_(
                NotificationRule.pipeline_id == pipeline_id,
                NotificationRule.stage_id.in_(stage_ids),
            ),
        ),
        "forms": (
            Form.workspace_id == workspace_id,
            sa.or_(Form.pipeline_id == pipeline_id, Form.stage_id.in_(stage_ids)),
        ),
        "webhook_endpoints": (
            WebhookEndpoint.workspace_id == workspace_id,
            sa.or_(
                WebhookEndpoint.pipeline_id == pipeline_id,
                WebhookEndpoint.stage_id.in_(stage_ids),
            ),
        ),
    }
    return [name for name, conditions in checks.items() if await _has_reference(db, *conditions)]


async def _pipeline_read(db: AsyncSession, pipeline: Pipeline) -> PipelineRead:
    stages = list(
        (
            await db.scalars(
                sa.select(Stage)
                .where(
                    Stage.workspace_id == pipeline.workspace_id,
                    Stage.pipeline_id == pipeline.id,
                )
                .order_by(Stage.position, Stage.created_at)
            )
        ).all()
    )
    return PipelineRead(
        id=pipeline.id,
        name=pipeline.name,
        position=pipeline.position,
        is_active=pipeline.is_active,
        version=pipeline.version,
        stages=[StageRead.model_validate(stage) for stage in stages],
    )


@router.get("/companies", response_model=CompanyPage)
async def list_companies(
    context: CurrentUser,
    db: AsyncSession = Depends(get_session),
    search: str | None = Query(default=None, max_length=200),
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> CompanyPage:
    await enforce_cursor_page_budget(
        db,
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        resource="companies",
        cursor=cursor,
    )
    query = sa.select(Company).where(
        Company.workspace_id == context.workspace_id, Company.deleted_at.is_(None)
    )
    if search and (term := search.strip()):
        query = query.where(
            sa.or_(
                Company.name.ilike(f"%{term}%"),
                _tags_search_condition(Company.tags, term, db.get_bind().dialect.name),
            )
        )
    if (condition := _cursor_condition(Company, cursor)) is not None:
        query = query.where(condition)
    items = list(
        (
            await db.scalars(
                query.order_by(Company.created_at.desc(), Company.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    items, next_cursor = _next_cursor(items, limit)
    return CompanyPage(
        items=[CompanyRead.model_validate(item) for item in items], next_cursor=next_cursor
    )


@router.post("/companies", response_model=CompanyRead, status_code=status.HTTP_201_CREATED)
async def create_company(
    payload: CompanyCreate,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> Company:
    company = Company(workspace_id=context.workspace_id, **payload.model_dump(mode="json"))
    db.add(company)
    await db.flush()
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="company.created",
        entity_type="company",
        entity_id=company.id,
        actor_id=context.user_id,
    )
    await db.commit()
    return company


@router.get("/companies/{company_id}", response_model=CompanyRead)
async def get_company(
    company_id: uuid.UUID, context: CurrentUser, db: AsyncSession = Depends(get_session)
) -> Company:
    return await _ensure_company(db, context.workspace_id, company_id)


@router.patch("/companies/{company_id}", response_model=CompanyRead)
async def update_company(
    company_id: uuid.UUID,
    payload: CompanyUpdate,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> Company:
    values = payload.model_dump(exclude_unset=True, exclude={"expected_version"}, mode="json")
    values.update(version=payload.expected_version + 1, updated_at=datetime.now(UTC))
    company = (
        await db.execute(
            sa.update(Company)
            .where(
                Company.id == company_id,
                Company.workspace_id == context.workspace_id,
                Company.deleted_at.is_(None),
                Company.version == payload.expected_version,
            )
            .values(**values)
            .returning(Company)
        )
    ).scalar_one_or_none()
    if company is None:
        if await db.scalar(
            sa.select(Company.id).where(
                Company.id == company_id, Company.workspace_id == context.workspace_id
            )
        ):
            raise conflict()
        raise not_found("company")
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="company.updated",
        entity_type="company",
        entity_id=company.id,
        actor_id=context.user_id,
        payload={"fields": sorted(values.keys() - {"updated_at", "version"})},
    )
    await db.commit()
    return company


@router.get("/contacts", response_model=ContactPage)
async def list_contacts(
    context: CurrentUser,
    db: AsyncSession = Depends(get_session),
    search: str | None = Query(default=None, max_length=200),
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> ContactPage:
    await enforce_cursor_page_budget(
        db,
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        resource="contacts",
        cursor=cursor,
    )
    query = sa.select(Contact).where(
        Contact.workspace_id == context.workspace_id, Contact.deleted_at.is_(None)
    )
    if search and (search_term := search.strip()):
        term = f"%{search_term}%"
        tags_match = _tags_search_condition(
            Contact.tags, search_term, db.get_bind().dialect.name
        )
        if db.get_bind().dialect.name == "postgresql":
            document = sa.func.to_tsvector(
                sa.text("'simple'"),
                sa.func.coalesce(Contact.first_name, "")
                + " "
                + sa.func.coalesce(Contact.last_name, "")
                + " "
                + sa.func.coalesce(Contact.primary_email, "")
                + " "
                + sa.func.coalesce(Contact.primary_phone, ""),
            )
            query = query.where(
                sa.or_(
                    document.op("@@")(
                        sa.func.websearch_to_tsquery(sa.text("'simple'"), search_term)
                    ),
                    tags_match,
                )
            )
        else:
            query = query.where(
                sa.or_(
                    Contact.first_name.ilike(term),
                    Contact.last_name.ilike(term),
                    Contact.primary_email.ilike(term),
                    Contact.primary_phone.ilike(term),
                    tags_match,
                )
            )
    if (condition := _cursor_condition(Contact, cursor)) is not None:
        query = query.where(condition)
    items = list(
        (
            await db.scalars(
                query.order_by(Contact.created_at.desc(), Contact.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    items, next_cursor = _next_cursor(items, limit)
    return ContactPage(
        items=[ContactRead.model_validate(item) for item in items], next_cursor=next_cursor
    )


@router.post("/contacts", response_model=ContactRead, status_code=status.HTTP_201_CREATED)
async def create_contact(
    payload: ContactCreate,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> Contact:
    if payload.company_id:
        await _ensure_company(db, context.workspace_id, payload.company_id)
    data = payload.model_dump()
    contact = Contact(workspace_id=context.workspace_id, **data)
    db.add(contact)
    await db.flush()
    try:
        await sync_contact_points(db, contact)
    except IdentityNormalizationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="contact.created",
        entity_type="contact",
        entity_id=contact.id,
        actor_id=context.user_id,
    )
    await db.commit()
    return contact


@router.get("/contacts/{contact_id}", response_model=ContactRead)
async def get_contact(
    contact_id: uuid.UUID, context: CurrentUser, db: AsyncSession = Depends(get_session)
) -> Contact:
    return await _ensure_contact(db, context.workspace_id, contact_id)


@router.patch("/contacts/{contact_id}", response_model=ContactRead)
async def update_contact(
    contact_id: uuid.UUID,
    payload: ContactUpdate,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> Contact:
    for required_field in (
        "first_name",
        "last_name",
        "emails",
        "phones",
        "tags",
        "custom_fields",
    ):
        if required_field in payload.model_fields_set and getattr(payload, required_field) is None:
            raise HTTPException(
                status_code=422,
                detail=f"{required_field} cannot be null",
            )
    if "company_id" in payload.model_fields_set and payload.company_id:
        await _ensure_company(db, context.workspace_id, payload.company_id)
    values = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
    values.update(version=payload.expected_version + 1, updated_at=datetime.now(UTC))
    contact = (
        await db.execute(
            sa.update(Contact)
            .where(
                Contact.id == contact_id,
                Contact.workspace_id == context.workspace_id,
                Contact.deleted_at.is_(None),
                Contact.version == payload.expected_version,
            )
            .values(**values)
            .returning(Contact)
        )
    ).scalar_one_or_none()
    if contact is None:
        if await db.scalar(
            sa.select(Contact.id).where(
                Contact.id == contact_id,
                Contact.workspace_id == context.workspace_id,
                Contact.deleted_at.is_(None),
            )
        ):
            raise conflict()
        raise not_found("contact")
    try:
        await sync_contact_points(db, contact)
    except IdentityNormalizationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="contact.updated",
        entity_type="contact",
        entity_id=contact.id,
        actor_id=context.user_id,
        payload={"fields": sorted(values.keys() - {"updated_at", "version"})},
    )
    await db.commit()
    return contact


@router.delete("/contacts/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_contact(
    contact_id: uuid.UUID,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
    expected_version: int = Query(ge=1),
) -> None:
    contact = await db.scalar(
        sa.select(Contact)
        .where(
            Contact.id == contact_id,
            Contact.workspace_id == context.workspace_id,
            Contact.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if contact is None:
        raise not_found("contact")
    if contact.version != expected_version:
        raise conflict()

    deleted_at = datetime.now(UTC)
    contact.deleted_at = deleted_at
    contact.updated_at = deleted_at
    contact.version += 1
    # These tables are lookup indexes rather than the canonical contact
    # record.  Removing them prevents a future inbound message from resolving
    # to a soft-deleted contact and lets the address/identity be claimed by a
    # new active contact.
    await db.execute(
        sa.delete(ContactPoint).where(
            ContactPoint.workspace_id == context.workspace_id,
            ContactPoint.contact_id == contact.id,
        )
    )
    await db.execute(
        sa.delete(ExternalIdentity).where(
            ExternalIdentity.workspace_id == context.workspace_id,
            ExternalIdentity.contact_id == contact.id,
        )
    )
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="contact.deleted",
        entity_type="contact",
        entity_id=contact.id,
        actor_id=context.user_id,
    )
    await db.commit()


@router.get("/pipelines", response_model=list[PipelineRead])
async def list_pipelines(
    context: CurrentUser, db: AsyncSession = Depends(get_session)
) -> list[PipelineRead]:
    pipelines = list(
        (
            await db.scalars(
                sa.select(Pipeline)
                .where(Pipeline.workspace_id == context.workspace_id, Pipeline.is_active.is_(True))
                .order_by(Pipeline.position, Pipeline.created_at)
            )
        ).all()
    )
    stages = list(
        (
            await db.scalars(
                sa.select(Stage)
                .where(Stage.workspace_id == context.workspace_id)
                .order_by(Stage.pipeline_id, Stage.position)
            )
        ).all()
    )
    by_pipeline: dict[uuid.UUID, list[StageRead]] = {}
    for stage in stages:
        by_pipeline.setdefault(stage.pipeline_id, []).append(StageRead.model_validate(stage))
    return [
        PipelineRead(
            id=pipeline.id,
            name=pipeline.name,
            position=pipeline.position,
            is_active=pipeline.is_active,
            version=pipeline.version,
            stages=by_pipeline.get(pipeline.id, []),
        )
        for pipeline in pipelines
    ]


@router.post("/pipelines", response_model=PipelineRead, status_code=status.HTTP_201_CREATED)
async def create_pipeline(
    payload: PipelineCreate,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> PipelineRead:
    positions = [stage.position for stage in payload.stages]
    if len(set(positions)) != len(positions):
        raise HTTPException(status_code=422, detail="stage positions must be unique")
    pipeline = Pipeline(
        workspace_id=context.workspace_id,
        name=_clean_resource_name(payload.name, "pipeline"),
        position=payload.position,
    )
    db.add(pipeline)
    try:
        await db.flush()
        stages = [
            Stage(
                workspace_id=context.workspace_id,
                pipeline_id=pipeline.id,
                name=_clean_resource_name(item.name, "stage"),
                color=item.color.upper(),
                position=item.position,
                stage_type=item.stage_type,
            )
            for item in payload.stages
        ]
        db.add_all(stages)
        await db.flush()
        record_domain_event(
            db,
            workspace_id=context.workspace_id,
            event_type="pipeline.created",
            entity_type="pipeline",
            entity_id=pipeline.id,
            actor_id=context.user_id,
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="pipeline name or stage position already exists"
        ) from exc
    return PipelineRead(
        id=pipeline.id,
        name=pipeline.name,
        position=pipeline.position,
        is_active=pipeline.is_active,
        version=pipeline.version,
        stages=[
            StageRead.model_validate(item)
            for item in sorted(stages, key=lambda item: item.position)
        ],
    )


@router.patch("/pipelines/{pipeline_id}", response_model=PipelineRead)
async def update_pipeline(
    pipeline_id: uuid.UUID,
    payload: PipelineUpdate,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> PipelineRead:
    name = _clean_resource_name(payload.name, "pipeline")
    try:
        pipeline = (
            await db.execute(
                sa.update(Pipeline)
                .where(
                    Pipeline.id == pipeline_id,
                    Pipeline.workspace_id == context.workspace_id,
                    Pipeline.version == payload.expected_version,
                )
                .values(name=name, version=Pipeline.version + 1, updated_at=datetime.now(UTC))
                .returning(Pipeline)
            )
        ).scalar_one_or_none()
    except IntegrityError as exc:
        await db.rollback()
        raise deletion_conflict(
            "pipeline_name_conflict",
            "a pipeline with this name already exists",
        ) from exc
    if pipeline is None:
        if await db.scalar(
            sa.select(Pipeline.id).where(
                Pipeline.id == pipeline_id,
                Pipeline.workspace_id == context.workspace_id,
            )
        ):
            raise conflict()
        raise not_found("pipeline")
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="pipeline.updated",
        entity_type="pipeline",
        entity_id=pipeline.id,
        actor_id=context.user_id,
        payload={"name": name, "version": pipeline.version},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise deletion_conflict(
            "pipeline_name_conflict",
            "a pipeline with this name already exists",
        ) from exc
    return await _pipeline_read(db, pipeline)


@router.post(
    "/pipelines/{pipeline_id}/stages",
    response_model=StageRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_stage(
    pipeline_id: uuid.UUID,
    payload: StageAppendCreate,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> Stage:
    name = _clean_resource_name(payload.name, "stage")
    pipeline = await _ensure_pipeline(
        db,
        context.workspace_id,
        pipeline_id,
        for_update=True,
    )
    stages = list(
        (
            await db.scalars(
                sa.select(Stage)
                .where(
                    Stage.workspace_id == context.workspace_id,
                    Stage.pipeline_id == pipeline.id,
                )
                .order_by(Stage.position)
                .with_for_update()
            )
        ).all()
    )
    if len(stages) >= 50:
        raise deletion_conflict(
            "pipeline_stage_limit",
            "a pipeline cannot contain more than 50 stages",
        )

    original_positions = {existing.id: existing.position for existing in stages}
    offset = (stages[-1].position + 2) if stages else 1
    for existing in stages:
        existing.position += offset
    await db.flush()

    insert_at = next(
        (
            index
            for index, existing in enumerate(stages)
            if existing.stage_type is not StageType.open
        ),
        len(stages),
    )
    stage = Stage(
        workspace_id=context.workspace_id,
        pipeline_id=pipeline.id,
        name=name,
        color=payload.color.upper(),
        position=insert_at,
        stage_type=StageType.open,
    )
    ordered_stages = [*stages[:insert_at], stage, *stages[insert_at:]]
    for position, existing in enumerate(ordered_stages):
        existing.position = position
        if existing is not stage and original_positions[existing.id] != position:
            existing.version += 1
    db.add(stage)
    pipeline.version += 1
    pipeline.updated_at = datetime.now(UTC)
    await db.flush()
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="stage.created",
        entity_type="stage",
        entity_id=stage.id,
        actor_id=context.user_id,
        payload={"pipeline_id": str(pipeline.id), "position": stage.position},
    )
    await db.commit()
    return stage


@router.patch("/stages/{stage_id}", response_model=StageRead)
async def update_stage(
    stage_id: uuid.UUID,
    payload: StageUpdate,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> Stage:
    name = _clean_resource_name(payload.name, "stage")
    values: dict[str, Any] = {
        "name": name,
        "version": Stage.version + 1,
        "updated_at": datetime.now(UTC),
    }
    if payload.color is not None:
        values["color"] = payload.color.upper()
    changed_fields = ["name"]
    if payload.color is not None:
        changed_fields.append("color")
    stage = (
        await db.execute(
            sa.update(Stage)
            .where(
                Stage.id == stage_id,
                Stage.workspace_id == context.workspace_id,
                Stage.version == payload.expected_version,
            )
            .values(**values)
            .returning(Stage)
        )
    ).scalar_one_or_none()
    if stage is None:
        if await db.scalar(
            sa.select(Stage.id).where(
                Stage.id == stage_id,
                Stage.workspace_id == context.workspace_id,
            )
        ):
            raise conflict()
        raise not_found("stage")
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="stage.updated",
        entity_type="stage",
        entity_id=stage.id,
        actor_id=context.user_id,
        payload={
            "fields": changed_fields,
            "pipeline_id": str(stage.pipeline_id),
            "version": stage.version,
        },
    )
    await db.commit()
    return stage


@router.delete("/stages/{stage_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_stage(
    stage_id: uuid.UUID,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
    expected_version: int = Query(ge=1),
) -> None:
    stage = await _ensure_stage(
        db,
        context.workspace_id,
        stage_id,
    )
    pipeline = await _ensure_pipeline(
        db,
        context.workspace_id,
        stage.pipeline_id,
        for_update=True,
    )
    stage = await _ensure_stage(
        db,
        context.workspace_id,
        stage_id,
        pipeline_id=pipeline.id,
        for_update=True,
    )
    if stage.version != expected_version:
        raise conflict()
    if stage.stage_type is not StageType.open:
        raise deletion_conflict(
            "stage_not_open",
            "only open stages can be deleted",
        )
    references = await _stage_references(db, context.workspace_id, stage.id)
    if references:
        raise deletion_conflict(
            "stage_in_use",
            "stage is in use and cannot be deleted",
            references,
        )
    open_stage_count = await db.scalar(
        sa.select(sa.func.count())
        .select_from(Stage)
        .where(
            Stage.workspace_id == context.workspace_id,
            Stage.pipeline_id == stage.pipeline_id,
            Stage.stage_type == StageType.open,
        )
    )
    if not open_stage_count or open_stage_count <= 1:
        raise deletion_conflict(
            "last_open_stage",
            "a pipeline must keep at least one open stage",
        )

    deleted_name = stage.name
    pipeline_id = stage.pipeline_id
    await db.execute(
        sa.delete(ExternalEntityMap).where(
            ExternalEntityMap.workspace_id == context.workspace_id,
            ExternalEntityMap.entity_type == "stages",
            ExternalEntityMap.internal_id == stage.id,
        )
    )
    await db.delete(stage)
    pipeline.version += 1
    pipeline.updated_at = datetime.now(UTC)
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="stage.deleted",
        entity_type="stage",
        entity_id=stage.id,
        actor_id=context.user_id,
        payload={"name": deleted_name, "pipeline_id": str(pipeline_id)},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise deletion_conflict(
            "stage_in_use",
            "stage is in use and cannot be deleted",
            ["database_constraints"],
        ) from exc


@router.delete("/pipelines/{pipeline_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pipeline(
    pipeline_id: uuid.UUID,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
    expected_version: int = Query(ge=1),
) -> None:
    pipeline = await _ensure_pipeline(
        db,
        context.workspace_id,
        pipeline_id,
        for_update=True,
    )
    if pipeline.version != expected_version:
        raise conflict()
    references = await _pipeline_references(db, context.workspace_id, pipeline.id)
    if references:
        raise deletion_conflict(
            "pipeline_in_use",
            "pipeline is in use and cannot be deleted",
            references,
        )
    active_pipeline_count = await db.scalar(
        sa.select(sa.func.count())
        .select_from(Pipeline)
        .where(
            Pipeline.workspace_id == context.workspace_id,
            Pipeline.is_active.is_(True),
        )
    )
    if pipeline.is_active and (not active_pipeline_count or active_pipeline_count <= 1):
        raise deletion_conflict(
            "last_active_pipeline",
            "a workspace must keep at least one active pipeline",
        )

    stage_ids = list(
        (
            await db.scalars(
                sa.select(Stage.id).where(
                    Stage.workspace_id == context.workspace_id,
                    Stage.pipeline_id == pipeline.id,
                )
            )
        ).all()
    )
    mapping_filter = sa.and_(
        ExternalEntityMap.entity_type == "pipelines",
        ExternalEntityMap.internal_id == pipeline.id,
    )
    if stage_ids:
        mapping_filter = sa.or_(
            mapping_filter,
            sa.and_(
                ExternalEntityMap.entity_type == "stages",
                ExternalEntityMap.internal_id.in_(stage_ids),
            ),
        )
    deleted_name = pipeline.name
    try:
        await db.execute(
            sa.delete(ExternalEntityMap).where(
                ExternalEntityMap.workspace_id == context.workspace_id,
                mapping_filter,
            )
        )
        await db.execute(
            sa.delete(Stage).where(
                Stage.workspace_id == context.workspace_id,
                Stage.pipeline_id == pipeline.id,
            )
        )
        await db.delete(pipeline)
        record_domain_event(
            db,
            workspace_id=context.workspace_id,
            event_type="pipeline.deleted",
            entity_type="pipeline",
            entity_id=pipeline.id,
            actor_id=context.user_id,
            payload={"name": deleted_name, "stage_count": len(stage_ids)},
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise deletion_conflict(
            "pipeline_in_use",
            "pipeline is in use and cannot be deleted",
            ["database_constraints"],
        ) from exc


@router.get("/sources", response_model=list[SourceRead])
async def list_sources(
    context: CurrentUser, db: AsyncSession = Depends(get_session)
) -> list[Source]:
    return list(
        (
            await db.scalars(
                sa.select(Source)
                .where(Source.workspace_id == context.workspace_id, Source.is_active.is_(True))
                .order_by(Source.name)
            )
        ).all()
    )


@router.get("/custom-fields", response_model=list[CustomFieldRead])
async def list_custom_fields(
    context: CurrentUser,
    db: AsyncSession = Depends(get_session),
    entity_type: FieldEntity | None = None,
) -> list[CustomFieldDefinition]:
    query = sa.select(CustomFieldDefinition).where(
        CustomFieldDefinition.workspace_id == context.workspace_id,
        CustomFieldDefinition.is_active.is_(True),
    )
    if entity_type:
        query = query.where(CustomFieldDefinition.entity_type == entity_type)
    return list((await db.scalars(query.order_by(CustomFieldDefinition.name))).all())


@router.post("/custom-fields", response_model=CustomFieldRead, status_code=status.HTTP_201_CREATED)
async def create_custom_field(
    payload: CustomFieldCreate,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> CustomFieldDefinition:
    field = CustomFieldDefinition(workspace_id=context.workspace_id, **payload.model_dump())
    db.add(field)
    try:
        await db.flush()
        record_domain_event(
            db,
            workspace_id=context.workspace_id,
            event_type="custom_field.created",
            entity_type="custom_field",
            entity_id=field.id,
            actor_id=context.user_id,
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="custom field key already exists") from exc
    return field


@router.delete("/custom-fields/{field_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_custom_field(
    field_id: uuid.UUID,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> None:
    field = await db.scalar(
        sa.select(CustomFieldDefinition)
        .where(
            CustomFieldDefinition.id == field_id,
            CustomFieldDefinition.workspace_id == context.workspace_id,
            CustomFieldDefinition.is_active.is_(True),
        )
        .with_for_update()
    )
    if field is None:
        raise not_found("custom field")

    await db.execute(
        sa.delete(StageRequiredField).where(
            StageRequiredField.workspace_id == context.workspace_id,
            StageRequiredField.field_definition_id == field.id,
        )
    )
    field.is_active = False
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="custom_field.deleted",
        entity_type="custom_field",
        entity_id=field.id,
        actor_id=context.user_id,
        payload={"entity_type": field.entity_type.value, "key": field.key, "name": field.name},
    )
    await db.commit()


@router.get("/stages/{stage_id}/required-fields", response_model=list[RequiredFieldRead])
async def list_required_fields(
    stage_id: uuid.UUID,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> list[StageRequiredField]:
    stage = await db.scalar(
        sa.select(Stage.id).where(
            Stage.id == stage_id,
            Stage.workspace_id == context.workspace_id,
        )
    )
    if stage is None:
        raise not_found("stage")
    return list(
        (
            await db.scalars(
                sa.select(StageRequiredField)
                .where(
                    StageRequiredField.stage_id == stage_id,
                    StageRequiredField.workspace_id == context.workspace_id,
                )
                .order_by(StageRequiredField.created_at, StageRequiredField.id)
            )
        ).all()
    )


@router.put("/stages/{stage_id}/required-fields", response_model=list[RequiredFieldRead])
async def replace_required_fields(
    stage_id: uuid.UUID,
    payload: RequiredFieldsReplace,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> list[StageRequiredField]:
    stage = await db.scalar(
        sa.select(Stage).where(Stage.id == stage_id, Stage.workspace_id == context.workspace_id)
    )
    if stage is None:
        raise not_found("stage")
    builtin_keys = [item.built_in_key for item in payload.fields if item.built_in_key]
    if any(key not in BUILT_IN_REQUIRED_FIELDS for key in builtin_keys):
        raise HTTPException(status_code=422, detail="unsupported built-in required field")
    definition_ids = [
        item.field_definition_id for item in payload.fields if item.field_definition_id
    ]
    if len(set(builtin_keys)) != len(builtin_keys) or len(set(definition_ids)) != len(
        definition_ids
    ):
        raise HTTPException(status_code=422, detail="required fields must be unique")
    if definition_ids:
        active_definition_ids = set(
            (
                await db.scalars(
                    sa.select(CustomFieldDefinition.id)
                    .where(
                        CustomFieldDefinition.id.in_(definition_ids),
                        CustomFieldDefinition.workspace_id == context.workspace_id,
                        CustomFieldDefinition.entity_type == FieldEntity.deal,
                        CustomFieldDefinition.is_active.is_(True),
                    )
                    .with_for_update()
                )
            ).all()
        )
        if active_definition_ids != set(definition_ids):
            raise HTTPException(status_code=422, detail="unknown or non-deal custom field")
    await db.execute(
        sa.delete(StageRequiredField).where(
            StageRequiredField.stage_id == stage.id,
            StageRequiredField.workspace_id == context.workspace_id,
        )
    )
    fields = [
        StageRequiredField(
            workspace_id=context.workspace_id,
            stage_id=stage.id,
            field_definition_id=item.field_definition_id,
            built_in_key=item.built_in_key,
        )
        for item in payload.fields
    ]
    db.add_all(fields)
    await db.flush()
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="stage.required_fields_changed",
        entity_type="stage",
        entity_id=stage.id,
        actor_id=context.user_id,
        payload={"count": len(fields)},
    )
    await db.commit()
    return fields


async def _validate_deal_references(
    db: AsyncSession, workspace_id: uuid.UUID, payload: DealCreate
) -> Stage:
    stage = await db.scalar(
        sa.select(Stage).where(
            Stage.id == payload.stage_id,
            Stage.pipeline_id == payload.pipeline_id,
            Stage.workspace_id == workspace_id,
        )
    )
    if stage is None:
        raise HTTPException(status_code=422, detail="stage does not belong to pipeline")
    if payload.company_id:
        await _ensure_company(db, workspace_id, payload.company_id)
    if payload.assignee_id:
        await _ensure_member(db, workspace_id, payload.assignee_id)
    if payload.source_id and not await db.scalar(
        sa.select(Source.id).where(
            Source.id == payload.source_id, Source.workspace_id == workspace_id
        )
    ):
        raise not_found("source")
    if payload.contact_ids:
        count = await db.scalar(
            sa.select(sa.func.count())
            .select_from(Contact)
            .where(
                Contact.id.in_(set(payload.contact_ids)),
                Contact.workspace_id == workspace_id,
                Contact.deleted_at.is_(None),
            )
        )
        if count != len(set(payload.contact_ids)):
            raise not_found("contact")
    return stage


async def _sync_purchase_schedule(
    db: AsyncSession,
    *,
    deal: Deal,
    fallback_assignee_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """Keep the durable next-purchase schedule aligned with a deal date."""

    active = list(
        (
            await db.scalars(
                sa.select(PurchaseSchedule).where(
                    PurchaseSchedule.workspace_id == deal.workspace_id,
                    PurchaseSchedule.deal_id == deal.id,
                    PurchaseSchedule.status == PurchaseScheduleStatus.active,
                ).with_for_update()
            )
        ).all()
    )
    if deal.next_purchase_at is None:
        for schedule in active:
            schedule.status = PurchaseScheduleStatus.cancelled
            schedule.version += 1
        return

    scheduled_for = deal.next_purchase_at
    if scheduled_for.tzinfo is None:
        scheduled_for = scheduled_for.replace(tzinfo=UTC)
    else:
        scheduled_for = scheduled_for.astimezone(UTC)
    contact_id = await db.scalar(
        sa.select(DealContact.contact_id)
        .where(
            DealContact.workspace_id == deal.workspace_id,
            DealContact.deal_id == deal.id,
        )
        .order_by(DealContact.is_primary.desc(), DealContact.created_at.asc())
        .limit(1)
    )
    for schedule in active:
        existing_scheduled_for = schedule.scheduled_for
        if existing_scheduled_for.tzinfo is None:
            existing_scheduled_for = existing_scheduled_for.replace(tzinfo=UTC)
        else:
            existing_scheduled_for = existing_scheduled_for.astimezone(UTC)
        if existing_scheduled_for != scheduled_for:
            schedule.status = PurchaseScheduleStatus.cancelled
            schedule.version += 1
        else:
            assignee_id = deal.assignee_id or fallback_assignee_id
            schedule.contact_id = contact_id
            schedule.assignee_id = assignee_id
            if schedule.task_id is not None:
                task = await db.scalar(
                    sa.select(Task)
                    .where(
                        Task.id == schedule.task_id,
                        Task.workspace_id == deal.workspace_id,
                        Task.status == TaskStatus.open,
                    )
                    .with_for_update()
                )
                if task is not None and task.assignee_id != assignee_id:
                    task.assignee_id = assignee_id
                    task.version += 1
                    task.updated_at = datetime.now(UTC)
                    record_domain_event(
                        db,
                        workspace_id=deal.workspace_id,
                        event_type="task.updated",
                        entity_type="task",
                        entity_id=task.id,
                        actor_id=actor_id,
                        payload={"fields": ["assignee_id"], "deal_id": str(deal.id)},
                    )
            return

    await create_purchase_schedule(
        db,
        workspace_id=deal.workspace_id,
        deal_id=deal.id,
        contact_id=contact_id,
        assignee_id=deal.assignee_id or fallback_assignee_id,
        scheduled_for=scheduled_for,
        remind_at=scheduled_for - timedelta(days=7),
    )


async def _cancel_purchase_tasks_and_reminders(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    deal_id: uuid.UUID,
    schedules: list[PurchaseSchedule],
    actor_id: uuid.UUID,
    cancelled_at: datetime,
) -> None:
    task_ids = {schedule.task_id for schedule in schedules if schedule.task_id is not None}
    if not task_ids:
        return

    tasks = list(
        (
            await db.scalars(
                sa.select(Task)
                .where(
                    Task.id.in_(task_ids),
                    Task.workspace_id == workspace_id,
                    Task.status == TaskStatus.open,
                )
                .with_for_update()
            )
        ).all()
    )
    if not tasks:
        return

    cancelled_task_ids = [task.id for task in tasks]
    for task in tasks:
        task.status = TaskStatus.cancelled
        task.completed_at = None
        task.updated_at = cancelled_at
        task.version += 1
        record_domain_event(
            db,
            workspace_id=workspace_id,
            event_type="task.updated",
            entity_type="task",
            entity_id=task.id,
            actor_id=actor_id,
            payload={"fields": ["status"], "deal_id": str(deal_id)},
        )

    reminder_events = list(
        (
            await db.scalars(
                sa.select(OutboxEvent)
                .where(
                    OutboxEvent.workspace_id == workspace_id,
                    OutboxEvent.aggregate_type == "task",
                    OutboxEvent.aggregate_id.in_(cancelled_task_ids),
                    OutboxEvent.event_type.in_(["task.due_soon", "task.overdue"]),
                )
                .with_for_update()
            )
        ).all()
    )
    for event in reminder_events:
        if event.processed_at is None:
            event.processed_at = cancelled_at

    reminder_event_ids = [event.id for event in reminder_events]
    deliveries: list[NotificationDelivery] = []
    if reminder_event_ids:
        deliveries = list(
            (
                await db.scalars(
                    sa.select(NotificationDelivery)
                    .where(
                        NotificationDelivery.workspace_id == workspace_id,
                        NotificationDelivery.status.in_(
                            [DeliveryStatus.pending, DeliveryStatus.processing]
                        ),
                        sa.or_(
                            *[
                                NotificationDelivery.dedupe_key.like(
                                    f"%:event:{event_id}:recipient:%"
                                )
                                for event_id in reminder_event_ids
                            ]
                        ),
                    )
                    .with_for_update()
                )
            ).all()
        )
        for delivery in deliveries:
            delivery.status = DeliveryStatus.failed
            delivery.last_error = "source task cancelled because its deal was deleted"
            delivery.updated_at = cancelled_at

    delivery_ids = [delivery.id for delivery in deliveries]
    delivery_events: list[OutboxEvent] = []
    if delivery_ids:
        delivery_events = list(
            (
                await db.scalars(
                    sa.select(OutboxEvent)
                    .where(
                        OutboxEvent.workspace_id == workspace_id,
                        OutboxEvent.aggregate_type == "notification_delivery",
                        OutboxEvent.aggregate_id.in_(delivery_ids),
                        OutboxEvent.event_type == "notification.delivery.queued",
                        OutboxEvent.processed_at.is_(None),
                    )
                    .with_for_update()
                )
            ).all()
        )
        for event in delivery_events:
            event.processed_at = cancelled_at

    queued_job_keys: list[Any] = []
    for task_id in cancelled_task_ids:
        queued_job_keys.extend(
            [
                BackgroundJob.dedupe_key.like(f"monitor.task.due:{task_id}:%"),
                BackgroundJob.dedupe_key.like(f"monitor.task.overdue:{task_id}:%"),
            ]
        )
    for event_id in reminder_event_ids:
        queued_job_keys.extend(
            [
                BackgroundJob.dedupe_key == f"outbox:{event_id}:dispatch",
                BackgroundJob.dedupe_key == f"outbox:{event_id}:notifications",
            ]
        )
    for delivery_id in delivery_ids:
        queued_job_keys.append(
            BackgroundJob.dedupe_key == f"notification-delivery:{delivery_id}:send"
        )
    for event in delivery_events:
        queued_job_keys.append(BackgroundJob.dedupe_key == f"outbox:{event.id}:dispatch")
    await db.execute(
        sa.update(BackgroundJob)
        .where(
            BackgroundJob.workspace_id == workspace_id,
            BackgroundJob.status == JobStatus.queued,
            sa.or_(*queued_job_keys),
        )
        .values(
            status=JobStatus.succeeded,
            lease_owner=None,
            lease_until=None,
            last_error=None,
            updated_at=cancelled_at,
        )
    )


async def _deal_reads(db: AsyncSession, deals: list[Deal]) -> list[DealRead]:
    if not deals:
        return []
    deal_ids = [deal.id for deal in deals]
    contact_rows = (
        await db.execute(
            sa.select(DealContact.deal_id, Contact)
            .join(Contact, Contact.id == DealContact.contact_id)
            .where(
                DealContact.deal_id.in_(deal_ids),
                Contact.deleted_at.is_(None),
            )
            .order_by(
                DealContact.deal_id,
                DealContact.is_primary.desc(),
                DealContact.created_at,
            )
        )
    ).all()
    contact_ids_by_deal: dict[uuid.UUID, list[uuid.UUID]] = {deal_id: [] for deal_id in deal_ids}
    primary_contact_by_deal: dict[uuid.UUID, ContactRead] = {}
    for deal_id, contact in contact_rows:
        contact_ids_by_deal[deal_id].append(contact.id)
        primary_contact_by_deal.setdefault(deal_id, ContactRead.model_validate(contact))
    company_ids = {deal.company_id for deal in deals if deal.company_id is not None}
    companies_by_id = {
        company.id: CompanyRead.model_validate(company)
        for company in (
            await db.scalars(
                sa.select(Company).where(
                    Company.id.in_(company_ids),
                    Company.deleted_at.is_(None),
                )
            )
        ).all()
    }
    return [
        DealRead.model_validate(deal).model_copy(
            update={
                "contact_ids": contact_ids_by_deal[deal.id],
                "primary_contact": primary_contact_by_deal.get(deal.id),
                "company": companies_by_id.get(deal.company_id) if deal.company_id else None,
            }
        )
        for deal in deals
    ]


async def _deal_read(db: AsyncSession, deal: Deal) -> DealRead:
    return (await _deal_reads(db, [deal]))[0]


@router.get("/contacts/{contact_id}/purchases", response_model=DealPage)
async def list_contact_purchases(
    contact_id: uuid.UUID,
    context: CurrentUser,
    db: AsyncSession = Depends(get_session),
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> DealPage:
    await enforce_cursor_page_budget(
        db,
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        resource="contact_purchases",
        cursor=cursor,
    )
    await _ensure_contact(db, context.workspace_id, contact_id)
    query = (
        sa.select(Deal)
        .join(DealContact, DealContact.deal_id == Deal.id)
        .join(Stage, Stage.id == Deal.stage_id)
        .where(
            Deal.workspace_id == context.workspace_id,
            Deal.deleted_at.is_(None),
            DealContact.workspace_id == context.workspace_id,
            DealContact.contact_id == contact_id,
            Stage.stage_type == StageType.won,
        )
    )
    if (condition := _cursor_condition(Deal, cursor)) is not None:
        query = query.where(condition)
    items = list(
        (
            await db.scalars(
                query.order_by(Deal.created_at.desc(), Deal.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    items, next_cursor = _next_cursor(items, limit)
    return DealPage(items=await _deal_reads(db, items), next_cursor=next_cursor)


@router.get("/deals", response_model=DealPage)
async def list_deals(
    context: CurrentUser,
    db: AsyncSession = Depends(get_session),
    pipeline_id: uuid.UUID | None = None,
    stage_id: uuid.UUID | None = None,
    search: str | None = Query(default=None, max_length=200),
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> DealPage:
    await enforce_cursor_page_budget(
        db,
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        resource="deals",
        cursor=cursor,
    )
    query = sa.select(Deal).where(
        Deal.workspace_id == context.workspace_id, Deal.deleted_at.is_(None)
    )
    if pipeline_id:
        query = query.where(Deal.pipeline_id == pipeline_id)
    if stage_id:
        query = query.where(Deal.stage_id == stage_id)
    if search and (term := search.strip()):
        dialect_name = db.get_bind().dialect.name
        literal_pattern = _literal_contains_pattern(term)
        tags_match = _tags_search_condition(Deal.tags, term, dialect_name)
        custom_fields_match = _json_object_values_search_condition(
            Deal.custom_fields, term, dialect_name
        )
        source_match = sa.exists(
            sa.select(1).where(
                Source.id == Deal.source_id,
                Source.workspace_id == context.workspace_id,
                sa.or_(
                    Source.key.ilike(literal_pattern, escape="\\"),
                    Source.name.ilike(literal_pattern, escape="\\"),
                ),
            )
        )
        if dialect_name == "postgresql":
            query = query.where(
                sa.or_(
                    sa.func.to_tsvector(
                        sa.text("'simple'"), sa.func.coalesce(Deal.title, "")
                    ).op("@@")(
                        sa.func.websearch_to_tsquery(sa.text("'simple'"), term)
                    ),
                    tags_match,
                    custom_fields_match,
                    source_match,
                )
            )
        else:
            query = query.where(
                sa.or_(
                    Deal.title.ilike(literal_pattern, escape="\\"),
                    tags_match,
                    custom_fields_match,
                    source_match,
                )
            )
    if (condition := _cursor_condition(Deal, cursor)) is not None:
        query = query.where(condition)
    items = list(
        (
            await db.scalars(
                query.order_by(Deal.created_at.desc(), Deal.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    items, next_cursor = _next_cursor(items, limit)
    return DealPage(items=await _deal_reads(db, items), next_cursor=next_cursor)


@router.post("/deals", response_model=DealRead, status_code=status.HTTP_201_CREATED)
async def create_deal(
    payload: DealCreate,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> DealRead:
    await _validate_deal_references(db, context.workspace_id, payload)
    data = payload.model_dump(exclude={"contact_ids"})
    deal = Deal(workspace_id=context.workspace_id, **data)
    db.add(deal)
    await db.flush()
    db.add_all(
        [
            DealContact(
                workspace_id=context.workspace_id,
                deal_id=deal.id,
                contact_id=contact_id,
                is_primary=index == 0,
            )
            for index, contact_id in enumerate(dict.fromkeys(payload.contact_ids))
        ]
    )
    await db.flush()
    await _sync_purchase_schedule(
        db,
        deal=deal,
        fallback_assignee_id=context.user_id,
        actor_id=context.user_id,
    )
    db.add(
        DealStageHistory(
            workspace_id=context.workspace_id,
            deal_id=deal.id,
            to_stage_id=deal.stage_id,
            actor_id=context.user_id,
        )
    )
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="deal.created",
        entity_type="deal",
        entity_id=deal.id,
        actor_id=context.user_id,
        payload={"pipeline_id": str(deal.pipeline_id), "stage_id": str(deal.stage_id)},
    )
    if deal.assignee_id is not None:
        record_domain_event(
            db,
            workspace_id=context.workspace_id,
            event_type="deal.assigned",
            entity_type="deal",
            entity_id=deal.id,
            actor_id=context.user_id,
            payload={
                "assignee_id": str(deal.assignee_id),
                "pipeline_id": str(deal.pipeline_id),
                "stage_id": str(deal.stage_id),
                "source_id": str(deal.source_id) if deal.source_id else None,
            },
        )
    await db.commit()
    return await _deal_read(db, deal)


@router.get("/deals/{deal_id}", response_model=DealRead)
async def get_deal(
    deal_id: uuid.UUID, context: CurrentUser, db: AsyncSession = Depends(get_session)
) -> DealRead:
    return await _deal_read(db, await _ensure_deal(db, context.workspace_id, deal_id))


@router.patch("/deals/{deal_id}", response_model=DealRead)
async def update_deal(
    deal_id: uuid.UUID,
    payload: DealUpdate,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> DealRead:
    if "contact_ids" in payload.model_fields_set:
        if payload.contact_ids is None:
            raise HTTPException(status_code=422, detail="contact_ids must be a list")
        if payload.contact_ids:
            count = await db.scalar(
                sa.select(sa.func.count())
                .select_from(Contact)
                .where(
                    Contact.id.in_(set(payload.contact_ids)),
                    Contact.workspace_id == context.workspace_id,
                    Contact.deleted_at.is_(None),
                )
            )
            if count != len(set(payload.contact_ids)):
                raise not_found("contact")
    if "company_id" in payload.model_fields_set and payload.company_id:
        await _ensure_company(db, context.workspace_id, payload.company_id)
    if "assignee_id" in payload.model_fields_set and payload.assignee_id:
        await _ensure_member(db, context.workspace_id, payload.assignee_id)
    if (
        "source_id" in payload.model_fields_set
        and payload.source_id
        and not await db.scalar(
            sa.select(Source.id).where(
                Source.id == payload.source_id, Source.workspace_id == context.workspace_id
            )
        )
    ):
        raise not_found("source")
    values = payload.model_dump(
        exclude_unset=True,
        exclude={"expected_version", "contact_ids"},
    )
    values.update(
        version=payload.expected_version + 1,
        updated_at=datetime.now(UTC),
        last_activity_at=datetime.now(UTC),
    )
    deal = (
        await db.execute(
            sa.update(Deal)
            .where(
                Deal.id == deal_id,
                Deal.workspace_id == context.workspace_id,
                Deal.deleted_at.is_(None),
                Deal.version == payload.expected_version,
            )
            .values(**values)
            .returning(Deal)
        )
    ).scalar_one_or_none()
    if deal is None:
        if await db.scalar(
            sa.select(Deal.id).where(Deal.id == deal_id, Deal.workspace_id == context.workspace_id)
        ):
            raise conflict()
        raise not_found("deal")
    if "contact_ids" in payload.model_fields_set:
        await db.execute(
            sa.delete(DealContact).where(
                DealContact.deal_id == deal.id,
                DealContact.workspace_id == context.workspace_id,
            )
        )
        db.add_all(
            [
                DealContact(
                    workspace_id=context.workspace_id,
                    deal_id=deal.id,
                    contact_id=contact_id,
                    is_primary=index == 0,
                )
                for index, contact_id in enumerate(dict.fromkeys(payload.contact_ids or []))
            ]
        )
        await db.flush()
    if {
        "next_purchase_at",
        "assignee_id",
        "contact_ids",
    } & payload.model_fields_set:
        await _sync_purchase_schedule(
            db,
            deal=deal,
            fallback_assignee_id=context.user_id,
            actor_id=context.user_id,
        )
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="deal.updated",
        entity_type="deal",
        entity_id=deal.id,
        actor_id=context.user_id,
        payload={"fields": sorted(payload.model_fields_set - {"expected_version"})},
    )
    if "assignee_id" in payload.model_fields_set and deal.assignee_id is not None:
        record_domain_event(
            db,
            workspace_id=context.workspace_id,
            event_type="deal.assigned",
            entity_type="deal",
            entity_id=deal.id,
            actor_id=context.user_id,
            payload={
                "assignee_id": str(deal.assignee_id),
                "pipeline_id": str(deal.pipeline_id),
                "stage_id": str(deal.stage_id),
                "source_id": str(deal.source_id) if deal.source_id else None,
            },
        )
    await db.commit()
    return await _deal_read(db, deal)


@router.delete("/deals/{deal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_deal(
    deal_id: uuid.UUID,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
    expected_version: int = Query(ge=1),
) -> None:
    deal = await db.scalar(
        sa.select(Deal)
        .where(
            Deal.id == deal_id,
            Deal.workspace_id == context.workspace_id,
            Deal.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if deal is None:
        raise not_found("deal")
    if deal.version != expected_version:
        raise conflict()

    deleted_at = datetime.now(UTC)
    active_schedules = list(
        (
            await db.scalars(
                sa.select(PurchaseSchedule)
                .where(
                    PurchaseSchedule.workspace_id == context.workspace_id,
                    PurchaseSchedule.deal_id == deal.id,
                    PurchaseSchedule.status == PurchaseScheduleStatus.active,
                )
                .with_for_update()
            )
        ).all()
    )
    await _cancel_purchase_tasks_and_reminders(
        db,
        workspace_id=context.workspace_id,
        deal_id=deal.id,
        schedules=active_schedules,
        actor_id=context.user_id,
        cancelled_at=deleted_at,
    )
    for schedule in active_schedules:
        schedule.status = PurchaseScheduleStatus.cancelled
        schedule.completed_at = deleted_at
        schedule.updated_at = deleted_at
        schedule.version += 1
    if active_schedules:
        await db.execute(
            sa.update(OutboxEvent)
            .where(
                OutboxEvent.workspace_id == context.workspace_id,
                OutboxEvent.aggregate_type == "purchase_schedule",
                OutboxEvent.aggregate_id.in_([schedule.id for schedule in active_schedules]),
                OutboxEvent.event_type == "purchase.due_soon",
                OutboxEvent.processed_at.is_(None),
            )
            .values(processed_at=deleted_at)
        )

    deal.deleted_at = deleted_at
    deal.updated_at = deleted_at
    deal.version += 1
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="deal.deleted",
        entity_type="deal",
        entity_id=deal.id,
        actor_id=context.user_id,
    )
    await db.commit()


def _is_blank(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


@router.patch("/deals/{deal_id}/stage", response_model=DealRead)
async def transition_deal_stage(
    deal_id: uuid.UUID,
    payload: StageTransition,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> DealRead:
    deal = await _ensure_deal(db, context.workspace_id, deal_id)
    if deal.version != payload.expected_version:
        raise conflict()
    target = await db.scalar(
        sa.select(Stage).where(
            Stage.id == payload.target_stage_id,
            Stage.workspace_id == context.workspace_id,
            Stage.pipeline_id == deal.pipeline_id,
        )
    )
    if target is None:
        raise HTTPException(status_code=422, detail="target stage does not belong to deal pipeline")
    required_rows = (
        await db.execute(
            sa.select(StageRequiredField, CustomFieldDefinition)
            .outerjoin(
                CustomFieldDefinition,
                sa.and_(
                    CustomFieldDefinition.id == StageRequiredField.field_definition_id,
                    CustomFieldDefinition.workspace_id == context.workspace_id,
                    CustomFieldDefinition.is_active.is_(True),
                ),
            )
            .where(
                StageRequiredField.stage_id == target.id,
                StageRequiredField.workspace_id == context.workspace_id,
            )
        )
    ).all()
    missing: list[dict[str, str]] = []
    for requirement, definition in required_rows:
        if requirement.built_in_key:
            if requirement.built_in_key == "contact_ids":
                value = await db.scalar(
                    sa.select(DealContact.id)
                    .where(
                        DealContact.workspace_id == context.workspace_id,
                        DealContact.deal_id == deal.id,
                    )
                    .limit(1)
                )
            else:
                value = getattr(deal, requirement.built_in_key)
            if _is_blank(value):
                missing.append({"key": requirement.built_in_key, "name": requirement.built_in_key})
        elif definition is not None and _is_blank(deal.custom_fields.get(definition.key)):
            missing.append({"key": definition.key, "name": definition.name})
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "missing_required_fields", "fields": missing},
        )
    if deal.stage_id == target.id:
        return await _deal_read(db, deal)
    old_stage_id = deal.stage_id
    changed = (
        await db.execute(
            sa.update(Deal)
            .where(
                Deal.id == deal.id,
                Deal.workspace_id == context.workspace_id,
                Deal.version == payload.expected_version,
            )
            .values(
                stage_id=target.id,
                version=payload.expected_version + 1,
                updated_at=datetime.now(UTC),
                last_activity_at=datetime.now(UTC),
            )
            .returning(Deal)
        )
    ).scalar_one_or_none()
    if changed is None:
        raise conflict()
    db.add(
        DealStageHistory(
            workspace_id=context.workspace_id,
            deal_id=deal.id,
            from_stage_id=old_stage_id,
            to_stage_id=target.id,
            actor_id=context.user_id,
        )
    )
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="deal.stage_changed",
        entity_type="deal",
        entity_id=deal.id,
        actor_id=context.user_id,
        payload={"from_stage_id": str(old_stage_id), "to_stage_id": str(target.id)},
    )
    await db.commit()
    return await _deal_read(db, changed)


async def _validate_task_references(
    db: AsyncSession, workspace_id: uuid.UUID, payload: TaskCreate
) -> None:
    await _ensure_member(db, workspace_id, payload.assignee_id)
    if payload.deal_id:
        await _ensure_deal(db, workspace_id, payload.deal_id)
    if payload.contact_id:
        await _ensure_contact(db, workspace_id, payload.contact_id)
    if payload.company_id:
        await _ensure_company(db, workspace_id, payload.company_id)


@router.get("/tasks", response_model=TaskPage)
async def list_tasks(
    context: CurrentUser,
    db: AsyncSession = Depends(get_session),
    task_status: TaskStatus | None = Query(default=None, alias="status"),
    scope: Literal["all", "today", "overdue", "upcoming"] = "all",
    include_completed: bool = False,
    search: str | None = Query(default=None, min_length=1, max_length=120),
    overdue: bool = False,
    cursor: str | None = None,
    limit: int = Query(default=25, ge=1, le=100),
) -> TaskPage:
    await enforce_cursor_page_budget(
        db,
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        resource="tasks",
        cursor=cursor,
    )
    query = sa.select(Task).where(Task.workspace_id == context.workspace_id)
    if task_status:
        query = query.where(Task.status == task_status)
    elif include_completed:
        query = query.where(
            Task.status.in_((TaskStatus.open, TaskStatus.completed, TaskStatus.cancelled))
        )
    else:
        query = query.where(Task.status == TaskStatus.open)

    if search and (term := search.strip()):
        pattern = _literal_contains_pattern(term)
        query = query.join(User, User.id == Task.assignee_id).where(
            sa.or_(
                Task.title.ilike(pattern, escape="\\"),
                Task.description.ilike(pattern, escape="\\"),
                User.full_name.ilike(pattern, escape="\\"),
            )
        )

    if scope != "all":
        try:
            workspace_timezone = ZoneInfo(context.workspace.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            workspace_timezone = ZoneInfo("UTC")
        now = datetime.now(UTC)
        local_now = now.astimezone(workspace_timezone)
        today_start = datetime.combine(
            local_now.date(), datetime.min.time(), tzinfo=workspace_timezone
        ).astimezone(UTC)
        tomorrow_start = datetime.combine(
            local_now.date() + timedelta(days=1),
            datetime.min.time(),
            tzinfo=workspace_timezone,
        ).astimezone(UTC)
        if scope == "today":
            query = query.where(Task.due_at >= today_start, Task.due_at < tomorrow_start)
        elif scope == "overdue":
            query = query.where(Task.due_at < now)
        else:
            query = query.where(Task.due_at >= tomorrow_start)
    if overdue:
        query = query.where(Task.status == TaskStatus.open, Task.due_at < sa.func.now())
    if (condition := _cursor_condition(Task, cursor)) is not None:
        query = query.where(condition)
    items = list(
        (
            await db.scalars(
                query.order_by(Task.created_at.desc(), Task.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    items, next_cursor = _next_cursor(items, limit)
    return TaskPage(
        items=[TaskRead.model_validate(item) for item in items], next_cursor=next_cursor
    )


@router.post("/tasks", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreate,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> Task:
    await _validate_task_references(db, context.workspace_id, payload)
    task = Task(workspace_id=context.workspace_id, **payload.model_dump())
    db.add(task)
    await db.flush()
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="task.created",
        entity_type="task",
        entity_id=task.id,
        actor_id=context.user_id,
    )
    await db.commit()
    return task


@router.patch("/tasks/{task_id}", response_model=TaskRead)
async def update_task(
    task_id: uuid.UUID,
    payload: TaskUpdate,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> Task:
    task = await db.scalar(
        sa.select(Task)
        .where(
            Task.id == task_id,
            Task.workspace_id == context.workspace_id,
        )
        .with_for_update()
    )
    if task is None:
        raise not_found("task")
    if task.version != payload.expected_version:
        raise conflict()

    fields = payload.model_fields_set
    for required_text_field in ("title", "task_type"):
        if required_text_field in fields and getattr(payload, required_text_field) is None:
            raise HTTPException(
                status_code=422,
                detail=f"{required_text_field} cannot be null",
            )
    if "assignee_id" in fields:
        if payload.assignee_id is None:
            raise HTTPException(status_code=422, detail="assignee_id cannot be null")
        await _ensure_member(db, context.workspace_id, payload.assignee_id)
    if "deal_id" in fields and payload.deal_id is not None:
        await _ensure_deal(db, context.workspace_id, payload.deal_id)
    if "contact_id" in fields and payload.contact_id is not None:
        await _ensure_contact(db, context.workspace_id, payload.contact_id)
    if "company_id" in fields and payload.company_id is not None:
        await _ensure_company(db, context.workspace_id, payload.company_id)

    effective_due_at = payload.due_at if "due_at" in fields else task.due_at
    effective_remind_at = payload.remind_at if "remind_at" in fields else task.remind_at
    if effective_due_at is None:
        raise HTTPException(status_code=422, detail="due_at cannot be null")
    comparison_due_at = (
        effective_due_at.replace(tzinfo=UTC)
        if effective_due_at.tzinfo is None
        else effective_due_at.astimezone(UTC)
    )
    comparison_remind_at = (
        None
        if effective_remind_at is None
        else (
            effective_remind_at.replace(tzinfo=UTC)
            if effective_remind_at.tzinfo is None
            else effective_remind_at.astimezone(UTC)
        )
    )
    if comparison_remind_at is not None and comparison_remind_at > comparison_due_at:
        raise HTTPException(status_code=422, detail="remind_at cannot be after due_at")

    values = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
    if payload.status is TaskStatus.completed:
        values["completed_at"] = datetime.now(UTC)
    elif "status" in payload.model_fields_set:
        if payload.status is None:
            raise HTTPException(status_code=422, detail="status cannot be null")
        values["completed_at"] = None
    changed_fields = sorted(values)
    for field, value in values.items():
        setattr(task, field, value)
    task.version += 1
    task.updated_at = datetime.now(UTC)
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="task.updated",
        entity_type="task",
        entity_id=task.id,
        actor_id=context.user_id,
        payload={"fields": changed_fields},
    )
    await db.commit()
    return task


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: uuid.UUID,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
    expected_version: int = Query(ge=1),
) -> None:
    task = await db.scalar(
        sa.select(Task)
        .where(
            Task.id == task_id,
            Task.workspace_id == context.workspace_id,
        )
        .with_for_update()
    )
    if task is None:
        raise not_found("task")
    if task.version != expected_version:
        raise conflict()

    purchase_schedule = await db.scalar(
        sa.select(PurchaseSchedule)
        .where(
            PurchaseSchedule.workspace_id == context.workspace_id,
            PurchaseSchedule.task_id == task.id,
        )
        .with_for_update()
    )
    if purchase_schedule is not None:
        cancelled_at = datetime.now(UTC)
        purchase_schedule.status = PurchaseScheduleStatus.cancelled
        purchase_schedule.task_id = None
        purchase_schedule.completed_at = cancelled_at
        purchase_schedule.updated_at = cancelled_at
        purchase_schedule.version += 1
        await db.execute(
            sa.update(OutboxEvent)
            .where(
                OutboxEvent.workspace_id == context.workspace_id,
                OutboxEvent.aggregate_type == "purchase_schedule",
                OutboxEvent.aggregate_id == purchase_schedule.id,
                OutboxEvent.event_type == "purchase.due_soon",
                OutboxEvent.processed_at.is_(None),
            )
            .values(processed_at=cancelled_at)
        )

    await db.delete(task)
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="task.deleted",
        entity_type="task",
        entity_id=task.id,
        actor_id=context.user_id,
    )
    await db.commit()


@router.get("/activity", response_model=ActivityPage)
async def list_activity(
    context: CurrentUser,
    db: AsyncSession = Depends(get_session),
    entity_type: str | None = Query(default=None, max_length=64),
    entity_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> ActivityPage:
    await enforce_cursor_page_budget(
        db,
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        resource="activity",
        cursor=cursor,
    )
    query = sa.select(ActivityEvent).where(ActivityEvent.workspace_id == context.workspace_id)
    if entity_type:
        query = query.where(ActivityEvent.entity_type == entity_type)
    if entity_id:
        query = query.where(ActivityEvent.entity_id == entity_id)
    decoded = decode_cursor(cursor)
    if decoded:
        query = query.where(
            sa.or_(
                ActivityEvent.occurred_at < decoded.created_at,
                sa.and_(
                    ActivityEvent.occurred_at == decoded.created_at,
                    ActivityEvent.id < decoded.entity_id,
                ),
            )
        )
    items = list(
        (
            await db.scalars(
                query.order_by(ActivityEvent.occurred_at.desc(), ActivityEvent.id.desc()).limit(
                    limit + 1
                )
            )
        ).all()
    )
    has_more = len(items) > limit
    visible = items[:limit]
    next_cursor = (
        encode_cursor(visible[-1].occurred_at, visible[-1].id) if has_more and visible else None
    )
    return ActivityPage(
        items=[ActivityRead.model_validate(item) for item in visible],
        next_cursor=next_cursor,
    )


async def _create_note(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    body: str,
) -> ActivityEvent:
    activity = record_domain_event(
        db,
        workspace_id=workspace_id,
        event_type=f"{entity_type}.note.created",
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=actor_id,
        payload={"body": body.strip()},
    )
    await db.commit()
    return activity


@router.post(
    "/deals/{deal_id}/notes",
    response_model=ActivityRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_deal_note(
    deal_id: uuid.UUID,
    payload: NoteCreate,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> ActivityEvent:
    await _ensure_deal(db, context.workspace_id, deal_id)
    return await _create_note(
        db,
        workspace_id=context.workspace_id,
        actor_id=context.user_id,
        entity_type="deal",
        entity_id=deal_id,
        body=payload.body,
    )


@router.post(
    "/contacts/{contact_id}/notes",
    response_model=ActivityRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_contact_note(
    contact_id: uuid.UUID,
    payload: NoteCreate,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> ActivityEvent:
    await _ensure_contact(db, context.workspace_id, contact_id)
    return await _create_note(
        db,
        workspace_id=context.workspace_id,
        actor_id=context.user_id,
        entity_type="contact",
        entity_id=contact_id,
        body=payload.body,
    )


@router.post(
    "/companies/{company_id}/notes",
    response_model=ActivityRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_company_note(
    company_id: uuid.UUID,
    payload: NoteCreate,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> ActivityEvent:
    await _ensure_company(db, context.workspace_id, company_id)
    return await _create_note(
        db,
        workspace_id=context.workspace_id,
        actor_id=context.user_id,
        entity_type="company",
        entity_id=company_id,
        body=payload.body,
    )

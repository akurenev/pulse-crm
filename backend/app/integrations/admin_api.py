"""Owner/admin API for integration settings and amoCRM import control.

Mount ``router`` below ``/api/v1``.  Secret-bearing request fields are always
encrypted before persistence and are deliberately absent from every read
schema.  The generated generic-webhook secret is returned only by create or
explicit rotation responses.
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.integrations.amo import AMO_ENTITY_TYPES
from app.integrations.forms import FormOriginError, normalize_origin
from app.integrations.models import (
    AmoConnectionStatus,
    AmoCRMConnection,
    ChannelConnection,
    ChannelKind,
    ConnectionStatus,
    Form,
    ImportJob,
    ImportStatus,
    NotificationAudience,
    NotificationRule,
    NotificationTemplate,
    WebhookEndpoint,
)
from app.integrations.s3 import AttachmentStorage
from app.integrations.secrets import SecretCipher
from app.models import BackgroundJob, Membership, Pipeline, Source, Stage
from app.security import CurrentAdmin
from app.services.events import record_domain_event

router = APIRouter(prefix="/admin/integrations", tags=["integration-settings"])
ALLOWED_NOTIFICATION_CHANNELS = frozenset({"in_app", "email", "telegram", "max"})
ALLOWED_NOTIFICATION_EVENTS = frozenset(
    {
        "lead.created",
        "deal.assigned",
        "message.inbound.received",
        "task.due_soon",
        "task.overdue",
        "deal.inactive",
        "purchase.due_soon",
        "deal.stage_changed",
    }
)


def _not_found(entity: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{entity} not found")


def _version_conflict() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "version_conflict", "message": "record was modified"},
    )


def _constraint_conflict(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)


def _secret_cipher(request: Request) -> SecretCipher:
    cipher = getattr(request.app.state, "integration_secret_cipher", None)
    if not isinstance(cipher, SecretCipher):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="integration secret cipher is not configured",
        )
    return cipher


def _secret_payload(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


class VersionedUpdate(BaseModel):
    expected_version: int = Field(ge=1)


class ChannelConnectionCreate(BaseModel):
    kind: ChannelKind
    name: str = Field(min_length=1, max_length=160)
    status: ConnectionStatus = ConnectionStatus.disabled
    credentials: dict[str, Any] = Field(default_factory=dict, max_length=100)
    settings: dict[str, Any] = Field(default_factory=dict, max_length=100)
    default_pipeline_id: uuid.UUID
    default_stage_id: uuid.UUID
    default_assignee_id: uuid.UUID | None = None

    @field_validator("kind")
    @classmethod
    def public_kind(cls, value: ChannelKind) -> ChannelKind:
        if value is ChannelKind.internal:
            raise ValueError("internal channel connections are managed by Pulse CRM")
        return value


class ChannelConnectionUpdate(VersionedUpdate):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    status: ConnectionStatus | None = None
    credentials: dict[str, Any] | None = Field(default=None, max_length=100)
    settings: dict[str, Any] | None = Field(default=None, max_length=100)
    default_pipeline_id: uuid.UUID | None = None
    default_stage_id: uuid.UUID | None = None
    default_assignee_id: uuid.UUID | None = None


class ChannelConnectionRead(BaseModel):
    id: uuid.UUID
    kind: ChannelKind
    name: str
    status: ConnectionStatus
    settings: dict[str, Any]
    default_pipeline_id: uuid.UUID | None
    default_stage_id: uuid.UUID | None
    default_assignee_id: uuid.UUID | None
    has_credentials: bool
    last_healthcheck_at: datetime | None
    last_error: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class FormCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", min_length=4, max_length=100)
    title: str = Field(min_length=1, max_length=240)
    pipeline_id: uuid.UUID
    stage_id: uuid.UUID
    assignee_id: uuid.UUID | None = None
    source_id: uuid.UUID | None = None
    fields_schema: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    allowed_origins: list[str] = Field(default_factory=list, max_length=100)
    honeypot_field: str = Field(default="company_website", min_length=1, max_length=100)
    success_message: str = Field(
        default="Спасибо! Мы свяжемся с вами.", min_length=1, max_length=500
    )
    is_active: bool = True

    @field_validator("allowed_origins")
    @classmethod
    def valid_origins(cls, values: list[str]) -> list[str]:
        try:
            return list(dict.fromkeys(normalize_origin(value) for value in values))
        except FormOriginError as exc:
            raise ValueError(str(exc)) from exc


class FormUpdate(VersionedUpdate):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    pipeline_id: uuid.UUID | None = None
    stage_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    source_id: uuid.UUID | None = None
    fields_schema: list[dict[str, Any]] | None = Field(default=None, max_length=100)
    allowed_origins: list[str] | None = Field(default=None, max_length=100)
    honeypot_field: str | None = Field(default=None, min_length=1, max_length=100)
    success_message: str | None = Field(default=None, min_length=1, max_length=500)
    is_active: bool | None = None

    @field_validator("allowed_origins")
    @classmethod
    def valid_origins(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        try:
            return list(dict.fromkeys(normalize_origin(value) for value in values))
        except FormOriginError as exc:
            raise ValueError(str(exc)) from exc


class FormRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    title: str
    pipeline_id: uuid.UUID
    stage_id: uuid.UUID
    assignee_id: uuid.UUID | None
    source_id: uuid.UUID | None
    fields_schema: list[dict[str, Any]]
    allowed_origins: list[str]
    honeypot_field: str
    success_message: str
    is_active: bool
    version: int
    created_at: datetime
    updated_at: datetime


class WebhookEndpointCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", min_length=12, max_length=100)
    name: str = Field(min_length=1, max_length=160)
    pipeline_id: uuid.UUID
    stage_id: uuid.UUID
    assignee_id: uuid.UUID | None = None
    source_id: uuid.UUID | None = None
    is_active: bool = True


class WebhookEndpointUpdate(VersionedUpdate):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    pipeline_id: uuid.UUID | None = None
    stage_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    source_id: uuid.UUID | None = None
    is_active: bool | None = None


class WebhookEndpointRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    pipeline_id: uuid.UUID
    stage_id: uuid.UUID
    assignee_id: uuid.UUID | None
    source_id: uuid.UUID | None
    is_active: bool
    version: int
    created_at: datetime
    updated_at: datetime


class WebhookEndpointCreated(WebhookEndpointRead):
    secret: str


class WebhookSecretRotated(BaseModel):
    id: uuid.UUID
    secret: str
    version: int


class NotificationTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    channel: str = Field(min_length=1, max_length=32)
    subject_template: str | None = Field(default=None, max_length=998)
    body_template: str = Field(min_length=1, max_length=20_000)
    is_active: bool = True

    @field_validator("channel")
    @classmethod
    def allowed_channel(cls, value: str) -> str:
        if value not in ALLOWED_NOTIFICATION_CHANNELS:
            raise ValueError("unsupported notification channel")
        return value


class NotificationTemplateUpdate(VersionedUpdate):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    channel: str | None = Field(default=None, min_length=1, max_length=32)
    subject_template: str | None = Field(default=None, max_length=998)
    body_template: str | None = Field(default=None, min_length=1, max_length=20_000)
    is_active: bool | None = None

    @field_validator("channel")
    @classmethod
    def allowed_channel(cls, value: str | None) -> str | None:
        if value is not None and value not in ALLOWED_NOTIFICATION_CHANNELS:
            raise ValueError("unsupported notification channel")
        return value


class NotificationTemplateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    channel: str
    subject_template: str | None
    body_template: str
    is_active: bool
    version: int
    created_at: datetime
    updated_at: datetime


class NotificationRuleCreate(BaseModel):
    template_id: uuid.UUID
    name: str = Field(min_length=1, max_length=160)
    event_type: str = Field(min_length=1, max_length=100)
    audience: NotificationAudience
    channel: str = Field(min_length=1, max_length=32)
    pipeline_id: uuid.UUID | None = None
    stage_id: uuid.UUID | None = None
    source_id: uuid.UUID | None = None
    filters: dict[str, Any] = Field(default_factory=dict, max_length=100)
    recipients: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    delay_seconds: int = Field(default=0, ge=0, le=365 * 24 * 3600)
    require_client_consent: bool = True
    is_enabled: bool = False

    @model_validator(mode="after")
    def validate_catalog(self) -> NotificationRuleCreate:
        if self.event_type not in ALLOWED_NOTIFICATION_EVENTS:
            raise ValueError("unsupported notification event")
        if self.channel not in ALLOWED_NOTIFICATION_CHANNELS:
            raise ValueError("unsupported notification channel")
        if self.audience is NotificationAudience.client and self.channel == "in_app":
            raise ValueError("clients cannot receive in-app notifications")
        if self.audience is NotificationAudience.client and not self.require_client_consent:
            raise ValueError("client notifications always require consent")
        return self


class NotificationRuleUpdate(VersionedUpdate):
    template_id: uuid.UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=160)
    event_type: str | None = Field(default=None, min_length=1, max_length=100)
    audience: NotificationAudience | None = None
    channel: str | None = Field(default=None, min_length=1, max_length=32)
    pipeline_id: uuid.UUID | None = None
    stage_id: uuid.UUID | None = None
    source_id: uuid.UUID | None = None
    filters: dict[str, Any] | None = Field(default=None, max_length=100)
    recipients: list[dict[str, Any]] | None = Field(default=None, max_length=100)
    delay_seconds: int | None = Field(default=None, ge=0, le=365 * 24 * 3600)
    require_client_consent: bool | None = None
    is_enabled: bool | None = None


class NotificationRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    template_id: uuid.UUID
    name: str
    event_type: str
    audience: NotificationAudience
    channel: str
    pipeline_id: uuid.UUID | None
    stage_id: uuid.UUID | None
    source_id: uuid.UUID | None
    filters: dict[str, Any]
    recipients: list[dict[str, Any]]
    delay_seconds: int
    require_client_consent: bool
    is_enabled: bool
    version: int
    created_at: datetime
    updated_at: datetime


class ImportStart(BaseModel):
    entity_type: str
    dry_run: bool = True
    user_mapping: dict[str, str] = Field(default_factory=dict, max_length=1_000)

    @field_validator("entity_type")
    @classmethod
    def supported_entity(cls, value: str) -> str:
        if value not in AMO_ENTITY_TYPES:
            raise ValueError("unsupported amoCRM entity type")
        return value


class ImportAction(BaseModel):
    expected_version: int = Field(ge=1)


class ImportJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: str
    status: ImportStatus
    dry_run: bool
    entity_type: str | None
    cursor: dict[str, Any]
    user_mapping: dict[str, str]
    counts: dict[str, int]
    report_object_key: str | None
    started_at: datetime | None
    completed_at: datetime | None
    last_error: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class ImportReportDownload(BaseModel):
    url: str
    expires_in: int


async def _validate_routing(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    pipeline_id: uuid.UUID | None,
    stage_id: uuid.UUID | None,
    assignee_id: uuid.UUID | None,
    source_id: uuid.UUID | None = None,
    require_pipeline_stage: bool = True,
) -> None:
    if require_pipeline_stage and (pipeline_id is None or stage_id is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="pipeline and stage are required",
        )
    if (pipeline_id is None) != (stage_id is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="pipeline and stage must be configured together",
        )
    if pipeline_id is not None:
        pipeline = await db.scalar(
            sa.select(Pipeline).where(
                Pipeline.id == pipeline_id,
                Pipeline.workspace_id == workspace_id,
            )
        )
        if pipeline is None:
            raise _not_found("pipeline")
        stage = await db.scalar(
            sa.select(Stage).where(
                Stage.id == stage_id,
                Stage.workspace_id == workspace_id,
                Stage.pipeline_id == pipeline_id,
            )
        )
        if stage is None:
            raise _not_found("stage")
    if assignee_id is not None:
        member_id = await db.scalar(
            sa.select(Membership.id).where(
                Membership.workspace_id == workspace_id,
                Membership.user_id == assignee_id,
            )
        )
        if member_id is None:
            raise _not_found("workspace member")
    if source_id is not None:
        valid_source = await db.scalar(
            sa.select(Source.id).where(
                Source.id == source_id,
                Source.workspace_id == workspace_id,
            )
        )
        if valid_source is None:
            raise _not_found("source")


async def _commit_or_conflict(db: AsyncSession, message: str) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _constraint_conflict(message) from exc


def _channel_read(entity: ChannelConnection) -> ChannelConnectionRead:
    return ChannelConnectionRead(
        id=entity.id,
        kind=entity.kind,
        name=entity.name,
        status=entity.status,
        settings=entity.settings,
        default_pipeline_id=entity.default_pipeline_id,
        default_stage_id=entity.default_stage_id,
        default_assignee_id=entity.default_assignee_id,
        has_credentials=entity.encrypted_credentials is not None,
        last_healthcheck_at=entity.last_healthcheck_at,
        last_error=entity.last_error,
        version=entity.version,
        created_at=entity.created_at,
        updated_at=entity.updated_at,
    )


async def _get_channel(
    db: AsyncSession, workspace_id: uuid.UUID, entity_id: uuid.UUID
) -> ChannelConnection:
    entity = await db.scalar(
        sa.select(ChannelConnection).where(
            ChannelConnection.id == entity_id,
            ChannelConnection.workspace_id == workspace_id,
            ChannelConnection.kind != ChannelKind.internal,
        )
    )
    if entity is None:
        raise _not_found("channel connection")
    return entity


@router.get("/channels", response_model=list[ChannelConnectionRead])
async def list_channels(
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=200),
) -> list[ChannelConnectionRead]:
    entities = list(
        (
            await db.scalars(
                sa.select(ChannelConnection)
                .where(
                    ChannelConnection.workspace_id == context.workspace_id,
                    ChannelConnection.kind != ChannelKind.internal,
                )
                .order_by(ChannelConnection.created_at.desc())
                .limit(limit)
            )
        ).all()
    )
    return [_channel_read(entity) for entity in entities]


@router.post("/channels", response_model=ChannelConnectionRead, status_code=status.HTTP_201_CREATED)
async def create_channel(
    payload: ChannelConnectionCreate,
    request: Request,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> ChannelConnectionRead:
    await _validate_routing(
        db,
        workspace_id=context.workspace_id,
        pipeline_id=payload.default_pipeline_id,
        stage_id=payload.default_stage_id,
        assignee_id=payload.default_assignee_id,
    )
    entity_id = uuid.uuid4()
    cipher = _secret_cipher(request)
    entity = ChannelConnection(
        id=entity_id,
        workspace_id=context.workspace_id,
        kind=payload.kind,
        name=payload.name.strip(),
        status=payload.status,
        encrypted_credentials=(
            cipher.encrypt(
                _secret_payload(payload.credentials),
                associated_data=f"channel:{entity_id}".encode(),
            )
            if payload.credentials
            else None
        ),
        credentials_key_id=cipher.key_id if payload.credentials else None,
        settings=payload.settings,
        default_pipeline_id=payload.default_pipeline_id,
        default_stage_id=payload.default_stage_id,
        default_assignee_id=payload.default_assignee_id,
    )
    db.add(entity)
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="channel_connection.created",
        entity_type="channel_connection",
        entity_id=entity.id,
        actor_id=context.user_id,
        payload={"kind": entity.kind.value},
    )
    await _commit_or_conflict(db, "channel connection already exists")
    return _channel_read(entity)


@router.get("/channels/{entity_id}", response_model=ChannelConnectionRead)
async def get_channel(
    entity_id: uuid.UUID,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> ChannelConnectionRead:
    return _channel_read(await _get_channel(db, context.workspace_id, entity_id))


@router.patch("/channels/{entity_id}", response_model=ChannelConnectionRead)
async def update_channel(
    entity_id: uuid.UUID,
    payload: ChannelConnectionUpdate,
    request: Request,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> ChannelConnectionRead:
    current = await _get_channel(db, context.workspace_id, entity_id)
    values = payload.model_dump(exclude_unset=True, exclude={"expected_version", "credentials"})
    pipeline_id = values.get("default_pipeline_id", current.default_pipeline_id)
    stage_id = values.get("default_stage_id", current.default_stage_id)
    assignee_id = values.get("default_assignee_id", current.default_assignee_id)
    await _validate_routing(
        db,
        workspace_id=context.workspace_id,
        pipeline_id=pipeline_id,
        stage_id=stage_id,
        assignee_id=assignee_id,
    )
    if "credentials" in payload.model_fields_set:
        credentials = payload.credentials
        if credentials:
            cipher = _secret_cipher(request)
            values["encrypted_credentials"] = cipher.encrypt(
                _secret_payload(credentials), associated_data=f"channel:{entity_id}".encode()
            )
            values["credentials_key_id"] = cipher.key_id
        else:
            values["encrypted_credentials"] = None
            values["credentials_key_id"] = None
    values.update(version=payload.expected_version + 1, updated_at=datetime.now(UTC))
    entity = (
        await db.execute(
            sa.update(ChannelConnection)
            .where(
                ChannelConnection.id == entity_id,
                ChannelConnection.workspace_id == context.workspace_id,
                ChannelConnection.version == payload.expected_version,
            )
            .values(**values)
            .returning(ChannelConnection)
        )
    ).scalar_one_or_none()
    if entity is None:
        raise _version_conflict()
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="channel_connection.updated",
        entity_type="channel_connection",
        entity_id=entity.id,
        actor_id=context.user_id,
    )
    await _commit_or_conflict(db, "channel connection name already exists")
    return _channel_read(entity)


@router.delete("/channels/{entity_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    entity_id: uuid.UUID,
    expected_version: int,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> Response:
    deleted_id = (
        await db.execute(
            sa.delete(ChannelConnection)
            .where(
                ChannelConnection.id == entity_id,
                ChannelConnection.workspace_id == context.workspace_id,
                ChannelConnection.version == expected_version,
            )
            .returning(ChannelConnection.id)
        )
    ).scalar_one_or_none()
    if deleted_id is None:
        exists = await db.scalar(
            sa.select(ChannelConnection.id).where(
                ChannelConnection.id == entity_id,
                ChannelConnection.workspace_id == context.workspace_id,
            )
        )
        if exists is not None:
            raise _version_conflict()
        raise _not_found("channel connection")
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="channel_connection.deleted",
        entity_type="channel_connection",
        entity_id=deleted_id,
        actor_id=context.user_id,
    )
    await _commit_or_conflict(db, "channel connection is in use")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _get_form(db: AsyncSession, workspace_id: uuid.UUID, entity_id: uuid.UUID) -> Form:
    entity = await db.scalar(
        sa.select(Form).where(Form.id == entity_id, Form.workspace_id == workspace_id)
    )
    if entity is None:
        raise _not_found("form")
    return entity


@router.get("/forms", response_model=list[FormRead])
async def list_forms(
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=200),
) -> list[Form]:
    return list(
        (
            await db.scalars(
                sa.select(Form)
                .where(Form.workspace_id == context.workspace_id)
                .order_by(Form.created_at.desc())
                .limit(limit)
            )
        ).all()
    )


@router.post("/forms", response_model=FormRead, status_code=status.HTTP_201_CREATED)
async def create_form(
    payload: FormCreate,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> Form:
    await _validate_routing(
        db,
        workspace_id=context.workspace_id,
        pipeline_id=payload.pipeline_id,
        stage_id=payload.stage_id,
        assignee_id=payload.assignee_id,
        source_id=payload.source_id,
    )
    entity = Form(id=uuid.uuid4(), workspace_id=context.workspace_id, **payload.model_dump())
    db.add(entity)
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="form.created",
        entity_type="form",
        entity_id=entity.id,
        actor_id=context.user_id,
    )
    await _commit_or_conflict(db, "form slug already exists")
    return entity


@router.get("/forms/{entity_id}", response_model=FormRead)
async def get_form(
    entity_id: uuid.UUID,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> Form:
    return await _get_form(db, context.workspace_id, entity_id)


@router.patch("/forms/{entity_id}", response_model=FormRead)
async def update_form(
    entity_id: uuid.UUID,
    payload: FormUpdate,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> Form:
    current = await _get_form(db, context.workspace_id, entity_id)
    values = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
    await _validate_routing(
        db,
        workspace_id=context.workspace_id,
        pipeline_id=values.get("pipeline_id", current.pipeline_id),
        stage_id=values.get("stage_id", current.stage_id),
        assignee_id=values.get("assignee_id", current.assignee_id),
        source_id=values.get("source_id", current.source_id),
    )
    values.update(version=payload.expected_version + 1, updated_at=datetime.now(UTC))
    entity = (
        await db.execute(
            sa.update(Form)
            .where(
                Form.id == entity_id,
                Form.workspace_id == context.workspace_id,
                Form.version == payload.expected_version,
            )
            .values(**values)
            .returning(Form)
        )
    ).scalar_one_or_none()
    if entity is None:
        raise _version_conflict()
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="form.updated",
        entity_type="form",
        entity_id=entity.id,
        actor_id=context.user_id,
    )
    await _commit_or_conflict(db, "form update conflicts with existing settings")
    return entity


@router.delete("/forms/{entity_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_form(
    entity_id: uuid.UUID,
    expected_version: int,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> Response:
    await _delete_versioned(
        db,
        model=Form,
        workspace_id=context.workspace_id,
        entity_id=entity_id,
        expected_version=expected_version,
        entity_name="form",
        actor_id=context.user_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _get_webhook(
    db: AsyncSession, workspace_id: uuid.UUID, entity_id: uuid.UUID
) -> WebhookEndpoint:
    entity = await db.scalar(
        sa.select(WebhookEndpoint).where(
            WebhookEndpoint.id == entity_id,
            WebhookEndpoint.workspace_id == workspace_id,
        )
    )
    if entity is None:
        raise _not_found("webhook endpoint")
    return entity


@router.get("/webhooks", response_model=list[WebhookEndpointRead])
async def list_webhooks(
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=200),
) -> list[WebhookEndpoint]:
    return list(
        (
            await db.scalars(
                sa.select(WebhookEndpoint)
                .where(WebhookEndpoint.workspace_id == context.workspace_id)
                .order_by(WebhookEndpoint.created_at.desc())
                .limit(limit)
            )
        ).all()
    )


@router.post(
    "/webhooks", response_model=WebhookEndpointCreated, status_code=status.HTTP_201_CREATED
)
async def create_webhook(
    payload: WebhookEndpointCreate,
    request: Request,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> WebhookEndpointCreated:
    await _validate_routing(
        db,
        workspace_id=context.workspace_id,
        pipeline_id=payload.pipeline_id,
        stage_id=payload.stage_id,
        assignee_id=payload.assignee_id,
        source_id=payload.source_id,
    )
    entity_id = uuid.uuid4()
    raw_secret = secrets.token_urlsafe(32)
    cipher = _secret_cipher(request)
    entity = WebhookEndpoint(
        id=entity_id,
        workspace_id=context.workspace_id,
        encrypted_secret=cipher.encrypt(
            raw_secret, associated_data=f"webhook:{entity_id}".encode()
        ),
        secret_key_id=cipher.key_id,
        **payload.model_dump(),
    )
    db.add(entity)
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="webhook_endpoint.created",
        entity_type="webhook_endpoint",
        entity_id=entity.id,
        actor_id=context.user_id,
    )
    await _commit_or_conflict(db, "webhook slug already exists")
    return WebhookEndpointCreated(
        **WebhookEndpointRead.model_validate(entity).model_dump(), secret=raw_secret
    )


@router.get("/webhooks/{entity_id}", response_model=WebhookEndpointRead)
async def get_webhook(
    entity_id: uuid.UUID,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> WebhookEndpoint:
    return await _get_webhook(db, context.workspace_id, entity_id)


@router.patch("/webhooks/{entity_id}", response_model=WebhookEndpointRead)
async def update_webhook(
    entity_id: uuid.UUID,
    payload: WebhookEndpointUpdate,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> WebhookEndpoint:
    current = await _get_webhook(db, context.workspace_id, entity_id)
    values = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
    await _validate_routing(
        db,
        workspace_id=context.workspace_id,
        pipeline_id=values.get("pipeline_id", current.pipeline_id),
        stage_id=values.get("stage_id", current.stage_id),
        assignee_id=values.get("assignee_id", current.assignee_id),
        source_id=values.get("source_id", current.source_id),
    )
    values.update(version=payload.expected_version + 1, updated_at=datetime.now(UTC))
    entity = (
        await db.execute(
            sa.update(WebhookEndpoint)
            .where(
                WebhookEndpoint.id == entity_id,
                WebhookEndpoint.workspace_id == context.workspace_id,
                WebhookEndpoint.version == payload.expected_version,
            )
            .values(**values)
            .returning(WebhookEndpoint)
        )
    ).scalar_one_or_none()
    if entity is None:
        raise _version_conflict()
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="webhook_endpoint.updated",
        entity_type="webhook_endpoint",
        entity_id=entity.id,
        actor_id=context.user_id,
    )
    await _commit_or_conflict(db, "webhook update conflicts with existing settings")
    return entity


@router.post("/webhooks/{entity_id}/rotate-secret", response_model=WebhookSecretRotated)
async def rotate_webhook_secret(
    entity_id: uuid.UUID,
    payload: ImportAction,
    request: Request,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> WebhookSecretRotated:
    await _get_webhook(db, context.workspace_id, entity_id)
    raw_secret = secrets.token_urlsafe(32)
    cipher = _secret_cipher(request)
    version = payload.expected_version + 1
    updated_id = (
        await db.execute(
            sa.update(WebhookEndpoint)
            .where(
                WebhookEndpoint.id == entity_id,
                WebhookEndpoint.workspace_id == context.workspace_id,
                WebhookEndpoint.version == payload.expected_version,
            )
            .values(
                encrypted_secret=cipher.encrypt(
                    raw_secret, associated_data=f"webhook:{entity_id}".encode()
                ),
                secret_key_id=cipher.key_id,
                version=version,
                updated_at=datetime.now(UTC),
            )
            .returning(WebhookEndpoint.id)
        )
    ).scalar_one_or_none()
    if updated_id is None:
        raise _version_conflict()
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="webhook_endpoint.secret_rotated",
        entity_type="webhook_endpoint",
        entity_id=entity_id,
        actor_id=context.user_id,
    )
    await db.commit()
    return WebhookSecretRotated(id=entity_id, secret=raw_secret, version=version)


@router.delete("/webhooks/{entity_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    entity_id: uuid.UUID,
    expected_version: int,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> Response:
    await _delete_versioned(
        db,
        model=WebhookEndpoint,
        workspace_id=context.workspace_id,
        entity_id=entity_id,
        expected_version=expected_version,
        entity_name="webhook_endpoint",
        actor_id=context.user_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _get_template(
    db: AsyncSession, workspace_id: uuid.UUID, entity_id: uuid.UUID
) -> NotificationTemplate:
    entity = await db.scalar(
        sa.select(NotificationTemplate).where(
            NotificationTemplate.id == entity_id,
            NotificationTemplate.workspace_id == workspace_id,
        )
    )
    if entity is None:
        raise _not_found("notification template")
    return entity


@router.get("/notification-templates", response_model=list[NotificationTemplateRead])
async def list_notification_templates(
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=200),
) -> list[NotificationTemplate]:
    return list(
        (
            await db.scalars(
                sa.select(NotificationTemplate)
                .where(NotificationTemplate.workspace_id == context.workspace_id)
                .order_by(NotificationTemplate.created_at.desc())
                .limit(limit)
            )
        ).all()
    )


@router.post(
    "/notification-templates",
    response_model=NotificationTemplateRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_notification_template(
    payload: NotificationTemplateCreate,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> NotificationTemplate:
    entity = NotificationTemplate(
        id=uuid.uuid4(), workspace_id=context.workspace_id, **payload.model_dump()
    )
    db.add(entity)
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="notification_template.created",
        entity_type="notification_template",
        entity_id=entity.id,
        actor_id=context.user_id,
    )
    await _commit_or_conflict(db, "notification template already exists")
    return entity


@router.get("/notification-templates/{entity_id}", response_model=NotificationTemplateRead)
async def get_notification_template(
    entity_id: uuid.UUID,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> NotificationTemplate:
    return await _get_template(db, context.workspace_id, entity_id)


@router.patch("/notification-templates/{entity_id}", response_model=NotificationTemplateRead)
async def update_notification_template(
    entity_id: uuid.UUID,
    payload: NotificationTemplateUpdate,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> NotificationTemplate:
    current = await _get_template(db, context.workspace_id, entity_id)
    values = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
    if "channel" in values and values["channel"] != current.channel:
        linked_rule = await db.scalar(
            sa.select(NotificationRule.id).where(NotificationRule.template_id == entity_id).limit(1)
        )
        if linked_rule is not None:
            raise _constraint_conflict("template channel cannot change while rules use it")
    values.update(version=payload.expected_version + 1, updated_at=datetime.now(UTC))
    entity = (
        await db.execute(
            sa.update(NotificationTemplate)
            .where(
                NotificationTemplate.id == entity_id,
                NotificationTemplate.workspace_id == context.workspace_id,
                NotificationTemplate.version == payload.expected_version,
            )
            .values(**values)
            .returning(NotificationTemplate)
        )
    ).scalar_one_or_none()
    if entity is None:
        raise _version_conflict()
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="notification_template.updated",
        entity_type="notification_template",
        entity_id=entity.id,
        actor_id=context.user_id,
    )
    await _commit_or_conflict(db, "notification template already exists")
    return entity


@router.delete("/notification-templates/{entity_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_notification_template(
    entity_id: uuid.UUID,
    expected_version: int,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> Response:
    await _delete_versioned(
        db,
        model=NotificationTemplate,
        workspace_id=context.workspace_id,
        entity_id=entity_id,
        expected_version=expected_version,
        entity_name="notification_template",
        actor_id=context.user_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _get_rule(
    db: AsyncSession, workspace_id: uuid.UUID, entity_id: uuid.UUID
) -> NotificationRule:
    entity = await db.scalar(
        sa.select(NotificationRule).where(
            NotificationRule.id == entity_id,
            NotificationRule.workspace_id == workspace_id,
        )
    )
    if entity is None:
        raise _not_found("notification rule")
    return entity


async def _validate_rule(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    template_id: uuid.UUID,
    event_type: str,
    audience: NotificationAudience,
    channel: str,
    pipeline_id: uuid.UUID | None,
    stage_id: uuid.UUID | None,
    source_id: uuid.UUID | None,
    require_client_consent: bool,
) -> None:
    if event_type not in ALLOWED_NOTIFICATION_EVENTS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="event")
    if channel not in ALLOWED_NOTIFICATION_CHANNELS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="channel")
    if audience is NotificationAudience.client and channel == "in_app":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="clients cannot receive in-app notifications",
        )
    if audience is NotificationAudience.client and not require_client_consent:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="client notifications always require consent",
        )
    template = await _get_template(db, workspace_id, template_id)
    if template.channel != channel:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="rule channel does not match template",
        )
    await _validate_routing(
        db,
        workspace_id=workspace_id,
        pipeline_id=pipeline_id,
        stage_id=stage_id,
        assignee_id=None,
        source_id=source_id,
        require_pipeline_stage=False,
    )


@router.get("/notification-rules", response_model=list[NotificationRuleRead])
async def list_notification_rules(
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=200),
) -> list[NotificationRule]:
    return list(
        (
            await db.scalars(
                sa.select(NotificationRule)
                .where(NotificationRule.workspace_id == context.workspace_id)
                .order_by(NotificationRule.created_at.desc())
                .limit(limit)
            )
        ).all()
    )


@router.post(
    "/notification-rules",
    response_model=NotificationRuleRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_notification_rule(
    payload: NotificationRuleCreate,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> NotificationRule:
    await _validate_rule(
        db,
        workspace_id=context.workspace_id,
        template_id=payload.template_id,
        event_type=payload.event_type,
        audience=payload.audience,
        channel=payload.channel,
        pipeline_id=payload.pipeline_id,
        stage_id=payload.stage_id,
        source_id=payload.source_id,
        require_client_consent=payload.require_client_consent,
    )
    entity = NotificationRule(
        id=uuid.uuid4(), workspace_id=context.workspace_id, **payload.model_dump()
    )
    db.add(entity)
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="notification_rule.created",
        entity_type="notification_rule",
        entity_id=entity.id,
        actor_id=context.user_id,
    )
    await db.commit()
    return entity


@router.get("/notification-rules/{entity_id}", response_model=NotificationRuleRead)
async def get_notification_rule(
    entity_id: uuid.UUID,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> NotificationRule:
    return await _get_rule(db, context.workspace_id, entity_id)


@router.patch("/notification-rules/{entity_id}", response_model=NotificationRuleRead)
async def update_notification_rule(
    entity_id: uuid.UUID,
    payload: NotificationRuleUpdate,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> NotificationRule:
    current = await _get_rule(db, context.workspace_id, entity_id)
    values = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
    await _validate_rule(
        db,
        workspace_id=context.workspace_id,
        template_id=values.get("template_id", current.template_id),
        event_type=values.get("event_type", current.event_type),
        audience=values.get("audience", current.audience),
        channel=values.get("channel", current.channel),
        pipeline_id=values.get("pipeline_id", current.pipeline_id),
        stage_id=values.get("stage_id", current.stage_id),
        source_id=values.get("source_id", current.source_id),
        require_client_consent=values.get("require_client_consent", current.require_client_consent),
    )
    values.update(version=payload.expected_version + 1, updated_at=datetime.now(UTC))
    entity = (
        await db.execute(
            sa.update(NotificationRule)
            .where(
                NotificationRule.id == entity_id,
                NotificationRule.workspace_id == context.workspace_id,
                NotificationRule.version == payload.expected_version,
            )
            .values(**values)
            .returning(NotificationRule)
        )
    ).scalar_one_or_none()
    if entity is None:
        raise _version_conflict()
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="notification_rule.updated",
        entity_type="notification_rule",
        entity_id=entity.id,
        actor_id=context.user_id,
    )
    await db.commit()
    return entity


@router.delete("/notification-rules/{entity_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_notification_rule(
    entity_id: uuid.UUID,
    expected_version: int,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> Response:
    await _delete_versioned(
        db,
        model=NotificationRule,
        workspace_id=context.workspace_id,
        entity_id=entity_id,
        expected_version=expected_version,
        entity_name="notification_rule",
        actor_id=context.user_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _delete_versioned(
    db: AsyncSession,
    *,
    model: Any,
    workspace_id: uuid.UUID,
    entity_id: uuid.UUID,
    expected_version: int,
    entity_name: str,
    actor_id: uuid.UUID,
) -> None:
    deleted_id = (
        await db.execute(
            sa.delete(model)
            .where(
                model.id == entity_id,
                model.workspace_id == workspace_id,
                model.version == expected_version,
            )
            .returning(model.id)
        )
    ).scalar_one_or_none()
    if deleted_id is None:
        exists = await db.scalar(
            sa.select(model.id).where(model.id == entity_id, model.workspace_id == workspace_id)
        )
        if exists is not None:
            raise _version_conflict()
        raise _not_found(entity_name.replace("_", " "))
    record_domain_event(
        db,
        workspace_id=workspace_id,
        event_type=f"{entity_name}.deleted",
        entity_type=entity_name,
        entity_id=deleted_id,
        actor_id=actor_id,
    )
    await _commit_or_conflict(db, f"{entity_name.replace('_', ' ')} is in use")


async def _get_import(db: AsyncSession, workspace_id: uuid.UUID, entity_id: uuid.UUID) -> ImportJob:
    entity = await db.scalar(
        sa.select(ImportJob).where(
            ImportJob.id == entity_id,
            ImportJob.workspace_id == workspace_id,
        )
    )
    if entity is None:
        raise _not_found("import job")
    return entity


@router.get("/imports", response_model=list[ImportJobRead])
async def list_imports(
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=200),
) -> list[ImportJob]:
    return list(
        (
            await db.scalars(
                sa.select(ImportJob)
                .where(ImportJob.workspace_id == context.workspace_id)
                .order_by(ImportJob.created_at.desc())
                .limit(limit)
            )
        ).all()
    )


@router.get("/imports/{entity_id}", response_model=ImportJobRead)
async def get_import(
    entity_id: uuid.UUID,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> ImportJob:
    return await _get_import(db, context.workspace_id, entity_id)


@router.get("/imports/{entity_id}/report", response_model=ImportReportDownload)
async def get_import_report(
    entity_id: uuid.UUID,
    request: Request,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> ImportReportDownload:
    import_job = await _get_import(db, context.workspace_id, entity_id)
    if import_job.status is not ImportStatus.succeeded:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="import is not completed",
        )
    if import_job.report_object_key is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="import report is not available",
        )
    storage = getattr(request.app.state, "attachment_storage", None)
    if not isinstance(storage, AttachmentStorage):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="object storage is not configured",
        )
    expires_in = 300
    expected_key = storage.import_report_key(context.workspace_id, import_job.id)
    if import_job.report_object_key != expected_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="import report is not available",
        )
    try:
        url = await storage.signed_import_report_url(
            workspace_id=context.workspace_id,
            object_key=import_job.report_object_key,
            expires_seconds=expires_in,
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="import report is not available",
        ) from exc
    return ImportReportDownload(url=url, expires_in=expires_in)


@router.post("/imports/start", response_model=ImportJobRead, status_code=status.HTTP_202_ACCEPTED)
async def start_import(
    payload: ImportStart,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> ImportJob:
    connection_id = await db.scalar(
        sa.select(AmoCRMConnection.id).where(
            AmoCRMConnection.workspace_id == context.workspace_id,
            AmoCRMConnection.status == AmoConnectionStatus.connected,
        )
    )
    if connection_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="connect amoCRM before starting an import",
        )
    entity = ImportJob(
        id=uuid.uuid4(),
        workspace_id=context.workspace_id,
        provider="amocrm",
        status=ImportStatus.running,
        dry_run=payload.dry_run,
        entity_type=payload.entity_type,
        user_mapping=payload.user_mapping,
        started_at=datetime.now(UTC),
    )
    db.add(entity)
    db.add(
        BackgroundJob(
            job_type="amo_import.page",
            payload={"import_job_id": str(entity.id)},
            dedupe_key=f"amo-import:{entity.id}:initial",
        )
    )
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="amo_import.started",
        entity_type="import_job",
        entity_id=entity.id,
        actor_id=context.user_id,
        payload={"dry_run": entity.dry_run, "entity_type": entity.entity_type},
    )
    await db.commit()
    return entity


@router.post("/imports/{entity_id}/pause", response_model=ImportJobRead)
async def pause_import_job(
    entity_id: uuid.UUID,
    payload: ImportAction,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> ImportJob:
    current = await _get_import(db, context.workspace_id, entity_id)
    if current.status not in {ImportStatus.pending, ImportStatus.running}:
        raise _constraint_conflict("only pending or running imports can be paused")
    entity = (
        await db.execute(
            sa.update(ImportJob)
            .where(
                ImportJob.id == entity_id,
                ImportJob.workspace_id == context.workspace_id,
                ImportJob.version == payload.expected_version,
            )
            .values(
                status=ImportStatus.paused,
                version=payload.expected_version + 1,
                updated_at=datetime.now(UTC),
            )
            .returning(ImportJob)
        )
    ).scalar_one_or_none()
    if entity is None:
        raise _version_conflict()
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="amo_import.paused",
        entity_type="import_job",
        entity_id=entity.id,
        actor_id=context.user_id,
    )
    await db.commit()
    return entity


@router.post("/imports/{entity_id}/resume", response_model=ImportJobRead)
async def resume_import_job(
    entity_id: uuid.UUID,
    payload: ImportAction,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> ImportJob:
    current = await _get_import(db, context.workspace_id, entity_id)
    if current.status not in {ImportStatus.paused, ImportStatus.failed}:
        raise _constraint_conflict("only paused or failed imports can be resumed")
    next_version = payload.expected_version + 1
    entity = (
        await db.execute(
            sa.update(ImportJob)
            .where(
                ImportJob.id == entity_id,
                ImportJob.workspace_id == context.workspace_id,
                ImportJob.version == payload.expected_version,
            )
            .values(
                status=ImportStatus.running,
                last_error=None,
                completed_at=None,
                version=next_version,
                updated_at=datetime.now(UTC),
            )
            .returning(ImportJob)
        )
    ).scalar_one_or_none()
    if entity is None:
        raise _version_conflict()
    db.add(
        BackgroundJob(
            job_type="amo_import.page",
            payload={"import_job_id": str(entity.id)},
            dedupe_key=f"amo-import:{entity.id}:resume:{next_version}",
        )
    )
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="amo_import.resumed",
        entity_type="import_job",
        entity_id=entity.id,
        actor_id=context.user_id,
    )
    await db.commit()
    return entity

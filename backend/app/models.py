from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


JSON_TYPE = sa.JSON().with_variant(JSONB(), "postgresql")
UUID_TYPE = sa.Uuid(as_uuid=True)


class Role(StrEnum):
    owner = "owner"
    admin = "admin"
    manager = "manager"
    employee = "employee"


class StageType(StrEnum):
    open = "open"
    won = "won"
    lost = "lost"


class FieldEntity(StrEnum):
    contact = "contact"
    company = "company"
    deal = "deal"


class FieldType(StrEnum):
    text = "text"
    number = "number"
    date = "date"
    boolean = "boolean"
    select = "select"


class TaskStatus(StrEnum):
    open = "open"
    completed = "completed"
    cancelled = "cancelled"


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class DeliveryStatus(StrEnum):
    pending = "pending"
    processing = "processing"
    delivered = "delivered"
    failed = "failed"


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=utcnow,
        server_default=sa.func.now(),
        onupdate=utcnow,
        nullable=False,
    )


class Workspace(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    slug: Mapped[str] = mapped_column(sa.String(80), unique=True, nullable=False)
    timezone: Mapped[str] = mapped_column(
        sa.String(64), default="Asia/Yekaterinburg", nullable=False
    )
    currency: Mapped[str] = mapped_column(sa.String(3), default="RUB", nullable=False)


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(sa.String(320), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    password_hash: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)


class Membership(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_membership_workspace_user"),
        sa.Index("ix_memberships_user_id", "user_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[Role] = mapped_column(sa.Enum(Role, native_enum=False), nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)


class Session(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "sessions"
    __table_args__ = (
        sa.Index("ix_sessions_user_id", "user_id"),
        sa.Index("ix_sessions_workspace_id", "workspace_id"),
        sa.Index(
            "ix_sessions_active_expires",
            "expires_at",
            postgresql_where=sa.text("revoked_at IS NULL"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)
    csrf_token_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(sa.String(512))
    ip_address: Mapped[str | None] = mapped_column(sa.String(64))


class Invitation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "invitations"
    __table_args__ = (
        sa.Index("ix_invitations_workspace_id", "workspace_id"),
        sa.Index("ix_invitations_email", "email"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    role: Mapped[Role] = mapped_column(sa.Enum(Role, native_enum=False), nullable=False)
    token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)
    invited_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class Company(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "companies"
    __table_args__ = (
        sa.Index(
            "ix_companies_workspace_deleted_created_id",
            "workspace_id",
            "deleted_at",
            "created_at",
            "id",
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(sa.String(240), nullable=False)
    website: Mapped[str | None] = mapped_column(sa.String(512))
    phone: Mapped[str | None] = mapped_column(sa.String(64))
    email: Mapped[str | None] = mapped_column(sa.String(320))
    tags: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    custom_fields: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class Contact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "contacts"
    __table_args__ = (
        sa.Index("ix_contacts_company_id", "company_id"),
        sa.Index(
            "ix_contacts_workspace_assignee_deleted_created_id",
            "workspace_id",
            "assignee_id",
            "deleted_at",
            "created_at",
            "id",
        ),
        sa.Index("ix_contacts_workspace_email", "workspace_id", "primary_email"),
        sa.Index("ix_contacts_workspace_phone", "workspace_id", "primary_phone"),
        sa.Index(
            "ix_contacts_workspace_deleted_created_id",
            "workspace_id",
            "deleted_at",
            "created_at",
            "id",
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("companies.id", ondelete="SET NULL")
    )
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    first_name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    last_name: Mapped[str] = mapped_column(sa.String(120), default="", nullable=False)
    primary_email: Mapped[str | None] = mapped_column(sa.String(320))
    primary_phone: Mapped[str | None] = mapped_column(sa.String(64))
    emails: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    phones: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    custom_fields: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class Pipeline(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "pipelines"
    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "name", name="uq_pipeline_workspace_name"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    position: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)


class Stage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "stages"
    __table_args__ = (
        sa.UniqueConstraint("pipeline_id", "position", name="uq_stage_pipeline_position"),
        sa.Index("ix_stages_workspace_id", "workspace_id"),
        sa.Index("ix_stages_pipeline_id", "pipeline_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("pipelines.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    color: Mapped[str] = mapped_column(sa.String(7), default="#64748B", nullable=False)
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    stage_type: Mapped[StageType] = mapped_column(
        sa.Enum(StageType, native_enum=False), default=StageType.open, nullable=False
    )
    version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)


class Source(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "sources"
    __table_args__ = (sa.UniqueConstraint("workspace_id", "key", name="uq_source_workspace_key"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)


class CustomFieldDefinition(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "custom_field_definitions"
    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "entity_type", "key", name="uq_custom_field_key"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_type: Mapped[FieldEntity] = mapped_column(
        sa.Enum(FieldEntity, native_enum=False), nullable=False
    )
    key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    field_type: Mapped[FieldType] = mapped_column(
        sa.Enum(FieldType, native_enum=False), nullable=False
    )
    options: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)


class StageRequiredField(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "stage_required_fields"
    __table_args__ = (
        sa.CheckConstraint(
            "(field_definition_id IS NULL) <> (built_in_key IS NULL)",
            name="exactly_one_field_reference",
        ),
        sa.UniqueConstraint(
            "stage_id", "field_definition_id", name="uq_stage_required_custom_field"
        ),
        sa.UniqueConstraint("stage_id", "built_in_key", name="uq_stage_required_builtin_field"),
        sa.Index("ix_stage_required_workspace_id", "workspace_id"),
        sa.Index("ix_stage_required_stage_id", "stage_id"),
        sa.Index("ix_stage_required_definition_id", "field_definition_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    stage_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("stages.id", ondelete="CASCADE"), nullable=False
    )
    field_definition_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("custom_field_definitions.id", ondelete="CASCADE")
    )
    built_in_key: Mapped[str | None] = mapped_column(sa.String(64))


class Deal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "deals"
    __table_args__ = (
        sa.Index("ix_deals_workspace_pipeline_stage", "workspace_id", "pipeline_id", "stage_id"),
        sa.Index(
            "ix_deals_workspace_pipeline_stage_deleted_created_id",
            "workspace_id",
            "pipeline_id",
            "stage_id",
            "deleted_at",
            "created_at",
            "id",
        ),
        sa.Index("ix_deals_company_id", "company_id"),
        sa.Index("ix_deals_assignee_id", "assignee_id"),
        sa.Index("ix_deals_source_id", "source_id"),
        sa.Index("ix_deals_next_purchase_at", "workspace_id", "next_purchase_at"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("pipelines.id", ondelete="RESTRICT"), nullable=False
    )
    stage_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("stages.id", ondelete="RESTRICT"), nullable=False
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("companies.id", ondelete="SET NULL")
    )
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("sources.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(sa.String(240), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(sa.Numeric(14, 2))
    currency: Mapped[str] = mapped_column(sa.String(3), default="RUB", nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    custom_fields: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    next_purchase_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_activity_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, server_default=sa.func.now(), nullable=False
    )
    version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class DealContact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "deal_contacts"
    __table_args__ = (
        sa.UniqueConstraint("deal_id", "contact_id", name="uq_deal_contact"),
        sa.Index("ix_deal_contacts_workspace_id", "workspace_id"),
        sa.Index("ix_deal_contacts_deal_id", "deal_id"),
        sa.Index("ix_deal_contacts_contact_id", "contact_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    deal_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("deals.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    is_primary: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)


class DealStageHistory(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "deal_stage_history"
    __table_args__ = (
        sa.Index("ix_deal_stage_history_workspace_id", "workspace_id"),
        sa.Index("ix_deal_stage_history_deal_changed", "deal_id", "changed_at"),
        sa.Index("ix_deal_stage_history_actor_id", "actor_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    deal_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("deals.id", ondelete="CASCADE"), nullable=False
    )
    from_stage_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("stages.id", ondelete="SET NULL")
    )
    to_stage_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("stages.id", ondelete="RESTRICT"), nullable=False
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    changed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, server_default=sa.func.now(), nullable=False
    )


class Task(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        sa.Index("ix_tasks_workspace_status_due", "workspace_id", "status", "due_at"),
        sa.Index(
            "ix_tasks_workspace_status_created_id",
            "workspace_id",
            "status",
            "created_at",
            "id",
        ),
        sa.Index("ix_tasks_assignee_id", "assignee_id"),
        sa.Index("ix_tasks_deal_id", "deal_id"),
        sa.Index("ix_tasks_contact_id", "contact_id"),
        sa.Index("ix_tasks_company_id", "company_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(sa.String(240), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    task_type: Mapped[str] = mapped_column(sa.String(64), default="follow_up", nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        sa.Enum(TaskStatus, native_enum=False), default=TaskStatus.open, nullable=False
    )
    due_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    remind_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    assignee_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("deals.id", ondelete="CASCADE")
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("contacts.id", ondelete="CASCADE")
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("companies.id", ondelete="CASCADE")
    )
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)


class ActivityEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "activity_events"
    __table_args__ = (
        sa.Index("ix_activity_workspace_occurred", "workspace_id", "occurred_at", "id"),
        sa.Index("ix_activity_entity", "workspace_id", "entity_type", "entity_id"),
        sa.Index("ix_activity_actor_id", "actor_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, server_default=sa.func.now(), nullable=False
    )


class NoteAttachment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Private file attached to a CRM note, never to a conversation message."""

    __tablename__ = "note_attachments"
    __table_args__ = (
        sa.UniqueConstraint("object_key", name="uq_note_attachment_object_key"),
        sa.UniqueConstraint(
            "activity_event_id",
            "position",
            name="uq_note_attachment_activity_position",
        ),
        sa.Index(
            "ix_note_attachments_workspace_activity",
            "workspace_id",
            "activity_event_id",
            "position",
            "id",
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    activity_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("activity_events.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    object_key: Mapped[str] = mapped_column(sa.String(1024), nullable=False)
    original_filename: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)


class OutboxEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        sa.Index(
            "ix_outbox_pending_available",
            "available_at",
            postgresql_where=sa.text("processed_at IS NULL"),
        ),
        sa.Index("ix_outbox_workspace_id", "workspace_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, server_default=sa.func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(sa.Text)


class BackgroundJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "background_jobs"
    __table_args__ = (
        sa.Index(
            "ix_background_jobs_claim",
            "run_at",
            "created_at",
            postgresql_where=sa.text("status = 'queued'"),
        ),
        sa.Index("ix_background_jobs_lease", "lease_until"),
        sa.Index("ix_background_jobs_workspace_updated", "workspace_id", "updated_at"),
    )

    # Null is reserved for application-wide maintenance jobs.  Every job that
    # acts on CRM or integration data must carry its workspace explicitly.
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    job_type: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        sa.Enum(JobStatus, native_enum=False), default=JobStatus.queued, nullable=False
    )
    run_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, server_default=sa.func.now(), nullable=False
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(sa.Integer, default=5, nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(sa.String(255), unique=True)
    lease_owner: Mapped[str | None] = mapped_column(sa.String(160))
    lease_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(sa.Text)


class CursorAccessBucket(UUIDPrimaryKeyMixin, Base):
    """One fixed-window pagination counter per user and CRM resource."""

    __tablename__ = "cursor_access_buckets"
    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id",
            "user_id",
            "resource",
            name="uq_cursor_access_bucket_scope",
        ),
        sa.Index("ix_cursor_access_buckets_workspace", "workspace_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    resource: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    request_count: Mapped[int] = mapped_column(sa.Integer, nullable=False)


class RealtimeEvent(Base):
    __tablename__ = "realtime_events"
    __table_args__ = (sa.Index("ix_realtime_workspace_id", "workspace_id", "id"),)

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, server_default=sa.func.now(), nullable=False
    )

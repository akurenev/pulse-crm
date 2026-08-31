from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models import FieldEntity, FieldType, Role, StageType, TaskStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class VersionedUpdate(BaseModel):
    expected_version: int = Field(ge=1)


class WorkspaceRead(ORMModel):
    id: uuid.UUID
    name: str
    slug: str
    timezone: str
    currency: str


class UserRead(ORMModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str
    role: Role | None = None
    version: int = 1


class UserUpdate(VersionedUpdate):
    full_name: str | None = Field(default=None, min_length=2, max_length=160)
    role: Role | None = None

    @field_validator("full_name")
    @classmethod
    def normalize_full_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if len(normalized) < 2:
            raise ValueError("full_name must contain at least two visible characters")
        return normalized

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if not ({"full_name", "role"} & self.model_fields_set):
            raise ValueError("provide full_name or role")
        if "full_name" in self.model_fields_set and self.full_name is None:
            raise ValueError("full_name cannot be null")
        if "role" in self.model_fields_set and self.role is None:
            raise ValueError("role cannot be null")
        return self


class BootstrapRequest(BaseModel):
    workspace_name: str = Field(min_length=2, max_length=160)
    workspace_slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", min_length=2, max_length=80)
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=160)
    password: str = Field(min_length=12, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class AuthResponse(BaseModel):
    user: UserRead
    workspace: WorkspaceRead
    csrf_token: str


class InvitationCreate(BaseModel):
    email: EmailStr
    role: Role = Role.manager
    expires_in_hours: int = Field(default=72, ge=1, le=24 * 30)

    @model_validator(mode="after")
    def owner_cannot_be_invited(self) -> Self:
        if self.role is Role.owner:
            raise ValueError("owner role cannot be assigned by invitation")
        return self


class InvitationCreated(ORMModel):
    id: uuid.UUID
    email: EmailStr
    role: Role
    expires_at: datetime
    token: str


class InvitationAccept(BaseModel):
    token: str = Field(min_length=20, max_length=256)
    full_name: str = Field(min_length=2, max_length=160)
    password: str = Field(min_length=12, max_length=128)


class CompanyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    website: str | None = Field(default=None, max_length=512)
    phone: str | None = Field(default=None, max_length=64)
    email: EmailStr | None = None
    tags: list[str] = Field(default_factory=list, max_length=100)
    custom_fields: dict[str, Any] = Field(default_factory=dict)


class CompanyUpdate(VersionedUpdate):
    name: str | None = Field(default=None, min_length=1, max_length=240)
    website: str | None = Field(default=None, max_length=512)
    phone: str | None = Field(default=None, max_length=64)
    email: EmailStr | None = None
    tags: list[str] | None = Field(default=None, max_length=100)
    custom_fields: dict[str, Any] | None = None


class CompanyRead(ORMModel):
    id: uuid.UUID
    name: str
    website: str | None
    phone: str | None
    email: str | None
    tags: list[str]
    custom_fields: dict[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime


class ContactCreate(BaseModel):
    first_name: str = Field(min_length=1, max_length=120)
    last_name: str = Field(default="", max_length=120)
    company_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    primary_email: EmailStr | None = None
    primary_phone: str | None = Field(default=None, max_length=64)
    emails: list[EmailStr] = Field(default_factory=list, max_length=20)
    phones: list[str] = Field(default_factory=list, max_length=20)
    tags: list[str] = Field(default_factory=list, max_length=100)
    custom_fields: dict[str, Any] = Field(default_factory=dict)


class ContactUpdate(VersionedUpdate):
    first_name: str | None = Field(default=None, min_length=1, max_length=120)
    last_name: str | None = Field(default=None, max_length=120)
    company_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    primary_email: EmailStr | None = None
    primary_phone: str | None = Field(default=None, max_length=64)
    emails: list[EmailStr] | None = Field(default=None, max_length=20)
    phones: list[str] | None = Field(default=None, max_length=20)
    tags: list[str] | None = Field(default=None, max_length=100)
    custom_fields: dict[str, Any] | None = None


class ContactRead(ORMModel):
    id: uuid.UUID
    first_name: str
    last_name: str
    company_id: uuid.UUID | None
    assignee_id: uuid.UUID | None
    primary_email: str | None
    primary_phone: str | None
    emails: list[str]
    phones: list[str]
    tags: list[str]
    custom_fields: dict[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime


class StageCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    color: str = Field(default="#64748B", pattern=r"^#[0-9A-Fa-f]{6}$")
    position: int = Field(ge=0)
    stage_type: StageType = StageType.open


class StageRead(ORMModel):
    id: uuid.UUID
    pipeline_id: uuid.UUID
    name: str
    color: str
    position: int
    stage_type: StageType
    version: int


class PipelineCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    position: int = Field(default=0, ge=0)
    stages: list[StageCreate] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def require_open_stage(self) -> Self:
        if not any(stage.stage_type is StageType.open for stage in self.stages):
            raise ValueError("pipeline requires at least one open stage")
        return self


class PipelineUpdate(VersionedUpdate):
    name: str = Field(min_length=1, max_length=160)


class StageAppendCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    color: str = Field(default="#64748B", pattern=r"^#[0-9A-Fa-f]{6}$")
    stage_type: Literal[StageType.open] = StageType.open


class StageUpdate(VersionedUpdate):
    name: str = Field(min_length=1, max_length=120)
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")


class PipelineRead(ORMModel):
    id: uuid.UUID
    name: str
    position: int
    is_active: bool
    version: int
    stages: list[StageRead] = Field(default_factory=list)


class CustomFieldCreate(BaseModel):
    entity_type: FieldEntity
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$", min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    field_type: FieldType
    options: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_options(self) -> Self:
        if self.field_type is FieldType.select and not self.options:
            raise ValueError("select fields require at least one option")
        if self.field_type is not FieldType.select and self.options:
            raise ValueError("options are only valid for select fields")
        return self


class CustomFieldRead(ORMModel):
    id: uuid.UUID
    entity_type: FieldEntity
    key: str
    name: str
    field_type: FieldType
    options: list[str]
    is_active: bool


class RequiredFieldInput(BaseModel):
    field_definition_id: uuid.UUID | None = None
    built_in_key: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def exactly_one_reference(self) -> Self:
        if (self.field_definition_id is None) == (self.built_in_key is None):
            raise ValueError("provide exactly one field reference")
        return self


class RequiredFieldsReplace(BaseModel):
    fields: list[RequiredFieldInput] = Field(max_length=100)


class RequiredFieldRead(ORMModel):
    id: uuid.UUID
    field_definition_id: uuid.UUID | None
    built_in_key: str | None


class DealCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    pipeline_id: uuid.UUID
    stage_id: uuid.UUID
    company_id: uuid.UUID | None = None
    contact_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    assignee_id: uuid.UUID | None = None
    source_id: uuid.UUID | None = None
    amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    currency: str = Field(default="RUB", pattern=r"^[A-Z]{3}$")
    tags: list[str] = Field(default_factory=list, max_length=100)
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    next_purchase_at: datetime | None = None


class DealUpdate(VersionedUpdate):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    company_id: uuid.UUID | None = None
    contact_ids: list[uuid.UUID] | None = Field(default=None, max_length=100)
    assignee_id: uuid.UUID | None = None
    source_id: uuid.UUID | None = None
    amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    tags: list[str] | None = Field(default=None, max_length=100)
    custom_fields: dict[str, Any] | None = None
    next_purchase_at: datetime | None = None


class StageTransition(BaseModel):
    target_stage_id: uuid.UUID
    expected_version: int = Field(ge=1)


class DealRead(ORMModel):
    id: uuid.UUID
    title: str
    pipeline_id: uuid.UUID
    stage_id: uuid.UUID
    company_id: uuid.UUID | None
    company: CompanyRead | None = None
    contact_ids: list[uuid.UUID] = Field(default_factory=list)
    primary_contact: ContactRead | None = None
    assignee_id: uuid.UUID | None
    source_id: uuid.UUID | None
    amount: Decimal | None
    currency: str
    tags: list[str]
    custom_fields: dict[str, Any]
    next_purchase_at: datetime | None
    last_activity_at: datetime
    version: int
    created_at: datetime
    updated_at: datetime


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    description: str | None = None
    task_type: str = Field(default="follow_up", min_length=1, max_length=64)
    due_at: datetime
    remind_at: datetime | None = None
    assignee_id: uuid.UUID
    deal_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    company_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def reminder_precedes_due_date(self) -> Self:
        if self.remind_at and self.remind_at > self.due_at:
            raise ValueError("remind_at cannot be after due_at")
        return self


class TaskUpdate(VersionedUpdate):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    description: str | None = None
    task_type: str | None = Field(default=None, min_length=1, max_length=64)
    due_at: datetime | None = None
    remind_at: datetime | None = None
    assignee_id: uuid.UUID | None = None
    deal_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    company_id: uuid.UUID | None = None
    status: TaskStatus | None = None


class TaskRead(ORMModel):
    id: uuid.UUID
    title: str
    description: str | None
    task_type: str
    status: TaskStatus
    due_at: datetime
    remind_at: datetime | None
    assignee_id: uuid.UUID
    deal_id: uuid.UUID | None
    contact_id: uuid.UUID | None
    company_id: uuid.UUID | None
    completed_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class NoteAttachmentRead(ORMModel):
    id: uuid.UUID
    activity_event_id: uuid.UUID
    position: int
    original_filename: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: datetime


class ActivityRead(ORMModel):
    id: uuid.UUID
    event_type: str
    entity_type: str
    entity_id: uuid.UUID
    actor_id: uuid.UUID | None
    payload: dict[str, Any]
    occurred_at: datetime
    attachments: list[NoteAttachmentRead] = Field(default_factory=list)


class NoteCreate(BaseModel):
    body: str = Field(min_length=1, max_length=10_000)

    @field_validator("body")
    @classmethod
    def body_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("note must not be blank")
        return value


class PageMeta(BaseModel):
    next_cursor: str | None = None


class CompanyPage(PageMeta):
    items: list[CompanyRead]


class ContactPage(PageMeta):
    items: list[ContactRead]


class DealPage(PageMeta):
    items: list[DealRead]


class TaskPage(PageMeta):
    items: list[TaskRead]


class ActivityPage(PageMeta):
    items: list[ActivityRead]


class SourceRead(ORMModel):
    id: uuid.UUID
    key: str
    name: str
    is_active: bool


class HealthResponse(BaseModel):
    status: str
    database: str | None = None
    job_runner: str | None = None

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import (
    JSON_TYPE,
    UUID_TYPE,
    DeliveryStatus,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    utcnow,
)


class ChannelKind(StrEnum):
    email = "email"
    telegram = "telegram"
    max = "max"
    internal = "internal"


class ConnectionStatus(StrEnum):
    disabled = "disabled"
    active = "active"
    degraded = "degraded"


class ConversationStatus(StrEnum):
    active = "active"
    closed = "closed"
    review_needed = "review_needed"


class MessageDirection(StrEnum):
    inbound = "inbound"
    outbound = "outbound"


class MessageStatus(StrEnum):
    received = "received"
    queued = "queued"
    sent = "sent"
    failed = "failed"


class InboundStatus(StrEnum):
    accepted = "accepted"
    processing = "processing"
    processed = "processed"
    failed = "failed"


class NotificationAudience(StrEnum):
    employee = "employee"
    client = "client"


class ImportStatus(StrEnum):
    pending = "pending"
    running = "running"
    paused = "paused"
    succeeded = "succeeded"
    failed = "failed"


class FormSubmissionStatus(StrEnum):
    accepted = "accepted"
    processing = "processing"
    processed = "processed"
    failed = "failed"


class ContactPointKind(StrEnum):
    email = "email"
    phone = "phone"


class ConsentStatus(StrEnum):
    granted = "granted"
    revoked = "revoked"


class PurchaseScheduleStatus(StrEnum):
    active = "active"
    completed = "completed"
    cancelled = "cancelled"


class AmoConnectionStatus(StrEnum):
    connected = "connected"
    disconnected = "disconnected"


class AmoCRMConnection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One encrypted amoCRM OAuth connection per Pulse workspace."""

    __tablename__ = "amocrm_connections"
    __table_args__ = (
        sa.UniqueConstraint("workspace_id", name="uq_amocrm_connection_workspace"),
        sa.Index("ix_amocrm_connections_account_domain", "account_domain"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[AmoConnectionStatus] = mapped_column(
        sa.Enum(AmoConnectionStatus, native_enum=False),
        default=AmoConnectionStatus.connected,
        nullable=False,
    )
    account_domain: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    account_id: Mapped[str | None] = mapped_column(sa.String(64))
    client_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(sa.String(2048), nullable=False)
    encrypted_client_secret: Mapped[bytes | None] = mapped_column(sa.LargeBinary)
    encrypted_access_token: Mapped[bytes | None] = mapped_column(sa.LargeBinary)
    encrypted_refresh_token: Mapped[bytes | None] = mapped_column(sa.LargeBinary)
    credentials_key_id: Mapped[str | None] = mapped_column(sa.String(100))
    token_expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    connected_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    disconnected_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)


class AmoOAuthState(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Short-lived one-time OAuth state; the bearer value itself is never stored."""

    __tablename__ = "amocrm_oauth_states"
    __table_args__ = (
        sa.UniqueConstraint("state_digest", name="uq_amocrm_oauth_state_digest"),
        sa.Index(
            "ix_amocrm_oauth_states_workspace_expiry",
            "workspace_id",
            "expires_at",
        ),
        sa.Index("ix_amocrm_oauth_states_initiated_by_id", "initiated_by_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    initiated_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    state_digest: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    client_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(sa.String(2048), nullable=False)
    encrypted_client_secret: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    credentials_key_id: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    allowed_referers: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class ContactPoint(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "contact_points"
    __table_args__ = (
        sa.UniqueConstraint(
            "contact_id", "kind", "normalized_value", name="uq_contact_point_value"
        ),
        sa.Index(
            "ix_contact_points_workspace_kind_normalized",
            "workspace_id",
            "kind",
            "normalized_value",
        ),
        sa.Index("ix_contact_points_contact_id", "contact_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[ContactPointKind] = mapped_column(
        sa.Enum(ContactPointKind, native_enum=False), nullable=False
    )
    value: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    normalized_value: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    label: Mapped[str | None] = mapped_column(sa.String(100))
    is_primary: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    is_verified: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)


class ExternalIdentity(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "external_identities"
    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id",
            "provider",
            "connection_scope",
            "external_user_id",
            name="uq_external_identity_provider_user",
        ),
        sa.Index("ix_external_identities_contact_id", "contact_id"),
        sa.Index("ix_external_identities_channel_connection_id", "channel_connection_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    channel_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("channel_connections.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    # A non-null stable scope avoids PostgreSQL's "NULLs are distinct" behavior
    # in the identity uniqueness constraint.  Use "global" when a provider
    # identity is not tied to one connection.
    connection_scope: Mapped[str] = mapped_column(sa.String(64), default="global", nullable=False)
    external_user_id: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    display_name: Mapped[str | None] = mapped_column(sa.String(240))
    profile: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)


class ContactChannelConsent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "contact_channel_consents"
    __table_args__ = (
        sa.UniqueConstraint(
            "contact_id",
            "channel",
            "normalized_address",
            "purpose",
            name="uq_contact_consent_channel_address_purpose",
        ),
        sa.Index(
            "ix_contact_consents_workspace_status",
            "workspace_id",
            "status",
            "channel",
        ),
        sa.Index("ix_contact_consents_contact_id", "contact_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    address: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    normalized_address: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    purpose: Mapped[str] = mapped_column(sa.String(100), default="notifications", nullable=False)
    status: Mapped[ConsentStatus] = mapped_column(
        sa.Enum(ConsentStatus, native_enum=False), nullable=False
    )
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    source: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    granted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class ChannelConnection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "channel_connections"
    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id", "kind", "name", name="uq_channel_connection_workspace_kind_name"
        ),
        sa.Index(
            "ix_channel_connections_workspace_kind_status",
            "workspace_id",
            "kind",
            "status",
        ),
        sa.Index("ix_channel_connections_default_pipeline_id", "default_pipeline_id"),
        sa.Index("ix_channel_connections_default_stage_id", "default_stage_id"),
        sa.Index("ix_channel_connections_default_assignee_id", "default_assignee_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[ChannelKind] = mapped_column(
        sa.Enum(ChannelKind, native_enum=False), nullable=False
    )
    name: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    status: Mapped[ConnectionStatus] = mapped_column(
        sa.Enum(ConnectionStatus, native_enum=False),
        default=ConnectionStatus.disabled,
        nullable=False,
    )
    encrypted_credentials: Mapped[bytes | None] = mapped_column(sa.LargeBinary)
    credentials_key_id: Mapped[str | None] = mapped_column(sa.String(100))
    settings: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    default_pipeline_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("pipelines.id", ondelete="SET NULL")
    )
    default_stage_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("stages.id", ondelete="SET NULL")
    )
    default_assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    last_healthcheck_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        sa.UniqueConstraint(
            "channel_connection_id",
            "external_thread_id",
            name="uq_conversation_connection_external_thread",
        ),
        sa.Index(
            "ix_conversations_workspace_status_updated",
            "workspace_id",
            "status",
            "updated_at",
        ),
        sa.Index("ix_conversations_channel_connection_id", "channel_connection_id"),
        sa.Index("ix_conversations_contact_id", "contact_id"),
        sa.Index("ix_conversations_deal_id", "deal_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    channel_connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("channel_connections.id", ondelete="RESTRICT"), nullable=False
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("contacts.id", ondelete="SET NULL")
    )
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("deals.id", ondelete="SET NULL")
    )
    external_thread_id: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    status: Mapped[ConversationStatus] = mapped_column(
        sa.Enum(ConversationStatus, native_enum=False),
        default=ConversationStatus.active,
        nullable=False,
    )
    participant: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)


class Message(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        sa.UniqueConstraint(
            "conversation_id", "provider_message_id", name="uq_message_conversation_provider_id"
        ),
        sa.Index(
            "ix_messages_workspace_conversation_created",
            "workspace_id",
            "conversation_id",
            "created_at",
            "id",
        ),
        sa.Index("ix_messages_reply_to_id", "reply_to_id"),
        sa.Index(
            "ix_messages_outbound_pending",
            "created_at",
            postgresql_where=sa.text("direction = 'outbound' AND status = 'queued'"),
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    reply_to_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("messages.id", ondelete="SET NULL")
    )
    direction: Mapped[MessageDirection] = mapped_column(
        sa.Enum(MessageDirection, native_enum=False), nullable=False
    )
    status: Mapped[MessageStatus] = mapped_column(
        sa.Enum(MessageStatus, native_enum=False), nullable=False
    )
    provider_message_id: Mapped[str | None] = mapped_column(sa.String(512))
    sender_external_id: Mapped[str | None] = mapped_column(sa.String(512))
    body: Mapped[str] = mapped_column(sa.Text, default="", nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(sa.Text)


class Attachment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "attachments"
    __table_args__ = (
        sa.UniqueConstraint("object_key", name="uq_attachment_object_key"),
        sa.Index("ix_attachments_workspace_message", "workspace_id", "message_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(sa.String(1024), nullable=False)
    original_filename: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)


class InboundEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "inbound_events"
    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id",
            "source_key",
            "external_event_id",
            name="uq_inbound_event_external_id",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "source_key",
            "idempotency_key",
            name="uq_inbound_event_idempotency_key",
        ),
        sa.Index(
            "ix_inbound_events_workspace_status_received",
            "workspace_id",
            "status",
            "received_at",
        ),
        sa.Index("ix_inbound_events_channel_connection_id", "channel_connection_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    channel_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("channel_connections.id", ondelete="SET NULL")
    )
    source_key: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    external_event_id: Mapped[str | None] = mapped_column(sa.String(512))
    idempotency_key: Mapped[str | None] = mapped_column(sa.String(255))
    request_digest: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    status: Mapped[InboundStatus] = mapped_column(
        sa.Enum(InboundStatus, native_enum=False),
        default=InboundStatus.accepted,
        nullable=False,
    )
    received_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, server_default=sa.func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(sa.Text)


class NotificationTemplate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "notification_templates"
    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id", "name", "channel", name="uq_notification_template_name_channel"
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    channel: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    subject_template: Mapped[str | None] = mapped_column(sa.String(998))
    body_template: Mapped[str] = mapped_column(sa.Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)


class NotificationRule(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "notification_rules"
    __table_args__ = (
        sa.Index(
            "ix_notification_rules_workspace_event_enabled",
            "workspace_id",
            "event_type",
            "is_enabled",
        ),
        sa.Index("ix_notification_rules_template_id", "template_id"),
        sa.Index("ix_notification_rules_pipeline_id", "pipeline_id"),
        sa.Index("ix_notification_rules_stage_id", "stage_id"),
        sa.Index("ix_notification_rules_source_id", "source_id"),
        sa.CheckConstraint("delay_seconds >= 0", name="notification_delay_non_negative"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE,
        sa.ForeignKey("notification_templates.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    event_type: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    audience: Mapped[NotificationAudience] = mapped_column(
        sa.Enum(NotificationAudience, native_enum=False), nullable=False
    )
    channel: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    pipeline_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("pipelines.id", ondelete="CASCADE")
    )
    stage_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("stages.id", ondelete="CASCADE")
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("sources.id", ondelete="CASCADE")
    )
    filters: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    recipients: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON_TYPE, default=list, nullable=False
    )
    delay_seconds: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    require_client_consent: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)


class NotificationDelivery(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "dedupe_key", name="uq_notification_delivery_dedupe"),
        sa.Index(
            "ix_notification_deliveries_workspace_status_scheduled",
            "workspace_id",
            "status",
            "scheduled_at",
        ),
        sa.Index("ix_notification_deliveries_rule_id", "rule_id"),
        sa.Index("ix_notification_deliveries_template_id", "template_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("notification_rules.id", ondelete="SET NULL")
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("notification_templates.id", ondelete="SET NULL")
    )
    audience: Mapped[NotificationAudience] = mapped_column(
        sa.Enum(NotificationAudience, native_enum=False), nullable=False
    )
    channel: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(UUID_TYPE)
    recipient_address: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    subject: Mapped[str | None] = mapped_column(sa.String(998))
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[DeliveryStatus] = mapped_column(
        sa.Enum(DeliveryStatus, native_enum=False),
        default=DeliveryStatus.pending,
        nullable=False,
    )
    dedupe_key: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, server_default=sa.func.now(), nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(sa.String(512))
    last_error: Mapped[str | None] = mapped_column(sa.Text)


class WebPushSubscription(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Encrypted browser PushSubscription scoped to one employee and workspace."""

    __tablename__ = "web_push_subscriptions"
    __table_args__ = (
        sa.UniqueConstraint(
            "endpoint_hash",
            name="uq_web_push_subscription_endpoint_hash",
        ),
        sa.Index(
            "ix_web_push_subscriptions_workspace_user_active",
            "workspace_id",
            "user_id",
            "is_active",
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    endpoint_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    encrypted_subscription: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    encryption_key_id: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    expiration_time: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(sa.String(500))


class PurchaseSchedule(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "purchase_schedules"
    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id",
            "deal_id",
            "scheduled_for",
            name="uq_purchase_schedule_deal_occurrence",
        ),
        sa.UniqueConstraint("task_id", name="uq_purchase_schedule_task"),
        sa.Index(
            "ix_purchase_schedules_workspace_status_scheduled",
            "workspace_id",
            "status",
            "scheduled_for",
        ),
        sa.Index("ix_purchase_schedules_contact_id", "contact_id"),
        sa.Index("ix_purchase_schedules_assignee_id", "assignee_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    deal_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("deals.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("contacts.id", ondelete="SET NULL")
    )
    assignee_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("tasks.id", ondelete="SET NULL")
    )
    scheduled_for: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    remind_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    status: Mapped[PurchaseScheduleStatus] = mapped_column(
        sa.Enum(PurchaseScheduleStatus, native_enum=False),
        default=PurchaseScheduleStatus.active,
        nullable=False,
    )
    reminder_enqueued_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)


class ScheduledEventMarker(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "scheduled_event_markers"
    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id",
            "event_type",
            "entity_type",
            "entity_id",
            "occurrence_at",
            name="uq_scheduled_event_occurrence",
        ),
        sa.Index(
            "ix_scheduled_event_lookup",
            "workspace_id",
            "event_type",
            "occurrence_at",
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, nullable=False)
    occurrence_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class Form(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "forms"
    __table_args__ = (
        sa.UniqueConstraint("slug", name="uq_form_public_slug"),
        sa.Index("ix_forms_slug_active", "slug", "is_active"),
        sa.Index("ix_forms_pipeline_id", "pipeline_id"),
        sa.Index("ix_forms_stage_id", "stage_id"),
        sa.Index("ix_forms_assignee_id", "assignee_id"),
        sa.Index("ix_forms_source_id", "source_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(240), nullable=False)
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("pipelines.id", ondelete="RESTRICT"), nullable=False
    )
    stage_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("stages.id", ondelete="RESTRICT"), nullable=False
    )
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("sources.id", ondelete="SET NULL")
    )
    fields_schema: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON_TYPE, default=list, nullable=False
    )
    allowed_origins: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    honeypot_field: Mapped[str] = mapped_column(
        sa.String(100), default="company_website", nullable=False
    )
    success_message: Mapped[str] = mapped_column(
        sa.String(500), default="Спасибо! Мы свяжемся с вами.", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)


class FormSubmission(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "form_submissions"
    __table_args__ = (
        sa.UniqueConstraint("form_id", "idempotency_key", name="uq_form_submission_idempotency"),
        sa.Index(
            "ix_form_submissions_workspace_status_created",
            "workspace_id",
            "status",
            "created_at",
        ),
        sa.Index("ix_form_submissions_form_id", "form_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    form_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("forms.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    request_digest: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    status: Mapped[FormSubmissionStatus] = mapped_column(
        sa.Enum(FormSubmissionStatus, native_enum=False),
        default=FormSubmissionStatus.accepted,
        nullable=False,
    )
    processed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(sa.Text)


class FormRateLimitBucket(Base):
    __tablename__ = "form_rate_limit_buckets"
    __table_args__ = (
        sa.UniqueConstraint(
            "form_id",
            "subject_hash",
            "window_started_at",
            name="uq_form_rate_bucket_subject_window",
        ),
        sa.Index("ix_form_rate_bucket_expires", "expires_at"),
        sa.Index("ix_form_rate_bucket_workspace_id", "workspace_id"),
    )

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    form_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("forms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    request_count: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)


class WebhookEndpoint(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "webhook_endpoints"
    __table_args__ = (
        sa.UniqueConstraint("slug", name="uq_webhook_endpoint_public_slug"),
        sa.Index("ix_webhook_endpoints_slug_active", "slug", "is_active"),
        sa.Index("ix_webhook_endpoints_pipeline_id", "pipeline_id"),
        sa.Index("ix_webhook_endpoints_stage_id", "stage_id"),
        sa.Index("ix_webhook_endpoints_assignee_id", "assignee_id"),
        sa.Index("ix_webhook_endpoints_source_id", "source_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    encrypted_secret: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    secret_key_id: Mapped[str | None] = mapped_column(sa.String(100))
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("pipelines.id", ondelete="RESTRICT"), nullable=False
    )
    stage_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("stages.id", ondelete="RESTRICT"), nullable=False
    )
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("sources.id", ondelete="SET NULL")
    )
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)


class ImportJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "import_jobs"
    __table_args__ = (
        sa.Index(
            "ix_import_jobs_workspace_status_updated",
            "workspace_id",
            "status",
            "updated_at",
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(sa.String(64), default="amocrm", nullable=False)
    status: Mapped[ImportStatus] = mapped_column(
        sa.Enum(ImportStatus, native_enum=False), default=ImportStatus.pending, nullable=False
    )
    dry_run: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(sa.String(64))
    cursor: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    user_mapping: Mapped[dict[str, str]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    counts: Mapped[dict[str, int]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    report_object_key: Mapped[str | None] = mapped_column(sa.String(1024))
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)


class ExternalEntityMap(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "external_entity_maps"
    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id",
            "provider",
            "entity_type",
            "external_id",
            name="uq_external_entity_provider_id",
        ),
        sa.Index("ix_external_entity_maps_import_job_id", "import_job_id"),
        sa.Index(
            "ix_external_entity_maps_internal",
            "workspace_id",
            "entity_type",
            "internal_id",
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    import_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE, sa.ForeignKey("import_jobs.id", ondelete="SET NULL")
    )
    provider: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    internal_id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, nullable=False)
    source_updated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(sa.String(64), nullable=False)

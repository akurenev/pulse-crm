"""Single-process production runtime for integration jobs and scheduling.

The runtime deliberately uses PostgreSQL as its only coordination service:

* :class:`RuntimeScheduler` acquires a transaction-scoped advisory try-lock,
  discovers durable work and enqueues idempotent ``BackgroundJob`` rows;
* :class:`RuntimeHandlers` exposes the complete handler registry consumed by
  the core :class:`~app.services.jobs.JobSupervisor`;
* external network calls are made outside database transactions, while state
  transitions and queue operations stay short and transactional.

Inbound and form handlers use the conservative domain router from
``app.integrations.inbound`` by default. Deployments may override those
callbacks; an override must be DB-only, must not commit, and must be
idempotent inside the transaction supplied by the runtime.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.integrations.channels.base import (
    ChannelAdapter,
    NormalizedInboundMessage,
    OutboundAttachment,
    OutboundMessage,
    SendResult,
)
from app.integrations.channels.email import EmailAdapter
from app.integrations.inbound import (
    process_form_submission,
    process_inbound_event,
    process_normalized_channel_message,
)
from app.integrations.models import (
    AmoOAuthState,
    Attachment,
    ChannelConnection,
    ChannelKind,
    ConnectionStatus,
    Conversation,
    FormRateLimitBucket,
    FormSubmission,
    FormSubmissionStatus,
    InboundEvent,
    InboundStatus,
    Message,
    MessageDirection,
    MessageStatus,
    NotificationDelivery,
    NotificationRule,
    NotificationTemplate,
    PurchaseSchedule,
    PurchaseScheduleStatus,
    ScheduledEventMarker,
)
from app.integrations.notifications import (
    NotificationConsentError,
    NotificationTemplateError,
    queue_notification,
)
from app.integrations.purchases import ensure_purchase_task
from app.integrations.s3 import AttachmentStorage, AttachmentValidationError
from app.integrations.transports import AsyncIMAPPoller
from app.integrations.webhooks import VerifiedWebhook, accept_inbound_event
from app.models import (
    BackgroundJob,
    Deal,
    DeliveryStatus,
    JobStatus,
    OutboxEvent,
    RealtimeEvent,
    Session,
    Stage,
    StageType,
    Task,
    TaskStatus,
)
from app.services.events import record_domain_event
from app.services.jobs import (
    ClaimedJob,
    JobHandler,
    JobSupervisor,
    SessionFactory,
    enqueue_job,
    recover_expired_leases,
)

logger = logging.getLogger(__name__)

JOB_OUTBOX_DISPATCH = "outbox.dispatch"
JOB_MESSAGE_SEND = "message.send"
JOB_INBOUND_PROCESS = "inbound.process"
JOB_FORM_PROCESS = "form.process"
JOB_NOTIFICATION_EXPAND = "notification.expand"
JOB_NOTIFICATION_DELIVER = "notification.deliver"
JOB_PURCHASE_SCHEDULE = "purchase.schedule"
JOB_RUNTIME_CLEANUP = "runtime.cleanup"
JOB_RUNTIME_RECOVER = "runtime.recover"
JOB_IMAP_POLL = "email.imap.poll"
JOB_TASK_DUE_EVENT = "monitor.task.due"
JOB_TASK_OVERDUE_EVENT = "monitor.task.overdue"
JOB_DEAL_INACTIVE_EVENT = "monitor.deal.inactive"

RUNTIME_JOB_TYPES = frozenset(
    {
        JOB_OUTBOX_DISPATCH,
        JOB_MESSAGE_SEND,
        JOB_INBOUND_PROCESS,
        JOB_FORM_PROCESS,
        JOB_NOTIFICATION_EXPAND,
        JOB_NOTIFICATION_DELIVER,
        JOB_PURCHASE_SCHEDULE,
        JOB_RUNTIME_CLEANUP,
        JOB_RUNTIME_RECOVER,
        JOB_IMAP_POLL,
        JOB_TASK_DUE_EVENT,
        JOB_TASK_OVERDUE_EVENT,
        JOB_DEAL_INACTIVE_EVENT,
    }
)

# Stable signed 64-bit key spelling "PULSECRM" in ASCII. Transaction-level
# try-locks are released automatically on commit, rollback or disconnect.
SCHEDULER_ADVISORY_LOCK_KEY = 0x50554C534543524D
MAX_ERROR_LENGTH = 4_000

type ConnectionAdapterFactory = Callable[
    [ChannelConnection], ChannelAdapter | Awaitable[ChannelAdapter]
]
type NotificationAdapterFactory = Callable[
    [uuid.UUID, str], ChannelAdapter | Awaitable[ChannelAdapter]
]
type IMAPPollerFactory = Callable[[ChannelConnection], AsyncIMAPPoller | Awaitable[AsyncIMAPPoller]]
type InboundProcessor = Callable[[AsyncSession, InboundEvent], Awaitable[None]]
type FormProcessor = Callable[[AsyncSession, FormSubmission], Awaitable[None]]
type NotificationExpander = Callable[[AsyncSession, OutboxEvent], Awaitable[None]]


class RuntimeWiringError(RuntimeError):
    """A job reached a runtime whose required integration callback is absent."""


class Clock(Protocol):
    def __call__(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class CleanupResult:
    realtime_events: int = 0
    sessions: int = 0
    rate_limit_buckets: int = 0
    outbox_events: int = 0
    background_jobs: int = 0
    oauth_states: int = 0

    @property
    def total(self) -> int:
        return sum(
            (
                self.realtime_events,
                self.sessions,
                self.rate_limit_buckets,
                self.outbox_events,
                self.background_jobs,
            )
        )


@dataclass(frozen=True, slots=True)
class SchedulerTickResult:
    lock_acquired: bool
    scheduled: Mapping[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.scheduled.values())


@dataclass(frozen=True, slots=True)
class RuntimeHealth:
    healthy: bool
    scheduler_running: bool
    supervisor_running: bool
    supervisor_active_jobs: int
    last_scheduler_tick_at: datetime | None
    scheduler_last_error: str | None


async def _resolve[T](value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _payload_uuid(job: ClaimedJob, key: str) -> uuid.UUID:
    raw = job.payload.get(key)
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"job payload has invalid {key}") from exc


def _optional_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _payload_datetime(job: ClaimedJob, key: str) -> datetime:
    raw = job.payload.get(key)
    try:
        value = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"job payload has invalid {key}") from exc
    return _as_utc(value)


async def _enqueue_runtime_job(
    session: AsyncSession,
    job_type: str,
    payload: Mapping[str, Any],
    *,
    dedupe_key: str,
    run_at: datetime | None = None,
    max_attempts: int = 5,
) -> BackgroundJob:
    """Use the core enqueue primitive, with a serial SQLite test fallback.

    Production is always PostgreSQL and therefore uses the atomic
    ``INSERT .. ON CONFLICT`` implementation from ``enqueue_job``. SQLite is
    supported only so the repository's isolated unit tests can exercise the
    runtime without emulating PostgreSQL syntax.
    """

    if session.get_bind().dialect.name == "postgresql":
        return await enqueue_job(
            session,
            job_type,
            payload,
            run_at=run_at,
            max_attempts=max_attempts,
            dedupe_key=dedupe_key,
        )

    existing = await session.scalar(
        sa.select(BackgroundJob).where(BackgroundJob.dedupe_key == dedupe_key)
    )
    if existing is not None:
        return existing
    job = await enqueue_job(
        session,
        job_type,
        payload,
        run_at=run_at,
        max_attempts=max_attempts,
    )
    job.dedupe_key = dedupe_key
    await session.flush()
    return job


class RuntimeHandlers:
    """Concrete integration handlers registered with ``JobSupervisor``."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory = SessionLocal,
        adapter_factory: ConnectionAdapterFactory | None = None,
        notification_adapter_factory: NotificationAdapterFactory | None = None,
        imap_poller_factory: IMAPPollerFactory | None = None,
        attachment_storage: AttachmentStorage | None = None,
        inbound_processor: InboundProcessor | None = None,
        form_processor: FormProcessor | None = None,
        notification_expander: NotificationExpander | None = None,
        now: Clock = _utcnow,
        cleanup_batch_size: int = 1_000,
    ) -> None:
        if not 1 <= cleanup_batch_size <= 10_000:
            raise ValueError("cleanup_batch_size must be between 1 and 10000")
        self.session_factory = session_factory
        self.adapter_factory = adapter_factory
        self.notification_adapter_factory = notification_adapter_factory
        self.imap_poller_factory = imap_poller_factory
        self.attachment_storage = attachment_storage
        self.inbound_processor: InboundProcessor = (
            inbound_processor or self._default_inbound_processor
        )
        self.form_processor: FormProcessor = form_processor or process_form_submission
        self.notification_expander = notification_expander
        self.now = now
        self.cleanup_batch_size = cleanup_batch_size

    async def _default_inbound_processor(self, session: AsyncSession, event: InboundEvent) -> None:
        await process_inbound_event(
            session,
            event,
            adapter_factory=self.adapter_factory,
        )

    def registry(self) -> dict[str, JobHandler]:
        return {
            JOB_OUTBOX_DISPATCH: self.dispatch_outbox,
            JOB_MESSAGE_SEND: self.send_message,
            JOB_INBOUND_PROCESS: self.process_inbound,
            JOB_FORM_PROCESS: self.process_form,
            JOB_NOTIFICATION_EXPAND: self.expand_notification,
            JOB_NOTIFICATION_DELIVER: self.deliver_notification,
            JOB_PURCHASE_SCHEDULE: self.schedule_purchase,
            JOB_RUNTIME_CLEANUP: self.cleanup,
            JOB_RUNTIME_RECOVER: self.recover,
            JOB_IMAP_POLL: self.poll_imap,
            JOB_TASK_DUE_EVENT: self.emit_task_due,
            JOB_TASK_OVERDUE_EVENT: self.emit_task_overdue,
            JOB_DEAL_INACTIVE_EVENT: self.emit_deal_inactive,
        }

    async def dispatch_outbox(self, job: ClaimedJob) -> None:
        event_id = _payload_uuid(job, "outbox_event_id")
        async with self.session_factory() as session:
            async with session.begin():
                event = await session.scalar(
                    sa.select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
                )
                if event is None:
                    raise LookupError("outbox event not found")
                if event.processed_at is not None:
                    return
                child_type, child_payload, child_key = self._route_outbox(event)
                await _enqueue_runtime_job(
                    session,
                    child_type,
                    child_payload,
                    dedupe_key=child_key,
                    # The scheduler only dispatches events whose available_at
                    # is due. Using its clock also avoids driver-specific
                    # aware/naive datetime comparisons in application code.
                    run_at=self.now(),
                )
                event.attempts += 1
                event.processed_at = self.now()
                event.last_error = None

    @staticmethod
    def _route_outbox(event: OutboxEvent) -> tuple[str, dict[str, str], str]:
        aggregate_id = str(event.aggregate_id)
        if event.event_type == "message.outbound.queued":
            return JOB_MESSAGE_SEND, {"message_id": aggregate_id}, f"message:{aggregate_id}:send"
        if event.event_type == "inbound.event.accepted":
            return (
                JOB_INBOUND_PROCESS,
                {"inbound_event_id": aggregate_id},
                f"inbound:{aggregate_id}:process",
            )
        if event.event_type == "form.submission.accepted":
            return (
                JOB_FORM_PROCESS,
                {"form_submission_id": aggregate_id},
                f"form-submission:{aggregate_id}:process",
            )
        if event.event_type == "notification.delivery.queued":
            return (
                JOB_NOTIFICATION_DELIVER,
                {"notification_delivery_id": aggregate_id},
                f"notification-delivery:{aggregate_id}:send",
            )
        return (
            JOB_NOTIFICATION_EXPAND,
            {"outbox_event_id": str(event.id)},
            f"outbox:{event.id}:notifications",
        )

    async def send_message(self, job: ClaimedJob) -> None:
        message_id = _payload_uuid(job, "message_id")
        try:
            context = await self._message_context(message_id)
            if context is None:
                return
            message, conversation, connection, attachment_records, reply_to_provider_id = context
            if self.adapter_factory is None:
                raise RuntimeWiringError("connection adapter factory is not configured")
            attachments = await self._materialize_outbound_attachments(
                workspace_id=message.workspace_id,
                records=attachment_records,
            )
            adapter = await _resolve(self.adapter_factory(connection))
            result = await adapter.send_message(
                OutboundMessage(
                    thread_id=conversation.external_thread_id,
                    recipient_id=str(conversation.participant.get("recipient_id") or "") or None,
                    text=message.body,
                    reply_to_message_id=reply_to_provider_id,
                    attachments=attachments,
                )
            )
            await self._mark_message_sent(message.id, result)
        except Exception as exc:
            await self._mark_message_failed_if_terminal(message_id, job, exc)
            raise

    async def _message_context(
        self, message_id: uuid.UUID
    ) -> (
        tuple[
            Message,
            Conversation,
            ChannelConnection,
            tuple[Attachment, ...],
            str | None,
        ]
        | None
    ):
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    sa.select(Message, Conversation, ChannelConnection)
                    .join(Conversation, Conversation.id == Message.conversation_id)
                    .join(
                        ChannelConnection,
                        ChannelConnection.id == Conversation.channel_connection_id,
                    )
                    .where(Message.id == message_id)
                )
            ).one_or_none()
            if row is None:
                raise LookupError("outbound message not found")
            message, conversation, connection = row
            if message.status in {MessageStatus.sent, MessageStatus.failed}:
                return None
            if (
                message.direction is not MessageDirection.outbound
                or message.status is not MessageStatus.queued
            ):
                raise ValueError("message is not a queued outbound message")
            if connection.status is not ConnectionStatus.active:
                raise RuntimeError("message channel connection is not active")
            attachment_records = tuple(
                (
                    await session.scalars(
                        sa.select(Attachment)
                        .where(
                            Attachment.message_id == message.id,
                            Attachment.workspace_id == message.workspace_id,
                        )
                        .order_by(Attachment.created_at, Attachment.id)
                    )
                ).all()
            )
            reply_to_provider_id = None
            if message.reply_to_id:
                reply_to_provider_id = await session.scalar(
                    sa.select(Message.provider_message_id).where(
                        Message.id == message.reply_to_id,
                        Message.conversation_id == conversation.id,
                    )
                )
            return message, conversation, connection, attachment_records, reply_to_provider_id

    async def _materialize_outbound_attachments(
        self,
        *,
        workspace_id: uuid.UUID,
        records: tuple[Attachment, ...],
    ) -> tuple[OutboundAttachment, ...]:
        if not records:
            return ()
        if self.attachment_storage is None:
            raise RuntimeWiringError("attachment storage is not configured")
        result: list[OutboundAttachment] = []
        for record in records:
            content = await self.attachment_storage.read_attachment(
                workspace_id=workspace_id,
                object_key=record.object_key,
                filename=record.original_filename,
                content_type=record.content_type,
                expected_size_bytes=record.size_bytes,
                expected_sha256=record.sha256,
            )
            result.append(
                OutboundAttachment(
                    filename=record.original_filename,
                    content_type=record.content_type,
                    content=content,
                )
            )
        return tuple(result)

    async def _mark_message_sent(self, message_id: uuid.UUID, result: SendResult) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                message = await session.scalar(
                    sa.select(Message).where(Message.id == message_id).with_for_update()
                )
                if message is None or message.status is MessageStatus.sent:
                    return
                if message.status is not MessageStatus.queued:
                    raise RuntimeError("outbound message left queued state before acknowledgement")
                message.status = MessageStatus.sent
                message.provider_message_id = result.provider_message_id
                message.sent_at = result.sent_at
                message.failed_at = None
                message.last_error = None
                record_domain_event(
                    session,
                    workspace_id=message.workspace_id,
                    event_type="message.outbound.sent",
                    entity_type="message",
                    entity_id=message.id,
                    actor_id=None,
                    payload={"provider_message_id": result.provider_message_id},
                )

    async def _mark_message_failed_if_terminal(
        self, message_id: uuid.UUID, job: ClaimedJob, error: BaseException
    ) -> None:
        if job.attempts < job.max_attempts:
            return
        async with self.session_factory() as session:
            async with session.begin():
                message = await session.scalar(
                    sa.select(Message).where(Message.id == message_id).with_for_update()
                )
                if message is None or message.status is not MessageStatus.queued:
                    return
                message.status = MessageStatus.failed
                message.failed_at = self.now()
                message.last_error = str(error)[:MAX_ERROR_LENGTH]

    async def process_inbound(self, job: ClaimedJob) -> None:
        event_id = _payload_uuid(job, "inbound_event_id")
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    event = await session.scalar(
                        sa.select(InboundEvent).where(InboundEvent.id == event_id).with_for_update()
                    )
                    if event is None:
                        raise LookupError("inbound event not found")
                    if event.status is InboundStatus.processed:
                        return
                    if event.status is InboundStatus.failed:
                        return
                    event.status = InboundStatus.processing
                    await self.inbound_processor(session, event)
                    event.status = InboundStatus.processed
                    event.processed_at = self.now()
                    event.last_error = None
        except Exception as exc:
            await self._mark_inbound_failed_if_terminal(event_id, job, exc)
            raise

    async def _mark_inbound_failed_if_terminal(
        self, event_id: uuid.UUID, job: ClaimedJob, error: BaseException
    ) -> None:
        if job.attempts < job.max_attempts:
            return
        async with self.session_factory() as session:
            async with session.begin():
                event = await session.scalar(
                    sa.select(InboundEvent).where(InboundEvent.id == event_id).with_for_update()
                )
                if event is None or event.status is InboundStatus.processed:
                    return
                event.status = InboundStatus.failed
                event.last_error = str(error)[:MAX_ERROR_LENGTH]

    async def process_form(self, job: ClaimedJob) -> None:
        submission_id = _payload_uuid(job, "form_submission_id")
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    submission = await session.scalar(
                        sa.select(FormSubmission)
                        .where(FormSubmission.id == submission_id)
                        .with_for_update()
                    )
                    if submission is None:
                        raise LookupError("form submission not found")
                    if submission.status is FormSubmissionStatus.processed:
                        return
                    if submission.status is FormSubmissionStatus.failed:
                        return
                    submission.status = FormSubmissionStatus.processing
                    await self.form_processor(session, submission)
                    submission.status = FormSubmissionStatus.processed
                    submission.processed_at = self.now()
                    submission.last_error = None
        except Exception as exc:
            await self._mark_form_failed_if_terminal(submission_id, job, exc)
            raise

    async def _mark_form_failed_if_terminal(
        self, submission_id: uuid.UUID, job: ClaimedJob, error: BaseException
    ) -> None:
        if job.attempts < job.max_attempts:
            return
        async with self.session_factory() as session:
            async with session.begin():
                submission = await session.scalar(
                    sa.select(FormSubmission)
                    .where(FormSubmission.id == submission_id)
                    .with_for_update()
                )
                if submission is None or submission.status is FormSubmissionStatus.processed:
                    return
                submission.status = FormSubmissionStatus.failed
                submission.last_error = str(error)[:MAX_ERROR_LENGTH]

    async def expand_notification(self, job: ClaimedJob) -> None:
        event_id = _payload_uuid(job, "outbox_event_id")
        async with self.session_factory() as session:
            async with session.begin():
                event = await session.get(OutboxEvent, event_id)
                if event is None:
                    raise LookupError("notification source outbox event not found")
                if self.notification_expander is not None:
                    await self.notification_expander(session, event)
                else:
                    await self._expand_static_notification_rules(session, event)

    async def _expand_static_notification_rules(
        self, session: AsyncSession, event: OutboxEvent
    ) -> None:
        rows = (
            await session.execute(
                sa.select(NotificationRule, NotificationTemplate)
                .join(NotificationTemplate, NotificationTemplate.id == NotificationRule.template_id)
                .where(
                    NotificationRule.workspace_id == event.workspace_id,
                    NotificationRule.event_type == event.event_type,
                    NotificationRule.is_enabled.is_(True),
                    NotificationTemplate.is_active.is_(True),
                )
            )
        ).all()
        for rule, template in rows:
            if not self._rule_matches(rule, event.payload):
                continue
            variables = {
                "event_type": event.event_type,
                "entity_id": str(event.aggregate_id),
                **event.payload,
            }
            for index, recipient in enumerate(rule.recipients):
                if not isinstance(recipient, dict):
                    continue
                address = str(
                    recipient.get("address") or recipient.get("recipient_address") or ""
                ).strip()
                if not address:
                    logger.warning(
                        "notification rule recipient has no address",
                        extra={"rule_id": str(rule.id), "recipient_index": index},
                    )
                    continue
                try:
                    await queue_notification(
                        session,
                        workspace_id=event.workspace_id,
                        template=template,
                        rule=rule,
                        audience=rule.audience,
                        channel=rule.channel,
                        recipient_address=address,
                        recipient_id=_optional_uuid(recipient.get("recipient_id")),
                        contact_id=_optional_uuid(recipient.get("contact_id")),
                        normalized_address=(
                            str(recipient["normalized_address"])
                            if recipient.get("normalized_address")
                            else None
                        ),
                        dedupe_key=f"rule:{rule.id}:event:{event.id}:recipient:{index}",
                        variables=variables,
                        scheduled_at=self.now() + timedelta(seconds=rule.delay_seconds),
                    )
                except (NotificationConsentError, NotificationTemplateError, ValueError) as exc:
                    logger.warning(
                        "notification recipient skipped",
                        extra={
                            "rule_id": str(rule.id),
                            "recipient_index": index,
                            "reason": str(exc),
                        },
                    )

    @staticmethod
    def _rule_matches(rule: NotificationRule, payload: Mapping[str, Any]) -> bool:
        dimensions = {
            "pipeline_id": rule.pipeline_id,
            "stage_id": rule.stage_id,
            "source_id": rule.source_id,
        }
        for key, expected in dimensions.items():
            if expected is not None and str(payload.get(key) or "") != str(expected):
                return False
        return all(payload.get(key) == value for key, value in rule.filters.items())

    async def deliver_notification(self, job: ClaimedJob) -> None:
        delivery_id = _payload_uuid(job, "notification_delivery_id")
        try:
            delivery = await self._pending_delivery(delivery_id)
            if delivery is None:
                return
            if _as_utc(delivery.scheduled_at) > _as_utc(self.now()):
                raise RuntimeError("notification delivery was claimed before scheduled_at")
            if delivery.channel == "in_app":
                result = SendResult(provider_message_id=f"in-app:{delivery.id}", sent_at=self.now())
            else:
                if self.notification_adapter_factory is None:
                    raise RuntimeWiringError("notification adapter factory is not configured")
                adapter = await _resolve(
                    self.notification_adapter_factory(delivery.workspace_id, delivery.channel)
                )
                text = (
                    f"{delivery.subject}\n\n{delivery.body}" if delivery.subject else delivery.body
                )
                result = await adapter.send_message(
                    OutboundMessage(
                        thread_id=delivery.recipient_address,
                        recipient_id=delivery.recipient_address,
                        text=text,
                    )
                )
            await self._mark_delivery_sent(delivery.id, result, in_app=delivery.channel == "in_app")
        except Exception as exc:
            await self._mark_delivery_failed_if_terminal(delivery_id, job, exc)
            raise

    async def _pending_delivery(self, delivery_id: uuid.UUID) -> NotificationDelivery | None:
        async with self.session_factory() as session:
            delivery = await session.get(NotificationDelivery, delivery_id)
            if delivery is None:
                raise LookupError("notification delivery not found")
            if delivery.status in {DeliveryStatus.delivered, DeliveryStatus.failed}:
                return None
            if delivery.status not in {DeliveryStatus.pending, DeliveryStatus.processing}:
                raise ValueError("notification delivery is not sendable")
            return delivery

    async def _mark_delivery_sent(
        self, delivery_id: uuid.UUID, result: SendResult, *, in_app: bool
    ) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                delivery = await session.scalar(
                    sa.select(NotificationDelivery)
                    .where(NotificationDelivery.id == delivery_id)
                    .with_for_update()
                )
                if delivery is None or delivery.status is DeliveryStatus.delivered:
                    return
                delivery.status = DeliveryStatus.delivered
                delivery.provider_message_id = result.provider_message_id
                delivery.delivered_at = result.sent_at
                delivery.attempts += 1
                delivery.last_error = None
                if in_app:
                    session.add(
                        RealtimeEvent(
                            workspace_id=delivery.workspace_id,
                            event_type="notification.delivered",
                            payload={
                                "delivery_id": str(delivery.id),
                                "recipient_id": (
                                    str(delivery.recipient_id) if delivery.recipient_id else None
                                ),
                                "subject": delivery.subject,
                                "body": delivery.body,
                            },
                        )
                    )

    async def _mark_delivery_failed_if_terminal(
        self, delivery_id: uuid.UUID, job: ClaimedJob, error: BaseException
    ) -> None:
        if job.attempts < job.max_attempts:
            return
        async with self.session_factory() as session:
            async with session.begin():
                delivery = await session.scalar(
                    sa.select(NotificationDelivery)
                    .where(NotificationDelivery.id == delivery_id)
                    .with_for_update()
                )
                if delivery is None or delivery.status is DeliveryStatus.delivered:
                    return
                delivery.status = DeliveryStatus.failed
                delivery.attempts = max(delivery.attempts, job.attempts)
                delivery.last_error = str(error)[:MAX_ERROR_LENGTH]

    async def schedule_purchase(self, job: ClaimedJob) -> None:
        schedule_id = _payload_uuid(job, "purchase_schedule_id")
        workspace_id = _payload_uuid(job, "workspace_id")
        async with self.session_factory() as session:
            async with session.begin():
                schedule = await session.get(PurchaseSchedule, schedule_id)
                if schedule is None or schedule.workspace_id != workspace_id:
                    raise LookupError("purchase schedule not found")
                if schedule.status is not PurchaseScheduleStatus.active:
                    return
                await ensure_purchase_task(
                    session,
                    workspace_id=workspace_id,
                    schedule_id=schedule_id,
                )

    async def cleanup(self, job: ClaimedJob) -> None:
        del job
        now = self.now()
        async with self.session_factory() as session:
            async with session.begin():
                result = CleanupResult(
                    realtime_events=await self._delete_batch(
                        session,
                        RealtimeEvent,
                        RealtimeEvent.created_at < now - timedelta(days=30),
                    ),
                    sessions=await self._delete_batch(
                        session,
                        Session,
                        Session.expires_at < now - timedelta(days=1),
                    ),
                    rate_limit_buckets=await self._delete_batch(
                        session,
                        FormRateLimitBucket,
                        FormRateLimitBucket.expires_at < now,
                    ),
                    outbox_events=await self._delete_batch(
                        session,
                        OutboxEvent,
                        sa.and_(
                            OutboxEvent.processed_at.is_not(None),
                            OutboxEvent.processed_at < now - timedelta(days=7),
                        ),
                    ),
                    background_jobs=await self._delete_batch(
                        session,
                        BackgroundJob,
                        sa.and_(
                            BackgroundJob.status.in_([JobStatus.succeeded, JobStatus.failed]),
                            BackgroundJob.updated_at < now - timedelta(days=14),
                        ),
                    ),
                    oauth_states=await self._delete_batch(
                        session,
                        AmoOAuthState,
                        sa.or_(
                            AmoOAuthState.expires_at < now,
                            sa.and_(
                                AmoOAuthState.consumed_at.is_not(None),
                                AmoOAuthState.consumed_at < now - timedelta(hours=1),
                            ),
                        ),
                    ),
                )
        if result.total:
            logger.info("runtime cleanup completed", extra={"deleted_rows": result.total})

    async def _delete_batch(
        self,
        session: AsyncSession,
        model: type[Any],
        predicate: Any,
    ) -> int:
        ids = sa.select(model.id).where(predicate).limit(self.cleanup_batch_size)
        result = await session.execute(
            sa.delete(model).where(model.id.in_(ids)).execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def recover(self, job: ClaimedJob) -> None:
        del job
        async with self.session_factory() as session:
            async with session.begin():
                await recover_expired_leases(session, now=self.now())

    async def emit_task_due(self, job: ClaimedJob) -> None:
        await self._emit_task_event(job, event_type="task.due_soon", overdue=False)

    async def emit_task_overdue(self, job: ClaimedJob) -> None:
        await self._emit_task_event(job, event_type="task.overdue", overdue=True)

    async def _emit_task_event(
        self,
        job: ClaimedJob,
        *,
        event_type: str,
        overdue: bool,
    ) -> None:
        task_id = _payload_uuid(job, "task_id")
        workspace_id = _payload_uuid(job, "workspace_id")
        occurrence = _payload_datetime(job, "occurrence_at")
        async with self.session_factory() as session:
            async with session.begin():
                task = await session.scalar(
                    sa.select(Task)
                    .where(Task.id == task_id, Task.workspace_id == workspace_id)
                    .with_for_update()
                )
                if task is None or task.status is not TaskStatus.open:
                    return
                current_occurrence = task.due_at if overdue else task.remind_at
                if current_occurrence is None or _as_utc(current_occurrence) != occurrence:
                    return
                now = _as_utc(self.now())
                if overdue and _as_utc(task.due_at) > now:
                    return
                if not overdue and _as_utc(current_occurrence) > now:
                    return
                if await self._event_marked(
                    session,
                    workspace_id=workspace_id,
                    event_type=event_type,
                    entity_type="task",
                    entity_id=task.id,
                    occurrence_at=current_occurrence,
                ):
                    return
                self._add_event_marker(
                    session,
                    workspace_id=workspace_id,
                    event_type=event_type,
                    entity_type="task",
                    entity_id=task.id,
                    occurrence_at=current_occurrence,
                )
                record_domain_event(
                    session,
                    workspace_id=workspace_id,
                    event_type=event_type,
                    entity_type="task",
                    entity_id=task.id,
                    actor_id=None,
                    payload={
                        "task_id": str(task.id),
                        "deal_id": str(task.deal_id) if task.deal_id else None,
                        "assignee_id": str(task.assignee_id),
                        "due_at": task.due_at.isoformat(),
                    },
                )

    async def emit_deal_inactive(self, job: ClaimedJob) -> None:
        deal_id = _payload_uuid(job, "deal_id")
        workspace_id = _payload_uuid(job, "workspace_id")
        occurrence = _payload_datetime(job, "occurrence_at")
        async with self.session_factory() as session:
            async with session.begin():
                deal = await session.scalar(
                    sa.select(Deal)
                    .where(
                        Deal.id == deal_id,
                        Deal.workspace_id == workspace_id,
                        Deal.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
                if deal is None or _as_utc(deal.last_activity_at) != occurrence:
                    return
                stage_type = await session.scalar(
                    sa.select(Stage.stage_type).where(
                        Stage.id == deal.stage_id,
                        Stage.workspace_id == workspace_id,
                    )
                )
                if stage_type is not StageType.open:
                    return
                if _as_utc(deal.last_activity_at) > _as_utc(self.now()) - timedelta(days=7):
                    return
                if await self._event_marked(
                    session,
                    workspace_id=workspace_id,
                    event_type="deal.inactive",
                    entity_type="deal",
                    entity_id=deal.id,
                    occurrence_at=deal.last_activity_at,
                ):
                    return
                self._add_event_marker(
                    session,
                    workspace_id=workspace_id,
                    event_type="deal.inactive",
                    entity_type="deal",
                    entity_id=deal.id,
                    occurrence_at=deal.last_activity_at,
                )
                record_domain_event(
                    session,
                    workspace_id=workspace_id,
                    event_type="deal.inactive",
                    entity_type="deal",
                    entity_id=deal.id,
                    actor_id=None,
                    payload={
                        "deal_id": str(deal.id),
                        "pipeline_id": str(deal.pipeline_id),
                        "stage_id": str(deal.stage_id),
                        "source_id": str(deal.source_id) if deal.source_id else None,
                        "assignee_id": str(deal.assignee_id) if deal.assignee_id else None,
                        "last_activity_at": deal.last_activity_at.isoformat(),
                    },
                )

    @staticmethod
    async def _event_marked(
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        event_type: str,
        entity_type: str,
        entity_id: uuid.UUID,
        occurrence_at: datetime,
    ) -> bool:
        marker_id = await session.scalar(
            sa.select(ScheduledEventMarker.id).where(
                ScheduledEventMarker.workspace_id == workspace_id,
                ScheduledEventMarker.event_type == event_type,
                ScheduledEventMarker.entity_type == entity_type,
                ScheduledEventMarker.entity_id == entity_id,
                ScheduledEventMarker.occurrence_at == occurrence_at,
            )
        )
        return marker_id is not None

    @staticmethod
    def _add_event_marker(
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        event_type: str,
        entity_type: str,
        entity_id: uuid.UUID,
        occurrence_at: datetime,
    ) -> None:
        session.add(
            ScheduledEventMarker(
                workspace_id=workspace_id,
                event_type=event_type,
                entity_type=entity_type,
                entity_id=entity_id,
                occurrence_at=occurrence_at,
            )
        )

    async def poll_imap(self, job: ClaimedJob) -> None:
        """Fetch one bounded UID page and route each message idempotently."""

        connection_id = _payload_uuid(job, "channel_connection_id")
        try:
            async with self.session_factory() as session:
                connection = await session.scalar(
                    sa.select(ChannelConnection).where(
                        ChannelConnection.id == connection_id,
                        ChannelConnection.kind == ChannelKind.email,
                        ChannelConnection.status == ConnectionStatus.active,
                    )
                )
            if connection is None:
                return
            if self.adapter_factory is None or self.imap_poller_factory is None:
                raise RuntimeWiringError("email adapter and IMAP poller factories are required")
            adapter = await _resolve(self.adapter_factory(connection))
            if not isinstance(adapter, EmailAdapter):
                raise RuntimeWiringError("email connection did not produce an EmailAdapter")
            poller = await _resolve(self.imap_poller_factory(connection))
            raw_state = connection.settings.get("imap_state", {})
            state = raw_state if isinstance(raw_state, dict) else {}
            previous_validity = _optional_int(state.get("uidvalidity"))
            previous_uid = _optional_int(state.get("last_uid"))
            batch = await poller.poll(
                after_uid=previous_uid if previous_validity is not None else None,
                limit=50,
            )
            if previous_validity is not None and previous_validity != batch.uidvalidity:
                batch = await poller.poll(after_uid=None, limit=50)

            for envelope in batch.envelopes:
                normalized = adapter.normalize_envelope(envelope)
                message_id = await self._persist_email_envelope(
                    connection=connection,
                    normalized=normalized,
                    request_digest=hashlib.sha256(envelope.raw_message).hexdigest(),
                )
                await self._store_email_attachments(
                    adapter=adapter,
                    normalized=normalized,
                    workspace_id=connection.workspace_id,
                    message_id=message_id,
                )

            last_uid = batch.last_uid
            if last_uid is None and previous_validity == batch.uidvalidity:
                last_uid = previous_uid
            await self._update_imap_state(
                connection_id,
                uidvalidity=batch.uidvalidity,
                last_uid=last_uid or 0,
            )
        except Exception as exc:
            await self._record_imap_error(connection_id, exc)
            raise

    async def _persist_email_envelope(
        self,
        *,
        connection: ChannelConnection,
        normalized: NormalizedInboundMessage,
        request_digest: str,
    ) -> uuid.UUID:
        verified = VerifiedWebhook(
            timestamp=normalized.occurred_at,
            idempotency_key=hashlib.sha256(normalized.event_id.encode("utf-8")).hexdigest(),
            request_digest=request_digest,
        )
        async with self.session_factory() as session:
            async with session.begin():
                acceptance = await accept_inbound_event(
                    session,
                    workspace_id=connection.workspace_id,
                    channel_connection_id=connection.id,
                    source_key=f"email:{connection.id}",
                    external_event_id=normalized.event_id,
                    verified=verified,
                    payload={
                        "message_id": normalized.message_id,
                        "thread_id": normalized.thread_id,
                        "sender_id": normalized.sender_id,
                        "sender_display_name": normalized.sender_display_name,
                        "occurred_at": normalized.occurred_at.isoformat(),
                        "subject": normalized.metadata.get("subject"),
                    },
                )
                event = acceptance.event
                event.status = InboundStatus.processing
                message = await process_normalized_channel_message(
                    session,
                    event=event,
                    connection=connection,
                    normalized=normalized,
                )
                event.status = InboundStatus.processed
                event.processed_at = self.now()
                event.last_error = None
                return message.id

    async def _store_email_attachments(
        self,
        *,
        adapter: EmailAdapter,
        normalized: NormalizedInboundMessage,
        workspace_id: uuid.UUID,
        message_id: uuid.UUID,
    ) -> None:
        if not normalized.attachments:
            return
        if self.attachment_storage is None:
            logger.warning(
                "email attachments skipped because S3 is not configured",
                extra={"message_id": str(message_id)},
            )
            return
        for reference in normalized.attachments:
            content = await adapter.download_attachment(reference)
            try:
                stored = await self.attachment_storage.store(
                    workspace_id=workspace_id,
                    filename=reference.filename,
                    content_type=reference.content_type or "application/octet-stream",
                    content=content,
                )
            except AttachmentValidationError as exc:
                logger.warning(
                    "inbound email attachment rejected",
                    extra={"message_id": str(message_id), "reason": str(exc)},
                )
                continue
            keep_object = False
            try:
                inserted = False
                async with self.session_factory() as session:
                    async with session.begin():
                        existing = await session.scalar(
                            sa.select(Attachment.id).where(
                                Attachment.workspace_id == workspace_id,
                                Attachment.message_id == message_id,
                                Attachment.sha256 == stored.attachment.sha256,
                                Attachment.original_filename == stored.attachment.filename,
                            )
                        )
                        if existing is None:
                            session.add(
                                Attachment(
                                    workspace_id=workspace_id,
                                    message_id=message_id,
                                    object_key=stored.object_key,
                                    original_filename=stored.attachment.filename,
                                    content_type=stored.attachment.content_type,
                                    size_bytes=stored.attachment.size_bytes,
                                    sha256=stored.attachment.sha256,
                                )
                            )
                            await session.flush()
                            inserted = True
                keep_object = inserted
            finally:
                if not keep_object:
                    await self.attachment_storage.delete(
                        workspace_id=workspace_id,
                        object_key=stored.object_key,
                    )

    async def _update_imap_state(
        self,
        connection_id: uuid.UUID,
        *,
        uidvalidity: int,
        last_uid: int,
    ) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                connection = await session.scalar(
                    sa.select(ChannelConnection)
                    .where(ChannelConnection.id == connection_id)
                    .with_for_update()
                )
                if connection is None:
                    return
                connection.settings = {
                    **connection.settings,
                    "imap_state": {
                        "uidvalidity": uidvalidity,
                        "last_uid": last_uid,
                    },
                }
                connection.last_healthcheck_at = self.now()
                connection.last_error = None
                connection.version += 1

    async def _record_imap_error(self, connection_id: uuid.UUID, error: BaseException) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                await session.execute(
                    sa.update(ChannelConnection)
                    .where(ChannelConnection.id == connection_id)
                    .values(
                        last_healthcheck_at=self.now(),
                        last_error=str(error)[:MAX_ERROR_LENGTH],
                    )
                )


class RuntimeScheduler:
    """Discover durable integration work under one PostgreSQL advisory lock."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory = SessionLocal,
        interval_seconds: float = 2.0,
        batch_size: int = 100,
        now: Clock = _utcnow,
        advisory_lock_key: int = SCHEDULER_ADVISORY_LOCK_KEY,
    ) -> None:
        if not 0.1 <= interval_seconds <= 60:
            raise ValueError("scheduler interval must be between 0.1 and 60 seconds")
        if not 1 <= batch_size <= 500:
            raise ValueError("scheduler batch_size must be between 1 and 500")
        self.session_factory = session_factory
        self.interval_seconds = interval_seconds
        self.batch_size = batch_size
        self.now = now
        self.advisory_lock_key = advisory_lock_key
        self._stop_event = asyncio.Event()
        self._started_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._last_tick_monotonic: float | None = None
        self._last_tick_at: datetime | None = None
        self._last_error: str | None = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_tick_at(self) -> datetime | None:
        return self._last_tick_at

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def is_healthy(self, *, max_age_seconds: float = 30.0) -> bool:
        return bool(
            self._running
            and self._last_tick_monotonic is not None
            and time.monotonic() - self._last_tick_monotonic <= max_age_seconds
        )

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._started_event.clear()
        self._task = asyncio.create_task(self.run(), name="integration-runtime-scheduler")
        await self._started_event.wait()

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None and self._task is not asyncio.current_task():
            await self._task

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("runtime scheduler is already running")
        self._running = True
        self._started_event.set()
        try:
            while not self._stop_event.is_set():
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._last_error = str(exc)[:MAX_ERROR_LENGTH]
                    logger.exception("integration runtime scheduler tick failed")
                else:
                    self._last_tick_monotonic = time.monotonic()
                    self._last_tick_at = self.now()
                    self._last_error = None
                await self._wait_or_stop()
        finally:
            self._running = False

    async def _wait_or_stop(self) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
        except TimeoutError:
            pass

    async def tick(self) -> SchedulerTickResult:
        now = self.now()
        scheduled: dict[str, int] = {}
        async with self.session_factory() as session:
            async with session.begin():
                if not await self._acquire_lock(session):
                    return SchedulerTickResult(lock_acquired=False)
                await self._schedule_outbox(session, now, scheduled)
                await self._schedule_messages(session, now, scheduled)
                await self._schedule_inbound(session, now, scheduled)
                await self._schedule_forms(session, now, scheduled)
                await self._schedule_notifications(session, now, scheduled)
                await self._schedule_purchases(session, now, scheduled)
                await self._schedule_task_events(session, now, scheduled)
                await self._schedule_inactive_deals(session, now, scheduled)
                await self._schedule_imap(session, now, scheduled)
                await self._schedule_maintenance(session, now, scheduled)
        return SchedulerTickResult(lock_acquired=True, scheduled=scheduled)

    async def _acquire_lock(self, session: AsyncSession) -> bool:
        if session.get_bind().dialect.name != "postgresql":
            return True
        return bool(
            await session.scalar(
                sa.text("SELECT pg_try_advisory_xact_lock(:lock_key)").bindparams(
                    lock_key=self.advisory_lock_key
                )
            )
        )

    @staticmethod
    def _count(scheduled: dict[str, int], job_type: str) -> None:
        scheduled[job_type] = scheduled.get(job_type, 0) + 1

    async def _schedule_outbox(
        self, session: AsyncSession, now: datetime, scheduled: dict[str, int]
    ) -> None:
        events = list(
            (
                await session.scalars(
                    sa.select(OutboxEvent)
                    .where(
                        OutboxEvent.processed_at.is_(None),
                        OutboxEvent.available_at <= now,
                    )
                    .order_by(OutboxEvent.available_at, OutboxEvent.created_at, OutboxEvent.id)
                    .limit(self.batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for event in events:
            await _enqueue_runtime_job(
                session,
                JOB_OUTBOX_DISPATCH,
                {"outbox_event_id": str(event.id)},
                dedupe_key=f"outbox:{event.id}:dispatch",
            )
            self._count(scheduled, JOB_OUTBOX_DISPATCH)

    async def _schedule_messages(
        self, session: AsyncSession, now: datetime, scheduled: dict[str, int]
    ) -> None:
        del now
        ids = list(
            (
                await session.scalars(
                    sa.select(Message.id)
                    .where(
                        Message.direction == MessageDirection.outbound,
                        Message.status == MessageStatus.queued,
                    )
                    .order_by(Message.created_at, Message.id)
                    .limit(self.batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for message_id in ids:
            await _enqueue_runtime_job(
                session,
                JOB_MESSAGE_SEND,
                {"message_id": str(message_id)},
                dedupe_key=f"message:{message_id}:send",
            )
            self._count(scheduled, JOB_MESSAGE_SEND)

    async def _schedule_inbound(
        self, session: AsyncSession, now: datetime, scheduled: dict[str, int]
    ) -> None:
        del now
        ids = list(
            (
                await session.scalars(
                    sa.select(InboundEvent.id)
                    .where(InboundEvent.status == InboundStatus.accepted)
                    .order_by(InboundEvent.received_at, InboundEvent.id)
                    .limit(self.batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for event_id in ids:
            await _enqueue_runtime_job(
                session,
                JOB_INBOUND_PROCESS,
                {"inbound_event_id": str(event_id)},
                dedupe_key=f"inbound:{event_id}:process",
            )
            self._count(scheduled, JOB_INBOUND_PROCESS)

    async def _schedule_forms(
        self, session: AsyncSession, now: datetime, scheduled: dict[str, int]
    ) -> None:
        del now
        ids = list(
            (
                await session.scalars(
                    sa.select(FormSubmission.id)
                    .where(FormSubmission.status == FormSubmissionStatus.accepted)
                    .order_by(FormSubmission.created_at, FormSubmission.id)
                    .limit(self.batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for submission_id in ids:
            await _enqueue_runtime_job(
                session,
                JOB_FORM_PROCESS,
                {"form_submission_id": str(submission_id)},
                dedupe_key=f"form-submission:{submission_id}:process",
            )
            self._count(scheduled, JOB_FORM_PROCESS)

    async def _schedule_notifications(
        self, session: AsyncSession, now: datetime, scheduled: dict[str, int]
    ) -> None:
        ids = list(
            (
                await session.scalars(
                    sa.select(NotificationDelivery.id)
                    .where(
                        NotificationDelivery.status == DeliveryStatus.pending,
                        NotificationDelivery.scheduled_at <= now,
                    )
                    .order_by(NotificationDelivery.scheduled_at, NotificationDelivery.id)
                    .limit(self.batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for delivery_id in ids:
            await _enqueue_runtime_job(
                session,
                JOB_NOTIFICATION_DELIVER,
                {"notification_delivery_id": str(delivery_id)},
                dedupe_key=f"notification-delivery:{delivery_id}:send",
            )
            self._count(scheduled, JOB_NOTIFICATION_DELIVER)

    async def _schedule_purchases(
        self, session: AsyncSession, now: datetime, scheduled: dict[str, int]
    ) -> None:
        del now
        schedules = list(
            (
                await session.scalars(
                    sa.select(PurchaseSchedule)
                    .where(
                        PurchaseSchedule.status == PurchaseScheduleStatus.active,
                        PurchaseSchedule.task_id.is_(None),
                    )
                    .order_by(PurchaseSchedule.created_at, PurchaseSchedule.id)
                    .limit(self.batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for schedule in schedules:
            await _enqueue_runtime_job(
                session,
                JOB_PURCHASE_SCHEDULE,
                {
                    "purchase_schedule_id": str(schedule.id),
                    "workspace_id": str(schedule.workspace_id),
                },
                dedupe_key=f"purchase-schedule:{schedule.id}:task",
            )
            self._count(scheduled, JOB_PURCHASE_SCHEDULE)

    async def _schedule_imap(
        self, session: AsyncSession, now: datetime, scheduled: dict[str, int]
    ) -> None:
        connection_ids = list(
            (
                await session.scalars(
                    sa.select(ChannelConnection.id)
                    .where(
                        ChannelConnection.kind == ChannelKind.email,
                        ChannelConnection.status == ConnectionStatus.active,
                    )
                    .order_by(ChannelConnection.created_at, ChannelConnection.id)
                    .limit(self.batch_size)
                )
            ).all()
        )
        bucket = now.astimezone(UTC).strftime("%Y%m%d%H%M")
        for connection_id in connection_ids:
            await _enqueue_runtime_job(
                session,
                JOB_IMAP_POLL,
                {"channel_connection_id": str(connection_id)},
                dedupe_key=f"imap:{connection_id}:{bucket}",
                max_attempts=5,
            )
            self._count(scheduled, JOB_IMAP_POLL)

    async def _schedule_task_events(
        self, session: AsyncSession, now: datetime, scheduled: dict[str, int]
    ) -> None:
        due_tasks = list(
            (
                await session.scalars(
                    sa.select(Task)
                    .where(
                        Task.status == TaskStatus.open,
                        Task.remind_at.is_not(None),
                        Task.remind_at <= now,
                        Task.due_at > now,
                        ~sa.exists(
                            sa.select(ScheduledEventMarker.id).where(
                                ScheduledEventMarker.workspace_id == Task.workspace_id,
                                ScheduledEventMarker.event_type == "task.due_soon",
                                ScheduledEventMarker.entity_type == "task",
                                ScheduledEventMarker.entity_id == Task.id,
                                ScheduledEventMarker.occurrence_at == Task.remind_at,
                            )
                        ),
                    )
                    .order_by(Task.remind_at, Task.id)
                    .limit(self.batch_size)
                )
            ).all()
        )
        overdue_tasks = list(
            (
                await session.scalars(
                    sa.select(Task)
                    .where(
                        Task.status == TaskStatus.open,
                        Task.due_at <= now,
                        ~sa.exists(
                            sa.select(ScheduledEventMarker.id).where(
                                ScheduledEventMarker.workspace_id == Task.workspace_id,
                                ScheduledEventMarker.event_type == "task.overdue",
                                ScheduledEventMarker.entity_type == "task",
                                ScheduledEventMarker.entity_id == Task.id,
                                ScheduledEventMarker.occurrence_at == Task.due_at,
                            )
                        ),
                    )
                    .order_by(Task.due_at, Task.id)
                    .limit(self.batch_size)
                )
            ).all()
        )
        for task, job_type, occurrence in (
            *((task, JOB_TASK_DUE_EVENT, task.remind_at) for task in due_tasks),
            *((task, JOB_TASK_OVERDUE_EVENT, task.due_at) for task in overdue_tasks),
        ):
            if occurrence is None:
                continue
            await _enqueue_runtime_job(
                session,
                job_type,
                {
                    "task_id": str(task.id),
                    "workspace_id": str(task.workspace_id),
                    "occurrence_at": occurrence.isoformat(),
                },
                dedupe_key=f"{job_type}:{task.id}:{occurrence.isoformat()}",
            )
            self._count(scheduled, job_type)

    async def _schedule_inactive_deals(
        self, session: AsyncSession, now: datetime, scheduled: dict[str, int]
    ) -> None:
        deals = list(
            (
                await session.scalars(
                    sa.select(Deal)
                    .join(Stage, Stage.id == Deal.stage_id)
                    .where(
                        Deal.deleted_at.is_(None),
                        Deal.last_activity_at <= now - timedelta(days=7),
                        Stage.workspace_id == Deal.workspace_id,
                        Stage.stage_type == StageType.open,
                        ~sa.exists(
                            sa.select(ScheduledEventMarker.id).where(
                                ScheduledEventMarker.workspace_id == Deal.workspace_id,
                                ScheduledEventMarker.event_type == "deal.inactive",
                                ScheduledEventMarker.entity_type == "deal",
                                ScheduledEventMarker.entity_id == Deal.id,
                                ScheduledEventMarker.occurrence_at == Deal.last_activity_at,
                            )
                        ),
                    )
                    .order_by(Deal.last_activity_at, Deal.id)
                    .limit(self.batch_size)
                )
            ).all()
        )
        for deal in deals:
            occurrence = deal.last_activity_at
            await _enqueue_runtime_job(
                session,
                JOB_DEAL_INACTIVE_EVENT,
                {
                    "deal_id": str(deal.id),
                    "workspace_id": str(deal.workspace_id),
                    "occurrence_at": occurrence.isoformat(),
                },
                dedupe_key=f"{JOB_DEAL_INACTIVE_EVENT}:{deal.id}:{occurrence.isoformat()}",
            )
            self._count(scheduled, JOB_DEAL_INACTIVE_EVENT)

    async def _schedule_maintenance(
        self, session: AsyncSession, now: datetime, scheduled: dict[str, int]
    ) -> None:
        utc = now.astimezone(UTC)
        cleanup_bucket = utc.strftime("%Y%m%d%H")
        recovery_bucket = utc.strftime("%Y%m%d%H%M")
        await _enqueue_runtime_job(
            session,
            JOB_RUNTIME_CLEANUP,
            {},
            dedupe_key=f"runtime:cleanup:{cleanup_bucket}",
            max_attempts=3,
        )
        self._count(scheduled, JOB_RUNTIME_CLEANUP)
        await _enqueue_runtime_job(
            session,
            JOB_RUNTIME_RECOVER,
            {},
            dedupe_key=f"runtime:recover:{recovery_bucket}",
            max_attempts=3,
        )
        self._count(scheduled, JOB_RUNTIME_RECOVER)


class IntegrationRuntime:
    """Own the bounded job supervisor and the advisory-lock scheduler."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory = SessionLocal,
        adapter_factory: ConnectionAdapterFactory | None = None,
        notification_adapter_factory: NotificationAdapterFactory | None = None,
        imap_poller_factory: IMAPPollerFactory | None = None,
        attachment_storage: AttachmentStorage | None = None,
        inbound_processor: InboundProcessor | None = None,
        form_processor: FormProcessor | None = None,
        notification_expander: NotificationExpander | None = None,
        extra_handlers: Mapping[str, JobHandler] | None = None,
        concurrency: int = 4,
        scheduler_interval_seconds: float = 2.0,
        scheduler_batch_size: int = 100,
        now: Clock = _utcnow,
    ) -> None:
        if not 1 <= concurrency <= 4:
            raise ValueError("runtime concurrency must be between 1 and 4")
        self.handlers = RuntimeHandlers(
            session_factory=session_factory,
            adapter_factory=adapter_factory,
            notification_adapter_factory=notification_adapter_factory,
            imap_poller_factory=imap_poller_factory,
            attachment_storage=attachment_storage,
            inbound_processor=inbound_processor,
            form_processor=form_processor,
            notification_expander=notification_expander,
            now=now,
        )
        registry = self.handlers.registry()
        for job_type, handler in (extra_handlers or {}).items():
            if job_type in registry:
                raise ValueError(f"duplicate runtime job handler: {job_type}")
            registry[job_type] = handler
        self.supervisor = JobSupervisor(
            registry,
            session_factory=session_factory,
            concurrency=concurrency,
            batch_size=concurrency,
        )
        self.scheduler = RuntimeScheduler(
            session_factory=session_factory,
            interval_seconds=scheduler_interval_seconds,
            batch_size=scheduler_batch_size,
            now=now,
        )

    async def start(self) -> None:
        await self.supervisor.start()
        try:
            await self.scheduler.start()
        except Exception:
            await self.supervisor.stop()
            raise

    async def stop(self) -> None:
        await self.scheduler.stop()
        await self.supervisor.stop()

    def is_healthy(self, *, max_age_seconds: float = 30.0) -> bool:
        return self.supervisor.is_healthy(
            max_age_seconds=max_age_seconds
        ) and self.scheduler.is_healthy(max_age_seconds=max_age_seconds)

    def health(self, *, max_age_seconds: float = 30.0) -> RuntimeHealth:
        return RuntimeHealth(
            healthy=self.is_healthy(max_age_seconds=max_age_seconds),
            scheduler_running=self.scheduler.running,
            supervisor_running=self.supervisor.running,
            supervisor_active_jobs=self.supervisor.active_job_count,
            last_scheduler_tick_at=self.scheduler.last_tick_at,
            scheduler_last_error=self.scheduler.last_error,
        )


__all__ = [
    "ConnectionAdapterFactory",
    "FormProcessor",
    "InboundProcessor",
    "IntegrationRuntime",
    "IMAPPollerFactory",
    "JOB_IMAP_POLL",
    "JOB_TASK_DUE_EVENT",
    "JOB_TASK_OVERDUE_EVENT",
    "JOB_DEAL_INACTIVE_EVENT",
    "JOB_FORM_PROCESS",
    "JOB_INBOUND_PROCESS",
    "JOB_MESSAGE_SEND",
    "JOB_NOTIFICATION_DELIVER",
    "JOB_NOTIFICATION_EXPAND",
    "JOB_OUTBOX_DISPATCH",
    "JOB_PURCHASE_SCHEDULE",
    "JOB_RUNTIME_CLEANUP",
    "JOB_RUNTIME_RECOVER",
    "NotificationAdapterFactory",
    "NotificationExpander",
    "RUNTIME_JOB_TYPES",
    "RuntimeHandlers",
    "RuntimeHealth",
    "RuntimeScheduler",
    "RuntimeWiringError",
    "SCHEDULER_ADVISORY_LOCK_KEY",
    "SchedulerTickResult",
]

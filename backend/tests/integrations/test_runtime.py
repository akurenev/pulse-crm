from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import SessionLocal, engine
from app.integrations.channels.base import (
    AdapterHealth,
    AttachmentReference,
    NormalizedInboundMessage,
    OutboundAttachment,
    OutboundMessage,
    SendResult,
)
from app.integrations.inbound import (
    InboundRoutingError,
    process_form_submission,
    process_inbound_event,
)
from app.integrations.models import (
    Attachment,
    ChannelConnection,
    ChannelKind,
    ConnectionStatus,
    Conversation,
    ConversationStatus,
    ExternalIdentity,
    Form,
    FormSubmission,
    FormSubmissionStatus,
    InboundEvent,
    InboundStatus,
    Message,
    MessageDirection,
    MessageStatus,
    NotificationAudience,
    NotificationDelivery,
    NotificationRule,
    NotificationTemplate,
    PurchaseSchedule,
    PurchaseScheduleStatus,
    WebhookEndpoint,
)
from app.integrations.runtime import (
    JOB_INBOUND_PROCESS,
    JOB_MESSAGE_SEND,
    JOB_NOTIFICATION_EXPAND,
    JOB_RUNTIME_CLEANUP,
    JOB_RUNTIME_RECOVER,
    RUNTIME_JOB_TYPES,
    RuntimeHandlers,
    RuntimeScheduler,
    RuntimeWiringError,
)
from app.integrations.s3 import AttachmentStorage
from app.integrations.secrets import SecretCipher
from app.integrations.web_push import TEST_PUSH_BODY, TEST_PUSH_SUBJECT, register_subscription
from app.models import (
    ActivityEvent,
    BackgroundJob,
    Contact,
    Deal,
    DealContact,
    DeliveryStatus,
    Membership,
    OutboxEvent,
    RealtimeEvent,
    Role,
    Source,
    Task,
    TaskStatus,
    User,
    Workspace,
)
from app.services.jobs import ClaimedJob

FIXED_NOW = datetime(2026, 8, 27, 12, tzinfo=UTC)


def claimed_job(
    job_type: str,
    payload: dict[str, Any],
    *,
    attempts: int = 1,
    max_attempts: int = 5,
    workspace_id: uuid.UUID | None = None,
) -> ClaimedJob:
    return ClaimedJob(
        id=uuid.uuid4(),
        job_type=job_type,
        payload=payload,
        attempts=attempts,
        max_attempts=max_attempts,
        lease_owner="runtime-test",
        workspace_id=workspace_id,
    )


class RecordingAdapter:
    provider = "telegram"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def verify(self, headers: Mapping[str, str], body: bytes) -> None:
        del headers, body

    def normalize_inbound(self, payload: Mapping[str, Any]) -> NormalizedInboundMessage:
        return NormalizedInboundMessage(
            provider="telegram",
            event_id=str(payload["event_id"]),
            message_id=str(payload["message_id"]),
            thread_id=str(payload["thread_id"]),
            sender_id=str(payload["sender_id"]),
            sender_display_name=str(payload.get("sender_name") or "") or None,
            text=str(payload.get("text") or ""),
            occurred_at=FIXED_NOW,
        )

    async def send_message(self, message: OutboundMessage) -> SendResult:
        self.sent.append(message)
        return SendResult(provider_message_id="provider-out-1", sent_at=FIXED_NOW)

    async def download_attachment(self, reference: AttachmentReference) -> bytes:
        return reference.provider_file_id.encode()

    async def healthcheck(self) -> AdapterHealth:
        return AdapterHealth(healthy=True)


class FlakyAttachmentAdapter(RecordingAdapter):
    async def send_message(self, message: OutboundMessage) -> SendResult:
        self.sent.append(message)
        if len(self.sent) == 1:
            raise RuntimeError("provider temporarily unavailable")
        return SendResult(provider_message_id="provider-out-attachment", sent_at=FIXED_NOW)


class RuntimeAttachmentS3:
    def __init__(self, object_key: str, content: bytes) -> None:
        self.object_key = object_key
        self.content = content
        self.gets: list[dict[str, Any]] = []

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.gets.append(kwargs)
        if kwargs["Key"] != self.object_key:
            raise KeyError("unknown object")
        return {"Body": self.content, "ContentLength": len(self.content)}


def channel_connection(
    domain: dict[str, Any],
    *,
    kind: ChannelKind = ChannelKind.telegram,
) -> ChannelConnection:
    return ChannelConnection(
        workspace_id=domain["workspace"].id,
        kind=kind,
        name=f"Runtime {kind.value}",
        status=ConnectionStatus.active,
        settings={},
        default_pipeline_id=domain["pipeline"].id,
        default_stage_id=domain["stage"].id,
        default_assignee_id=domain["user"].id,
    )


def inbound_event(
    domain: dict[str, Any],
    connection: ChannelConnection,
    *,
    event_id: str,
    message_id: str,
    text: str,
) -> InboundEvent:
    return InboundEvent(
        workspace_id=domain["workspace"].id,
        channel_connection_id=connection.id,
        source_key=f"telegram:{connection.id}",
        external_event_id=event_id,
        request_digest=event_id.rjust(64, "0"),
        payload={
            "event_id": event_id,
            "message_id": message_id,
            "thread_id": "chat-42",
            "sender_id": "customer-7",
            "sender_name": "Анна Иванова",
            "text": text,
        },
        status=InboundStatus.accepted,
        received_at=FIXED_NOW,
    )


def test_runtime_registry_contains_every_supported_job_type() -> None:
    registry = RuntimeHandlers().registry()

    assert set(registry) == RUNTIME_JOB_TYPES
    assert all(callable(handler) for handler in registry.values())


@pytest.mark.asyncio
async def test_scheduler_tick_is_idempotent_for_same_durable_work(
    db: AsyncSession,
    integration_domain: dict[str, Any],
) -> None:
    event = InboundEvent(
        workspace_id=integration_domain["workspace"].id,
        source_key="generic:00000000-0000-0000-0000-000000000001",
        external_event_id="scheduler-event",
        request_digest="a" * 64,
        payload={},
        status=InboundStatus.accepted,
        received_at=FIXED_NOW,
    )
    db.add(event)
    await db.commit()

    scheduler = RuntimeScheduler(session_factory=SessionLocal, now=lambda: FIXED_NOW)
    first = await scheduler.tick()
    second = await scheduler.tick()

    jobs = list((await db.scalars(sa.select(BackgroundJob))).all())
    dedupe_keys = {job.dedupe_key for job in jobs}

    assert first.lock_acquired is True
    assert second.lock_acquired is True
    assert first.scheduled[JOB_INBOUND_PROCESS] == 1
    assert second.scheduled[JOB_INBOUND_PROCESS] == 1
    assert dedupe_keys == {
        f"inbound:{event.id}:process",
        "runtime:cleanup:2026082712",
        "runtime:recover:202608271200",
    }
    assert len(jobs) == 3
    assert {job.job_type for job in jobs} == {
        JOB_INBOUND_PROCESS,
        JOB_RUNTIME_CLEANUP,
        JOB_RUNTIME_RECOVER,
    }
    scoped = next(job for job in jobs if job.job_type == JOB_INBOUND_PROCESS)
    assert scoped.workspace_id == integration_domain["workspace"].id
    assert all(
        job.workspace_id is None
        for job in jobs
        if job.job_type in {JOB_RUNTIME_CLEANUP, JOB_RUNTIME_RECOVER}
    )


@pytest.mark.asyncio
async def test_outbox_dispatch_enqueues_one_deduplicated_child_job(
    db: AsyncSession,
    integration_domain: dict[str, Any],
) -> None:
    message_id = uuid.uuid4()
    event = OutboxEvent(
        workspace_id=integration_domain["workspace"].id,
        event_type="message.outbound.queued",
        aggregate_type="message",
        aggregate_id=message_id,
        payload={"message_id": str(message_id)},
        available_at=FIXED_NOW,
    )
    db.add(event)
    await db.commit()
    handlers = RuntimeHandlers(session_factory=SessionLocal, now=lambda: FIXED_NOW)
    job = claimed_job(
        "outbox.dispatch",
        {"outbox_event_id": str(event.id)},
        workspace_id=event.workspace_id,
    )

    await handlers.dispatch_outbox(job)
    await handlers.dispatch_outbox(job)

    await db.refresh(event)
    child_jobs = list(
        (
            await db.scalars(
                sa.select(BackgroundJob).where(
                    BackgroundJob.dedupe_key == f"message:{message_id}:send"
                )
            )
        ).all()
    )
    assert event.processed_at == FIXED_NOW.replace(tzinfo=None)
    assert event.attempts == 1
    assert len(child_jobs) == 1
    assert child_jobs[0].job_type == JOB_MESSAGE_SEND
    assert child_jobs[0].payload == {"message_id": str(message_id)}
    assert child_jobs[0].workspace_id == integration_domain["workspace"].id


@pytest.mark.asyncio
async def test_purchase_notification_expansion_skips_cancelled_or_missing_schedule(
    db: AsyncSession,
    integration_domain: dict[str, Any],
) -> None:
    workspace_id = integration_domain["workspace"].id
    cancelled_schedule = PurchaseSchedule(
        workspace_id=workspace_id,
        deal_id=integration_domain["deal"].id,
        contact_id=integration_domain["contact"].id,
        assignee_id=integration_domain["user"].id,
        scheduled_for=FIXED_NOW + timedelta(days=30),
        remind_at=FIXED_NOW,
        status=PurchaseScheduleStatus.cancelled,
        completed_at=FIXED_NOW,
    )
    active_schedule = PurchaseSchedule(
        workspace_id=workspace_id,
        deal_id=integration_domain["deal"].id,
        contact_id=integration_domain["contact"].id,
        assignee_id=integration_domain["user"].id,
        scheduled_for=FIXED_NOW + timedelta(days=31),
        remind_at=FIXED_NOW,
        status=PurchaseScheduleStatus.active,
    )
    db.add_all([cancelled_schedule, active_schedule])
    await db.flush()
    events = [
        OutboxEvent(
            workspace_id=workspace_id,
            event_type="purchase.due_soon",
            aggregate_type="purchase_schedule",
            aggregate_id=schedule_id,
            payload={"schedule_id": str(schedule_id)},
            available_at=FIXED_NOW,
        )
        for schedule_id in (cancelled_schedule.id, uuid.uuid4(), active_schedule.id)
    ]
    db.add_all(events)
    await db.commit()

    expanded_event_ids: list[uuid.UUID] = []

    async def record_expansion(session: AsyncSession, event: OutboxEvent) -> None:
        del session
        expanded_event_ids.append(event.id)

    handlers = RuntimeHandlers(
        session_factory=SessionLocal,
        notification_expander=record_expansion,
        now=lambda: FIXED_NOW,
    )
    for event in events:
        await handlers.expand_notification(
            claimed_job(
                JOB_NOTIFICATION_EXPAND,
                {"outbox_event_id": str(event.id)},
                workspace_id=workspace_id,
            )
        )

    assert expanded_event_ids == [events[-1].id]


@pytest.mark.parametrize(
    ("event_case", "expected_target_type"),
    [
        ("lead", "deal"),
        ("task", "task"),
        ("message", "deal"),
        ("purchase", "task"),
        ("invalid_message", None),
        ("foreign_task", None),
    ],
)
@pytest.mark.asyncio
async def test_static_notification_expansion_materializes_tenant_owned_target(
    db: AsyncSession,
    integration_domain: dict[str, Any],
    event_case: str,
    expected_target_type: str | None,
) -> None:
    workspace = integration_domain["workspace"]
    user = integration_domain["user"]
    deal = integration_domain["deal"]
    task = Task(
        workspace_id=workspace.id,
        title="Deep-link task",
        status=TaskStatus.open,
        due_at=FIXED_NOW + timedelta(days=2),
        assignee_id=user.id,
        deal_id=deal.id,
    )
    db.add(task)
    await db.flush()

    if event_case == "lead":
        event_type = "lead.created"
        aggregate_type = "deal"
        aggregate_id = deal.id
        payload: dict[str, Any] = {}
        expected_target_id = deal.id
    elif event_case == "task":
        event_type = "task.due_soon"
        aggregate_type = "task"
        aggregate_id = task.id
        payload = {}
        expected_target_id = task.id
    elif event_case == "message":
        event_type = "message.inbound.received"
        aggregate_type = "message"
        aggregate_id = uuid.uuid4()
        payload = {"deal_id": str(deal.id)}
        expected_target_id = deal.id
    elif event_case == "purchase":
        schedule = PurchaseSchedule(
            workspace_id=workspace.id,
            deal_id=deal.id,
            contact_id=integration_domain["contact"].id,
            assignee_id=user.id,
            scheduled_for=FIXED_NOW + timedelta(days=2),
            remind_at=FIXED_NOW,
            task_id=task.id,
            status=PurchaseScheduleStatus.active,
        )
        db.add(schedule)
        await db.flush()
        event_type = "purchase.due_soon"
        aggregate_type = "purchase_schedule"
        aggregate_id = schedule.id
        payload = {"task_id": str(task.id)}
        expected_target_id = task.id
    elif event_case == "invalid_message":
        event_type = "message.inbound.received"
        aggregate_type = "message"
        aggregate_id = uuid.uuid4()
        payload = {"deal_id": "https://attacker.invalid/not-a-uuid"}
        expected_target_id = None
    else:
        other_workspace = Workspace(name="Foreign Target", slug="foreign-target")
        db.add(other_workspace)
        await db.flush()
        foreign_task = Task(
            workspace_id=other_workspace.id,
            title="Foreign task",
            status=TaskStatus.open,
            due_at=FIXED_NOW + timedelta(days=2),
            assignee_id=user.id,
        )
        db.add(foreign_task)
        await db.flush()
        event_type = "task.overdue"
        aggregate_type = "task"
        aggregate_id = foreign_task.id
        payload = {}
        expected_target_id = None

    template = NotificationTemplate(
        workspace_id=workspace.id,
        name=f"Deep link {event_case}",
        channel="in_app",
        subject_template="CRM event",
        body_template="Open {entity_id}",
        is_active=True,
    )
    db.add(template)
    await db.flush()
    rule = NotificationRule(
        workspace_id=workspace.id,
        template_id=template.id,
        name=f"Deep link rule {event_case}",
        event_type=event_type,
        audience=NotificationAudience.employee,
        channel="in_app",
        recipients=[{"address": str(user.id), "recipient_id": str(user.id)}],
        delay_seconds=30 * 24 * 3600,
        require_client_consent=False,
        is_enabled=True,
    )
    event = OutboxEvent(
        workspace_id=workspace.id,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload,
        available_at=FIXED_NOW,
    )
    db.add_all([rule, event])
    await db.commit()

    handlers = RuntimeHandlers(session_factory=SessionLocal, now=lambda: FIXED_NOW)
    await handlers.expand_notification(
        claimed_job(
            JOB_NOTIFICATION_EXPAND,
            {"outbox_event_id": str(event.id)},
            workspace_id=workspace.id,
        )
    )

    delivery = await db.scalar(
        sa.select(NotificationDelivery).where(
            NotificationDelivery.workspace_id == workspace.id,
            NotificationDelivery.dedupe_key.like(f"%:event:{event.id}:recipient:%"),
        )
    )
    assert delivery is not None
    assert delivery.target_entity_type == expected_target_type
    assert delivery.target_entity_id == expected_target_id
    assert delivery.scheduled_at == (FIXED_NOW + timedelta(days=30)).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_static_notification_skips_foreign_target_for_restricted_employee(
    db: AsyncSession,
    integration_domain: dict[str, Any],
) -> None:
    workspace = integration_domain["workspace"]
    user = integration_domain["user"]
    deal = integration_domain["deal"]
    membership = await db.scalar(
        sa.select(Membership).where(
            Membership.workspace_id == workspace.id,
            Membership.user_id == user.id,
        )
    )
    assert membership is not None
    membership.role = Role.employee
    other_user = User(
        email="other-assignee@example.com",
        full_name="Other Assignee",
        password_hash="not-used-in-service-tests",
    )
    db.add(other_user)
    await db.flush()
    deal.assignee_id = other_user.id

    template = NotificationTemplate(
        workspace_id=workspace.id,
        name="Restricted employee target",
        channel="in_app",
        subject_template="CRM event",
        body_template="Open {entity_id}",
        is_active=True,
    )
    db.add(template)
    await db.flush()
    rule = NotificationRule(
        workspace_id=workspace.id,
        template_id=template.id,
        name="Restricted employee target rule",
        event_type="deal.stage_changed",
        audience=NotificationAudience.employee,
        channel="in_app",
        recipients=[{"address": str(user.id), "recipient_id": str(user.id)}],
        delay_seconds=0,
        require_client_consent=False,
        is_enabled=True,
    )
    event = OutboxEvent(
        workspace_id=workspace.id,
        event_type="deal.stage_changed",
        aggregate_type="deal",
        aggregate_id=deal.id,
        payload={},
        available_at=FIXED_NOW,
    )
    db.add_all([rule, event])
    await db.commit()

    handlers = RuntimeHandlers(session_factory=SessionLocal, now=lambda: FIXED_NOW)
    await handlers.expand_notification(
        claimed_job(
            JOB_NOTIFICATION_EXPAND,
            {"outbox_event_id": str(event.id)},
            workspace_id=workspace.id,
        )
    )

    delivery = await db.scalar(
        sa.select(NotificationDelivery).where(
            NotificationDelivery.workspace_id == workspace.id,
            NotificationDelivery.dedupe_key.like(f"%:event:{event.id}:recipient:%"),
        )
    )
    assert delivery is None


@pytest.mark.asyncio
async def test_static_notification_scrubs_secondary_ids_before_manager_is_demoted(
    db: AsyncSession,
    integration_domain: dict[str, Any],
) -> None:
    workspace = integration_domain["workspace"]
    user = integration_domain["user"]
    membership = await db.scalar(
        sa.select(Membership).where(
            Membership.workspace_id == workspace.id,
            Membership.user_id == user.id,
        )
    )
    assert membership is not None
    assert membership.role is Role.owner
    other_user = User(
        email="secondary-owner@example.com",
        full_name="Secondary Owner",
        password_hash="not-used-in-service-tests",
    )
    db.add(other_user)
    await db.flush()
    foreign_deal = Deal(
        workspace_id=workspace.id,
        pipeline_id=integration_domain["pipeline"].id,
        stage_id=integration_domain["stage"].id,
        assignee_id=other_user.id,
        title="Inaccessible linked deal",
    )
    db.add(foreign_deal)
    await db.flush()
    own_task = Task(
        workspace_id=workspace.id,
        title="Own task with inaccessible references",
        status=TaskStatus.open,
        due_at=FIXED_NOW + timedelta(hours=1),
        assignee_id=user.id,
        deal_id=foreign_deal.id,
    )
    db.add(own_task)
    await db.flush()
    foreign_contact_id = uuid.uuid4()
    foreign_conversation_id = uuid.uuid4()
    template = NotificationTemplate(
        workspace_id=workspace.id,
        name="Restricted variables",
        channel="in_app",
        body_template=(
            "task={task_id}; deal={deal_id}; contact={contact_id}; "
            "conversation={conversation_id}; entity={entity_id}"
        ),
        is_active=True,
    )
    db.add(template)
    await db.flush()
    rule = NotificationRule(
        workspace_id=workspace.id,
        template_id=template.id,
        name="Restricted variables rule",
        event_type="task.due_soon",
        audience=NotificationAudience.employee,
        channel="in_app",
        recipients=[{"address": str(user.id), "recipient_id": str(user.id)}],
        delay_seconds=0,
        require_client_consent=False,
        is_enabled=True,
    )
    event = OutboxEvent(
        workspace_id=workspace.id,
        event_type="task.due_soon",
        aggregate_type="task",
        aggregate_id=own_task.id,
        payload={
            "task_id": str(own_task.id),
            "deal_id": str(foreign_deal.id),
            "contact_id": str(foreign_contact_id),
            "conversation_id": str(foreign_conversation_id),
        },
        available_at=FIXED_NOW,
    )
    db.add_all([rule, event])
    await db.commit()

    handlers = RuntimeHandlers(session_factory=SessionLocal, now=lambda: FIXED_NOW)
    await handlers.expand_notification(
        claimed_job(
            JOB_NOTIFICATION_EXPAND,
            {"outbox_event_id": str(event.id)},
            workspace_id=workspace.id,
        )
    )

    delivery = await db.scalar(
        sa.select(NotificationDelivery).where(
            NotificationDelivery.workspace_id == workspace.id,
            NotificationDelivery.dedupe_key.like(f"%:event:{event.id}:recipient:%"),
        )
    )
    assert delivery is not None
    assert str(own_task.id) in delivery.body
    assert str(foreign_deal.id) not in delivery.body
    assert str(foreign_contact_id) not in delivery.body
    assert str(foreign_conversation_id) not in delivery.body

    membership.role = Role.employee
    await db.commit()
    await handlers.deliver_notification(
        claimed_job(
            "notification.deliver",
            {"notification_delivery_id": str(delivery.id)},
            workspace_id=workspace.id,
        )
    )
    await db.refresh(delivery)
    assert delivery.status is DeliveryStatus.delivered
    assert str(foreign_deal.id) not in delivery.body


@pytest.mark.asyncio
async def test_runtime_rejects_cross_workspace_claim_before_domain_mutation(
    db: AsyncSession,
    integration_domain: dict[str, Any],
) -> None:
    event = OutboxEvent(
        workspace_id=integration_domain["workspace"].id,
        event_type="message.outbound.queued",
        aggregate_type="message",
        aggregate_id=uuid.uuid4(),
        payload={},
        available_at=FIXED_NOW,
    )
    db.add(event)
    await db.commit()
    handlers = RuntimeHandlers(session_factory=SessionLocal, now=lambda: FIXED_NOW)

    with pytest.raises(RuntimeWiringError, match="does not match"):
        await handlers.dispatch_outbox(
            claimed_job(
                "outbox.dispatch",
                {"outbox_event_id": str(event.id)},
                workspace_id=uuid.uuid4(),
            )
        )

    await db.refresh(event)
    assert event.processed_at is None
    assert event.attempts == 0
    assert (
        await db.scalar(
            sa.select(sa.func.count()).select_from(BackgroundJob).where(
                BackgroundJob.dedupe_key == f"message:{event.aggregate_id}:send"
            )
        )
        == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("job_type", [JOB_RUNTIME_CLEANUP, JOB_RUNTIME_RECOVER])
async def test_global_maintenance_handlers_reject_workspace_scoped_jobs(
    job_type: str,
) -> None:
    handlers = RuntimeHandlers(session_factory=SessionLocal, now=lambda: FIXED_NOW)
    handler = handlers.registry()[job_type]

    with pytest.raises(RuntimeWiringError, match="must not have workspace_id"):
        await handler(claimed_job(job_type, {}, workspace_id=uuid.uuid4()))


@pytest.mark.asyncio
async def test_provider_inbound_reuses_conversation_and_deduplicates_message(
    db: AsyncSession,
    integration_domain: dict[str, Any],
) -> None:
    connection = channel_connection(integration_domain)
    db.add(connection)
    await db.flush()
    adapter = RecordingAdapter()

    first = inbound_event(
        integration_domain,
        connection,
        event_id="event-1",
        message_id="message-1",
        text="Первое сообщение",
    )
    db.add(first)
    await db.flush()
    await process_inbound_event(db, first, adapter_factory=lambda _: adapter)
    await db.flush()

    conversation = await db.scalar(
        sa.select(Conversation).where(Conversation.channel_connection_id == connection.id)
    )
    assert conversation is not None
    original_conversation_id = conversation.id
    original_contact_id = conversation.contact_id
    original_deal_id = conversation.deal_id
    assert original_contact_id is not None
    routed_contact = await db.get(Contact, original_contact_id)
    assert routed_contact is not None
    assert routed_contact.assignee_id == integration_domain["user"].id

    second = inbound_event(
        integration_domain,
        connection,
        event_id="event-2",
        message_id="message-2",
        text="Второе сообщение",
    )
    db.add(second)
    await db.flush()
    await process_inbound_event(db, second, adapter_factory=lambda _: adapter)

    duplicate = inbound_event(
        integration_domain,
        connection,
        event_id="event-3",
        message_id="message-2",
        text="Повтор провайдера",
    )
    db.add(duplicate)
    await db.flush()
    await process_inbound_event(db, duplicate, adapter_factory=lambda _: adapter)
    await db.flush()

    conversations = list(
        (
            await db.scalars(
                sa.select(Conversation).where(Conversation.channel_connection_id == connection.id)
            )
        ).all()
    )
    messages = list(
        (
            await db.scalars(
                sa.select(Message)
                .where(Message.conversation_id == original_conversation_id)
                .order_by(Message.provider_message_id)
            )
        ).all()
    )
    identities = list(
        (
            await db.scalars(
                sa.select(ExternalIdentity).where(
                    ExternalIdentity.channel_connection_id == connection.id
                )
            )
        ).all()
    )

    assert len(conversations) == 1
    assert conversations[0].contact_id == original_contact_id
    assert conversations[0].deal_id == original_deal_id
    assert [message.provider_message_id for message in messages] == ["message-1", "message-2"]
    assert [message.body for message in messages] == ["Первое сообщение", "Второе сообщение"]
    assert len(identities) == 1
    assert identities[0].contact_id == original_contact_id
    routed_deal = await db.get(Deal, original_deal_id)
    assert routed_deal is not None
    assert routed_deal.source_id == integration_domain["source"].id


@pytest.mark.asyncio
async def test_provider_inbound_rejects_inactive_default_assignee(
    db: AsyncSession,
    integration_domain: dict[str, Any],
) -> None:
    connection = channel_connection(integration_domain)
    db.add(connection)
    await db.flush()
    integration_domain["user"].is_active = False
    event = inbound_event(
        integration_domain,
        connection,
        event_id="inactive-route-event",
        message_id="inactive-route-message",
        text="Не должно быть маршрутизировано",
    )
    db.add(event)
    await db.flush()

    with pytest.raises(InboundRoutingError, match="active workspace member"):
        await process_inbound_event(db, event, adapter_factory=lambda _: RecordingAdapter())

    assert await db.scalar(
        sa.select(sa.func.count(Conversation.id)).where(
            Conversation.channel_connection_id == connection.id
        )
    ) == 0


@pytest.mark.asyncio
async def test_generic_and_form_submissions_create_contacts_deals_and_timeline_events(
    db: AsyncSession,
    integration_domain: dict[str, Any],
) -> None:
    endpoint = WebhookEndpoint(
        workspace_id=integration_domain["workspace"].id,
        slug="runtime-hook",
        name="Runtime hook",
        encrypted_secret=b"test",
        pipeline_id=integration_domain["pipeline"].id,
        stage_id=integration_domain["stage"].id,
        assignee_id=integration_domain["user"].id,
        source_id=None,
    )
    form = Form(
        workspace_id=integration_domain["workspace"].id,
        slug="runtime-form",
        title="Runtime form",
        pipeline_id=integration_domain["pipeline"].id,
        stage_id=integration_domain["stage"].id,
        assignee_id=integration_domain["user"].id,
        source_id=None,
        fields_schema=[],
        allowed_origins=[],
    )
    db.add_all([endpoint, form])
    await db.flush()

    generic = InboundEvent(
        workspace_id=integration_domain["workspace"].id,
        source_key=f"generic:{endpoint.id}",
        external_event_id="generic-1",
        request_digest="b" * 64,
        payload={
            "contact": {
                "first_name": "Иван",
                "last_name": "Петров",
                "email": "ivan@example.com",
            },
            "deal": {"title": "Webhook deal", "amount": "1500", "currency": "rub"},
            "message": {"text": "Сообщение из webhook"},
        },
        status=InboundStatus.accepted,
        received_at=FIXED_NOW,
    )
    submission = FormSubmission(
        workspace_id=integration_domain["workspace"].id,
        form_id=form.id,
        idempotency_key="form-1",
        request_digest="c" * 64,
        payload={
            "first_name": "Мария",
            "last_name": "Сидорова",
            "email": "maria@example.com",
            "title": "Form deal",
            "amount": "2500",
            "message": "Сообщение из формы",
        },
        status=FormSubmissionStatus.accepted,
    )
    db.add_all([generic, submission])
    await db.flush()

    await process_inbound_event(db, generic)
    await process_form_submission(db, submission)
    await db.flush()

    contacts = list(
        (
            await db.scalars(
                sa.select(Contact).where(
                    Contact.primary_email.in_(["ivan@example.com", "maria@example.com"])
                )
            )
        ).all()
    )
    deals = list(
        (
            await db.scalars(sa.select(Deal).where(Deal.title.in_(["Webhook deal", "Form deal"])))
        ).all()
    )
    routed_events = list(
        (
            await db.scalars(
                sa.select(ActivityEvent)
                .where(ActivityEvent.event_type.in_(["webhook.lead.routed", "form.lead.routed"]))
                .order_by(ActivityEvent.event_type)
            )
        ).all()
    )
    structured_messages = list(
        (
            await db.scalars(
                sa.select(Message)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Conversation.external_thread_id.in_(
                        [f"webhook:{generic.id}", f"form:{submission.id}"]
                    )
                )
                .order_by(Message.body)
            )
        ).all()
    )
    sources = list(
        (await db.scalars(sa.select(Source).where(Source.key.in_(["webhook", "html_form"])))).all()
    )

    assert {contact.primary_email for contact in contacts} == {
        "ivan@example.com",
        "maria@example.com",
    }
    assert {contact.assignee_id for contact in contacts} == {
        integration_domain["user"].id
    }
    assert {deal.title for deal in deals} == {"Webhook deal", "Form deal"}
    assert {event.event_type for event in routed_events} == {
        "webhook.lead.routed",
        "form.lead.routed",
    }
    assert {event.payload["message"] for event in routed_events} == {
        "Сообщение из webhook",
        "Сообщение из формы",
    }
    assert len(structured_messages) == 2
    assert {message.body for message in structured_messages} == {
        "Сообщение из webhook",
        "Сообщение из формы",
    }
    assert {message.metadata_json["origin"] for message in structured_messages} == {
        "webhook",
        "form",
    }
    assert all(message.direction is MessageDirection.inbound for message in structured_messages)
    assert all(message.status is MessageStatus.received for message in structured_messages)
    assert {source.key for source in sources} == {"webhook", "html_form"}
    sources_by_key = {source.key: source.id for source in sources}
    deals_by_title = {deal.title: deal for deal in deals}
    assert deals_by_title["Webhook deal"].source_id == sources_by_key["webhook"]
    assert deals_by_title["Form deal"].source_id == sources_by_key["html_form"]
    message_ids = {str(message.id) for message in structured_messages}
    assert {event.payload["message_id"] for event in routed_events} == message_ids


@pytest.mark.asyncio
async def test_ambiguous_contact_match_is_left_for_review(
    db: AsyncSession,
    integration_domain: dict[str, Any],
) -> None:
    workspace_id = integration_domain["workspace"].id
    contacts = [
        Contact(
            workspace_id=workspace_id,
            first_name="Первый",
            primary_email="shared@example.com",
        ),
        Contact(
            workspace_id=workspace_id,
            first_name="Второй",
            primary_email="shared@example.com",
        ),
    ]
    endpoint = WebhookEndpoint(
        workspace_id=workspace_id,
        slug="ambiguous-runtime-hook",
        name="Ambiguous runtime hook",
        encrypted_secret=b"test",
        pipeline_id=integration_domain["pipeline"].id,
        stage_id=integration_domain["stage"].id,
        assignee_id=integration_domain["user"].id,
        source_id=None,
    )
    db.add_all([*contacts, endpoint])
    await db.flush()
    event = InboundEvent(
        workspace_id=workspace_id,
        source_key=f"generic:{endpoint.id}",
        external_event_id="generic-ambiguous",
        request_digest="d" * 64,
        payload={
            "contact": {"email": "shared@example.com"},
            "deal": {"title": "Needs manual review"},
            "message": {"text": "Не объединять автоматически"},
        },
        status=InboundStatus.accepted,
        received_at=FIXED_NOW,
    )
    db.add(event)
    await db.flush()

    await process_inbound_event(db, event)
    await db.flush()

    deal = await db.scalar(sa.select(Deal).where(Deal.title == "Needs manual review"))
    assert deal is not None
    conversation = await db.scalar(
        sa.select(Conversation).where(Conversation.external_thread_id == f"webhook:{event.id}")
    )
    assert conversation is not None
    linked_contacts = await db.scalar(
        sa.select(sa.func.count()).select_from(DealContact).where(DealContact.deal_id == deal.id)
    )
    routed = await db.scalar(
        sa.select(ActivityEvent).where(
            ActivityEvent.event_type == "webhook.lead.routed",
            ActivityEvent.entity_id == deal.id,
        )
    )

    assert conversation.status is ConversationStatus.review_needed
    assert conversation.contact_id is None
    assert linked_contacts == 0
    assert deal.custom_fields["routing_review_needed"] is True
    assert routed is not None
    assert routed.payload["review_needed"] is True


@pytest.mark.asyncio
async def test_runtime_sends_outbound_message_and_marks_it_sent(
    db: AsyncSession,
    integration_domain: dict[str, Any],
) -> None:
    connection = channel_connection(integration_domain)
    db.add(connection)
    await db.flush()
    conversation = Conversation(
        workspace_id=integration_domain["workspace"].id,
        channel_connection_id=connection.id,
        contact_id=integration_domain["contact"].id,
        deal_id=integration_domain["deal"].id,
        external_thread_id="chat-outbound",
        participant={"recipient_id": "customer-10"},
    )
    db.add(conversation)
    await db.flush()
    previous = Message(
        workspace_id=integration_domain["workspace"].id,
        conversation_id=conversation.id,
        direction=MessageDirection.inbound,
        status=MessageStatus.received,
        provider_message_id="provider-in-1",
        body="Question",
        received_at=FIXED_NOW - timedelta(minutes=1),
    )
    db.add(previous)
    await db.flush()
    outbound = Message(
        workspace_id=integration_domain["workspace"].id,
        conversation_id=conversation.id,
        reply_to_id=previous.id,
        direction=MessageDirection.outbound,
        status=MessageStatus.queued,
        body="Answer",
    )
    db.add(outbound)
    await db.commit()

    adapter = RecordingAdapter()
    handlers = RuntimeHandlers(
        session_factory=SessionLocal,
        adapter_factory=lambda _: adapter,
        now=lambda: FIXED_NOW,
    )
    await handlers.send_message(
        claimed_job(
            "message.send",
            {"message_id": str(outbound.id)},
            workspace_id=outbound.workspace_id,
        )
    )

    await db.refresh(outbound)
    sent_event = await db.scalar(
        sa.select(ActivityEvent).where(
            ActivityEvent.event_type == "message.outbound.sent",
            ActivityEvent.entity_id == outbound.id,
        )
    )

    assert adapter.sent == [
        OutboundMessage(
            thread_id="chat-outbound",
            recipient_id="customer-10",
            text="Answer",
            reply_to_message_id="provider-in-1",
            attachments=(),
        )
    ]
    assert outbound.status is MessageStatus.sent
    assert outbound.provider_message_id == "provider-out-1"
    assert outbound.sent_at == FIXED_NOW.replace(tzinfo=None)
    assert sent_event is not None


@pytest.mark.asyncio
async def test_runtime_materializes_private_attachment_and_retry_is_status_idempotent(
    db: AsyncSession,
    integration_domain: dict[str, Any],
) -> None:
    workspace_id = integration_domain["workspace"].id
    connection = channel_connection(integration_domain)
    db.add(connection)
    await db.flush()
    conversation = Conversation(
        workspace_id=workspace_id,
        channel_connection_id=connection.id,
        contact_id=integration_domain["contact"].id,
        deal_id=integration_domain["deal"].id,
        external_thread_id="chat-with-attachment",
        participant={"recipient_id": "customer-attachment"},
    )
    db.add(conversation)
    await db.flush()
    outbound = Message(
        workspace_id=workspace_id,
        conversation_id=conversation.id,
        direction=MessageDirection.outbound,
        status=MessageStatus.queued,
        body="Proposal",
    )
    db.add(outbound)
    await db.flush()
    content = b"%PDF-1.7\nprivate proposal"
    object_key = f"attachments/{workspace_id}/2026/08/object/proposal.pdf"
    db.add(
        Attachment(
            workspace_id=workspace_id,
            message_id=outbound.id,
            object_key=object_key,
            original_filename="proposal.pdf",
            content_type="application/pdf",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
    )
    await db.commit()

    s3 = RuntimeAttachmentS3(object_key, content)
    adapter = FlakyAttachmentAdapter()
    handlers = RuntimeHandlers(
        session_factory=SessionLocal,
        adapter_factory=lambda _: adapter,
        attachment_storage=AttachmentStorage(s3, bucket="pulse-private"),  # type: ignore[arg-type]
        now=lambda: FIXED_NOW,
    )
    payload = {"message_id": str(outbound.id)}

    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        await handlers.send_message(
            claimed_job(
                JOB_MESSAGE_SEND,
                payload,
                attempts=1,
                max_attempts=3,
                workspace_id=outbound.workspace_id,
            )
        )
    await db.refresh(outbound)
    assert outbound.status is MessageStatus.queued
    assert outbound.last_error is None

    await handlers.send_message(
        claimed_job(
            JOB_MESSAGE_SEND,
            payload,
            attempts=2,
            max_attempts=3,
            workspace_id=outbound.workspace_id,
        )
    )
    await db.refresh(outbound)
    assert outbound.status is MessageStatus.sent
    assert outbound.provider_message_id == "provider-out-attachment"

    # A duplicate/replayed job observes the persisted sent status and performs
    # neither another S3 read nor another provider send.
    await handlers.send_message(
        claimed_job(
            JOB_MESSAGE_SEND,
            payload,
            attempts=3,
            max_attempts=3,
            workspace_id=outbound.workspace_id,
        )
    )
    expected_attachment = OutboundAttachment(
        filename="proposal.pdf",
        content_type="application/pdf",
        content=content,
    )
    assert len(adapter.sent) == 2
    assert adapter.sent[0].attachments == (expected_attachment,)
    assert adapter.sent[1].attachments == (expected_attachment,)
    assert len(s3.gets) == 2
    sent_event_count = await db.scalar(
        sa.select(sa.func.count())
        .select_from(ActivityEvent)
        .where(
            ActivityEvent.event_type == "message.outbound.sent",
            ActivityEvent.entity_id == outbound.id,
        )
    )
    assert sent_event_count == 1


@pytest.mark.asyncio
async def test_runtime_delivers_in_app_notification_and_publishes_realtime_event(
    db: AsyncSession,
    integration_domain: dict[str, Any],
) -> None:
    # SQLite returns DateTime(timezone=True) values without tzinfo, so the
    # fixed runtime clock intentionally mirrors that test-database behavior.
    runtime_now = FIXED_NOW.replace(tzinfo=None)
    delivery = NotificationDelivery(
        workspace_id=integration_domain["workspace"].id,
        audience=NotificationAudience.employee,
        channel="in_app",
        recipient_id=integration_domain["user"].id,
        recipient_address=str(integration_domain["user"].id),
        subject="Новая задача",
        body="Проверьте сделку",
        status=DeliveryStatus.pending,
        dedupe_key="runtime-in-app-1",
        scheduled_at=runtime_now - timedelta(minutes=1),
    )
    db.add(delivery)
    await db.commit()

    handlers = RuntimeHandlers(session_factory=SessionLocal, now=lambda: runtime_now)
    await handlers.deliver_notification(
        claimed_job(
            "notification.deliver",
            {"notification_delivery_id": str(delivery.id)},
            workspace_id=delivery.workspace_id,
        )
    )

    await db.refresh(delivery)
    realtime = await db.scalar(
        sa.select(RealtimeEvent).where(RealtimeEvent.event_type == "notification.delivered")
    )

    assert delivery.status is DeliveryStatus.delivered
    assert delivery.provider_message_id == f"in-app:{delivery.id}"
    assert delivery.delivered_at == runtime_now
    assert delivery.attempts == 1
    assert realtime is not None
    assert realtime.payload == {
        "delivery_id": str(delivery.id),
        "recipient_id": str(integration_domain["user"].id),
    }
    assert (
        await db.scalar(
            sa.select(sa.func.count())
            .select_from(NotificationDelivery)
            .where(NotificationDelivery.channel == "web_push")
        )
        == 0
    )


@pytest.mark.asyncio
async def test_runtime_atomically_mirrors_employee_in_app_delivery_to_web_push(
    db: AsyncSession,
    integration_domain: dict[str, Any],
) -> None:
    runtime_now = FIXED_NOW.replace(tzinfo=None)
    cipher = SecretCipher(key=b"m" * 32, key_id="mirror-test")
    await register_subscription(
        db,
        cipher=cipher,
        workspace_id=integration_domain["workspace"].id,
        user_id=integration_domain["user"].id,
        endpoint="https://fcm.googleapis.com/fcm/send/mirror-device",
        expiration_time=None,
        p256dh="test-public-key",
        auth="test-auth-secret",
        now=FIXED_NOW,
    )
    delivery = NotificationDelivery(
        workspace_id=integration_domain["workspace"].id,
        audience=NotificationAudience.employee,
        channel="in_app",
        recipient_id=integration_domain["user"].id,
        recipient_address=str(integration_domain["user"].id),
        subject="Новая задача",
        body="Проверьте сделку",
        status=DeliveryStatus.pending,
        dedupe_key="runtime-in-app-mirror",
        scheduled_at=runtime_now - timedelta(minutes=1),
    )
    db.add(delivery)
    await db.commit()

    handlers = RuntimeHandlers(session_factory=SessionLocal, now=lambda: runtime_now)
    await handlers.deliver_notification(
        claimed_job(
            "notification.deliver",
            {"notification_delivery_id": str(delivery.id)},
            workspace_id=delivery.workspace_id,
        )
    )

    mirrored = await db.scalar(
        sa.select(NotificationDelivery).where(
            NotificationDelivery.workspace_id == delivery.workspace_id,
            NotificationDelivery.dedupe_key == f"web-push:in-app:{delivery.id}",
        )
    )
    assert mirrored is not None
    assert mirrored.channel == "web_push"
    assert mirrored.status is DeliveryStatus.pending
    assert mirrored.recipient_id == delivery.recipient_id
    queued = await db.scalar(
        sa.select(OutboxEvent).where(
            OutboxEvent.aggregate_type == "notification_delivery",
            OutboxEvent.aggregate_id == mirrored.id,
            OutboxEvent.event_type == "notification.delivery.queued",
        )
    )
    assert queued is not None

    # Replayed delivery jobs observe the committed in-app state, so neither a
    # second mirror nor a second outbox record is created.
    await handlers.deliver_notification(
        claimed_job(
            "notification.deliver",
            {"notification_delivery_id": str(delivery.id)},
            workspace_id=delivery.workspace_id,
        )
    )
    assert (
        await db.scalar(
            sa.select(sa.func.count())
            .select_from(NotificationDelivery)
            .where(
                NotificationDelivery.workspace_id == delivery.workspace_id,
                NotificationDelivery.dedupe_key == f"web-push:in-app:{delivery.id}",
            )
        )
        == 1
    )


@pytest.mark.asyncio
async def test_runtime_routes_web_push_delivery_through_dedicated_sender(
    db: AsyncSession,
    integration_domain: dict[str, Any],
) -> None:
    runtime_now = FIXED_NOW.replace(tzinfo=None)
    delivery = NotificationDelivery(
        workspace_id=integration_domain["workspace"].id,
        audience=NotificationAudience.employee,
        channel="web_push",
        recipient_id=integration_domain["user"].id,
        recipient_address=str(integration_domain["user"].id),
        subject="Тест",
        body="Push",
        status=DeliveryStatus.pending,
        dedupe_key="runtime-web-push",
        scheduled_at=runtime_now - timedelta(minutes=1),
    )
    db.add(delivery)
    await db.commit()
    sent: list[uuid.UUID] = []

    async def send_web_push(item: NotificationDelivery) -> SendResult:
        sent.append(item.id)
        return SendResult(provider_message_id="web-push-provider", sent_at=runtime_now)

    handlers = RuntimeHandlers(
        session_factory=SessionLocal,
        web_push_sender=send_web_push,
        now=lambda: runtime_now,
    )
    await handlers.deliver_notification(
        claimed_job(
            "notification.deliver",
            {"notification_delivery_id": str(delivery.id)},
            workspace_id=delivery.workspace_id,
        )
    )

    await db.refresh(delivery)
    assert sent == [delivery.id]
    assert delivery.status is DeliveryStatus.delivered
    assert delivery.provider_message_id == "web-push-provider"


@pytest.mark.asyncio
async def test_restricted_employee_delivery_requires_owned_target_or_exact_test_push(
    db: AsyncSession,
    integration_domain: dict[str, Any],
) -> None:
    runtime_now = FIXED_NOW.replace(tzinfo=None)
    workspace = integration_domain["workspace"]
    user = integration_domain["user"]
    membership = await db.scalar(
        sa.select(Membership).where(
            Membership.workspace_id == workspace.id,
            Membership.user_id == user.id,
        )
    )
    assert membership is not None
    membership.role = Role.employee
    own_task = Task(
        workspace_id=workspace.id,
        title="Owned notification task",
        status=TaskStatus.open,
        due_at=FIXED_NOW + timedelta(hours=1),
        assignee_id=user.id,
    )
    db.add(own_task)
    await db.flush()
    generic = NotificationDelivery(
        workspace_id=workspace.id,
        audience=NotificationAudience.employee,
        channel="web_push",
        recipient_id=user.id,
        recipient_address=str(user.id),
        subject="Generic",
        body="Targetless",
        status=DeliveryStatus.pending,
        dedupe_key="runtime-employee-targetless",
        scheduled_at=runtime_now - timedelta(minutes=1),
    )
    exact_test = NotificationDelivery(
        workspace_id=workspace.id,
        audience=NotificationAudience.employee,
        channel="web_push",
        recipient_id=user.id,
        recipient_address=str(user.id),
        subject=TEST_PUSH_SUBJECT,
        body=TEST_PUSH_BODY,
        status=DeliveryStatus.pending,
        dedupe_key=f"web-push:test:{workspace.id}:{user.id}:12345",
        scheduled_at=runtime_now - timedelta(minutes=1),
    )
    owned = NotificationDelivery(
        workspace_id=workspace.id,
        audience=NotificationAudience.employee,
        channel="web_push",
        recipient_id=user.id,
        recipient_address=str(user.id),
        subject="Owned deal",
        body="Open it",
        target_entity_type="deal",
        target_entity_id=integration_domain["deal"].id,
        status=DeliveryStatus.pending,
        dedupe_key="runtime-employee-owned",
        scheduled_at=runtime_now - timedelta(minutes=1),
    )
    owned_task = NotificationDelivery(
        workspace_id=workspace.id,
        audience=NotificationAudience.employee,
        channel="web_push",
        recipient_id=user.id,
        recipient_address=str(user.id),
        subject="Owned task",
        body="Open it",
        target_entity_type="task",
        target_entity_id=own_task.id,
        status=DeliveryStatus.pending,
        dedupe_key="runtime-employee-owned-task",
        scheduled_at=runtime_now - timedelta(minutes=1),
    )
    external = NotificationDelivery(
        workspace_id=workspace.id,
        audience=NotificationAudience.employee,
        channel="email",
        recipient_id=user.id,
        recipient_address="unverified-recipient@example.com",
        subject="Spoofed",
        body="Do not send",
        target_entity_type="deal",
        target_entity_id=integration_domain["deal"].id,
        status=DeliveryStatus.pending,
        dedupe_key="runtime-employee-external",
        scheduled_at=runtime_now - timedelta(minutes=1),
    )
    db.add_all([generic, exact_test, owned, owned_task, external])
    await db.commit()
    pushed: list[uuid.UUID] = []
    adapter = RecordingAdapter()

    async def send_web_push(item: NotificationDelivery) -> SendResult:
        pushed.append(item.id)
        return SendResult(provider_message_id=f"push:{item.id}", sent_at=runtime_now)

    handlers = RuntimeHandlers(
        session_factory=SessionLocal,
        notification_adapter_factory=lambda _workspace_id, _channel: adapter,
        web_push_sender=send_web_push,
        now=lambda: runtime_now,
    )
    for delivery in (generic, exact_test, owned, owned_task, external):
        await handlers.deliver_notification(
            claimed_job(
                "notification.deliver",
                {"notification_delivery_id": str(delivery.id)},
                workspace_id=workspace.id,
            )
        )

    for delivery in (generic, exact_test, owned, owned_task, external):
        await db.refresh(delivery)
    assert generic.status is DeliveryStatus.failed
    assert external.status is DeliveryStatus.failed
    assert exact_test.status is DeliveryStatus.delivered
    assert owned.status is DeliveryStatus.delivered
    assert owned_task.status is DeliveryStatus.delivered
    assert pushed == [exact_test.id, owned.id, owned_task.id]
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_restricted_employee_rechecks_ownership_after_delivery_has_started(
    db: AsyncSession,
    integration_domain: dict[str, Any],
) -> None:
    runtime_now = FIXED_NOW.replace(tzinfo=None)
    workspace = integration_domain["workspace"]
    user = integration_domain["user"]
    deal = integration_domain["deal"]
    membership = await db.scalar(
        sa.select(Membership).where(
            Membership.workspace_id == workspace.id,
            Membership.user_id == user.id,
        )
    )
    assert membership is not None
    membership.role = Role.employee
    other_user = User(
        email="race-assignee@example.com",
        full_name="Race Assignee",
        password_hash="not-used-in-service-tests",
    )
    db.add(other_user)
    await db.flush()
    delivery = NotificationDelivery(
        workspace_id=workspace.id,
        audience=NotificationAudience.employee,
        channel="web_push",
        recipient_id=user.id,
        recipient_address=str(user.id),
        subject="Race",
        body="Must not leak",
        target_entity_type="deal",
        target_entity_id=deal.id,
        status=DeliveryStatus.pending,
        dedupe_key="runtime-employee-race",
        scheduled_at=runtime_now - timedelta(minutes=1),
    )
    db.add(delivery)
    await db.commit()

    target_check_reached = asyncio.Event()
    continue_target_check = asyncio.Event()

    class TargetBarrierSession(AsyncSession):
        async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            descriptions = getattr(statement, "column_descriptions", ())
            checks_deal = any(
                item.get("entity") is Deal
                for item in descriptions
                if isinstance(item, dict)
            )
            if checks_deal and not target_check_reached.is_set():
                target_check_reached.set()
                await continue_target_check.wait()
            return await super().scalar(statement, *args, **kwargs)

    barrier_sessions = async_sessionmaker(
        engine,
        class_=TargetBarrierSession,
        expire_on_commit=False,
        autoflush=False,
    )
    pushed: list[uuid.UUID] = []

    async def send_web_push(item: NotificationDelivery) -> SendResult:
        pushed.append(item.id)
        return SendResult(provider_message_id="unexpected", sent_at=runtime_now)

    handlers = RuntimeHandlers(
        session_factory=barrier_sessions,
        web_push_sender=send_web_push,
        now=lambda: runtime_now,
    )
    delivery_task = asyncio.create_task(
        handlers.deliver_notification(
            claimed_job(
                "notification.deliver",
                {"notification_delivery_id": str(delivery.id)},
                workspace_id=workspace.id,
            )
        )
    )
    try:
        await asyncio.wait_for(target_check_reached.wait(), timeout=2)
        async with SessionLocal() as reassignment_session:
            async with reassignment_session.begin():
                reassigned = await reassignment_session.get(Deal, deal.id)
                assert reassigned is not None
                reassigned.assignee_id = other_user.id
        continue_target_check.set()
        await asyncio.wait_for(delivery_task, timeout=2)
    finally:
        continue_target_check.set()
        if not delivery_task.done():
            delivery_task.cancel()

    await db.refresh(delivery)
    assert pushed == []
    assert delivery.status is DeliveryStatus.failed
    assert delivery.last_error is not None
    assert "suppressed" in delivery.last_error

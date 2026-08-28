from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.integrations.channels.base import (
    AdapterHealth,
    AttachmentReference,
    NormalizedInboundMessage,
    OutboundAttachment,
    OutboundMessage,
    SendResult,
)
from app.integrations.inbound import process_form_submission, process_inbound_event
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
    WebhookEndpoint,
)
from app.integrations.runtime import (
    JOB_INBOUND_PROCESS,
    JOB_MESSAGE_SEND,
    JOB_RUNTIME_CLEANUP,
    JOB_RUNTIME_RECOVER,
    RUNTIME_JOB_TYPES,
    RuntimeHandlers,
    RuntimeScheduler,
)
from app.integrations.s3 import AttachmentStorage
from app.models import (
    ActivityEvent,
    BackgroundJob,
    Contact,
    Deal,
    DealContact,
    DeliveryStatus,
    OutboxEvent,
    RealtimeEvent,
    Source,
)
from app.services.jobs import ClaimedJob

FIXED_NOW = datetime(2026, 8, 27, 12, tzinfo=UTC)


def claimed_job(
    job_type: str,
    payload: dict[str, Any],
    *,
    attempts: int = 1,
    max_attempts: int = 5,
) -> ClaimedJob:
    return ClaimedJob(
        id=uuid.uuid4(),
        job_type=job_type,
        payload=payload,
        attempts=attempts,
        max_attempts=max_attempts,
        lease_owner="runtime-test",
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
    job = claimed_job("outbox.dispatch", {"outbox_event_id": str(event.id)})

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
    await handlers.send_message(claimed_job("message.send", {"message_id": str(outbound.id)}))

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
            claimed_job(JOB_MESSAGE_SEND, payload, attempts=1, max_attempts=3)
        )
    await db.refresh(outbound)
    assert outbound.status is MessageStatus.queued
    assert outbound.last_error is None

    await handlers.send_message(claimed_job(JOB_MESSAGE_SEND, payload, attempts=2, max_attempts=3))
    await db.refresh(outbound)
    assert outbound.status is MessageStatus.sent
    assert outbound.provider_message_id == "provider-out-attachment"

    # A duplicate/replayed job observes the persisted sent status and performs
    # neither another S3 read nor another provider send.
    await handlers.send_message(claimed_job(JOB_MESSAGE_SEND, payload, attempts=3, max_attempts=3))
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
        "subject": "Новая задача",
        "body": "Проверьте сделку",
    }

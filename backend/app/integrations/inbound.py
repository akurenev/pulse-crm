"""Idempotent inbound and HTML-form routing into the CRM domain."""

from __future__ import annotations

import inspect
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.channels.base import ChannelAdapter, NormalizedInboundMessage
from app.integrations.identity import (
    IdentityNormalizationError,
    MatchKind,
    connection_scope,
    match_contact_point,
    match_external_identity,
    normalize_email_address,
    normalize_phone_number,
)
from app.integrations.models import (
    ChannelConnection,
    ChannelKind,
    ConnectionStatus,
    ContactPoint,
    ContactPointKind,
    Conversation,
    ConversationStatus,
    ExternalIdentity,
    Form,
    FormSubmission,
    InboundEvent,
    Message,
    MessageDirection,
    MessageStatus,
    WebhookEndpoint,
)
from app.models import (
    Contact,
    Deal,
    DealContact,
    DealStageHistory,
    Source,
    Stage,
    StageType,
)
from app.services.events import record_domain_event

type AdapterFactory = Callable[[ChannelConnection], ChannelAdapter | Awaitable[ChannelAdapter]]


class InboundRoutingError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ContactResolution:
    contact: Contact | None
    ambiguous: bool
    created: bool


@dataclass(frozen=True, slots=True)
class LeadRoute:
    pipeline_id: uuid.UUID
    stage_id: uuid.UUID
    assignee_id: uuid.UUID | None
    source_id: uuid.UUID | None


async def _resolve[T](value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


async def process_inbound_event(
    session: AsyncSession,
    event: InboundEvent,
    *,
    adapter_factory: AdapterFactory | None = None,
) -> None:
    """Route one accepted provider or generic webhook event.

    The caller owns the transaction and the event row lock. Re-entry is safe:
    provider messages are unique per conversation and accepted events are
    themselves deduplicated before this function is called.
    """

    if event.channel_connection_id is not None:
        if adapter_factory is None:
            raise InboundRoutingError("connection adapter factory is not configured")
        connection = await session.scalar(
            sa.select(ChannelConnection).where(
                ChannelConnection.id == event.channel_connection_id,
                ChannelConnection.workspace_id == event.workspace_id,
            )
        )
        if connection is None:
            raise InboundRoutingError("inbound channel connection not found")
        adapter = await _resolve(adapter_factory(connection))
        normalized = adapter.normalize_inbound(event.payload)
        await process_normalized_channel_message(
            session,
            event=event,
            connection=connection,
            normalized=normalized,
        )
        return

    if event.source_key.startswith("generic:"):
        try:
            endpoint_id = uuid.UUID(event.source_key.partition(":")[2])
        except ValueError as exc:
            raise InboundRoutingError("generic webhook source key is invalid") from exc
        endpoint = await session.scalar(
            sa.select(WebhookEndpoint).where(
                WebhookEndpoint.id == endpoint_id,
                WebhookEndpoint.workspace_id == event.workspace_id,
            )
        )
        if endpoint is None:
            raise InboundRoutingError("generic webhook endpoint not found")
        route = LeadRoute(
            pipeline_id=endpoint.pipeline_id,
            stage_id=endpoint.stage_id,
            assignee_id=endpoint.assignee_id,
            source_id=endpoint.source_id,
        )
        await _route_structured_lead(
            session,
            workspace_id=event.workspace_id,
            route=route,
            payload=event.payload,
            origin_type="webhook",
            origin_id=event.id,
        )
        return

    raise InboundRoutingError(f"unsupported inbound source {event.source_key!r}")


async def process_normalized_channel_message(
    session: AsyncSession,
    *,
    event: InboundEvent,
    connection: ChannelConnection,
    normalized: NormalizedInboundMessage,
) -> Message:
    """Persist a normalized provider message, returning its durable row.

    IMAP polling uses this entrypoint because raw RFC822 messages and their
    attachment bytes should not be copied into PostgreSQL JSON payloads.
    """

    if event.workspace_id != connection.workspace_id:
        raise PermissionError("inbound connection belongs to another workspace")
    return await _route_channel_message(session, event, connection, normalized)


async def process_form_submission(
    session: AsyncSession,
    submission: FormSubmission,
) -> None:
    form = await session.scalar(
        sa.select(Form).where(
            Form.id == submission.form_id,
            Form.workspace_id == submission.workspace_id,
        )
    )
    if form is None:
        raise InboundRoutingError("form configuration not found")
    await _route_structured_lead(
        session,
        workspace_id=submission.workspace_id,
        route=LeadRoute(
            pipeline_id=form.pipeline_id,
            stage_id=form.stage_id,
            assignee_id=form.assignee_id,
            source_id=form.source_id,
        ),
        payload={"contact": submission.payload, "deal": submission.payload},
        origin_type="form",
        origin_id=submission.id,
    )


async def _route_channel_message(
    session: AsyncSession,
    event: InboundEvent,
    connection: ChannelConnection,
    normalized: NormalizedInboundMessage,
) -> Message:
    route = await _connection_route(session, connection)
    conversation = await session.scalar(
        sa.select(Conversation).where(
            Conversation.workspace_id == event.workspace_id,
            Conversation.channel_connection_id == connection.id,
            Conversation.external_thread_id == normalized.thread_id,
        )
    )
    if conversation is not None:
        duplicate = await session.scalar(
            sa.select(Message).where(
                Message.conversation_id == conversation.id,
                Message.provider_message_id == normalized.message_id,
            )
        )
        if duplicate is not None:
            return duplicate
        contact = (
            await session.get(Contact, conversation.contact_id) if conversation.contact_id else None
        )
        if conversation.status is ConversationStatus.closed:
            conversation.status = (
                ConversationStatus.active if contact else ConversationStatus.review_needed
            )
            conversation.version += 1
    else:
        identity = await match_external_identity(
            session,
            workspace_id=event.workspace_id,
            provider=normalized.provider,
            external_user_id=normalized.sender_id,
            channel_connection_id=connection.id,
        )
        contact = None
        ambiguous = identity.kind is MatchKind.ambiguous
        created_contact = False
        if identity.kind is MatchKind.unique:
            contact_id = identity.contact_id
            if contact_id is not None:
                contact = await session.get(Contact, contact_id)
        elif identity.kind is MatchKind.none:
            fallback = await _match_provider_contact(session, event.workspace_id, normalized)
            contact = fallback.contact
            ambiguous = fallback.ambiguous
            if contact is None and not ambiguous:
                contact = _new_provider_contact(event.workspace_id, normalized)
                session.add(contact)
                await session.flush()
                created_contact = True
            if contact is not None:
                session.add(
                    ExternalIdentity(
                        workspace_id=event.workspace_id,
                        contact_id=contact.id,
                        channel_connection_id=connection.id,
                        provider=normalized.provider,
                        connection_scope=connection_scope(connection.id),
                        external_user_id=normalized.sender_id,
                        display_name=normalized.sender_display_name,
                        profile=dict(normalized.metadata),
                    )
                )
            if created_contact and contact is not None and normalized.provider == "email":
                await _add_contact_point(
                    session,
                    workspace_id=event.workspace_id,
                    contact=contact,
                    kind=ContactPointKind.email,
                    value=normalized.sender_id,
                )
        deal = await _find_open_deal(
            session,
            workspace_id=event.workspace_id,
            contact_id=contact.id if contact else None,
            pipeline_id=route.pipeline_id,
        )
        if deal is None:
            deal = await _create_deal(
                session,
                workspace_id=event.workspace_id,
                route=route,
                title=_provider_deal_title(normalized),
                contact=contact,
                custom_fields={"routing_review_needed": ambiguous},
            )
        conversation = Conversation(
            workspace_id=event.workspace_id,
            channel_connection_id=connection.id,
            contact_id=contact.id if contact else None,
            deal_id=deal.id,
            external_thread_id=normalized.thread_id,
            status=(ConversationStatus.review_needed if ambiguous else ConversationStatus.active),
            participant={
                "recipient_id": normalized.sender_id,
                "display_name": normalized.sender_display_name,
            },
            last_message_at=normalized.occurred_at,
        )
        session.add(conversation)
        await session.flush()
        if created_contact and contact is not None:
            record_domain_event(
                session,
                workspace_id=event.workspace_id,
                event_type="contact.created.from_inbound",
                entity_type="contact",
                entity_id=contact.id,
                actor_id=None,
                payload={"provider": normalized.provider},
            )

    deal = await _open_conversation_deal(session, conversation, route, contact, normalized)
    reply_to_id = None
    if normalized.reply_to_message_id:
        reply_to_id = await session.scalar(
            sa.select(Message.id).where(
                Message.conversation_id == conversation.id,
                Message.provider_message_id == normalized.reply_to_message_id,
            )
        )
    message = Message(
        workspace_id=event.workspace_id,
        conversation_id=conversation.id,
        reply_to_id=reply_to_id,
        direction=MessageDirection.inbound,
        status=MessageStatus.received,
        provider_message_id=normalized.message_id,
        sender_external_id=normalized.sender_id,
        body=normalized.text,
        metadata_json={
            **dict(normalized.metadata),
            "attachments": [
                {
                    "provider_file_id": attachment.provider_file_id,
                    "filename": attachment.filename,
                    "content_type": attachment.content_type,
                    "size_bytes": attachment.size_bytes,
                }
                for attachment in normalized.attachments
            ],
        },
        received_at=normalized.occurred_at,
    )
    session.add(message)
    await session.flush()
    conversation.last_message_at = normalized.occurred_at
    conversation.version += 1
    deal.last_activity_at = normalized.occurred_at
    deal.version += 1
    record_domain_event(
        session,
        workspace_id=event.workspace_id,
        event_type="message.inbound.received",
        entity_type="message",
        entity_id=message.id,
        actor_id=None,
        payload={
            "deal_id": str(deal.id),
            "contact_id": str(contact.id) if contact else None,
            "conversation_id": str(conversation.id),
            "provider": normalized.provider,
            "review_needed": conversation.status is ConversationStatus.review_needed,
        },
    )
    return message


async def _open_conversation_deal(
    session: AsyncSession,
    conversation: Conversation,
    route: LeadRoute,
    contact: Contact | None,
    normalized: NormalizedInboundMessage,
) -> Deal:
    deal = await session.scalar(
        sa.select(Deal)
        .join(Stage, Stage.id == Deal.stage_id)
        .where(
            Deal.id == conversation.deal_id,
            Deal.workspace_id == conversation.workspace_id,
            Deal.deleted_at.is_(None),
            Stage.stage_type == StageType.open,
        )
    )
    if deal is not None:
        return deal
    deal = await _create_deal(
        session,
        workspace_id=conversation.workspace_id,
        route=route,
        title=_provider_deal_title(normalized),
        contact=contact,
        custom_fields={
            "routing_review_needed": conversation.status is ConversationStatus.review_needed
        },
    )
    conversation.deal_id = deal.id
    conversation.version += 1
    return deal


async def _connection_route(session: AsyncSession, connection: ChannelConnection) -> LeadRoute:
    if connection.default_pipeline_id is None or connection.default_stage_id is None:
        raise InboundRoutingError("channel connection has no default pipeline and stage")
    stage_id = await session.scalar(
        sa.select(Stage.id).where(
            Stage.id == connection.default_stage_id,
            Stage.pipeline_id == connection.default_pipeline_id,
            Stage.workspace_id == connection.workspace_id,
        )
    )
    if stage_id is None:
        raise InboundRoutingError("channel default stage does not belong to its pipeline")
    source_id = await _ensure_source(
        session,
        workspace_id=connection.workspace_id,
        configured_source_id=None,
        key=connection.kind.value,
        name={
            ChannelKind.email: "Email",
            ChannelKind.telegram: "Telegram",
            ChannelKind.max: "MAX",
        }[connection.kind],
    )
    return LeadRoute(
        pipeline_id=connection.default_pipeline_id,
        stage_id=connection.default_stage_id,
        assignee_id=connection.default_assignee_id,
        source_id=source_id,
    )


def _new_provider_contact(workspace_id: uuid.UUID, normalized: NormalizedInboundMessage) -> Contact:
    display_name = (normalized.sender_display_name or normalized.sender_id).strip()
    first_name, _, last_name = display_name.partition(" ")
    return Contact(
        workspace_id=workspace_id,
        first_name=first_name[:120] or "Новый клиент",
        last_name=last_name[:120],
        primary_email=(normalized.sender_id if normalized.provider == "email" else None),
    )


def _provider_deal_title(normalized: NormalizedInboundMessage) -> str:
    subject = normalized.metadata.get("subject")
    if isinstance(subject, str) and subject.strip():
        return subject.strip()[:240]
    name = normalized.sender_display_name or normalized.sender_id
    return f"Обращение: {name}"[:240]


async def _match_provider_contact(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    normalized: NormalizedInboundMessage,
) -> ContactResolution:
    """Fall back to normalized address matching when no provider identity exists."""

    if normalized.provider != "email":
        return ContactResolution(contact=None, ambiguous=False, created=False)
    try:
        normalized_email = normalize_email_address(normalized.sender_id)
        match = await match_contact_point(
            session,
            workspace_id=workspace_id,
            kind=ContactPointKind.email,
            value=normalized.sender_id,
        )
    except IdentityNormalizationError:
        return ContactResolution(contact=None, ambiguous=False, created=False)
    candidate_ids = set(match.contact_ids)
    candidate_ids.update(
        (
            await session.scalars(
                sa.select(Contact.id).where(
                    Contact.workspace_id == workspace_id,
                    Contact.deleted_at.is_(None),
                    sa.func.lower(Contact.primary_email) == normalized_email,
                )
            )
        ).all()
    )
    if len(candidate_ids) > 1:
        return ContactResolution(contact=None, ambiguous=True, created=False)
    if candidate_ids:
        contact = await session.get(Contact, next(iter(candidate_ids)))
        return ContactResolution(contact=contact, ambiguous=False, created=False)
    return ContactResolution(contact=None, ambiguous=False, created=False)


async def _route_structured_lead(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    route: LeadRoute,
    payload: Mapping[str, Any],
    origin_type: str,
    origin_id: uuid.UUID,
) -> None:
    await _validate_route(session, workspace_id, route)
    route = LeadRoute(
        pipeline_id=route.pipeline_id,
        stage_id=route.stage_id,
        assignee_id=route.assignee_id,
        source_id=await _ensure_source(
            session,
            workspace_id=workspace_id,
            configured_source_id=route.source_id,
            key="html_form" if origin_type == "form" else "webhook",
            name="HTML-форма" if origin_type == "form" else "Webhook",
        ),
    )
    contact_payload = _mapping(payload.get("contact"))
    deal_payload = _mapping(payload.get("deal"))
    message_payload = _mapping(payload.get("message"))
    resolution = await _resolve_structured_contact(session, workspace_id, contact_payload)
    deal = await _find_open_deal(
        session,
        workspace_id=workspace_id,
        contact_id=resolution.contact.id if resolution.contact else None,
        pipeline_id=route.pipeline_id,
    )
    created_deal = deal is None
    if deal is None:
        custom_fields = dict(_mapping(payload.get("custom_fields")))
        custom_fields.update(dict(_mapping(deal_payload.get("custom_fields"))))
        if resolution.ambiguous:
            custom_fields["routing_review_needed"] = True
        amount = _optional_decimal(deal_payload.get("amount"))
        deal = await _create_deal(
            session,
            workspace_id=workspace_id,
            route=route,
            title=_structured_deal_title(deal_payload, contact_payload, origin_type),
            contact=resolution.contact,
            custom_fields=custom_fields,
            amount=amount,
            currency=str(deal_payload.get("currency") or "RUB")[:3].upper(),
        )
    else:
        deal.last_activity_at = datetime.now(UTC)
        deal.version += 1

    message_text = str(
        message_payload.get("text")
        or message_payload.get("body")
        or deal_payload.get("message")
        or ""
    ).strip()
    message = await _create_structured_message(
        session,
        workspace_id=workspace_id,
        route=route,
        contact=resolution.contact,
        deal=deal,
        payload=payload,
        message_text=message_text,
        origin_type=origin_type,
        origin_id=origin_id,
        review_needed=resolution.ambiguous,
    )
    event_type = f"{origin_type}.lead.routed"
    record_domain_event(
        session,
        workspace_id=workspace_id,
        event_type=event_type,
        entity_type="deal",
        entity_id=deal.id,
        actor_id=None,
        payload={
            "origin_id": str(origin_id),
            "contact_id": str(resolution.contact.id) if resolution.contact else None,
            "deal_id": str(deal.id),
            "pipeline_id": str(route.pipeline_id),
            "stage_id": str(route.stage_id),
            "source_id": str(route.source_id) if route.source_id else None,
            "created_contact": resolution.created,
            "created_deal": created_deal,
            "review_needed": resolution.ambiguous,
            "message_id": str(message.id),
            "message": message_text[:10_000] or None,
        },
    )


async def _create_structured_message(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    route: LeadRoute,
    contact: Contact | None,
    deal: Deal,
    payload: Mapping[str, Any],
    message_text: str,
    origin_type: str,
    origin_id: uuid.UUID,
    review_needed: bool,
) -> Message:
    """Persist form/webhook text through the existing conversation model.

    The current schema requires every conversation to reference a
    ``ChannelConnection``. A disabled, hidden internal connection preserves
    the same timeline model for form/webhook messages and can never send data.
    """

    connection = await _ensure_synthetic_connection(
        session,
        workspace_id=workspace_id,
        route=route,
        origin_type=origin_type,
    )
    thread_id = f"{origin_type}:{origin_id}"
    conversation = await session.scalar(
        sa.select(Conversation).where(
            Conversation.workspace_id == workspace_id,
            Conversation.channel_connection_id == connection.id,
            Conversation.external_thread_id == thread_id,
        )
    )
    if conversation is None:
        conversation = Conversation(
            workspace_id=workspace_id,
            channel_connection_id=connection.id,
            contact_id=contact.id if contact else None,
            deal_id=deal.id,
            external_thread_id=thread_id,
            status=(
                ConversationStatus.review_needed if review_needed else ConversationStatus.active
            ),
            participant={
                "transport": origin_type,
                "email": contact.primary_email if contact else None,
                "phone": contact.primary_phone if contact else None,
            },
            last_message_at=datetime.now(UTC),
        )
        session.add(conversation)
        await session.flush()
    else:
        duplicate = await session.scalar(
            sa.select(Message).where(
                Message.conversation_id == conversation.id,
                Message.provider_message_id == str(origin_id),
            )
        )
        if duplicate is not None:
            return duplicate
        conversation.contact_id = contact.id if contact else None
        conversation.deal_id = deal.id
        conversation.version += 1

    body = message_text or json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    received_at = datetime.now(UTC)
    message = Message(
        workspace_id=workspace_id,
        conversation_id=conversation.id,
        direction=MessageDirection.inbound,
        status=MessageStatus.received,
        provider_message_id=str(origin_id),
        sender_external_id=None,
        body=body[:100_000],
        metadata_json={"origin": origin_type, "origin_id": str(origin_id)},
        received_at=received_at,
    )
    session.add(message)
    await session.flush()
    conversation.last_message_at = received_at
    conversation.version += 1
    deal.last_activity_at = received_at
    deal.version += 1
    record_domain_event(
        session,
        workspace_id=workspace_id,
        event_type=f"message.inbound.{origin_type}",
        entity_type="message",
        entity_id=message.id,
        actor_id=None,
        payload={
            "deal_id": str(deal.id),
            "contact_id": str(contact.id) if contact else None,
            "conversation_id": str(conversation.id),
            "review_needed": review_needed,
        },
    )
    return message


async def _ensure_synthetic_connection(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    route: LeadRoute,
    origin_type: str,
) -> ChannelConnection:
    name = f"__pulse_internal_{origin_type}_{route.pipeline_id}_{route.stage_id}"
    connection_id = uuid.uuid4()
    statement = (
        _insert_for(session, ChannelConnection)
        .values(
            id=connection_id,
            workspace_id=workspace_id,
            kind=ChannelKind.internal,
            name=name,
            status=ConnectionStatus.disabled,
            settings={"internal": True, "transport": origin_type},
            default_pipeline_id=route.pipeline_id,
            default_stage_id=route.stage_id,
            default_assignee_id=route.assignee_id,
            version=1,
        )
        .on_conflict_do_nothing(
            index_elements=[
                ChannelConnection.workspace_id,
                ChannelConnection.kind,
                ChannelConnection.name,
            ]
        )
        .returning(ChannelConnection.id)
    )
    inserted_id = (await session.execute(statement)).scalar_one_or_none()
    resolved_id = inserted_id or await session.scalar(
        sa.select(ChannelConnection.id).where(
            ChannelConnection.workspace_id == workspace_id,
            ChannelConnection.kind == ChannelKind.internal,
            ChannelConnection.name == name,
        )
    )
    if resolved_id is None:  # pragma: no cover - database constraint guard
        raise RuntimeError("synthetic inbound connection could not be loaded")
    connection = await session.get(ChannelConnection, resolved_id)
    if connection is None:  # pragma: no cover - database constraint guard
        raise RuntimeError("synthetic inbound connection disappeared")
    return connection


async def _validate_route(session: AsyncSession, workspace_id: uuid.UUID, route: LeadRoute) -> None:
    valid = await session.scalar(
        sa.select(Stage.id).where(
            Stage.id == route.stage_id,
            Stage.pipeline_id == route.pipeline_id,
            Stage.workspace_id == workspace_id,
        )
    )
    if valid is None:
        raise InboundRoutingError("lead target stage does not belong to its pipeline")


async def _ensure_source(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    configured_source_id: uuid.UUID | None,
    key: str,
    name: str,
) -> uuid.UUID:
    if configured_source_id is not None:
        source_id = await session.scalar(
            sa.select(Source.id).where(
                Source.id == configured_source_id,
                Source.workspace_id == workspace_id,
            )
        )
        if source_id is None:
            raise InboundRoutingError("configured source belongs to another workspace")
        return source_id

    new_source_id = uuid.uuid4()
    statement = (
        _insert_for(session, Source)
        .values(
            id=new_source_id,
            workspace_id=workspace_id,
            key=key,
            name=name,
            is_active=True,
        )
        .on_conflict_do_nothing(index_elements=[Source.workspace_id, Source.key])
        .returning(Source.id)
    )
    inserted_id = (await session.execute(statement)).scalar_one_or_none()
    source_id = inserted_id or await session.scalar(
        sa.select(Source.id).where(
            Source.workspace_id == workspace_id,
            Source.key == key,
        )
    )
    if source_id is None:  # pragma: no cover - database constraint guard
        raise RuntimeError("inbound source could not be loaded")
    return cast(uuid.UUID, source_id)


def _insert_for(session: AsyncSession, model: type[Any]) -> Any:
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return pg_insert(model)
    if dialect == "sqlite":
        return sqlite_insert(model)
    raise RuntimeError(f"unsupported database dialect for inbound routing: {dialect}")


async def _resolve_structured_contact(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    payload: Mapping[str, Any],
) -> ContactResolution:
    email = str(payload.get("email") or payload.get("primary_email") or "").strip()
    phone = str(payload.get("phone") or payload.get("primary_phone") or "").strip()
    candidate_ids: set[uuid.UUID] = set()
    if email:
        try:
            match = await match_contact_point(
                session,
                workspace_id=workspace_id,
                kind=ContactPointKind.email,
                value=email,
            )
            candidate_ids.update(match.contact_ids)
            normalized_email = normalize_email_address(email)
            candidate_ids.update(
                (
                    await session.scalars(
                        sa.select(Contact.id).where(
                            Contact.workspace_id == workspace_id,
                            Contact.deleted_at.is_(None),
                            sa.func.lower(Contact.primary_email) == normalized_email,
                        )
                    )
                ).all()
            )
        except IdentityNormalizationError:
            email = ""
    if phone:
        try:
            match = await match_contact_point(
                session,
                workspace_id=workspace_id,
                kind=ContactPointKind.phone,
                value=phone,
            )
            candidate_ids.update(match.contact_ids)
        except IdentityNormalizationError:
            phone = ""

    if len(candidate_ids) > 1:
        return ContactResolution(contact=None, ambiguous=True, created=False)
    if candidate_ids:
        contact = await session.get(Contact, next(iter(candidate_ids)))
        if contact is not None:
            return ContactResolution(contact=contact, ambiguous=False, created=False)

    first_name, last_name = _structured_name(payload)
    contact = Contact(
        workspace_id=workspace_id,
        first_name=first_name,
        last_name=last_name,
        primary_email=email or None,
        primary_phone=phone or None,
        emails=[email] if email else [],
        phones=[phone] if phone else [],
        custom_fields=dict(_mapping(payload.get("custom_fields"))),
    )
    session.add(contact)
    await session.flush()
    if email:
        await _add_contact_point(
            session,
            workspace_id=workspace_id,
            contact=contact,
            kind=ContactPointKind.email,
            value=email,
        )
    if phone:
        await _add_contact_point(
            session,
            workspace_id=workspace_id,
            contact=contact,
            kind=ContactPointKind.phone,
            value=phone,
        )
    record_domain_event(
        session,
        workspace_id=workspace_id,
        event_type="contact.created.from_inbound",
        entity_type="contact",
        entity_id=contact.id,
        actor_id=None,
    )
    return ContactResolution(contact=contact, ambiguous=False, created=True)


async def _add_contact_point(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    contact: Contact,
    kind: ContactPointKind,
    value: str,
) -> None:
    normalized = (
        normalize_email_address(value)
        if kind is ContactPointKind.email
        else normalize_phone_number(value)
    )
    existing = await session.scalar(
        sa.select(ContactPoint.id).where(
            ContactPoint.contact_id == contact.id,
            ContactPoint.kind == kind,
            ContactPoint.normalized_value == normalized,
        )
    )
    if existing is None:
        session.add(
            ContactPoint(
                workspace_id=workspace_id,
                contact_id=contact.id,
                kind=kind,
                value=value,
                normalized_value=normalized,
                is_primary=True,
            )
        )


async def _find_open_deal(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    contact_id: uuid.UUID | None,
    pipeline_id: uuid.UUID,
) -> Deal | None:
    if contact_id is None:
        return None
    return cast(
        Deal | None,
        await session.scalar(
            sa.select(Deal)
            .join(DealContact, DealContact.deal_id == Deal.id)
            .join(Stage, Stage.id == Deal.stage_id)
            .where(
                Deal.workspace_id == workspace_id,
                Deal.pipeline_id == pipeline_id,
                Deal.deleted_at.is_(None),
                DealContact.contact_id == contact_id,
                Stage.stage_type == StageType.open,
            )
            .order_by(Deal.last_activity_at.desc(), Deal.created_at.desc())
            .limit(1)
        ),
    )


async def _create_deal(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    route: LeadRoute,
    title: str,
    contact: Contact | None,
    custom_fields: dict[str, Any],
    amount: Decimal | None = None,
    currency: str = "RUB",
) -> Deal:
    deal = Deal(
        workspace_id=workspace_id,
        pipeline_id=route.pipeline_id,
        stage_id=route.stage_id,
        assignee_id=route.assignee_id,
        source_id=route.source_id,
        title=title[:240],
        amount=amount,
        currency=currency if len(currency) == 3 else "RUB",
        custom_fields=custom_fields,
    )
    session.add(deal)
    await session.flush()
    if contact is not None:
        session.add(
            DealContact(
                workspace_id=workspace_id,
                deal_id=deal.id,
                contact_id=contact.id,
                is_primary=True,
            )
        )
    session.add(
        DealStageHistory(
            workspace_id=workspace_id,
            deal_id=deal.id,
            to_stage_id=route.stage_id,
            actor_id=None,
        )
    )
    record_domain_event(
        session,
        workspace_id=workspace_id,
        event_type="lead.created",
        entity_type="deal",
        entity_id=deal.id,
        actor_id=None,
        payload={
            "pipeline_id": str(route.pipeline_id),
            "stage_id": str(route.stage_id),
            "source_id": str(route.source_id) if route.source_id else None,
        },
    )
    return deal


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _structured_name(payload: Mapping[str, Any]) -> tuple[str, str]:
    first_name = str(payload.get("first_name") or "").strip()
    last_name = str(payload.get("last_name") or "").strip()
    if not first_name:
        full_name = str(payload.get("name") or payload.get("full_name") or "").strip()
        first_name, _, inferred_last_name = full_name.partition(" ")
        last_name = last_name or inferred_last_name
    return (first_name[:120] or "Новый клиент", last_name[:120])


def _structured_deal_title(
    deal: Mapping[str, Any], contact: Mapping[str, Any], origin_type: str
) -> str:
    title = str(deal.get("title") or deal.get("name") or "").strip()
    if title:
        return title[:240]
    first_name, last_name = _structured_name(contact)
    return f"{origin_type.capitalize()}: {first_name} {last_name}".strip()[:240]


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result >= 0 else None


__all__ = [
    "AdapterFactory",
    "ContactResolution",
    "InboundRoutingError",
    "LeadRoute",
    "process_form_submission",
    "process_inbound_event",
]

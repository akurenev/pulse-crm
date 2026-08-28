"""Workspace-scoped conversation reads and durable outbound message queueing."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.models import (
    ChannelConnection,
    ConnectionStatus,
    Conversation,
    ConversationStatus,
    Message,
    MessageDirection,
    MessageStatus,
)
from app.models import Deal
from app.services.events import record_domain_event


class MessagingNotFoundError(LookupError):
    pass


class OriginConversationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ConversationRoute:
    conversation: Conversation
    connection: ChannelConnection


async def ensure_deal(
    session: AsyncSession, *, workspace_id: uuid.UUID, deal_id: uuid.UUID
) -> Deal:
    deal = await session.scalar(
        sa.select(Deal).where(
            Deal.id == deal_id,
            Deal.workspace_id == workspace_id,
            Deal.deleted_at.is_(None),
        )
    )
    if deal is None:
        raise MessagingNotFoundError("deal not found")
    return deal


async def resolve_origin_conversation(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    deal_id: uuid.UUID,
    conversation_id: uuid.UUID | None = None,
) -> ConversationRoute:
    query = (
        sa.select(Conversation, ChannelConnection)
        .join(ChannelConnection, ChannelConnection.id == Conversation.channel_connection_id)
        .where(
            Conversation.workspace_id == workspace_id,
            Conversation.deal_id == deal_id,
            Conversation.status == ConversationStatus.active,
            ChannelConnection.workspace_id == workspace_id,
            ChannelConnection.status == ConnectionStatus.active,
        )
    )
    if conversation_id is not None:
        query = query.where(Conversation.id == conversation_id)
    query = query.order_by(
        Conversation.last_message_at.desc().nullslast(), Conversation.updated_at.desc()
    ).limit(1)
    row = (await session.execute(query)).one_or_none()
    if row is None:
        raise OriginConversationError("deal has no active origin-channel conversation")
    conversation, connection = row
    return ConversationRoute(conversation=conversation, connection=connection)


async def queue_outbound_message(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    deal_id: uuid.UUID,
    actor_id: uuid.UUID,
    body: str,
    conversation_id: uuid.UUID | None = None,
) -> tuple[Message, ChannelConnection]:
    """Queue a reply through the deal's original active channel."""

    normalized_body = body.strip()
    if not normalized_body or len(normalized_body) > 10_000:
        raise ValueError("message body must contain between 1 and 10000 characters")
    await ensure_deal(session, workspace_id=workspace_id, deal_id=deal_id)
    route = await resolve_origin_conversation(
        session,
        workspace_id=workspace_id,
        deal_id=deal_id,
        conversation_id=conversation_id,
    )
    created_at = datetime.now(UTC)
    message = Message(
        workspace_id=workspace_id,
        conversation_id=route.conversation.id,
        direction=MessageDirection.outbound,
        status=MessageStatus.queued,
        body=normalized_body,
        metadata_json={"channel": route.connection.kind.value},
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(message)
    await session.flush()
    route.conversation.last_message_at = created_at
    route.conversation.version += 1
    record_domain_event(
        session,
        workspace_id=workspace_id,
        event_type="message.outbound.queued",
        entity_type="message",
        entity_id=message.id,
        actor_id=actor_id,
        payload={
            "conversation_id": str(route.conversation.id),
            "deal_id": str(deal_id),
            "channel_connection_id": str(route.connection.id),
            "channel": route.connection.kind.value,
        },
    )
    return message, route.connection


async def list_deal_messages(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    deal_id: uuid.UUID,
    limit: int,
    before_created_at: datetime | None = None,
    before_id: uuid.UUID | None = None,
) -> list[tuple[Message, ChannelConnection]]:
    await ensure_deal(session, workspace_id=workspace_id, deal_id=deal_id)
    query = (
        sa.select(Message, ChannelConnection)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(ChannelConnection, ChannelConnection.id == Conversation.channel_connection_id)
        .where(
            Message.workspace_id == workspace_id,
            Conversation.workspace_id == workspace_id,
            Conversation.deal_id == deal_id,
            ChannelConnection.workspace_id == workspace_id,
        )
    )
    if before_created_at is not None and before_id is not None:
        query = query.where(
            sa.or_(
                Message.created_at < before_created_at,
                sa.and_(Message.created_at == before_created_at, Message.id < before_id),
            )
        )
    rows = await session.execute(
        query.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit + 1)
    )
    return [(row[0], row[1]) for row in rows.all()]

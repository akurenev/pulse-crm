"""FastAPI routers ready to mount below ``/api/v1`` by the core application."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.integrations.messaging import (
    MessagingNotFoundError,
    OriginConversationError,
    list_deal_messages,
    queue_outbound_message,
)
from app.integrations.models import (
    ChannelConnection,
    Conversation,
    Message,
    MessageDirection,
    MessageStatus,
)
from app.models import BackgroundJob, Deal, JobStatus
from app.pagination import decode_cursor, encode_cursor
from app.security import CurrentMutationUser, CurrentUser
from app.services.access import deal_access_condition, ensure_deal_access, is_employee
from app.services.data_access import enforce_cursor_page_budget

router = APIRouter(tags=["messages"])


class MessageCreate(BaseModel):
    body: str = Field(min_length=1, max_length=10_000)


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    direction: str
    status: str
    body: str
    channel: str
    provider_message_id: str | None
    created_at: datetime
    sent_at: datetime | None
    received_at: datetime | None
    last_error: str | None


class MessagePage(BaseModel):
    items: list[MessageRead]
    next_cursor: str | None = None


def _message_read(message: Message, connection: ChannelConnection) -> MessageRead:
    return MessageRead(
        id=message.id,
        conversation_id=message.conversation_id,
        direction=message.direction.value,
        status=message.status.value,
        body=message.body,
        channel=connection.kind.value,
        provider_message_id=message.provider_message_id,
        created_at=message.created_at,
        sent_at=message.sent_at,
        received_at=message.received_at,
        last_error=message.last_error,
    )


@router.get("/deals/{deal_id}/messages", response_model=MessagePage)
async def get_deal_messages(
    deal_id: uuid.UUID,
    context: CurrentUser,
    db: AsyncSession = Depends(get_session),
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> MessagePage:
    await ensure_deal_access(db, context, deal_id)
    await enforce_cursor_page_budget(
        db,
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        resource="deal_messages",
        cursor=cursor,
    )
    decoded = decode_cursor(cursor)
    try:
        rows = await list_deal_messages(
            db,
            workspace_id=context.workspace_id,
            deal_id=deal_id,
            limit=limit,
            before_created_at=decoded.created_at if decoded else None,
            before_id=decoded.entity_id if decoded else None,
            required_deal_assignee_id=(context.user_id if is_employee(context) else None),
        )
    except MessagingNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    has_more = len(rows) > limit
    visible = rows[:limit]
    next_cursor = None
    if has_more and visible:
        oldest_message = visible[-1][0]
        next_cursor = encode_cursor(oldest_message.created_at, oldest_message.id)
    # API consumers display a page chronologically even though the cursor walks
    # backwards through history.
    return MessagePage(
        items=[_message_read(message, connection) for message, connection in reversed(visible)],
        next_cursor=next_cursor,
    )


@router.post(
    "/deals/{deal_id}/messages",
    response_model=MessageRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def post_deal_message(
    deal_id: uuid.UUID,
    payload: MessageCreate,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> MessageRead:
    await ensure_deal_access(db, context, deal_id)
    try:
        message, connection = await queue_outbound_message(
            db,
            workspace_id=context.workspace_id,
            deal_id=deal_id,
            actor_id=context.user_id,
            body=payload.body,
            required_deal_assignee_id=(context.user_id if is_employee(context) else None),
        )
    except MessagingNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except OriginConversationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await db.commit()
    return _message_read(message, connection)


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=MessageRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def post_conversation_message(
    conversation_id: uuid.UUID,
    deal_id: uuid.UUID,
    payload: MessageCreate,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> MessageRead:
    await ensure_deal_access(db, context, deal_id)
    try:
        message, connection = await queue_outbound_message(
            db,
            workspace_id=context.workspace_id,
            deal_id=deal_id,
            actor_id=context.user_id,
            body=payload.body,
            conversation_id=conversation_id,
            required_deal_assignee_id=(context.user_id if is_employee(context) else None),
        )
    except MessagingNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except OriginConversationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await db.commit()
    return _message_read(message, connection)


@router.post("/messages/{message_id}/retry", response_model=MessageRead)
async def retry_message(
    message_id: uuid.UUID,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> MessageRead:
    own_deal = sa.exists(
        sa.select(Deal.id).where(
            Deal.id == Conversation.deal_id,
            Deal.workspace_id == context.workspace_id,
            Deal.deleted_at.is_(None),
            deal_access_condition(context),
        )
    )
    access_filters = (own_deal,) if is_employee(context) else ()
    route_row = (
        await db.execute(
            sa.select(Conversation, ChannelConnection)
            .select_from(Conversation)
            .join(Message, Message.conversation_id == Conversation.id)
            .join(ChannelConnection, ChannelConnection.id == Conversation.channel_connection_id)
            .where(
                Message.id == message_id,
                Message.workspace_id == context.workspace_id,
                Message.direction == MessageDirection.outbound,
                ChannelConnection.workspace_id == context.workspace_id,
                *access_filters,
            )
            .with_for_update(of=Conversation)
        )
    ).one_or_none()
    if route_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="message not found")
    conversation, connection = route_row
    if conversation.deal_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="deal not found")
    await ensure_deal_access(db, context, conversation.deal_id, for_update=True)
    message = await db.scalar(
        sa.select(Message)
        .where(
            Message.id == message_id,
            Message.workspace_id == context.workspace_id,
            Message.conversation_id == conversation.id,
            Message.direction == MessageDirection.outbound,
        )
        .with_for_update()
    )
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="message not found")
    if message.status is not MessageStatus.failed:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="message is not failed")
    message.status = MessageStatus.queued
    message.failed_at = None
    message.last_error = None
    job = await db.scalar(
        sa.select(BackgroundJob).where(
            BackgroundJob.dedupe_key == f"message:{message.id}:send",
            BackgroundJob.workspace_id == context.workspace_id,
        )
    )
    if job is not None:
        job.status = JobStatus.queued
        job.attempts = 0
        job.run_at = datetime.now(UTC)
        job.lease_owner = None
        job.lease_until = None
        job.last_error = None
    await db.commit()
    return _message_read(message, connection)

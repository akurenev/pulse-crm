"""Authenticated upload and short-lived download links for private attachments."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.integrations.api import MessageRead, _message_read
from app.integrations.messaging import (
    MessagingNotFoundError,
    OriginConversationError,
    queue_outbound_message,
)
from app.integrations.models import Attachment, Conversation, Message
from app.integrations.s3 import (
    MAX_ATTACHMENT_BYTES,
    AttachmentStorage,
    AttachmentValidationError,
    StoredObject,
    validate_attachment,
)
from app.models import ActivityEvent, Deal, NoteAttachment
from app.schemas import ActivityRead, NoteAttachmentRead
from app.security import CurrentMutationUser, CurrentUser
from app.services.access import deal_access_condition, ensure_deal_access, is_employee
from app.services.events import record_audit_event, record_domain_event

router = APIRouter(tags=["attachments"])
MAX_NOTE_ATTACHMENTS = 5


class AttachmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    message_id: uuid.UUID
    original_filename: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: datetime


class AttachmentDownload(BaseModel):
    url: str
    expires_in: int


class MessageWithAttachmentRead(BaseModel):
    message: MessageRead
    attachment: AttachmentRead


class _PendingAttachment(BaseModel):
    filename: str
    content_type: str
    content: bytes


def _storage(request: Request) -> AttachmentStorage:
    storage = getattr(request.app.state, "attachment_storage", None)
    if not isinstance(storage, AttachmentStorage):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="attachment storage is not configured",
        )
    return storage


async def _read_attachment(file: UploadFile) -> bytes:
    content = await file.read(MAX_ATTACHMENT_BYTES + 1)
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="attachment exceeds the 20 MB limit",
        )
    return content


async def _cleanup_stored_objects(
    storage: AttachmentStorage,
    *,
    workspace_id: uuid.UUID,
    stored_objects: list[StoredObject],
) -> None:
    """Best-effort cleanup for objects whose database transaction did not commit."""

    await asyncio.gather(
        *(
            storage.delete(workspace_id=workspace_id, object_key=item.object_key)
            for item in stored_objects
        ),
        return_exceptions=True,
    )


async def _read_note_attachments(files: list[UploadFile]) -> list[_PendingAttachment]:
    if len(files) > MAX_NOTE_ATTACHMENTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"a note can have at most {MAX_NOTE_ATTACHMENTS} attachments",
        )
    pending: list[_PendingAttachment] = []
    for file in files:
        content = await _read_attachment(file)
        filename = file.filename or "attachment"
        content_type = file.content_type or "application/octet-stream"
        try:
            validate_attachment(filename, content_type, content)
        except AttachmentValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        pending.append(
            _PendingAttachment(
                filename=filename,
                content_type=content_type,
                content=content,
            )
        )
    return pending


@router.post(
    "/messages/{message_id}/attachments",
    response_model=AttachmentRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_attachment(
    message_id: uuid.UUID,
    request: Request,
    context: CurrentMutationUser,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
) -> AttachmentRead:
    message_query = (
        sa.select(Message, Conversation)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.id == message_id,
            Message.workspace_id == context.workspace_id,
        )
    )
    if is_employee(context):
        message_query = message_query.where(
            sa.exists(
                sa.select(Deal.id).where(
                    Deal.id == Conversation.deal_id,
                    Deal.workspace_id == context.workspace_id,
                    Deal.deleted_at.is_(None),
                    deal_access_condition(context),
                )
            )
        )
    message_query = message_query.with_for_update(of=Conversation)
    message_row = (await db.execute(message_query)).one_or_none()
    if message_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="message not found")
    message, conversation = message_row
    deal_id = conversation.deal_id
    if deal_id is not None:
        await ensure_deal_access(db, context, deal_id, for_update=True)

    content = await _read_attachment(file)
    storage = _storage(request)
    try:
        stored = await storage.store(
            workspace_id=context.workspace_id,
            filename=file.filename or "attachment",
            content_type=file.content_type or "application/octet-stream",
            content=content,
        )
    except AttachmentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    record = Attachment(
        workspace_id=context.workspace_id,
        message_id=message.id,
        object_key=stored.object_key,
        original_filename=stored.attachment.filename,
        content_type=stored.attachment.content_type,
        size_bytes=stored.attachment.size_bytes,
        sha256=stored.attachment.sha256,
    )
    db.add(record)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        await storage.delete(
            workspace_id=context.workspace_id,
            object_key=stored.object_key,
        )
        raise
    await db.refresh(record)
    return AttachmentRead.model_validate(record)


@router.post(
    "/deals/{deal_id}/messages/with-attachment",
    response_model=MessageWithAttachmentRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_message_with_attachment(
    deal_id: uuid.UUID,
    request: Request,
    context: CurrentMutationUser,
    body: str = Form(min_length=1, max_length=10_000),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
) -> MessageWithAttachmentRead:
    """Atomically persist an outbound message and its private S3 attachment."""

    await ensure_deal_access(db, context, deal_id)
    storage = _storage(request)
    content = await _read_attachment(file)
    stored = None
    try:
        message, connection = await queue_outbound_message(
            db,
            workspace_id=context.workspace_id,
            deal_id=deal_id,
            actor_id=context.user_id,
            body=body,
            required_deal_assignee_id=(context.user_id if is_employee(context) else None),
        )
        stored = await storage.store(
            workspace_id=context.workspace_id,
            filename=file.filename or "attachment",
            content_type=file.content_type or "application/octet-stream",
            content=content,
        )
        record = Attachment(
            workspace_id=context.workspace_id,
            message_id=message.id,
            object_key=stored.object_key,
            original_filename=stored.attachment.filename,
            content_type=stored.attachment.content_type,
            size_bytes=stored.attachment.size_bytes,
            sha256=stored.attachment.sha256,
        )
        db.add(record)
        await db.flush()
        await db.refresh(record)
        await db.commit()
    except MessagingNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except OriginConversationError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except AttachmentValidationError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except Exception:
        await db.rollback()
        if stored is not None:
            await storage.delete(
                workspace_id=context.workspace_id,
                object_key=stored.object_key,
            )
        raise
    return MessageWithAttachmentRead(
        message=_message_read(message, connection),
        attachment=AttachmentRead.model_validate(record),
    )


@router.post(
    "/deals/{deal_id}/notes/with-attachments",
    response_model=ActivityRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_deal_note_with_attachments(
    deal_id: uuid.UUID,
    request: Request,
    context: CurrentMutationUser,
    body: str = Form(min_length=1, max_length=10_000),
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_session),
) -> ActivityRead:
    """Atomically persist one deal note and up to five private note files."""

    await ensure_deal_access(db, context, deal_id, for_update=True)
    normalized_body = body.strip()
    if not normalized_body:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="note must not be blank",
        )
    pending = await _read_note_attachments(files)
    storage = _storage(request)
    stored_objects: list[StoredObject] = []
    try:
        for item in pending:
            stored_objects.append(
                await storage.store(
                    workspace_id=context.workspace_id,
                    filename=item.filename,
                    content_type=item.content_type,
                    content=item.content,
                )
            )
        activity = record_domain_event(
            db,
            workspace_id=context.workspace_id,
            event_type="deal.note.created",
            entity_type="deal",
            entity_id=deal_id,
            actor_id=context.user_id,
            payload={"body": normalized_body},
        )
        await db.flush()
        records = [
            NoteAttachment(
                workspace_id=context.workspace_id,
                activity_event_id=activity.id,
                position=position,
                object_key=stored.object_key,
                original_filename=stored.attachment.filename,
                content_type=stored.attachment.content_type,
                size_bytes=stored.attachment.size_bytes,
                sha256=stored.attachment.sha256,
            )
            for position, stored in enumerate(stored_objects)
        ]
        db.add_all(records)
        await db.flush()
        await db.refresh(activity)
        for record in records:
            await db.refresh(record)
        await db.commit()
    except AttachmentValidationError as exc:
        await db.rollback()
        await _cleanup_stored_objects(
            storage,
            workspace_id=context.workspace_id,
            stored_objects=stored_objects,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        await db.rollback()
        await _cleanup_stored_objects(
            storage,
            workspace_id=context.workspace_id,
            stored_objects=stored_objects,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="note attachments could not be stored",
        ) from exc

    return ActivityRead.model_validate(activity).model_copy(
        update={"attachments": [NoteAttachmentRead.model_validate(record) for record in records]}
    )


def _note_attachment_query(
    attachment_id: uuid.UUID,
    context: CurrentUser,
) -> sa.Select[tuple[NoteAttachment]]:
    query = (
        sa.select(NoteAttachment)
        .join(ActivityEvent, ActivityEvent.id == NoteAttachment.activity_event_id)
        .join(
            Deal,
            sa.and_(
                ActivityEvent.entity_type == "deal",
                ActivityEvent.entity_id == Deal.id,
            ),
        )
        .where(
            NoteAttachment.id == attachment_id,
            NoteAttachment.workspace_id == context.workspace_id,
            ActivityEvent.workspace_id == context.workspace_id,
            ActivityEvent.event_type == "deal.note.created",
            Deal.workspace_id == context.workspace_id,
            Deal.deleted_at.is_(None),
        )
    )
    if is_employee(context):
        query = query.where(deal_access_condition(context))
    return query


@router.get(
    "/note-attachments/{attachment_id}",
    response_model=NoteAttachmentRead,
)
async def get_note_attachment(
    attachment_id: uuid.UUID,
    context: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> NoteAttachmentRead:
    attachment = await db.scalar(_note_attachment_query(attachment_id, context))
    if attachment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="note attachment not found",
        )
    return NoteAttachmentRead.model_validate(attachment)


@router.get(
    "/note-attachments/{attachment_id}/download",
    response_model=AttachmentDownload,
)
async def download_note_attachment(
    attachment_id: uuid.UUID,
    request: Request,
    context: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> AttachmentDownload:
    attachment = await db.scalar(_note_attachment_query(attachment_id, context))
    if attachment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="note attachment not found",
        )
    expires_in = 300
    url = await _storage(request).signed_download_url(
        workspace_id=context.workspace_id,
        object_key=attachment.object_key,
        filename=attachment.original_filename,
        expires_seconds=expires_in,
    )
    record_audit_event(
        db,
        workspace_id=context.workspace_id,
        event_type="note_attachment.download_link_issued",
        entity_type="note_attachment",
        entity_id=attachment.id,
        actor_id=context.user_id,
        payload={
            "activity_event_id": str(attachment.activity_event_id),
            "size_bytes": attachment.size_bytes,
            "expires_in": expires_in,
        },
    )
    await db.commit()
    return AttachmentDownload(url=url, expires_in=expires_in)


@router.get("/attachments/{attachment_id}", response_model=AttachmentRead)
async def get_attachment(
    attachment_id: uuid.UUID,
    context: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> AttachmentRead:
    attachment_query = (
        sa.select(Attachment)
        .join(Message, Message.id == Attachment.message_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Attachment.id == attachment_id,
            Attachment.workspace_id == context.workspace_id,
        )
    )
    if is_employee(context):
        attachment_query = attachment_query.where(
            sa.exists(
                sa.select(Deal.id).where(
                    Deal.id == Conversation.deal_id,
                    Deal.workspace_id == context.workspace_id,
                    Deal.deleted_at.is_(None),
                    deal_access_condition(context),
                )
            )
        )
    attachment = await db.scalar(attachment_query)
    if attachment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="attachment not found")
    return AttachmentRead.model_validate(attachment)


@router.get(
    "/attachments/{attachment_id}/download",
    response_model=AttachmentDownload,
)
async def download_attachment(
    attachment_id: uuid.UUID,
    request: Request,
    context: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> AttachmentDownload:
    attachment_query = (
        sa.select(Attachment)
        .join(Message, Message.id == Attachment.message_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Attachment.id == attachment_id,
            Attachment.workspace_id == context.workspace_id,
        )
    )
    if is_employee(context):
        attachment_query = attachment_query.where(
            sa.exists(
                sa.select(Deal.id).where(
                    Deal.id == Conversation.deal_id,
                    Deal.workspace_id == context.workspace_id,
                    Deal.deleted_at.is_(None),
                    deal_access_condition(context),
                )
            )
        )
    attachment = await db.scalar(attachment_query)
    if attachment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="attachment not found")
    expires_in = 300
    url = await _storage(request).signed_download_url(
        workspace_id=context.workspace_id,
        object_key=attachment.object_key,
        filename=attachment.original_filename,
        expires_seconds=expires_in,
    )
    record_audit_event(
        db,
        workspace_id=context.workspace_id,
        event_type="attachment.download_link_issued",
        entity_type="attachment",
        entity_id=attachment.id,
        actor_id=context.user_id,
        payload={
            "message_id": str(attachment.message_id),
            "size_bytes": attachment.size_bytes,
            "expires_in": expires_in,
        },
    )
    await db.commit()
    return AttachmentDownload(url=url, expires_in=expires_in)

"""Authenticated upload and short-lived download links for private attachments."""

from __future__ import annotations

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
from app.integrations.models import Attachment, Message
from app.integrations.s3 import (
    MAX_ATTACHMENT_BYTES,
    AttachmentStorage,
    AttachmentValidationError,
)
from app.security import CurrentMutationUser, CurrentUser

router = APIRouter(tags=["attachments"])


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
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="attachment exceeds the 20 MB limit",
        )
    return content


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
    message = await db.scalar(
        sa.select(Message).where(
            Message.id == message_id,
            Message.workspace_id == context.workspace_id,
        )
    )
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="message not found")

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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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


@router.get("/attachments/{attachment_id}", response_model=AttachmentRead)
async def get_attachment(
    attachment_id: uuid.UUID,
    context: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> AttachmentRead:
    attachment = await db.scalar(
        sa.select(Attachment).where(
            Attachment.id == attachment_id,
            Attachment.workspace_id == context.workspace_id,
        )
    )
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
    attachment = await db.scalar(
        sa.select(Attachment).where(
            Attachment.id == attachment_id,
            Attachment.workspace_id == context.workspace_id,
        )
    )
    if attachment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="attachment not found")
    expires_in = 300
    url = await _storage(request).signed_download_url(
        workspace_id=context.workspace_id,
        object_key=attachment.object_key,
        filename=attachment.original_filename,
        expires_seconds=expires_in,
    )
    return AttachmentDownload(url=url, expires_in=expires_in)

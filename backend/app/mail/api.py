"""Personal mail endpoints. Administrative CRM roles never grant mailbox access."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from email.utils import make_msgid
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.integrations.s3 import (
    MAX_ATTACHMENT_BYTES,
    AttachmentStorage,
    AttachmentValidationError,
    StoredObject,
)
from app.mail.models import MailAccount, MailAttachment, MailFolder, MailLink, MailMessage
from app.mail.schemas import (
    AccountCreate,
    AccountPage,
    AccountRead,
    AccountUpdate,
    AttachmentRead,
    CheckRead,
    Compose,
    DownloadRead,
    DraftUpdate,
    FolderPage,
    FolderRead,
    LinkCreate,
    LinkRead,
    MessagePage,
    MessageRead,
    MessageUpdate,
    QueueRead,
    VersionRequest,
)
from app.mail.service import (
    account_read,
    cipher_for,
    credential_aad,
    decrypt_credentials,
    message_read,
    message_reads,
    require_account,
    require_folder,
    require_message,
    require_version,
    storage_for,
)
from app.models import NoteAttachment
from app.pagination import decode_cursor, encode_cursor
from app.security import CurrentMutationUser, CurrentUser
from app.services.access import ensure_deal_access
from app.services.data_access import enforce_cursor_page_budget
from app.services.events import record_domain_event
from app.services.jobs import enqueue_job


class PrivateValidationRoute(APIRoute):
    """Pydantic errors must never echo submitted mailbox passwords."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def private_handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError as exc:
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": [
                            {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
                            for error in exc.errors()
                        ]
                    },
                )

        return private_handler


router = APIRouter(prefix="/mail", tags=["personal-mail"], route_class=PrivateValidationRoute)
MAX_MAIL_ATTACHMENTS = 10


async def _cleanup_objects(
    storage: AttachmentStorage, workspace_id: uuid.UUID, keys: list[str]
) -> None:
    await asyncio.gather(
        *(storage.delete(workspace_id=workspace_id, object_key=key) for key in keys),
        return_exceptions=True,
    )


def _connected(account: MailAccount) -> None:
    if account.status == "disabled" or account.encrypted_credentials is None:
        raise HTTPException(409, "mail account is disconnected")


def _draft(message: MailMessage) -> None:
    if message.direction != "outbound" or message.status not in {"draft", "failed"}:
        raise HTTPException(409, "only an unsent draft can be edited")


async def _virtual_folder(db: AsyncSession, account: MailAccount, kind: str) -> MailFolder:
    name = f"__{kind}__"
    folder = await db.scalar(
        sa.select(MailFolder).where(
            MailFolder.account_id == account.id,
            MailFolder.name == name,
            MailFolder.workspace_id == account.workspace_id,
        )
    )
    if folder is None:
        folder = MailFolder(
            workspace_id=account.workspace_id,
            account_id=account.id,
            name=name,
            display_name="Черновики" if kind == "drafts" else "Исходящие",
            kind=kind,
        )
        db.add(folder)
        await db.flush()
    return folder


@router.get("/accounts", response_model=AccountPage)
async def list_accounts(
    context: CurrentUser, db: AsyncSession = Depends(get_session)
) -> AccountPage:
    accounts = await db.scalars(
        sa.select(MailAccount)
        .where(
            MailAccount.workspace_id == context.workspace_id,
            MailAccount.owner_id == context.user_id,
            MailAccount.status != "disconnected",
        )
        .order_by(MailAccount.created_at, MailAccount.id)
        .limit(100)
    )
    return AccountPage(items=[account_read(account) for account in accounts])


@router.post("/accounts", response_model=AccountRead, status_code=201)
async def create_account(
    payload: AccountCreate,
    request: Request,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> AccountRead:
    cipher = cipher_for(request)
    email = str(payload.email).casefold()
    account = await db.scalar(
        sa.select(MailAccount)
        .where(
            MailAccount.workspace_id == context.workspace_id,
            MailAccount.owner_id == context.user_id,
            MailAccount.email == email,
        )
        .with_for_update()
    )
    if account is not None and account.status != "disconnected":
        raise HTTPException(409, "this mail account is already connected")
    if account is None:
        account = MailAccount(
            id=uuid.uuid4(),
            workspace_id=context.workspace_id,
            owner_id=context.user_id,
            email=email,
        )
        db.add(account)
    else:
        server_changed = any(
            getattr(account, key) != getattr(payload, key)
            for key in (
                "imap_host",
                "imap_port",
                "imap_security",
                "smtp_host",
                "smtp_port",
                "smtp_security",
            )
        ) or account.username != (payload.username or str(payload.email))
        account.version += 1
        if server_changed:
            folders = await db.scalars(
                sa.select(MailFolder).where(MailFolder.account_id == account.id)
            )
            for folder in folders:
                folder.uidvalidity = None
                folder.last_uid = 0
                folder.sync_state = {}
            await db.execute(
                sa.update(MailMessage)
                .where(MailMessage.account_id == account.id)
                .values(
                    imap_uid=None,
                    imap_uidvalidity=None,
                )
            )
    account.username = payload.username or str(payload.email)
    for key in (
        "display_name",
        "imap_host",
        "imap_port",
        "imap_security",
        "smtp_host",
        "smtp_port",
        "smtp_security",
        "sync_days",
    ):
        setattr(account, key, getattr(payload, key))
    account.status = "active"
    account.last_error = None
    account.credentials_key_id = cipher.key_id
    account.encrypted_credentials = cipher.encrypt(
        json.dumps({"username": account.username, "password": payload.password}),
        associated_data=credential_aad(account.id),
    )
    db.add(account)
    try:
        await db.flush()
        await _virtual_folder(db, account, "drafts")
        await _virtual_folder(db, account, "outbox")
        await enqueue_job(
            db,
            "mail.sync",
            {"account_id": str(account.id)},
            workspace_id=context.workspace_id,
            dedupe_key=f"mail-initial-sync:{account.id}:{account.version}",
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "this mail account is already connected") from None
    return account_read(account)


@router.patch("/accounts/{account_id}", response_model=AccountRead)
async def update_account(
    account_id: uuid.UUID,
    payload: AccountUpdate,
    request: Request,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> AccountRead:
    account = await require_account(db, context, account_id, lock=True)
    require_version(account.version, payload.expected_version)
    if payload.display_name is not None:
        account.display_name = payload.display_name
    if payload.sync_days is not None:
        account.sync_days = payload.sync_days
    if payload.password is not None:
        cipher = cipher_for(request)
        account.encrypted_credentials = cipher.encrypt(
            json.dumps({"username": account.username, "password": payload.password}),
            associated_data=credential_aad(account.id),
        )
        account.credentials_key_id = cipher.key_id
        account.last_error = None
    if payload.status is not None:
        if payload.status == "active" and account.encrypted_credentials is None:
            raise HTTPException(409, "supply a password to reconnect the mailbox")
        account.status = payload.status
    account.version += 1
    await db.commit()
    return account_read(account)


@router.delete("/accounts/{account_id}", status_code=204)
async def disconnect_account(
    account_id: uuid.UUID,
    context: CurrentMutationUser,
    expected_version: int = Query(ge=1),
    db: AsyncSession = Depends(get_session),
) -> Response:
    account = await require_account(db, context, account_id, lock=True)
    require_version(account.version, expected_version)
    account.status = "disconnected"
    account.encrypted_credentials = None
    account.credentials_key_id = None
    account.last_error = None
    account.version += 1
    pending = await db.scalars(
        sa.select(MailMessage)
        .where(
            MailMessage.account_id == account.id,
            sa.or_(
                MailMessage.status.in_(["queued", "sending"]),
                MailMessage.metadata_json["pending_action"].as_string().is_not(None),
            ),
        )
        .with_for_update()
    )
    for message in pending:
        if message.status == "queued":
            message.status = "failed"
            message.last_error = "Sending cancelled because the mailbox was disconnected."
        elif message.status == "sending":
            message.status = "uncertain"
            message.last_error = "Check the server mailbox before sending this message again."
        message.metadata_json = {
            key: value for key, value in message.metadata_json.items() if key != "pending_action"
        }
        message.version += 1
    await db.commit()
    return Response(status_code=204)


@router.post("/accounts/{account_id}/check", response_model=CheckRead)
async def check_account(
    account_id: uuid.UUID,
    request: Request,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> CheckRead:
    account = await require_account(db, context, account_id, lock=True)
    _connected(account)
    transport = getattr(request.app.state, "mail_transport", None)
    if transport is None:
        raise HTTPException(503, "mail transport is not configured")
    try:
        await transport.check(account, decrypt_credentials(account, cipher_for(request)))
    except Exception:
        account.status = "degraded"
        account.last_error = "Mail connection failed. Check the server settings and password."
        result = CheckRead(healthy=False, detail=account.last_error)
    else:
        account.status = "active"
        account.last_error = None
        result = CheckRead(healthy=True)
    account.version += 1
    await db.commit()
    return result


@router.post("/accounts/{account_id}/sync", response_model=QueueRead, status_code=202)
async def sync_account(
    account_id: uuid.UUID,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> QueueRead:
    account = await require_account(db, context, account_id, lock=True)
    _connected(account)
    bucket = datetime.now(UTC).strftime("%Y%m%d%H%M")
    await enqueue_job(
        db,
        "mail.sync",
        {"account_id": str(account.id)},
        workspace_id=context.workspace_id,
        dedupe_key=f"mail-sync:{account.id}:{bucket}",
    )
    await db.commit()
    return QueueRead(queued=True)


@router.get("/accounts/{account_id}/folders", response_model=FolderPage)
async def list_folders(
    account_id: uuid.UUID,
    context: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> FolderPage:
    account = await require_account(db, context, account_id)
    folders = list(
        (
            await db.scalars(
                sa.select(MailFolder)
                .where(
                    MailFolder.account_id == account.id,
                    MailFolder.workspace_id == context.workspace_id,
                )
                .order_by(MailFolder.name)
                .limit(500)
            )
        ).all()
    )
    virtual_ids = {folder.id for folder in folders if folder.name.startswith("__")}
    counts: dict[uuid.UUID, int] = {}
    if virtual_ids:
        for folder_id, count in await db.execute(
            sa.select(
                MailMessage.folder_id,
                sa.func.count(MailMessage.id),
            )
            .where(
                MailMessage.workspace_id == context.workspace_id,
                MailMessage.account_id == account.id,
                MailMessage.folder_id.in_(virtual_ids),
            )
            .group_by(MailMessage.folder_id)
        ):
            counts[folder_id] = count
    return FolderPage(
        items=[
            FolderRead.model_validate(folder).model_copy(
                update={
                    "total_count": counts.get(folder.id, 0),
                    "unread_count": 0,
                }
            )
            if folder.id in virtual_ids
            else FolderRead.model_validate(folder)
            for folder in folders
        ]
    )


@router.get("/accounts/{account_id}/messages", response_model=MessagePage)
async def list_messages(
    account_id: uuid.UUID,
    context: CurrentUser,
    folder_id: uuid.UUID | None = None,
    thread_key: str | None = Query(default=None, max_length=998),
    search: str | None = Query(default=None, max_length=200),
    unread: bool | None = None,
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_session),
) -> MessagePage:
    account = await require_account(db, context, account_id)
    if folder_id is not None:
        await require_folder(db, account, folder_id)
    await enforce_cursor_page_budget(
        db,
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        resource="personal_mail",
        cursor=cursor,
    )
    query = sa.select(MailMessage).where(
        MailMessage.workspace_id == context.workspace_id,
        MailMessage.account_id == account.id,
    )
    if folder_id is not None:
        query = query.where(MailMessage.folder_id == folder_id)
    if thread_key is not None:
        query = query.where(MailMessage.thread_key == thread_key)
    if unread is not None:
        query = query.where(MailMessage.is_read.is_(not unread))
    if search and search.strip():
        term = (
            "%" + search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        )
        query = query.where(
            sa.or_(
                *[
                    field.ilike(term, escape="\\")
                    for field in (
                        MailMessage.subject,
                        MailMessage.from_address,
                        MailMessage.from_name,
                        MailMessage.body_text,
                        sa.cast(MailMessage.to_addresses, sa.Text),
                        sa.cast(MailMessage.cc_addresses, sa.Text),
                    )
                ]
            )
        )
    decoded = decode_cursor(cursor)
    if decoded:
        query = query.where(
            sa.or_(
                MailMessage.created_at < decoded.created_at,
                sa.and_(
                    MailMessage.created_at == decoded.created_at, MailMessage.id < decoded.entity_id
                ),
            )
        )
    rows = list(
        (
            await db.scalars(
                query.order_by(
                    MailMessage.created_at.desc(),
                    MailMessage.id.desc(),
                ).limit(limit + 1)
            )
        ).all()
    )
    page = rows[:limit]
    return MessagePage(
        items=await message_reads(db, context, page, summary=True),
        next_cursor=(
            encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None
        ),
    )


@router.get("/messages/{message_id}", response_model=MessageRead)
async def get_message(
    message_id: uuid.UUID,
    context: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> MessageRead:
    message, _ = await require_message(db, context, message_id)
    return await message_read(db, context, message)


async def _compose(
    db: AsyncSession,
    context: CurrentUser,
    account: MailAccount,
    message: MailMessage,
    payload: Compose,
) -> None:
    message.subject = payload.subject
    message.to_addresses = [str(value) for value in payload.to_addresses]
    message.cc_addresses = [str(value) for value in payload.cc_addresses]
    message.bcc_addresses = [str(value) for value in payload.bcc_addresses]
    message.body_text = payload.body_text
    message.body_html = None
    if payload.reply_to_message_id is not None:
        original, _ = await require_message(db, context, payload.reply_to_message_id)
        if original.account_id != account.id:
            raise HTTPException(404, "mail message not found")
        message.in_reply_to = original.internet_message_id
        message.references = list(
            dict.fromkeys(
                [
                    *original.references,
                    *([original.internet_message_id] if original.internet_message_id else []),
                ]
            )
        )[-50:]
        message.thread_key = original.thread_key
        message.metadata_json = {
            **(message.metadata_json or {}),
            "reply_to_message_id": str(original.id),
        }
    else:
        message.in_reply_to = None
        message.references = []
        message.metadata_json = {
            key: value
            for key, value in (message.metadata_json or {}).items()
            if key != "reply_to_message_id"
        }


@router.post("/accounts/{account_id}/drafts", response_model=MessageRead, status_code=201)
async def create_draft(
    account_id: uuid.UUID,
    payload: Compose,
    request: Request,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> MessageRead:
    account = await require_account(db, context, account_id, lock=True)
    _connected(account)
    folder = await _virtual_folder(db, account, "drafts")
    message = MailMessage(
        id=uuid.uuid4(),
        workspace_id=context.workspace_id,
        account_id=account.id,
        folder_id=folder.id,
        direction="outbound",
        status="draft",
        from_address=account.email,
        from_name=account.display_name or None,
        is_read=True,
    )
    source = None
    if payload.forward_message_id:
        source, _ = await require_message(db, context, payload.forward_message_id, lock=True)
        if source.account_id != account.id:
            raise HTTPException(404, "mail message not found")
    copied: list[StoredObject] = []
    storage = None
    try:
        await _compose(db, context, account, message, payload)
        db.add(message)
        await db.flush()
        if not message.thread_key:
            message.thread_key = str(message.id)
        if source is not None:
            originals = list(
                (
                    await db.scalars(
                        sa.select(MailAttachment)
                        .where(
                            MailAttachment.message_id == source.id,
                            MailAttachment.workspace_id == context.workspace_id,
                        )
                        .order_by(MailAttachment.created_at, MailAttachment.id)
                    )
                ).all()
            )
            if len(originals) > MAX_MAIL_ATTACHMENTS:
                raise HTTPException(422, "the original message has too many attachments to forward")
            if originals:
                storage = storage_for(request)
                for original in originals:
                    content = await storage.read_attachment(
                        workspace_id=context.workspace_id,
                        object_key=original.object_key,
                        filename=original.original_filename,
                        content_type=original.content_type,
                        expected_size_bytes=original.size_bytes,
                        expected_sha256=original.sha256,
                    )
                    stored = await storage.store(
                        workspace_id=context.workspace_id,
                        filename=original.original_filename,
                        content_type=original.content_type,
                        content=content,
                    )
                    copied.append(stored)
                    db.add(
                        MailAttachment(
                            workspace_id=context.workspace_id,
                            message_id=message.id,
                            object_key=stored.object_key,
                            original_filename=stored.attachment.filename,
                            content_type=stored.attachment.content_type,
                            size_bytes=stored.attachment.size_bytes,
                            sha256=stored.attachment.sha256,
                        )
                    )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        if storage is not None:
            await _cleanup_objects(
                storage, context.workspace_id, [stored.object_key for stored in copied]
            )
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(503, "the forwarded attachments could not be copied") from None
    return await message_read(db, context, message)


@router.patch("/messages/{message_id}/draft", response_model=MessageRead)
async def update_draft(
    message_id: uuid.UUID,
    payload: DraftUpdate,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> MessageRead:
    message, account = await require_message(db, context, message_id, lock=True)
    require_version(message.version, payload.expected_version)
    _draft(message)
    _connected(account)
    if message.status == "failed":
        message.internet_message_id = None
        message.metadata_json = {}
    await _compose(db, context, account, message, payload)
    message.status = "draft"
    message.last_error = None
    message.version += 1
    message.folder_id = (await _virtual_folder(db, account, "drafts")).id
    await db.commit()
    return await message_read(db, context, message)


@router.post("/messages/{message_id}/send", response_model=MessageRead, status_code=202)
async def send_message(
    message_id: uuid.UUID,
    payload: VersionRequest,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> MessageRead:
    message, account = await require_message(db, context, message_id, lock=True)
    require_version(message.version, payload.expected_version)
    _draft(message)
    _connected(account)
    if not (message.to_addresses or message.cc_addresses or message.bcc_addresses):
        raise HTTPException(422, "specify at least one recipient")
    message.status = "queued"
    message.last_error = None
    message.internet_message_id = message.internet_message_id or make_msgid(
        domain=account.email.split("@")[-1]
    )
    if (
        not message.in_reply_to
        and not message.references
        and not message.metadata_json.get("reply_to_message_id")
    ):
        message.thread_key = message.internet_message_id
    message.folder_id = (await _virtual_folder(db, account, "outbox")).id
    message.version += 1
    await enqueue_job(
        db,
        "mail.send",
        {"message_id": str(message.id)},
        workspace_id=context.workspace_id,
        dedupe_key=f"mail-send:{message.id}:{message.version}",
    )
    await db.commit()
    return await message_read(db, context, message)


@router.patch("/messages/{message_id}", response_model=MessageRead)
async def update_message(
    message_id: uuid.UUID,
    payload: MessageUpdate,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> MessageRead:
    message, account = await require_message(db, context, message_id, lock=True)
    require_version(message.version, payload.expected_version)
    if message.status in {"queued", "sending", "uncertain"}:
        raise HTTPException(409, "wait for the outbound message status to settle")
    raw_previous = message.metadata_json.get("pending_action")
    previous = raw_previous if isinstance(raw_previous, dict) else {}
    remote_uid = previous.get("uid", message.imap_uid)
    remote_validity = previous.get("uidvalidity", message.imap_uidvalidity)
    source = await require_folder(db, account, message.folder_id)
    target = await require_folder(db, account, payload.folder_id) if payload.folder_id else source
    if payload.folder_id and (remote_uid is None or target.name.startswith("__")):
        raise HTTPException(409, "only a server message can be moved to a server folder")
    if remote_uid is None and message.metadata_json.get("moved_from"):
        raise HTTPException(409, "wait for the moved message to synchronize before editing it")
    message.version += 1
    if payload.is_read is not None:
        message.is_read = payload.is_read
    if payload.is_starred is not None:
        message.is_starred = payload.is_starred
    if remote_uid is not None:
        _connected(account)
        message.metadata_json = {
            **message.metadata_json,
            "pending_action": {
                "version": message.version,
                "source_folder_id": previous.get("source_folder_id", str(source.id)),
                "source_name": previous.get("source_name", source.name),
                "uid": remote_uid,
                "uidvalidity": remote_validity,
                "target_folder_id": str(target.id),
                "target_name": target.name,
                "is_read": message.is_read,
                "is_starred": message.is_starred,
            },
        }
        await enqueue_job(
            db,
            "mail.action",
            {"message_id": str(message.id), "version": message.version},
            workspace_id=context.workspace_id,
            dedupe_key=f"mail-action:{message.id}:{message.version}",
        )
    if target.id != source.id:
        # UID and UIDVALIDITY belong to the source folder. Keeping them in the
        # optimistic destination can collide with an unrelated destination letter.
        message.imap_uid = None
        message.imap_uidvalidity = None
    message.folder_id = target.id
    await db.commit()
    return await message_read(db, context, message)


@router.delete("/messages/{message_id}", status_code=204)
async def delete_draft(
    message_id: uuid.UUID,
    request: Request,
    context: CurrentMutationUser,
    expected_version: int = Query(ge=1),
    db: AsyncSession = Depends(get_session),
) -> Response:
    message, _ = await require_message(db, context, message_id, lock=True)
    require_version(message.version, expected_version)
    _draft(message)
    if message.imap_uid is not None:
        raise HTTPException(409, "server messages must be moved to Trash")
    files = list(
        (
            await db.scalars(
                sa.select(MailAttachment).where(
                    MailAttachment.message_id == message.id,
                    MailAttachment.workspace_id == context.workspace_id,
                )
            )
        ).all()
    )
    for item in files:
        await db.delete(item)
    await db.execute(sa.delete(MailLink).where(MailLink.message_id == message.id))
    await db.delete(message)
    await db.commit()
    if files:
        await _cleanup_objects(
            storage_for(request), context.workspace_id, [item.object_key for item in files]
        )
    return Response(status_code=204)


@router.post("/messages/{message_id}/attachments", response_model=AttachmentRead, status_code=201)
async def upload_attachment(
    message_id: uuid.UUID,
    request: Request,
    context: CurrentMutationUser,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
) -> AttachmentRead:
    message, account = await require_message(db, context, message_id, lock=True)
    _draft(message)
    _connected(account)
    count = await db.scalar(
        sa.select(sa.func.count())
        .select_from(MailAttachment)
        .where(
            MailAttachment.message_id == message.id,
        )
    )
    if (count or 0) >= MAX_MAIL_ATTACHMENTS:
        raise HTTPException(422, "a message can have at most 10 attachments")
    content = await file.read(MAX_ATTACHMENT_BYTES + 1)
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(413, "attachment exceeds the 20 MB limit")
    storage = storage_for(request)
    try:
        stored = await storage.store(
            workspace_id=context.workspace_id,
            filename=file.filename or "attachment",
            content_type=file.content_type or "application/octet-stream",
            content=content,
        )
    except AttachmentValidationError as exc:
        raise HTTPException(422, str(exc)) from None
    item = MailAttachment(
        workspace_id=context.workspace_id,
        message_id=message.id,
        object_key=stored.object_key,
        original_filename=stored.attachment.filename,
        content_type=stored.attachment.content_type,
        size_bytes=stored.attachment.size_bytes,
        sha256=stored.attachment.sha256,
    )
    message.version += 1
    db.add(item)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        await _cleanup_objects(storage, context.workspace_id, [stored.object_key])
        raise
    return AttachmentRead.model_validate(item)


@router.post("/attachments/{attachment_id}/download", response_model=DownloadRead)
async def download_attachment(
    attachment_id: uuid.UUID,
    request: Request,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> DownloadRead:
    item = await db.scalar(
        sa.select(MailAttachment)
        .join(
            MailMessage,
            MailMessage.id == MailAttachment.message_id,
        )
        .join(MailAccount, MailAccount.id == MailMessage.account_id)
        .where(
            MailAttachment.id == attachment_id,
            MailAttachment.workspace_id == context.workspace_id,
            MailMessage.workspace_id == context.workspace_id,
            MailAccount.workspace_id == context.workspace_id,
            MailAccount.owner_id == context.user_id,
            MailAccount.status != "disconnected",
        )
    )
    if item is None:
        raise HTTPException(404, "mail attachment not found")
    url = await storage_for(request).signed_download_url(
        workspace_id=context.workspace_id,
        object_key=item.object_key,
        filename=item.original_filename,
        expires_seconds=300,
    )
    return DownloadRead(url=url, expires_in=300)


@router.delete("/attachments/{attachment_id}", status_code=204)
async def delete_attachment(
    attachment_id: uuid.UUID,
    request: Request,
    context: CurrentMutationUser,
    expected_version: int = Query(ge=1),
    db: AsyncSession = Depends(get_session),
) -> Response:
    item = await db.scalar(
        sa.select(MailAttachment)
        .join(
            MailMessage,
            MailMessage.id == MailAttachment.message_id,
        )
        .join(MailAccount, MailAccount.id == MailMessage.account_id)
        .where(
            MailAttachment.id == attachment_id,
            MailAttachment.workspace_id == context.workspace_id,
            MailMessage.workspace_id == context.workspace_id,
            MailAccount.workspace_id == context.workspace_id,
            MailAccount.owner_id == context.user_id,
            MailAccount.status != "disconnected",
        )
    )
    if item is None:
        raise HTTPException(404, "mail attachment not found")
    message, _ = await require_message(db, context, item.message_id, lock=True)
    require_version(message.version, expected_version)
    _draft(message)
    message.version += 1
    object_key = item.object_key
    await db.delete(item)
    await db.commit()
    await _cleanup_objects(storage_for(request), context.workspace_id, [object_key])
    return Response(status_code=204)


@router.post("/messages/{message_id}/link", response_model=LinkRead, status_code=201)
async def link_message(
    message_id: uuid.UUID,
    payload: LinkCreate,
    request: Request,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> LinkRead:
    message, _ = await require_message(db, context, message_id, lock=True)
    await ensure_deal_access(db, context, payload.deal_id, for_update=True)
    if message.status in {"draft", "queued", "sending", "failed", "uncertain"}:
        raise HTTPException(409, "only a received or sent message can be shared with a deal")
    existing = await db.scalar(
        sa.select(MailLink).where(
            MailLink.message_id == message.id,
            MailLink.deal_id == payload.deal_id,
            MailLink.workspace_id == context.workspace_id,
        )
    )
    if existing:
        return LinkRead(deal_id=existing.deal_id, note_id=existing.note_id)
    attachments = list(
        (
            await db.scalars(
                sa.select(MailAttachment)
                .where(
                    MailAttachment.message_id == message.id,
                    MailAttachment.workspace_id == context.workspace_id,
                )
                .order_by(MailAttachment.created_at, MailAttachment.id)
            )
        ).all()
    )
    copied: list[StoredObject] = []
    storage = storage_for(request) if attachments else None
    try:
        if storage is not None:
            for item in attachments:
                content = await storage.read_attachment(
                    workspace_id=context.workspace_id,
                    object_key=item.object_key,
                    filename=item.original_filename,
                    content_type=item.content_type,
                    expected_size_bytes=item.size_bytes,
                    expected_sha256=item.sha256,
                )
                copied.append(
                    await storage.store(
                        workspace_id=context.workspace_id,
                        filename=item.original_filename,
                        content_type=item.content_type,
                        content=content,
                    )
                )
        header = [
            f"Письмо: {message.subject or '(без темы)'}",
            f"От: {message.from_address}",
            f"Кому: {', '.join(message.to_addresses)}",
        ]
        if message.cc_addresses:
            header.append(f"Копия: {', '.join(message.cc_addresses)}")
        note = record_domain_event(
            db,
            workspace_id=context.workspace_id,
            event_type="deal.note.created",
            entity_type="deal",
            entity_id=payload.deal_id,
            actor_id=context.user_id,
            payload={"body": "\n".join(header) + "\n\n" + message.body_text},
        )
        await db.flush()
        for position, stored in enumerate(copied):
            db.add(
                NoteAttachment(
                    workspace_id=context.workspace_id,
                    activity_event_id=note.id,
                    position=position,
                    object_key=stored.object_key,
                    original_filename=stored.attachment.filename,
                    content_type=stored.attachment.content_type,
                    size_bytes=stored.attachment.size_bytes,
                    sha256=stored.attachment.sha256,
                )
            )
        db.add(
            MailLink(
                workspace_id=context.workspace_id,
                message_id=message.id,
                deal_id=payload.deal_id,
                note_id=note.id,
            )
        )
        await db.commit()
    except Exception:
        await db.rollback()
        if storage is not None:
            await _cleanup_objects(
                storage, context.workspace_id, [stored.object_key for stored in copied]
            )
        raise HTTPException(503, "mail snapshot could not be saved") from None
    return LinkRead(deal_id=payload.deal_id, note_id=note.id)

"""Durable personal mailbox jobs, separate from CRM channel inbound routing."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import make_msgid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.integrations.models import Attachment
from app.integrations.s3 import AttachmentStorage, AttachmentValidationError, StoredObject
from app.integrations.secrets import SecretCipher
from app.mail.mime import ParsedAttachment, build_mail, parse_mail, parsed_values
from app.mail.models import MailAccount, MailAttachment, MailFolder, MailMessage
from app.mail.service import decrypt_credentials
from app.mail.transport import (
    MAX_OUTBOUND_BYTES,
    MailDeliveryUncertain,
    MailIdentityChanged,
    MailTransport,
    MailTransportError,
)
from app.models import BackgroundJob, JobStatus, Membership, NoteAttachment, User
from app.services.jobs import ClaimedJob, JobHandler, SessionFactory, enqueue_job

JOB_MAIL_SYNC = "mail.sync"
JOB_MAIL_SEND = "mail.send"
JOB_MAIL_ACTION = "mail.action"
MAIL_JOB_TYPES = frozenset({JOB_MAIL_SYNC, JOB_MAIL_SEND, JOB_MAIL_ACTION})
logger = logging.getLogger(__name__)


def _id(payload: Mapping[str, Any], key: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(payload[key]))
    except (KeyError, TypeError, ValueError):
        raise ValueError("Invalid mailbox job identifier") from None


async def _enqueue(
    db: AsyncSession,
    job_type: str,
    payload: dict[str, Any],
    *,
    workspace_id: uuid.UUID,
    dedupe_key: str,
) -> None:
    if db.get_bind().dialect.name == "postgresql":
        await enqueue_job(
            db,
            job_type,
            payload,
            workspace_id=workspace_id,
            dedupe_key=dedupe_key,
        )
    elif (
        await db.scalar(sa.select(BackgroundJob.id).where(BackgroundJob.dedupe_key == dedupe_key))
        is None
    ):
        job = await enqueue_job(db, job_type, payload, workspace_id=workspace_id)
        job.dedupe_key = dedupe_key
        await db.flush()


async def schedule_mail_jobs(
    db: AsyncSession,
    now: datetime,
    batch_size: int = 100,
) -> dict[str, int]:
    """Recover durable work after restarts; queue domain IDs, never mail content."""
    accounts = list(
        (
            await db.scalars(
                sa.select(MailAccount)
                .join(
                    Membership,
                    sa.and_(
                        Membership.workspace_id == MailAccount.workspace_id,
                        Membership.user_id == MailAccount.owner_id,
                    ),
                )
                .join(User, User.id == MailAccount.owner_id)
                .where(
                    MailAccount.status.in_(["active", "degraded"]),
                    MailAccount.encrypted_credentials.is_not(None),
                    User.is_active.is_(True),
                )
                .order_by(MailAccount.last_synced_at.asc().nullsfirst(), MailAccount.id)
                .limit(batch_size)
            )
        ).all()
    )
    bucket = now.astimezone(UTC).strftime("%Y%m%d%H%M")
    counts: dict[str, int] = {}
    for account in accounts:
        await _enqueue(
            db,
            JOB_MAIL_SYNC,
            {"account_id": str(account.id)},
            workspace_id=account.workspace_id,
            dedupe_key=f"mail-sync:{account.id}:{bucket}",
        )
        counts[JOB_MAIL_SYNC] = counts.get(JOB_MAIL_SYNC, 0) + 1
    messages = list(
        (
            await db.scalars(
                sa.select(MailMessage)
                .join(MailAccount, MailAccount.id == MailMessage.account_id)
                .where(
                    MailAccount.status.in_(["active", "degraded"]),
                    MailAccount.id.in_([account.id for account in accounts]),
                    sa.or_(
                        MailMessage.status.in_(["queued", "sending"]),
                        MailMessage.metadata_json["sent_append_pending"].as_boolean().is_(True),
                    ),
                )
                .order_by(MailMessage.created_at, MailMessage.id)
                .limit(batch_size)
            )
        ).all()
    )
    for message in messages:
        # A minute key lets completed/failed queue records be rediscovered, while
        # sending state still guards a previously interrupted SMTP side effect.
        active = await db.scalar(
            sa.select(BackgroundJob.id).where(
                BackgroundJob.job_type == JOB_MAIL_SEND,
                BackgroundJob.payload["message_id"].as_string() == str(message.id),
                BackgroundJob.status.in_([JobStatus.queued, JobStatus.running]),
            )
        )
        if active is not None:
            continue
        await _enqueue(
            db,
            JOB_MAIL_SEND,
            {"message_id": str(message.id)},
            workspace_id=message.workspace_id,
            dedupe_key=f"mail-send-recover:{message.id}:{bucket}",
        )
        counts[JOB_MAIL_SEND] = counts.get(JOB_MAIL_SEND, 0) + 1
    actions = list(
        (
            await db.scalars(
                sa.select(MailMessage)
                .where(
                    MailMessage.account_id.in_([account.id for account in accounts]),
                    MailMessage.metadata_json["pending_action"]["version"]
                    .as_integer()
                    .is_not(None),
                )
                .order_by(MailMessage.updated_at, MailMessage.id)
                .limit(batch_size)
            )
        ).all()
    )
    for message in actions:
        pending = message.metadata_json["pending_action"]
        active = await db.scalar(
            sa.select(BackgroundJob.id).where(
                BackgroundJob.job_type == JOB_MAIL_ACTION,
                BackgroundJob.payload["message_id"].as_string() == str(message.id),
                BackgroundJob.status.in_([JobStatus.queued, JobStatus.running]),
            )
        )
        if active is not None:
            continue
        await _enqueue(
            db,
            JOB_MAIL_ACTION,
            {"message_id": str(message.id), "version": pending["version"]},
            workspace_id=message.workspace_id,
            dedupe_key=f"mail-action-recover:{message.id}:{pending['version']}:{bucket}",
        )
        counts[JOB_MAIL_ACTION] = counts.get(JOB_MAIL_ACTION, 0) + 1
    return counts


class MailRuntime:
    def __init__(
        self,
        *,
        cipher: SecretCipher,
        transport: MailTransport,
        session_factory: SessionFactory = SessionLocal,
        storage: AttachmentStorage | None = None,
    ) -> None:
        self.cipher = cipher
        self.transport = transport
        self.session_factory = session_factory
        self.storage = storage
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}

    def registry(self) -> dict[str, JobHandler]:
        return {JOB_MAIL_SYNC: self.sync, JOB_MAIL_SEND: self.send, JOB_MAIL_ACTION: self.action}

    def _lock(self, identifier: uuid.UUID) -> asyncio.Lock:
        return self._locks.setdefault(identifier, asyncio.Lock())

    async def _account(
        self,
        db: AsyncSession,
        account_id: uuid.UUID,
        job: ClaimedJob,
    ) -> MailAccount | None:
        account = await db.scalar(
            sa.select(MailAccount).where(
                MailAccount.id == account_id,
                MailAccount.workspace_id == job.workspace_id,
                MailAccount.status.in_(["active", "degraded"]),
                MailAccount.encrypted_credentials.is_not(None),
            )
        )
        if account is None:
            return None
        return account if await self._active_owner(db, account) else None

    @staticmethod
    async def _active_owner(db: AsyncSession, account: MailAccount) -> bool:
        allowed = await db.scalar(
            sa.select(Membership.id)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.workspace_id == account.workspace_id,
                Membership.user_id == account.owner_id,
                User.is_active.is_(True),
            )
        )
        return allowed is not None

    async def _error(self, original: MailAccount, message: str) -> None:
        async with self.session_factory() as db, db.begin():
            account = await db.get(MailAccount, original.id, with_for_update=True)
            if (
                account is not None
                and account.status in {"active", "degraded"}
                and account.version == original.version
                and account.encrypted_credentials == original.encrypted_credentials
                and await self._active_owner(db, account)
            ):
                account.status = "degraded"
                account.last_error = message

    async def sync(self, job: ClaimedJob) -> None:
        account_id = _id(job.payload, "account_id")
        async with self._lock(account_id):
            async with self.session_factory() as db:
                account = await self._account(db, account_id, job)
                if account is None:
                    return
                credentials = decrypt_credentials(account, self.cipher)
            try:
                if job.payload.get("folder_id"):
                    await self._sync_folder(account, credentials, job)
                else:
                    await self._discover(account, credentials)
            except (MailTransportError, ValueError):
                await self._error(
                    account, "Не удалось синхронизировать почту. Проверьте подключение."
                )
                raise MailTransportError("Mailbox synchronization failed") from None

    async def _discover(self, account: MailAccount, credentials: dict[str, str]) -> None:
        remote = await self.transport.folders(account, credentials)
        now = datetime.now(UTC)
        bucket = now.strftime("%Y%m%d%H%M")
        cleanup_keys: list[str] = []
        try:
            async with self.session_factory() as db, db.begin():
                current = await db.get(MailAccount, account.id, with_for_update=True)
                if (
                    current is None
                    or current.status not in {"active", "degraded"}
                    or current.version != account.version
                    or current.encrypted_credentials != account.encrypted_credentials
                    or not await self._active_owner(db, current)
                ):
                    return
                folders = list(
                    (
                        await db.scalars(
                            sa.select(MailFolder).where(
                                MailFolder.account_id == account.id,
                            )
                        )
                    ).all()
                )
                by_name = {folder.name: folder for folder in folders}
                remote_names = {info.name for info in remote}
                for removed_folder in folders:
                    if removed_folder.name.startswith("__") or removed_folder.name in remote_names:
                        continue
                    removed_ids = sa.select(MailMessage.id).where(
                        sa.or_(
                            MailMessage.folder_id == removed_folder.id,
                            MailMessage.metadata_json["pending_action"][
                                "source_folder_id"
                            ].as_string()
                            == str(removed_folder.id),
                        )
                    )
                    cleanup_keys.extend(
                        (
                            await db.scalars(
                                sa.select(MailAttachment.object_key).where(
                                    MailAttachment.message_id.in_(removed_ids),
                                )
                            )
                        ).all()
                    )
                    await db.execute(
                        sa.delete(MailAttachment).where(
                            MailAttachment.message_id.in_(removed_ids),
                        )
                    )
                    await db.execute(sa.delete(MailMessage).where(MailMessage.id.in_(removed_ids)))
                    await db.delete(removed_folder)
                for info in remote:
                    folder = by_name.get(info.name)
                    if folder is None:
                        folder = MailFolder(
                            workspace_id=account.workspace_id,
                            account_id=account.id,
                            name=info.name,
                            display_name=info.display_name,
                            kind=info.kind,
                        )
                        db.add(folder)
                        await db.flush()
                    else:
                        folder.display_name = info.display_name
                        folder.kind = info.kind
                    await _enqueue(
                        db,
                        JOB_MAIL_SYNC,
                        {"account_id": str(account.id), "folder_id": str(folder.id)},
                        workspace_id=account.workspace_id,
                        dedupe_key=f"mail-folder:{folder.id}:{bucket}",
                    )
                current.last_synced_at = now
                current.last_error = None
                current.status = "active"

        finally:
            await self._cleanup_objects(account.workspace_id, cleanup_keys)

    async def _sync_folder(
        self,
        account: MailAccount,
        credentials: dict[str, str],
        job: ClaimedJob,
    ) -> None:
        folder_id = _id(job.payload, "folder_id")
        async with self.session_factory() as db:
            folder = await db.scalar(
                sa.select(MailFolder).where(
                    MailFolder.id == folder_id,
                    MailFolder.account_id == account.id,
                )
            )
            if folder is None or folder.name.startswith("__"):
                return
            cached = list(
                (
                    await db.scalars(
                        sa.select(MailMessage)
                        .where(
                            MailMessage.folder_id == folder.id,
                            MailMessage.imap_uid.is_not(None),
                        )
                        .order_by(MailMessage.imap_uid)
                    )
                ).all()
            )
            old_validity, old_cursor = folder.uidvalidity, folder.last_uid
            offset = int(folder.sync_state.get("flags_offset", 0))
            known = [message.imap_uid for message in cached if message.imap_uid is not None]
            portion = tuple(known[offset : offset + 100])
        snapshot = await self.transport.sync_folder(
            account,
            credentials,
            path=folder.name,
            uidvalidity=old_validity,
            last_uid=old_cursor,
            sync_days=account.sync_days,
            known_uids=portion,
        )
        parsed_messages = [(item, parse_mail(item.raw)) for item in snapshot.messages]
        stored: dict[int, list[StoredObject]] = {}
        cleanup_keys: list[str] = []
        try:
            if self.storage is not None:
                for item, parsed in parsed_messages:
                    objects = []
                    for attachment in parsed.attachments:
                        objects.append(
                            await self.storage.store(
                                workspace_id=account.workspace_id,
                                filename=attachment.filename,
                                content_type=attachment.content_type,
                                content=attachment.content,
                            )
                        )
                        cleanup_keys.append(objects[-1].object_key)
                    stored[item.uid] = objects
            async with self.session_factory() as db, db.begin():
                current_account = await db.get(MailAccount, account.id, with_for_update=True)
                if (
                    current_account is None
                    or current_account.status not in {"active", "degraded"}
                    or current_account.version != account.version
                    or current_account.encrypted_credentials != account.encrypted_credentials
                    or not await self._active_owner(db, current_account)
                ):
                    return
                current_folder = await db.scalar(
                    sa.select(MailFolder)
                    .where(
                        MailFolder.id == folder_id,
                    )
                    .with_for_update()
                )
                if current_folder is None:
                    return
                if (current_folder.uidvalidity, current_folder.last_uid) != (
                    old_validity,
                    old_cursor,
                ):
                    return
                # A new UIDVALIDITY invalidates every old UID, even when the numbers
                # happen to coincide. CRM deal notes already contain independent snapshots.
                validity_changed = old_validity is not None and old_validity != snapshot.uidvalidity
                pending_messages = list(
                    (
                        await db.scalars(
                            sa.select(MailMessage).where(
                                MailMessage.account_id == account.id,
                                MailMessage.metadata_json["pending_action"][
                                    "source_folder_id"
                                ].as_string()
                                == str(folder_id),
                            )
                        )
                    ).all()
                )
                for pending_message in pending_messages:
                    pending = pending_message.metadata_json["pending_action"]
                    if pending.get("uidvalidity") != snapshot.uidvalidity:
                        await self._invalidate_action(db, pending_message, pending, cleanup_keys)
                await db.flush()
                messages = list(
                    (
                        await db.scalars(
                            sa.select(MailMessage).where(
                                MailMessage.folder_id == folder_id,
                            )
                        )
                    ).all()
                )
                by_uid = {
                    message.imap_uid: message
                    for message in messages
                    if message.imap_uidvalidity == snapshot.uidvalidity
                }
                for message in messages:
                    if message.imap_uid is None or message.metadata_json.get("pending_action"):
                        continue
                    if validity_changed or message.imap_uid not in snapshot.existing_uids:
                        await self._remove_cache_message(db, message, cleanup_keys)
                        continue
                    flags = snapshot.flags.get(message.imap_uid)
                    if flags is not None:
                        message.is_read = r"\Seen" in flags
                        message.is_starred = r"\Flagged" in flags
                await db.flush()
                for item, parsed in parsed_messages:
                    existing = by_uid.get(item.uid)
                    if existing is None and parsed.internet_message_id:
                        # Reuse our sent draft when it appears on IMAP; copies in
                        # distinct remote folders remain distinct cache entries.
                        existing = await db.scalar(
                            sa.select(MailMessage)
                            .where(
                                MailMessage.account_id == account.id,
                                MailMessage.internet_message_id == parsed.internet_message_id,
                                MailMessage.imap_uid.is_(None),
                                sa.or_(
                                    MailMessage.status.in_(["sent", "sending", "queued"]),
                                    MailMessage.metadata_json["moved_from"]
                                    .as_string()
                                    .is_not(None),
                                ),
                            )
                            .limit(1)
                        )
                    if existing is None:
                        values = parsed_values(parsed)
                        if item.truncated:
                            values["body_text"] = (
                                "Письмо превышает 32 МБ. "
                                "Откройте его в webmail для чтения и вложений."
                            )
                        existing = MailMessage(
                            workspace_id=account.workspace_id,
                            account_id=account.id,
                            folder_id=folder_id,
                            **values,
                            bcc_addresses=[],
                            body_html=None,
                            direction="outbound" if folder.kind == "sent" else "inbound",
                            status="sent" if folder.kind == "sent" else "received",
                            received_at=parsed.occurred_at,
                            sent_at=parsed.occurred_at if folder.kind == "sent" else None,
                            metadata_json={
                                "blocked_attachments": parsed.blocked_attachments,
                                "reply_to_addresses": parsed.reply_to_addresses,
                            },
                            last_error=(
                                "Некоторые вложения недоступны в CRM. Откройте письмо в webmail."
                                if parsed.blocked_attachments
                                or (parsed.attachments and self.storage is None)
                                else None
                            ),
                        )
                        db.add(existing)
                        await db.flush()
                        for obj in stored.get(item.uid, []):
                            db.add(
                                MailAttachment(
                                    workspace_id=account.workspace_id,
                                    message_id=existing.id,
                                    object_key=obj.object_key,
                                    original_filename=obj.attachment.filename,
                                    content_type=obj.attachment.content_type,
                                    size_bytes=obj.attachment.size_bytes,
                                    sha256=obj.attachment.sha256,
                                )
                            )
                    if not existing.metadata_json.get("pending_action"):
                        existing.folder_id = folder_id
                        existing.imap_uid = item.uid
                        existing.imap_uidvalidity = snapshot.uidvalidity
                        existing.is_read = r"\Seen" in item.flags
                        existing.is_starred = r"\Flagged" in item.flags
                current_folder.uidvalidity = snapshot.uidvalidity
                current_folder.last_uid = snapshot.last_uid
                current_folder.sync_state = {
                    "flags_offset": offset + 100 if offset + 100 < len(known) else 0,
                    "has_more": snapshot.has_more,
                }
                await db.flush()
                await self._counts(db, folder_id)
                if current_account.status in {"active", "degraded"}:
                    current_account.last_synced_at = datetime.now(UTC)
                    current_account.status = "active"
                    current_account.last_error = None
                if snapshot.has_more:
                    await _enqueue(
                        db,
                        JOB_MAIL_SYNC,
                        {"account_id": str(account.id), "folder_id": str(folder_id)},
                        workspace_id=account.workspace_id,
                        dedupe_key=f"mail-page:{folder_id}:{snapshot.uidvalidity}:{snapshot.last_uid}",
                    )

        finally:
            await self._cleanup_objects(account.workspace_id, cleanup_keys)

    @staticmethod
    async def _remove_cache_message(
        db: AsyncSession,
        message: MailMessage,
        cleanup_keys: list[str],
    ) -> None:
        cleanup_keys.extend(
            (
                await db.scalars(
                    sa.select(MailAttachment.object_key).where(
                        MailAttachment.message_id == message.id,
                    )
                )
            ).all()
        )
        await db.execute(sa.delete(MailAttachment).where(MailAttachment.message_id == message.id))
        await db.delete(message)

    async def _invalidate_action(
        self,
        db: AsyncSession,
        message: MailMessage,
        pending: dict[str, Any],
        cleanup_keys: list[str],
    ) -> None:
        source_id = _id(pending, "source_folder_id")
        if not message.internet_message_id or await db.get(MailFolder, source_id) is None:
            await self._remove_cache_message(db, message, cleanup_keys)
            return
        message.metadata_json = {
            key: value for key, value in message.metadata_json.items() if key != "pending_action"
        } | {"moved_from": str(source_id)}
        message.folder_id = source_id
        message.imap_uid = None
        message.imap_uidvalidity = None
        message.version += 1
        message.last_error = "Письмо изменилось на сервере. Проверьте данные и повторите изменение."

    async def _cleanup_objects(self, workspace_id: uuid.UUID, keys: list[str]) -> None:
        if self.storage is None or not keys:
            return
        try:
            async with self.session_factory() as db:
                referenced: set[str] = set()
                for model in (MailAttachment, NoteAttachment, Attachment):
                    referenced.update(
                        (
                            await db.scalars(
                                sa.select(model.object_key).where(
                                    model.workspace_id == workspace_id,
                                    model.object_key.in_(keys),
                                )
                            )
                        ).all()
                    )
            for key in set(keys) - referenced:
                try:
                    await self.storage.delete(workspace_id=workspace_id, object_key=key)
                except Exception:
                    logger.warning("Unused private mailbox object cleanup failed")
        except Exception:
            # Never delete if reference checks failed or mask a primary sync error.
            logger.warning("Private mailbox object reference check failed")

    @staticmethod
    async def _counts(db: AsyncSession, folder_id: uuid.UUID) -> None:
        folder = await db.get(MailFolder, folder_id)
        if folder is not None:
            folder.total_count = int(
                await db.scalar(
                    sa.select(sa.func.count()).where(
                        MailMessage.folder_id == folder_id,
                    )
                )
                or 0
            )
            folder.unread_count = int(
                await db.scalar(
                    sa.select(sa.func.count()).where(
                        MailMessage.folder_id == folder_id,
                        MailMessage.is_read.is_(False),
                    )
                )
                or 0
            )

    async def _outbound_bytes(self, message: MailMessage, account: MailAccount) -> bytes:
        async with self.session_factory() as db:
            attachments = list(
                (
                    await db.scalars(
                        sa.select(MailAttachment).where(
                            MailAttachment.message_id == message.id,
                            MailAttachment.workspace_id == account.workspace_id,
                        )
                    )
                ).all()
            )
        parts: list[ParsedAttachment] = []
        for item in attachments:
            if self.storage is None:
                raise AttachmentValidationError("Attachment storage is not configured")
            content = await self.storage.read_attachment(
                workspace_id=account.workspace_id,
                object_key=item.object_key,
                filename=item.original_filename,
                content_type=item.content_type,
                expected_size_bytes=item.size_bytes,
                expected_sha256=item.sha256,
            )
            parts.append(ParsedAttachment(item.original_filename, item.content_type, content))
        raw = build_mail(
            email=account.email,
            display_name=account.display_name,
            subject=message.subject,
            to_addresses=message.to_addresses,
            cc_addresses=message.cc_addresses,
            body_text=message.body_text,
            message_id=message.internet_message_id or "",
            sent_at=message.sent_at or datetime.now(UTC),
            in_reply_to=message.in_reply_to,
            references=message.references,
            attachments=tuple(parts),
        )
        if len(raw) > MAX_OUTBOUND_BYTES:
            raise AttachmentValidationError("Message exceeds the 30 MB sending limit")
        return raw

    async def send(self, job: ClaimedJob) -> None:
        message_id = _id(job.payload, "message_id")
        async with self._lock(message_id):
            async with self.session_factory() as db, db.begin():
                message = await db.scalar(
                    sa.select(MailMessage)
                    .where(
                        MailMessage.id == message_id,
                        MailMessage.workspace_id == job.workspace_id,
                    )
                    .with_for_update()
                )
                if message is None:
                    return
                if message.status == "sending":
                    message.status = "uncertain"
                    message.last_error = (
                        "Результат отправки неизвестен. Проверьте ящик перед повтором."
                    )
                    return
                account = await self._account(db, message.account_id, job)
                if account is None:
                    if message.status in {"queued", "sending"}:
                        message.status = "failed"
                        message.last_error = "Отправка отменена: доступ к ящику отключён."
                    return
                append_only = message.status == "sent" and bool(
                    message.metadata_json.get("sent_append_pending")
                )
                if message.status != "queued" and not append_only:
                    return
                credentials = decrypt_credentials(account, self.cipher)
                if not message.internet_message_id:
                    message.internet_message_id = make_msgid(domain="mail.pulse.invalid")
                if message.sent_at is None:
                    message.sent_at = datetime.now(UTC)
                if (
                    not message.in_reply_to
                    and not message.references
                    and not message.metadata_json.get("reply_to_message_id")
                ):
                    message.thread_key = message.internet_message_id
                else:
                    message.thread_key = (
                        message.thread_key
                        or next(iter(message.references), None)
                        or message.in_reply_to
                        or message.internet_message_id
                    )
                if not append_only:
                    message.status = "sending"
                    message.last_error = None
                await db.flush()
            try:
                raw = await self._outbound_bytes(message, account)
                if not append_only:
                    # Recheck membership immediately before the external send, after
                    # potentially slow object downloads and MIME assembly.
                    async with self.session_factory() as db:
                        current_account = await self._account(db, account.id, job)
                        current_message = await db.get(MailMessage, message_id)
                        if (
                            current_account is None
                            or current_account.version != account.version
                            or current_account.encrypted_credentials
                            != account.encrypted_credentials
                            or current_message is None
                            or current_message.status != "sending"
                        ):
                            await self._finish(
                                message_id, "failed", "Отправка отменена: доступ отключён."
                            )
                            return
                    await self.transport.send(
                        account,
                        credentials,
                        raw=raw,
                        recipients=tuple(
                            dict.fromkeys(
                                message.to_addresses + message.cc_addresses + message.bcc_addresses
                            )
                        ),
                    )
            except (MailDeliveryUncertain, asyncio.CancelledError):
                await self._finish(
                    message_id,
                    "sent" if append_only else "uncertain",
                    (
                        "Не удалось сохранить копию в отправленных."
                        if append_only
                        else "Результат отправки неизвестен. Проверьте ящик перед повтором."
                    ),
                )
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    raise
                return
            except AttachmentValidationError:
                await self._finish(
                    message_id,
                    "sent" if append_only else "failed",
                    "Проверьте вложения: общий размер письма не должен превышать 30 МБ.",
                )
                return
            except (MailTransportError, ValueError):
                if append_only:
                    await self._finish(
                        message_id, "sent", "Не удалось сохранить копию в отправленных."
                    )
                else:
                    status = "queued" if job.attempts < job.max_attempts else "failed"
                    await self._finish(message_id, status, "Не удалось отправить письмо.")
                raise MailTransportError("Mailbox send failed") from None
            if not append_only:
                async with self.session_factory() as db, db.begin():
                    current = await db.get(MailMessage, message_id)
                    if current is None:
                        return
                    current.status = "sent"
                    current.is_read = True
                    current.last_error = None
                    current.version += 1
                    current.metadata_json = {**current.metadata_json, "sent_append_pending": True}
            async with self.session_factory() as db:
                current_account = await self._account(db, account.id, job)
                if (
                    current_account is None
                    or current_account.version != account.version
                    or current_account.encrypted_credentials != account.encrypted_credentials
                ):
                    return
                sent_folder = await db.scalar(
                    sa.select(MailFolder)
                    .where(
                        MailFolder.account_id == account.id,
                        MailFolder.kind == "sent",
                    )
                    .order_by(MailFolder.id)
                    .limit(1)
                )
            if sent_folder is None:
                await self._finish(
                    message_id, "sent", "Папка отправленных ещё не синхронизирована."
                )
                return
            try:
                await self.transport.append_sent(
                    account,
                    credentials,
                    path=sent_folder.name,
                    raw=raw,
                    message_id=message.internet_message_id or "",
                )
            except MailTransportError:
                await self._finish(message_id, "sent", "Не удалось сохранить копию в отправленных.")
                raise
            async with self.session_factory() as db, db.begin():
                current = await db.get(MailMessage, message_id)
                if current is not None:
                    old_folder = current.folder_id
                    current.folder_id = sent_folder.id
                    current.metadata_json = {**current.metadata_json, "sent_append_pending": False}
                    current.last_error = None
                    await db.flush()
                    await self._counts(db, old_folder)
                    await self._counts(db, sent_folder.id)

    async def _finish(self, message_id: uuid.UUID, status: str, error: str) -> None:
        async with self.session_factory() as db, db.begin():
            account_id = await db.scalar(
                sa.select(MailMessage.account_id).where(
                    MailMessage.id == message_id,
                )
            )
            if account_id is None:
                return
            account = await db.get(MailAccount, account_id, with_for_update=True)
            message = await db.get(MailMessage, message_id, with_for_update=True)
            if message is not None:
                if status == "queued":
                    if (
                        account is None
                        or account.status not in {"active", "degraded"}
                        or not await self._active_owner(db, account)
                    ):
                        status = "failed"
                message.status = status
                message.last_error = error
                message.version += 1

    async def action(self, job: ClaimedJob) -> None:
        try:
            await self._apply_action(job)
        except MailTransportError:
            message_id = _id(job.payload, "message_id")
            async with self.session_factory() as db, db.begin():
                message = await db.get(MailMessage, message_id)
                if message is not None:
                    pending = message.metadata_json.get("pending_action")
                    if isinstance(pending, dict) and pending.get("version") == job.payload.get(
                        "version"
                    ):
                        message.last_error = (
                            "Изменение ещё не синхронизировано с сервером. CRM повторит попытку."
                        )
            raise

    async def _apply_action(self, job: ClaimedJob) -> None:
        message_id = _id(job.payload, "message_id")
        cleanup_keys: list[str] = []
        try:
            async with self._lock(message_id):
                # Locking this one letter also serializes local edits until a remote
                # MOVE result has been saved, so a newer edit cannot reuse an old UID.
                async with self.session_factory() as db, db.begin():
                    message = await db.scalar(
                        sa.select(MailMessage)
                        .where(
                            MailMessage.id == message_id,
                            MailMessage.workspace_id == job.workspace_id,
                        )
                        .with_for_update()
                    )
                    if message is None:
                        return
                    pending = message.metadata_json.get("pending_action")
                    if not isinstance(pending, dict) or pending.get("version") != job.payload.get(
                        "version"
                    ):
                        return
                    account = await self._account(db, message.account_id, job)
                    if account is None:
                        message.last_error = "Изменение отменено: доступ к ящику отключён."
                        return
                    try:
                        await self.transport.action(
                            account,
                            decrypt_credentials(account, self.cipher),
                            path=str(pending["source_name"]),
                            uid=int(pending["uid"]),
                            uidvalidity=int(pending["uidvalidity"]),
                            is_read=bool(pending["is_read"]),
                            is_starred=bool(pending["is_starred"]),
                            target_path=pending.get("target_name"),
                            message_id=message.internet_message_id,
                        )
                    except MailIdentityChanged:
                        await self._invalidate_action(db, message, pending, cleanup_keys)
                        await _enqueue(
                            db,
                            JOB_MAIL_SYNC,
                            {
                                "account_id": str(account.id),
                                "folder_id": pending["source_folder_id"],
                            },
                            workspace_id=account.workspace_id,
                            dedupe_key=f"mail-identity-sync:{message.id}:{pending['version']}",
                        )
                        return
                    except MailTransportError:
                        # Keep the desired action durable; repeating a flag assignment
                        # or a UID MOVE is idempotent even after a lost server reply.
                        raise MailTransportError("Mailbox change failed") from None
                    source_id = _id(pending, "source_folder_id")
                    target_id = _id(pending, "target_folder_id")
                    message.metadata_json = {
                        key: value
                        for key, value in message.metadata_json.items()
                        if key != "pending_action"
                    }
                    if source_id != target_id:
                        message.imap_uid = None
                        message.imap_uidvalidity = None
                        # Discover the destination UID on the next sync by Message-ID.
                        message.metadata_json = {
                            **message.metadata_json,
                            "moved_from": str(source_id),
                        }
                    else:
                        # A newer edit can cancel a queued optimistic move back
                        # to its original folder before any remote operation ran.
                        message.imap_uid = int(pending["uid"])
                        message.imap_uidvalidity = int(pending["uidvalidity"])
                    message.last_error = None
                    await db.flush()
                    await self._counts(db, source_id)
                    await self._counts(db, target_id)

        finally:
            if job.workspace_id is not None:
                await self._cleanup_objects(job.workspace_id, cleanup_keys)

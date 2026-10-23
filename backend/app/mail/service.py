"""Owner checks and private mail helpers; workspace roles never widen mailbox access."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from email_validator import EmailNotValidError, validate_email
from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.s3 import AttachmentStorage
from app.integrations.secrets import SecretCipher, SecretCipherError
from app.mail.models import MailAccount, MailAttachment, MailFolder, MailLink, MailMessage
from app.mail.schemas import AccountRead, AttachmentRead, MessageRead
from app.models import Deal
from app.security import AuthContext
from app.services.access import deal_access_condition


def credential_aad(account_id: uuid.UUID) -> bytes:
    return f"mail:{account_id}".encode()


def decrypt_credentials(account: MailAccount, cipher: SecretCipher) -> dict[str, str]:
    if not account.encrypted_credentials or account.credentials_key_id != cipher.key_id:
        raise SecretCipherError("mail credentials are unavailable")
    try:
        payload = json.loads(
            cipher.decrypt(
                account.encrypted_credentials, associated_data=credential_aad(account.id)
            )
        )
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("username"), str)
            or not isinstance(payload.get("password"), str)
            or not payload["username"]
            or not payload["password"]
        ):
            raise ValueError
        return {"username": payload["username"], "password": payload["password"]}
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise SecretCipherError("mail credentials are unavailable") from exc


def cipher_for(request: Request) -> SecretCipher:
    cipher = getattr(request.app.state, "integration_secret_cipher", None)
    if not isinstance(cipher, SecretCipher):
        raise HTTPException(503, "mail encryption is not configured")
    return cipher


def storage_for(request: Request) -> AttachmentStorage:
    storage = getattr(request.app.state, "attachment_storage", None)
    if not isinstance(storage, AttachmentStorage):
        raise HTTPException(503, "attachment storage is not configured")
    return storage


async def require_account(
    db: AsyncSession, context: AuthContext, account_id: uuid.UUID, *, lock: bool = False
) -> MailAccount:
    statement = sa.select(MailAccount).where(
        MailAccount.id == account_id,
        MailAccount.workspace_id == context.workspace_id,
        MailAccount.owner_id == context.user_id,
        MailAccount.status != "disconnected",
    )
    if lock:
        statement = statement.with_for_update()
    account = await db.scalar(statement)
    if account is None:
        raise HTTPException(404, "mail account not found")
    return account


async def require_message(
    db: AsyncSession, context: AuthContext, message_id: uuid.UUID, *, lock: bool = False
) -> tuple[MailMessage, MailAccount]:
    statement = (
        sa.select(MailMessage, MailAccount)
        .join(MailAccount, MailAccount.id == MailMessage.account_id)
        .where(
            MailMessage.id == message_id,
            MailMessage.workspace_id == context.workspace_id,
            MailAccount.workspace_id == context.workspace_id,
            MailAccount.owner_id == context.user_id,
            MailAccount.status != "disconnected",
        )
    )
    if lock:
        statement = statement.with_for_update(of=MailMessage)
    row = (await db.execute(statement)).one_or_none()
    if row is None:
        raise HTTPException(404, "mail message not found")
    return row[0], row[1]


async def require_folder(
    db: AsyncSession, account: MailAccount, folder_id: uuid.UUID
) -> MailFolder:
    folder = await db.scalar(
        sa.select(MailFolder).where(
            MailFolder.id == folder_id,
            MailFolder.account_id == account.id,
            MailFolder.workspace_id == account.workspace_id,
        )
    )
    if folder is None:
        raise HTTPException(404, "mail folder not found")
    return folder


def require_version(actual: int, expected: int) -> None:
    if actual != expected:
        raise HTTPException(409, "mail item has changed; reload and try again")


def account_read(account: MailAccount) -> AccountRead:
    return AccountRead(
        **{
            name: getattr(account, name)
            for name in AccountRead.model_fields
            if name != "has_credentials"
        },
        has_credentials=account.encrypted_credentials is not None,
    )


def _reply_to_addresses(message: MailMessage) -> list[str]:
    values = message.metadata_json.get("reply_to_addresses", [])
    if not isinstance(values, list):
        return []
    addresses: list[str] = []
    for value in values[:100]:
        if not isinstance(value, str) or any(character in value for character in "\r\n\x00"):
            continue
        try:
            addresses.append(validate_email(value, check_deliverability=False).normalized)
        except EmailNotValidError:
            continue
    return addresses


async def message_reads(
    db: AsyncSession,
    context: AuthContext,
    messages: Sequence[MailMessage],
    *,
    summary: bool = False,
) -> list[MessageRead]:
    if not messages:
        return []
    ids = [item.id for item in messages]
    attachments: dict[uuid.UUID, list[AttachmentRead]] = {}
    for item in await db.scalars(
        sa.select(MailAttachment)
        .where(
            MailAttachment.message_id.in_(ids), MailAttachment.workspace_id == context.workspace_id
        )
        .order_by(MailAttachment.created_at, MailAttachment.id)
    ):
        attachments.setdefault(item.message_id, []).append(AttachmentRead.model_validate(item))
    links: dict[uuid.UUID, list[uuid.UUID]] = {}
    for message_id, deal_id in await db.execute(
        sa.select(MailLink.message_id, MailLink.deal_id)
        .join(Deal, Deal.id == MailLink.deal_id)
        .where(
            MailLink.message_id.in_(ids),
            MailLink.workspace_id == context.workspace_id,
            Deal.workspace_id == context.workspace_id,
            Deal.deleted_at.is_(None),
            deal_access_condition(context),
        )
    ):
        links.setdefault(message_id, []).append(deal_id)
    return [
        MessageRead.model_validate(item).model_copy(
            update={
                "attachments": attachments.get(item.id, []),
                "linked_deal_ids": links.get(item.id, []),
                "body_text": item.body_text[:240] if summary else item.body_text,
                # HTML remains private source data and is never exposed for execution.
                "body_html": None,
                "bcc_addresses": item.bcc_addresses if item.direction == "outbound" else [],
                "reply_to_addresses": _reply_to_addresses(item),
            }
        )
        for item in messages
    ]


async def message_read(db: AsyncSession, context: AuthContext, message: MailMessage) -> MessageRead:
    return (await message_reads(db, context, [message]))[0]

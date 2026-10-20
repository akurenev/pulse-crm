from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import JSON_TYPE, UUID_TYPE, TimestampMixin, UUIDPrimaryKeyMixin


class MailAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "mail_accounts"
    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id", "owner_id", "email", name="uq_mail_account_owner_email"
        ),
        sa.Index("ix_mail_accounts_owner", "workspace_id", "owner_id", "created_at"),
        sa.Index("ix_mail_accounts_status", "status", "last_synced_at"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(sa.String(160), default="", nullable=False)
    username: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    imap_host: Mapped[str] = mapped_column(sa.String(253), nullable=False)
    imap_port: Mapped[int] = mapped_column(sa.Integer, default=993, nullable=False)
    imap_security: Mapped[str] = mapped_column(sa.String(16), default="ssl", nullable=False)
    smtp_host: Mapped[str] = mapped_column(sa.String(253), nullable=False)
    smtp_port: Mapped[int] = mapped_column(sa.Integer, default=587, nullable=False)
    smtp_security: Mapped[str] = mapped_column(sa.String(16), default="starttls", nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), default="active", nullable=False)
    sync_days: Mapped[int] = mapped_column(sa.Integer, default=30, nullable=False)
    encrypted_credentials: Mapped[bytes | None] = mapped_column(sa.LargeBinary)
    credentials_key_id: Mapped[str | None] = mapped_column(sa.String(100))
    last_synced_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(sa.String(300))
    version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)


class MailFolder(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "mail_folders"
    __table_args__ = (
        sa.UniqueConstraint("account_id", "name", name="uq_mail_folder_account_name"),
        sa.Index("ix_mail_folders_account", "workspace_id", "account_id"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("mail_accounts.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    display_name: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(16), default="custom", nullable=False)
    unread_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    total_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    uidvalidity: Mapped[int | None] = mapped_column(sa.BigInteger)
    last_uid: Mapped[int] = mapped_column(sa.BigInteger, default=0, nullable=False)
    sync_state: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)


class MailMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "mail_messages"
    __table_args__ = (
        sa.UniqueConstraint(
            "folder_id", "imap_uidvalidity", "imap_uid", name="uq_mail_message_uid"
        ),
        sa.Index(
            "ix_mail_messages_account_created", "workspace_id", "account_id", "created_at", "id"
        ),
        sa.Index("ix_mail_messages_folder", "folder_id", "created_at", "id"),
        sa.Index("ix_mail_messages_internet_id", "account_id", "internet_message_id"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("mail_accounts.id", ondelete="CASCADE"), nullable=False
    )
    folder_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("mail_folders.id", ondelete="CASCADE"), nullable=False
    )
    subject: Mapped[str] = mapped_column(sa.String(998), default="", nullable=False)
    from_address: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    from_name: Mapped[str | None] = mapped_column(sa.String(320))
    to_addresses: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    cc_addresses: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    bcc_addresses: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    body_text: Mapped[str] = mapped_column(sa.Text, default="", nullable=False)
    body_html: Mapped[str | None] = mapped_column(sa.Text)
    internet_message_id: Mapped[str | None] = mapped_column(sa.String(998))
    in_reply_to: Mapped[str | None] = mapped_column(sa.String(998))
    references: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    thread_key: Mapped[str] = mapped_column(sa.String(998), default="", nullable=False)
    direction: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    is_read: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    is_starred: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(sa.String(300))
    imap_uid: Mapped[int | None] = mapped_column(sa.BigInteger)
    imap_uidvalidity: Mapped[int | None] = mapped_column(sa.BigInteger)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)


class MailAttachment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "mail_attachments"
    __table_args__ = (
        sa.UniqueConstraint("object_key", name="uq_mail_attachment_object_key"),
        sa.Index("ix_mail_attachments_message", "workspace_id", "message_id"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("mail_messages.id", ondelete="CASCADE"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(sa.String(1024), nullable=False)
    original_filename: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)


class MailLink(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "mail_links"
    __table_args__ = (
        sa.UniqueConstraint("message_id", "deal_id", name="uq_mail_link_message_deal"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("mail_messages.id", ondelete="CASCADE"), nullable=False
    )
    deal_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("deals.id", ondelete="CASCADE"), nullable=False
    )
    note_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, sa.ForeignKey("activity_events.id", ondelete="CASCADE"), nullable=False
    )

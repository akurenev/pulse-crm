from __future__ import annotations

import ipaddress
import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


class AccountCreate(BaseModel):
    email: EmailStr
    display_name: str = Field(default="", max_length=160)
    username: str | None = Field(default=None, max_length=320)
    password: str = Field(min_length=1, max_length=4096, repr=False)
    imap_host: str = Field(min_length=1, max_length=253)
    imap_port: int = Field(default=993, ge=1, le=65535)
    imap_security: Literal["ssl", "starttls"] = "ssl"
    smtp_host: str = Field(min_length=1, max_length=253)
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_security: Literal["ssl", "starttls"] = "starttls"
    sync_days: int = Field(default=30, ge=1, le=365)

    @field_validator("password")
    @classmethod
    def valid_password(cls, value: str) -> str:
        if any(character in value for character in "\r\n\x00"):
            raise ValueError("password cannot contain line breaks or NUL")
        return value

    @field_validator("imap_host", "smtp_host")
    @classmethod
    def valid_host(cls, value: str) -> str:
        value = value.strip().lower().rstrip(".")
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            if (
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value)
                or "." not in value
                or value.endswith((".local", ".localhost"))
                or any(not label or len(label) > 63 for label in value.split("."))
            ):
                raise ValueError("use a public mail server hostname") from None
        else:
            if not address.is_global:
                raise ValueError("use a public mail server address")
        return value

    @field_validator("display_name", "username")
    @classmethod
    def no_header_breaks(cls, value: str | None) -> str | None:
        if value is not None and any(character in value for character in "\r\n\x00"):
            raise ValueError("header values cannot contain line breaks")
        return value.strip() if value is not None else None


class AccountUpdate(BaseModel):
    expected_version: int = Field(ge=1)
    display_name: str | None = Field(default=None, max_length=160)
    password: str | None = Field(default=None, min_length=1, max_length=4096, repr=False)
    status: Literal["active", "disabled"] | None = None
    sync_days: int | None = Field(default=None, ge=1, le=365)

    @field_validator("password")
    @classmethod
    def valid_password(cls, value: str | None) -> str | None:
        return AccountCreate.valid_password(value) if value is not None else None

    @field_validator("display_name")
    @classmethod
    def valid_name(cls, value: str | None) -> str | None:
        return AccountCreate.no_header_breaks(value)


class AccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: str
    display_name: str
    username: str
    imap_host: str
    imap_port: int
    imap_security: str
    smtp_host: str
    smtp_port: int
    smtp_security: str
    status: str
    sync_days: int
    last_synced_at: datetime | None
    last_error: str | None
    has_credentials: bool
    version: int
    created_at: datetime
    updated_at: datetime


class AccountPage(BaseModel):
    items: list[AccountRead]


class FolderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    account_id: uuid.UUID
    name: str
    display_name: str
    kind: str
    unread_count: int
    total_count: int


class FolderPage(BaseModel):
    items: list[FolderRead]


class Compose(BaseModel):
    subject: str = Field(default="", max_length=998)
    to_addresses: list[EmailStr] = Field(default_factory=list, max_length=100)
    cc_addresses: list[EmailStr] = Field(default_factory=list, max_length=100)
    bcc_addresses: list[EmailStr] = Field(default_factory=list, max_length=100)
    body_text: str = Field(default="", max_length=100_000)
    reply_to_message_id: uuid.UUID | None = None
    forward_message_id: uuid.UUID | None = None

    @field_validator("subject")
    @classmethod
    def valid_subject(cls, value: str) -> str:
        if any(character in value for character in "\r\n\x00"):
            raise ValueError("subject cannot contain line breaks")
        return value.strip()

    @model_validator(mode="after")
    def bounded_recipients(self) -> Compose:
        if len(self.to_addresses) + len(self.cc_addresses) + len(self.bcc_addresses) > 100:
            raise ValueError("a message can have at most 100 recipients")
        if self.reply_to_message_id and self.forward_message_id:
            raise ValueError("a draft cannot be both a reply and a forward")
        return self


class DraftUpdate(Compose):
    expected_version: int = Field(ge=1)


class VersionRequest(BaseModel):
    expected_version: int = Field(ge=1)


class MessageUpdate(VersionRequest):
    is_read: bool | None = None
    is_starred: bool | None = None
    folder_id: uuid.UUID | None = None


class AttachmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    message_id: uuid.UUID
    original_filename: str
    content_type: str
    size_bytes: int
    sha256: str


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    account_id: uuid.UUID
    folder_id: uuid.UUID
    subject: str
    from_address: str
    from_name: str | None
    to_addresses: list[str]
    cc_addresses: list[str]
    bcc_addresses: list[str]
    reply_to_addresses: list[str] = Field(default_factory=list)
    body_text: str
    body_html: str | None = None
    internet_message_id: str | None
    in_reply_to: str | None
    references: list[str]
    thread_key: str
    direction: str
    status: str
    is_read: bool
    is_starred: bool
    sent_at: datetime | None
    received_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int
    last_error: str | None
    attachments: list[AttachmentRead] = Field(default_factory=list)
    linked_deal_ids: list[uuid.UUID] = Field(default_factory=list)


class MessagePage(BaseModel):
    items: list[MessageRead]
    next_cursor: str | None = None


class CheckRead(BaseModel):
    healthy: bool
    detail: str | None = None


class QueueRead(BaseModel):
    queued: bool


class DownloadRead(BaseModel):
    url: str
    expires_in: int


class LinkCreate(BaseModel):
    deal_id: uuid.UUID


class LinkRead(BaseModel):
    deal_id: uuid.UUID
    note_id: uuid.UUID

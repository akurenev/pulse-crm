from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


class ChannelVerificationError(ValueError):
    """Raised when a provider webhook or envelope cannot be trusted."""


class ChannelPayloadError(ValueError):
    """Raised when a provider event is valid JSON but not an inbound message."""


@dataclass(frozen=True, slots=True)
class AttachmentReference:
    provider_file_id: str
    filename: str
    content_type: str | None = None
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class OutboundAttachment:
    """A validated private object materialized only for one send attempt."""

    filename: str
    content_type: str
    content: bytes

    @property
    def size_bytes(self) -> int:
        return len(self.content)


@dataclass(frozen=True, slots=True)
class NormalizedInboundMessage:
    provider: str
    event_id: str
    message_id: str
    thread_id: str
    sender_id: str
    sender_display_name: str | None
    text: str
    occurred_at: datetime
    attachments: tuple[AttachmentReference, ...] = ()
    reply_to_message_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    thread_id: str
    text: str
    recipient_id: str | None = None
    reply_to_message_id: str | None = None
    attachments: tuple[OutboundAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class SendResult:
    provider_message_id: str
    sent_at: datetime


@dataclass(frozen=True, slots=True)
class AdapterHealth:
    healthy: bool
    detail: str | None = None


class ChannelAdapter(Protocol):
    provider: str

    async def verify(self, headers: Mapping[str, str], body: bytes) -> None: ...

    def normalize_inbound(self, payload: Mapping[str, Any]) -> NormalizedInboundMessage: ...

    async def send_message(self, message: OutboundMessage) -> SendResult: ...

    async def download_attachment(self, reference: AttachmentReference) -> bytes: ...

    async def healthcheck(self) -> AdapterHealth: ...

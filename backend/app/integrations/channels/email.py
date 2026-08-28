from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Protocol

from app.integrations.channels.base import (
    AdapterHealth,
    AttachmentReference,
    ChannelPayloadError,
    NormalizedInboundMessage,
    OutboundAttachment,
    OutboundMessage,
    SendResult,
)


@dataclass(frozen=True, slots=True)
class EmailEnvelope:
    uidvalidity: int
    uid: int
    raw_message: bytes


class EmailTransport(Protocol):
    async def send_message(
        self,
        *,
        recipient: str,
        text: str,
        in_reply_to: str | None,
        attachments: tuple[OutboundAttachment, ...] = (),
    ) -> tuple[str, datetime]: ...

    async def healthcheck(self) -> AdapterHealth: ...


class EmailAdapter:
    """Normalize IMAP messages and send replies through an injected SMTP client."""

    provider = "email"

    def __init__(self, transport: EmailTransport) -> None:
        self._transport = transport
        self._attachments: dict[str, bytes] = {}

    async def verify(self, headers: Mapping[str, str], body: bytes) -> None:
        del headers, body
        # IMAP authentication happens when the poller opens the mailbox.  An
        # email is trusted only after it has been fetched by that connection.

    def normalize_envelope(self, envelope: EmailEnvelope) -> NormalizedInboundMessage:
        parsed = BytesParser(policy=policy.default).parsebytes(envelope.raw_message)
        message_id = _message_id(parsed) or f"imap:{envelope.uidvalidity}:{envelope.uid}"
        references = _references(parsed)
        thread_id = references[-1] if references else message_id
        sender_name, sender_address = _sender(parsed)
        attachments = self._extract_attachments(envelope, parsed)
        return NormalizedInboundMessage(
            provider=self.provider,
            event_id=f"{envelope.uidvalidity}:{envelope.uid}",
            message_id=message_id,
            thread_id=thread_id,
            sender_id=sender_address,
            sender_display_name=sender_name,
            text=_plain_body(parsed),
            occurred_at=_message_date(parsed),
            attachments=attachments,
            reply_to_message_id=(parsed.get("In-Reply-To") or "").strip() or None,
            metadata={"subject": str(parsed.get("Subject") or "")},
        )

    def normalize_inbound(self, payload: Mapping[str, Any]) -> NormalizedInboundMessage:
        envelope = payload.get("envelope")
        if not isinstance(envelope, EmailEnvelope):
            raise ChannelPayloadError("email normalization requires an EmailEnvelope")
        return self.normalize_envelope(envelope)

    async def send_message(self, message: OutboundMessage) -> SendResult:
        provider_id, sent_at = await self._transport.send_message(
            recipient=message.recipient_id or message.thread_id,
            text=message.text,
            in_reply_to=message.reply_to_message_id,
            attachments=message.attachments,
        )
        return SendResult(provider_message_id=provider_id, sent_at=sent_at)

    async def download_attachment(self, reference: AttachmentReference) -> bytes:
        try:
            return self._attachments.pop(reference.provider_file_id)
        except KeyError as exc:
            raise ChannelPayloadError("email attachment is no longer available") from exc

    async def healthcheck(self) -> AdapterHealth:
        return await self._transport.healthcheck()

    def _extract_attachments(
        self, envelope: EmailEnvelope, parsed: Message
    ) -> tuple[AttachmentReference, ...]:
        result: list[AttachmentReference] = []
        for index, part in enumerate(parsed.walk()):
            filename = part.get_filename()
            disposition = part.get_content_disposition()
            if not filename and disposition != "attachment":
                continue
            content = part.get_payload(decode=True)
            if not isinstance(content, bytes):
                continue
            reference = f"{envelope.uidvalidity}:{envelope.uid}:{index}"
            self._attachments[reference] = content
            result.append(
                AttachmentReference(
                    provider_file_id=reference,
                    filename=filename or f"email-attachment-{index}",
                    content_type=part.get_content_type(),
                    size_bytes=len(content),
                )
            )
        return tuple(result)


def _message_id(message: Message) -> str | None:
    value = (message.get("Message-ID") or "").strip()
    return value or None


def _references(message: Message) -> list[str]:
    value = " ".join(
        part for part in (message.get("References"), message.get("In-Reply-To")) if part
    )
    return [item.strip() for item in value.split() if item.strip()]


def _sender(message: Message) -> tuple[str | None, str]:
    addresses = getaddresses(message.get_all("From", []))
    if not addresses or not addresses[0][1]:
        raise ChannelPayloadError("email message does not contain a sender")
    name, address = addresses[0]
    return (name or None, address.casefold())


def _message_date(message: Message) -> datetime:
    raw = message.get("Date")
    if raw:
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except (TypeError, ValueError, OverflowError):
            pass
    return datetime.now(UTC)


def _plain_body(message: Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() != "text/plain":
                continue
            if part.get_content_disposition() == "attachment":
                continue
            raw = part.get_payload(decode=True)
            if isinstance(raw, bytes):
                charset = part.get_content_charset() or "utf-8"
                try:
                    return raw.decode(charset, errors="replace").strip()
                except LookupError:
                    return raw.decode("utf-8", errors="replace").strip()
            plain = part.get_payload()
            if isinstance(plain, str):
                return plain.strip()
        return ""
    raw = message.get_payload(decode=True)
    if isinstance(raw, bytes):
        charset = message.get_content_charset() or "utf-8"
        try:
            return raw.decode(charset, errors="replace").strip()
        except LookupError:
            return raw.decode("utf-8", errors="replace").strip()
    plain = message.get_payload()
    return plain.strip() if isinstance(plain, str) else ""

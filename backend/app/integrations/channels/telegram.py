from __future__ import annotations

import hmac
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from app.integrations.channels.base import (
    AdapterHealth,
    AttachmentReference,
    ChannelPayloadError,
    ChannelVerificationError,
    NormalizedInboundMessage,
    OutboundAttachment,
    OutboundMessage,
    SendResult,
)


class TelegramTransport(Protocol):
    async def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        reply_to_message_id: str | None,
        attachments: tuple[OutboundAttachment, ...] = (),
    ) -> tuple[str, datetime]: ...

    async def download_file(self, file_id: str) -> bytes: ...

    async def healthcheck(self) -> AdapterHealth: ...


class TelegramAdapter:
    provider = "telegram"

    def __init__(self, webhook_secret: str, transport: TelegramTransport) -> None:
        if not webhook_secret:
            raise ValueError("Telegram webhook secret must not be empty")
        self._webhook_secret = webhook_secret
        self._transport = transport

    async def verify(self, headers: Mapping[str, str], body: bytes) -> None:
        del body
        supplied = _header(headers, "x-telegram-bot-api-secret-token")
        if supplied is None or not hmac.compare_digest(supplied, self._webhook_secret):
            raise ChannelVerificationError("invalid Telegram webhook secret")

    def normalize_inbound(self, payload: Mapping[str, Any]) -> NormalizedInboundMessage:
        event_id = _required_scalar(payload, "update_id")
        message = payload.get("message") or payload.get("edited_message")
        if not isinstance(message, Mapping):
            raise ChannelPayloadError("Telegram update does not contain a message")

        chat = _required_mapping(message, "chat")
        sender = _required_mapping(message, "from")
        chat_id = _required_scalar(chat, "id")
        sender_id = _required_scalar(sender, "id")
        message_id = _required_scalar(message, "message_id")
        occurred_at = datetime.fromtimestamp(int(_required_scalar(message, "date")), UTC)
        display_name = (
            " ".join(
                value
                for value in (sender.get("first_name"), sender.get("last_name"))
                if isinstance(value, str) and value.strip()
            )
            or None
        )
        reply_to = message.get("reply_to_message")
        reply_to_id = None
        if isinstance(reply_to, Mapping) and reply_to.get("message_id") is not None:
            reply_to_id = str(reply_to["message_id"])

        return NormalizedInboundMessage(
            provider=self.provider,
            event_id=str(event_id),
            message_id=str(message_id),
            thread_id=str(chat_id),
            sender_id=str(sender_id),
            sender_display_name=display_name,
            text=str(message.get("text") or message.get("caption") or ""),
            occurred_at=occurred_at,
            attachments=_telegram_attachments(message),
            reply_to_message_id=reply_to_id,
            metadata={"username": sender.get("username"), "chat_type": chat.get("type")},
        )

    async def send_message(self, message: OutboundMessage) -> SendResult:
        provider_id, sent_at = await self._transport.send_message(
            chat_id=message.recipient_id or message.thread_id,
            text=message.text,
            reply_to_message_id=message.reply_to_message_id,
            attachments=message.attachments,
        )
        return SendResult(provider_message_id=provider_id, sent_at=sent_at)

    async def download_attachment(self, reference: AttachmentReference) -> bytes:
        return await self._transport.download_file(reference.provider_file_id)

    async def healthcheck(self) -> AdapterHealth:
        return await self._transport.healthcheck()


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    return next((value for key, value in headers.items() if key.lower() == lowered), None)


def _required_mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        raise ChannelPayloadError(f"Telegram message is missing {key}")
    return nested


def _required_scalar(value: Mapping[str, Any], key: str) -> str | int:
    scalar = value.get(key)
    if not isinstance(scalar, (str, int)):
        raise ChannelPayloadError(f"Telegram update is missing {key}")
    return scalar


def _telegram_attachments(message: Mapping[str, Any]) -> tuple[AttachmentReference, ...]:
    document = message.get("document")
    if isinstance(document, Mapping) and document.get("file_id") is not None:
        return (
            AttachmentReference(
                provider_file_id=str(document["file_id"]),
                filename=str(document.get("file_name") or "telegram-document"),
                content_type=(str(document["mime_type"]) if document.get("mime_type") else None),
                size_bytes=(int(document["file_size"]) if document.get("file_size") else None),
            ),
        )

    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        candidates = [photo for photo in photos if isinstance(photo, Mapping)]
        if candidates:
            largest = max(candidates, key=lambda photo: int(photo.get("file_size") or 0))
            if largest.get("file_id") is not None:
                return (
                    AttachmentReference(
                        provider_file_id=str(largest["file_id"]),
                        filename="telegram-photo.jpg",
                        content_type="image/jpeg",
                        size_bytes=(
                            int(largest["file_size"]) if largest.get("file_size") else None
                        ),
                    ),
                )
    return ()

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


class MaxTransport(Protocol):
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


class MaxAdapter:
    """MAX Bot API adapter for ``message_created`` webhook updates.

    MAX sends the subscription secret in ``X-Max-Bot-Api-Secret``.  Network
    calls stay in an injected transport so access tokens never enter logs or
    normalized event payloads.
    """

    provider = "max"

    def __init__(self, webhook_secret: str, transport: MaxTransport) -> None:
        if not webhook_secret:
            raise ValueError("MAX webhook secret must not be empty")
        self._webhook_secret = webhook_secret
        self._transport = transport

    async def verify(self, headers: Mapping[str, str], body: bytes) -> None:
        del body
        supplied = _header(headers, "x-max-bot-api-secret")
        if supplied is None or not hmac.compare_digest(supplied, self._webhook_secret):
            raise ChannelVerificationError("invalid MAX webhook secret")

    def normalize_inbound(self, payload: Mapping[str, Any]) -> NormalizedInboundMessage:
        if payload.get("update_type") != "message_created":
            raise ChannelPayloadError("MAX update is not message_created")
        message = payload.get("message")
        if not isinstance(message, Mapping):
            raise ChannelPayloadError("MAX update does not contain a message")

        sender = message.get("sender")
        recipient = message.get("recipient")
        body = message.get("body")
        if not isinstance(sender, Mapping) or not isinstance(recipient, Mapping):
            raise ChannelPayloadError("MAX message is missing sender or recipient")
        if not isinstance(body, Mapping):
            body = {}

        message_id = _first_scalar(message, "mid", "message_id", "id")
        sender_id = _first_scalar(sender, "user_id", "id")
        chat_id = _first_scalar(recipient, "chat_id") or _first_scalar(payload, "chat_id")
        if message_id is None or sender_id is None or chat_id is None:
            raise ChannelPayloadError("MAX message has incomplete identifiers")

        timestamp_ms = _first_scalar(message, "timestamp") or _first_scalar(payload, "timestamp")
        if timestamp_ms is None:
            raise ChannelPayloadError("MAX message has no timestamp")
        occurred_at = datetime.fromtimestamp(int(timestamp_ms) / 1000, UTC)
        event_id = f"{payload.get('update_type')}:{message_id}"
        display_name = sender.get("name") or sender.get("first_name")
        link = message.get("link")
        reply_to_id = None
        if isinstance(link, Mapping) and link.get("mid") is not None:
            reply_to_id = str(link["mid"])

        return NormalizedInboundMessage(
            provider=self.provider,
            event_id=event_id,
            message_id=str(message_id),
            thread_id=str(chat_id),
            sender_id=str(sender_id),
            sender_display_name=str(display_name) if display_name else None,
            text=str(body.get("text") or ""),
            occurred_at=occurred_at,
            attachments=_max_attachments(body),
            reply_to_message_id=reply_to_id,
            metadata={"update_type": payload.get("update_type")},
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


def _first_scalar(value: Mapping[str, Any], *keys: str) -> str | int | None:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, (str, int)):
            return candidate
    return None


def _max_attachments(body: Mapping[str, Any]) -> tuple[AttachmentReference, ...]:
    raw = body.get("attachments")
    if not isinstance(raw, list):
        return ()
    result: list[AttachmentReference] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            continue
        payload = item.get("payload")
        if not isinstance(payload, Mapping):
            payload = item
        file_id = _first_scalar(payload, "file_id", "token", "id")
        if file_id is None:
            continue
        attachment_type = str(item.get("type") or "file")
        result.append(
            AttachmentReference(
                provider_file_id=str(file_id),
                filename=str(payload.get("name") or f"max-{attachment_type}-{index + 1}"),
                content_type=(str(payload["mime_type"]) if payload.get("mime_type") else None),
                size_bytes=(int(payload["size"]) if payload.get("size") else None),
            )
        )
    return tuple(result)

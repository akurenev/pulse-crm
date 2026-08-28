from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.integrations.channels.base import (
    AdapterHealth,
    ChannelVerificationError,
    OutboundAttachment,
)
from app.integrations.channels.email import EmailAdapter, EmailEnvelope
from app.integrations.channels.max import MaxAdapter
from app.integrations.channels.telegram import TelegramAdapter


class FakeTransport:
    async def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        reply_to_message_id: str | None,
        attachments: tuple[OutboundAttachment, ...] = (),
    ) -> tuple[str, datetime]:
        del chat_id, text, reply_to_message_id, attachments
        return "sent-1", datetime.now(UTC)

    async def download_file(self, file_id: str) -> bytes:
        return file_id.encode()

    async def healthcheck(self) -> AdapterHealth:
        return AdapterHealth(healthy=True)


class FakeEmailTransport:
    async def send_message(
        self,
        *,
        recipient: str,
        text: str,
        in_reply_to: str | None,
        attachments: tuple[OutboundAttachment, ...] = (),
    ) -> tuple[str, datetime]:
        del recipient, text, in_reply_to, attachments
        return "<sent@example.com>", datetime.now(UTC)

    async def healthcheck(self) -> AdapterHealth:
        return AdapterHealth(healthy=True)


@pytest.mark.asyncio
async def test_telegram_secret_and_normalization() -> None:
    adapter = TelegramAdapter("secret-token", FakeTransport())
    await adapter.verify({"X-Telegram-Bot-Api-Secret-Token": "secret-token"}, b"{}")
    with pytest.raises(ChannelVerificationError):
        await adapter.verify({}, b"{}")

    message = adapter.normalize_inbound(
        {
            "update_id": 42,
            "message": {
                "message_id": 7,
                "date": 1_788_000_000,
                "chat": {"id": 99, "type": "private"},
                "from": {"id": 123, "first_name": "Anna"},
                "text": "Hello",
            },
        }
    )
    assert message.event_id == "42"
    assert message.thread_id == "99"
    assert message.sender_id == "123"
    assert message.text == "Hello"


@pytest.mark.asyncio
async def test_max_secret_header_and_message_created_normalization() -> None:
    adapter = MaxAdapter("max-secret", FakeTransport())
    await adapter.verify({"X-Max-Bot-Api-Secret": "max-secret"}, b"{}")
    message = adapter.normalize_inbound(
        {
            "update_type": "message_created",
            "timestamp": 1_788_000_000_000,
            "message": {
                "mid": "m-1",
                "sender": {"user_id": 10, "name": "Анна"},
                "recipient": {"chat_id": 20},
                "body": {"text": "Добрый день"},
            },
        }
    )
    assert message.event_id == "message_created:m-1"
    assert message.thread_id == "20"
    assert message.text == "Добрый день"


def test_email_uses_uidvalidity_uid_and_reply_headers() -> None:
    adapter = EmailAdapter(FakeEmailTransport())
    raw = (
        b"From: Anna <anna@example.com>\r\n"
        b"To: sales@example.com\r\n"
        b"Date: Thu, 27 Aug 2026 12:00:00 +0500\r\n"
        b"Message-ID: <message-2@example.com>\r\n"
        b"In-Reply-To: <message-1@example.com>\r\n"
        b"References: <message-1@example.com>\r\n"
        b"Subject: Test\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Hello from email"
    )
    message = adapter.normalize_envelope(EmailEnvelope(101, 55, raw))
    assert message.event_id == "101:55"
    assert message.sender_id == "anna@example.com"
    assert message.thread_id == "<message-1@example.com>"
    assert message.reply_to_message_id == "<message-1@example.com>"

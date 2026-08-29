from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

import httpx
import pytest

from app.integrations.channels.base import OutboundAttachment
from app.integrations.channels.email import EmailAdapter
from app.integrations.channels.max import MaxAdapter
from app.integrations.channels.telegram import TelegramAdapter
from app.integrations.models import ChannelConnection, ChannelKind, ConnectionStatus
from app.integrations.transports import (
    AsyncIMAPPoller,
    BoundedThreadHandoff,
    ChannelAdapterFactory,
    ChannelCredentialsError,
    ChannelTransportError,
    IMAPConfig,
    MaxHTTPTransport,
    SMTPConfig,
    SMTPTransport,
    TelegramHTTPTransport,
)


@pytest.mark.asyncio
async def test_telegram_http_transport_sends_replies_and_downloads_files() -> None:
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/sendMessage"):
            assert json.loads(request.content) == {
                "chat_id": "42",
                "text": "Hello",
                "reply_parameters": {"message_id": "7"},
            }
            return httpx.Response(
                200,
                json={"ok": True, "result": {"message_id": 8, "date": 1_788_000_000}},
            )
        if request.url.path.endswith("/getFile"):
            assert json.loads(request.content) == {"file_id": "file-1"}
            return httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "documents/file-1.pdf"}},
            )
        if request.url.path.endswith("/documents/file-1.pdf"):
            return httpx.Response(200, content=b"pdf-bytes")
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 123}})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = TelegramHTTPTransport("bot-token", client)
        message_id, sent_at = await transport.send_message(
            chat_id="42",
            text="Hello",
            reply_to_message_id="7",
        )
        content = await transport.download_file("file-1")
        health = await transport.healthcheck()

    assert message_id == "8"
    assert sent_at == datetime.fromtimestamp(1_788_000_000, UTC)
    assert content == b"pdf-bytes"
    assert health.healthy is True
    assert all(request.url.host == "api.telegram.org" for request in requests)


@pytest.mark.asyncio
async def test_telegram_transport_does_not_expose_token_in_provider_error() -> None:
    token = "super-secret-bot-token"

    async def reject(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(401, json={"description": f"bad token {token}"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
        transport = TelegramHTTPTransport(token, client)
        with pytest.raises(ChannelTransportError) as raised:
            await transport.send_message(chat_id="1", text="test", reply_to_message_id=None)

    assert token not in str(raised.value)
    assert token not in repr(transport)


@pytest.mark.asyncio
async def test_telegram_transport_uploads_photos_and_documents_as_multipart() -> None:
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        if request.url.path.endswith("/sendPhoto"):
            assert b'name="chat_id"' in request.content
            assert b'name="caption"' in request.content
            assert b"Proposal" in request.content
            assert b'name="reply_parameters"' in request.content
            assert b'name="photo"; filename="preview.png"' in request.content
            assert b"png-bytes" in request.content
            return httpx.Response(
                200,
                json={"ok": True, "result": {"message_id": 101, "date": 1_788_000_000}},
            )
        assert request.url.path.endswith("/sendDocument")
        assert b'name="document"; filename="proposal.pdf"' in request.content
        assert b"%PDF-1.7" in request.content
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 102, "date": 1_788_000_001}},
        )

    attachments = (
        OutboundAttachment("preview.png", "image/png", b"png-bytes"),
        OutboundAttachment("proposal.pdf", "application/pdf", b"%PDF-1.7\nproposal"),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        message_id, sent_at = await TelegramHTTPTransport("bot-token", client).send_message(
            chat_id="42",
            text="Proposal",
            reply_to_message_id="7",
            attachments=attachments,
        )

    assert message_id == "101"
    assert sent_at == datetime.fromtimestamp(1_788_000_000, UTC)
    assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == [
        "sendPhoto",
        "sendDocument",
    ]


@pytest.mark.asyncio
async def test_max_http_transport_uses_authorization_header_and_current_domain() -> None:
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "max-token"
        if request.url.path == "/messages":
            assert request.url.host == "platform-api2.max.ru"
            assert request.url.params["chat_id"] == "77"
            assert json.loads(request.content) == {
                "text": "Привет",
                "link": {"type": "reply", "mid": "mid.old"},
            }
            return httpx.Response(
                200,
                json={
                    "message": {
                        "timestamp": 1_788_000_000_000,
                        "body": {"mid": "mid.new"},
                    }
                },
            )
        if request.url.path == "/me":
            return httpx.Response(200, json={"user_id": 100, "is_bot": True})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = MaxHTTPTransport("max-token", client)
        message_id, sent_at = await transport.send_message(
            chat_id="77",
            text="Привет",
            reply_to_message_id="mid.old",
        )
        health = await transport.healthcheck()

    assert message_id == "mid.new"
    assert sent_at == datetime.fromtimestamp(1_788_000_000, UTC)
    assert health.healthy is True
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_max_transport_resolves_video_token_without_leaking_auth_to_cdn() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "platform-api2.max.ru":
            assert request.url.path == "/videos/video-token"
            assert request.headers["Authorization"] == "max-token"
            return httpx.Response(
                200,
                json={"token": "video-token", "urls": {"mp4": "https://cdn.test/v.mp4"}},
            )
        assert request.url == httpx.URL("https://cdn.test/v.mp4")
        assert "Authorization" not in request.headers
        return httpx.Response(200, content=b"video")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        content = await MaxHTTPTransport("max-token", client).download_file("video-token")

    assert content == b"video"


@pytest.mark.asyncio
async def test_max_transport_uses_upload_tokens_without_forwarding_authorization() -> None:
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "platform-api2.max.ru":
            assert request.headers["Authorization"] == "max-token"
            if request.url.path == "/uploads":
                upload_type = request.url.params["type"]
                host = "iu.oneme.ru" if upload_type == "image" else "fu.oneme.ru"
                return httpx.Response(
                    200,
                    json={"url": f"https://{host}/upload/{upload_type}"},
                )
            assert request.url.path == "/messages"
            assert request.url.params["chat_id"] == "77"
            assert json.loads(request.content) == {
                "text": "Files",
                "link": {"type": "reply", "mid": "old-mid"},
                "attachments": [
                    {"type": "image", "payload": {"token": "image-token"}},
                    {"type": "file", "payload": {"token": "file-token"}},
                ],
            }
            return httpx.Response(
                200,
                json={
                    "message": {
                        "timestamp": 1_788_000_000_000,
                        "body": {"mid": "new-mid"},
                    }
                },
            )

        assert "Authorization" not in request.headers
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        if request.url.host == "iu.oneme.ru":
            assert b'name="data"; filename="preview.png"' in request.content
            assert b"png-bytes" in request.content
            return httpx.Response(200, json={"token": "image-token"})
        assert request.url.host == "fu.oneme.ru"
        assert b'name="data"; filename="proposal.pdf"' in request.content
        assert b"%PDF-1.7" in request.content
        return httpx.Response(200, json={"token": "file-token"})

    attachments = (
        OutboundAttachment("preview.png", "image/png", b"png-bytes"),
        OutboundAttachment("proposal.pdf", "application/pdf", b"%PDF-1.7\nproposal"),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        message_id, sent_at = await MaxHTTPTransport("max-token", client).send_message(
            chat_id="77",
            text="Files",
            reply_to_message_id="old-mid",
            attachments=attachments,
        )

    assert message_id == "new-mid"
    assert sent_at == datetime.fromtimestamp(1_788_000_000, UTC)
    assert [request.url.host for request in requests] == [
        "platform-api2.max.ru",
        "iu.oneme.ru",
        "platform-api2.max.ru",
        "fu.oneme.ru",
        "platform-api2.max.ru",
    ]


@pytest.mark.asyncio
async def test_max_attachment_failure_redacts_provider_body_and_credentials() -> None:
    token = "max-super-secret-token"
    recipient = "+70000000002"

    async def reject(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            500,
            json={"message": f"token={token}; recipient={recipient}"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
        transport = MaxHTTPTransport(token, client)
        with pytest.raises(ChannelTransportError) as raised:
            await transport.send_message(
                chat_id=recipient,
                text="Attachment",
                reply_to_message_id=None,
                attachments=(
                    OutboundAttachment(
                        "proposal.pdf",
                        "application/pdf",
                        b"%PDF-1.7\nproposal",
                    ),
                ),
            )

    assert str(raised.value) == "MAX API request failed"
    assert token not in str(raised.value)
    assert recipient not in str(raised.value)


class FakeSMTP:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.ehlo_count = 0
        self.starttls_called = False
        self.login_args: tuple[str, str] | None = None
        self.messages: list[EmailMessage] = []
        self.quit_called = False

    def ehlo(self) -> tuple[int, bytes]:
        self.ehlo_count += 1
        return 250, b"ok"

    def starttls(self, *, context: Any) -> tuple[int, bytes]:
        assert context is not None
        self.starttls_called = True
        return 220, b"ready"

    def login(self, username: str, password: str) -> tuple[int, bytes]:
        self.login_args = (username, password)
        return 235, b"ok"

    def send_message(self, message: EmailMessage) -> dict[str, Any]:
        self.messages.append(message)
        return {}

    def noop(self) -> tuple[int, bytes]:
        return 250, b"ok"

    def quit(self) -> tuple[int, bytes]:
        self.quit_called = True
        return 221, b"bye"

    def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_smtp_transport_sends_message_in_bounded_thread() -> None:
    clients: list[FakeSMTP] = []

    def factory(*args: Any, **kwargs: Any) -> FakeSMTP:
        client = FakeSMTP(*args, **kwargs)
        clients.append(client)
        return client

    transport = SMTPTransport(
        SMTPConfig(
            host="smtp.example.com",
            port=587,
            security="starttls",
            username="sales@example.com",
            password="password",
            from_address="sales@example.com",
            subject="Pulse reply",
        ),
        BoundedThreadHandoff(max_concurrency=1),
        smtp_factory=factory,  # type: ignore[arg-type]
    )

    message_id, sent_at = await transport.send_message(
        recipient="client@example.com",
        text="Hello",
        in_reply_to="<previous@example.com>",
    )

    assert message_id.startswith("<") and message_id.endswith(">")
    assert sent_at.tzinfo is UTC
    assert len(clients) == 1
    client = clients[0]
    assert client.starttls_called is True
    assert client.ehlo_count == 2
    assert client.login_args == ("sales@example.com", "password")
    assert client.quit_called is True
    message = client.messages[0]
    assert message["To"] == "client@example.com"
    assert message["In-Reply-To"] == "<previous@example.com>"
    assert message.get_content().strip() == "Hello"


@pytest.mark.asyncio
async def test_smtp_transport_builds_mime_attachments() -> None:
    clients: list[FakeSMTP] = []

    def factory(*args: Any, **kwargs: Any) -> FakeSMTP:
        client = FakeSMTP(*args, **kwargs)
        clients.append(client)
        return client

    transport = SMTPTransport(
        SMTPConfig(
            host="smtp.example.com",
            security="plain",
            from_address="sales@example.com",
        ),
        BoundedThreadHandoff(max_concurrency=1),
        smtp_factory=factory,  # type: ignore[arg-type]
    )
    attachments = (
        OutboundAttachment("proposal.pdf", "application/pdf", b"%PDF-1.7\nproposal"),
        OutboundAttachment("notes.txt", "text/plain", b"Follow up"),
    )

    await transport.send_message(
        recipient="client@example.com",
        text="Files attached",
        in_reply_to=None,
        attachments=attachments,
    )

    message = clients[0].messages[0]
    assert message.is_multipart()
    body = message.get_body(preferencelist=("plain",))
    assert body is not None and body.get_content().strip() == "Files attached"
    mime_attachments = list(message.iter_attachments())
    assert [item.get_filename() for item in mime_attachments] == ["proposal.pdf", "notes.txt"]
    assert [item.get_content_type() for item in mime_attachments] == [
        "application/pdf",
        "text/plain",
    ]
    assert mime_attachments[0].get_payload(decode=True) == b"%PDF-1.7\nproposal"
    assert mime_attachments[1].get_payload(decode=True) == b"Follow up"


class FakeIMAP:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.login_args: tuple[str, str] | None = None
        self.closed = False
        self.logged_out = False

    def starttls(self, *, ssl_context: Any) -> tuple[str, list[bytes]]:
        assert ssl_context is not None
        return "OK", [b"ready"]

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        self.login_args = (username, password)
        return "OK", [b"authenticated"]

    def select(self, mailbox: str, *, readonly: bool) -> tuple[str, list[bytes]]:
        assert mailbox == "Sales"
        assert readonly is True
        return "OK", [b"2"]

    def response(self, code: str) -> tuple[str, list[bytes]]:
        assert code == "UIDVALIDITY"
        return code, [b"101"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        if command == "SEARCH":
            assert args == (None, "UID 11:*")
            return "OK", [b"10 11 12"]
        assert command == "FETCH"
        uid = int(args[0])
        raw = (
            f"From: User <user{uid}@example.com>\r\n"
            f"Message-ID: <message-{uid}@example.com>\r\n\r\n"
            f"Message {uid}"
        ).encode()
        return "OK", [(b"RFC822", raw), b")"]

    def close(self) -> tuple[str, list[bytes]]:
        self.closed = True
        return "OK", [b"closed"]

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return "BYE", [b"logout"]


@pytest.mark.asyncio
async def test_imap_poller_fetches_uid_page_and_returns_envelopes() -> None:
    clients: list[FakeIMAP] = []

    def factory(*args: Any, **kwargs: Any) -> FakeIMAP:
        client = FakeIMAP(*args, **kwargs)
        clients.append(client)
        return client

    poller = AsyncIMAPPoller(
        IMAPConfig(
            host="imap.example.com",
            port=143,
            security="starttls",
            username="sales@example.com",
            password="password",
            mailbox="Sales",
        ),
        BoundedThreadHandoff(max_concurrency=1),
        imap_factory=factory,  # type: ignore[arg-type]
    )

    batch = await poller.poll(after_uid=10, limit=2)

    assert batch.uidvalidity == 101
    assert [item.uid for item in batch.envelopes] == [11, 12]
    assert batch.last_uid == 12
    assert b"Message 11" in batch.envelopes[0].raw_message
    assert clients[0].login_args == ("sales@example.com", "password")
    assert clients[0].closed is True
    assert clients[0].logged_out is True


@pytest.mark.asyncio
async def test_bounded_thread_handoff_limits_blocking_concurrency() -> None:
    handoff = BoundedThreadHandoff(max_concurrency=1)
    lock = threading.Lock()
    active = 0
    peak = 0

    def blocking() -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with lock:
            active -= 1

    await asyncio.gather(*(handoff.run(blocking) for _ in range(3)))
    assert peak == 1


class RecordingCipher:
    key_id = "primary"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[tuple[bytes, bytes]] = []

    def decrypt(self, envelope: bytes, *, associated_data: bytes) -> bytes:
        self.calls.append((envelope, associated_data))
        return json.dumps(self.payload).encode()


def make_connection(
    kind: ChannelKind,
    *,
    settings: dict[str, Any] | None = None,
    key_id: str = "primary",
) -> ChannelConnection:
    now = datetime.now(UTC)
    return ChannelConnection(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        kind=kind,
        name=f"Test {kind.value}",
        status=ConnectionStatus.active,
        encrypted_credentials=b"encrypted-envelope",
        credentials_key_id=key_id,
        settings=settings or {},
        default_pipeline_id=None,
        default_stage_id=None,
        default_assignee_id=None,
        last_healthcheck_at=None,
        last_error=None,
        version=1,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_factory_decrypts_with_channel_aad_and_builds_bot_adapters() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as client:
        telegram_connection = make_connection(ChannelKind.telegram)
        telegram_cipher = RecordingCipher(
            {"bot_token": "telegram-token", "webhook_secret": "telegram-secret"}
        )
        telegram_factory = ChannelAdapterFactory(
            telegram_cipher,  # type: ignore[arg-type]
            http_client=client,
        )
        telegram_adapter = telegram_factory.build(telegram_connection)

        max_connection = make_connection(ChannelKind.max)
        max_cipher = RecordingCipher({"access_token": "max-token", "webhook_secret": "max-secret"})
        max_factory = ChannelAdapterFactory(max_cipher, http_client=client)  # type: ignore[arg-type]
        max_adapter = max_factory.build(max_connection)

    assert isinstance(telegram_adapter, TelegramAdapter)
    assert isinstance(max_adapter, MaxAdapter)
    assert telegram_cipher.calls == [
        (
            b"encrypted-envelope",
            f"channel:{telegram_connection.id}".encode(),
        )
    ]
    assert max_cipher.calls[0][1] == f"channel:{max_connection.id}".encode()


def test_factory_builds_email_adapter_and_imap_poller_from_nested_credentials() -> None:
    connection = make_connection(
        ChannelKind.email,
        settings={
            "smtp": {"host": "smtp.example.com", "port": 587},
            "imap": {"host": "imap.example.com", "port": 993},
        },
    )
    cipher = RecordingCipher(
        {
            "smtp": {
                "security": "starttls",
                "username": "sales@example.com",
                "password": "smtp-password",
                "from_address": "sales@example.com",
            },
            "imap": {
                "security": "ssl",
                "username": "sales@example.com",
                "password": "imap-password",
            },
        }
    )
    factory = ChannelAdapterFactory(cipher)  # type: ignore[arg-type]

    adapter = factory.build(connection)
    poller = factory.build_imap_poller(connection)

    assert isinstance(adapter, EmailAdapter)
    assert isinstance(poller, AsyncIMAPPoller)
    assert cipher.calls == [
        (b"encrypted-envelope", f"channel:{connection.id}".encode()),
        (b"encrypted-envelope", f"channel:{connection.id}".encode()),
    ]


def test_factory_rejects_wrong_key_without_decrypting() -> None:
    connection = make_connection(ChannelKind.telegram, key_id="retired")
    cipher = RecordingCipher({"bot_token": "telegram-token", "webhook_secret": "telegram-secret"})
    factory = ChannelAdapterFactory(cipher)  # type: ignore[arg-type]

    with pytest.raises(ChannelCredentialsError, match="unavailable key"):
        factory.build(connection)

    assert cipher.calls == []

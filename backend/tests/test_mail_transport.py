from __future__ import annotations

import asyncio
import builtins
import smtplib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.mail.mime import build_mail, parse_mail
from app.mail.transport import (
    MailDeliveryUncertain,
    MailTransport,
    MailTransportError,
    parse_folder_line,
)


def account() -> Any:
    return SimpleNamespace(
        email="employee@example.com",
        imap_host="imap.example.com",
        imap_port=993,
        imap_security="ssl",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_security="starttls",
    )


class FakeSMTP:
    def __init__(self, *, disconnect: bool = False, reject: bool = False) -> None:
        self.disconnect = disconnect
        self.reject = reject
        self.deliveries = 0
        self.tls = False
        self.closed = False

    def ehlo(self) -> tuple[int, bytes]:
        return 250, b"OK"

    def starttls(self, **kwargs: Any) -> tuple[int, bytes]:
        self.tls = True
        return 220, b"OK"

    def login(self, username: str, password: str) -> tuple[int, bytes]:
        assert self.tls
        return 235, b"OK"

    def mail(self, sender: str) -> tuple[int, bytes]:
        return 250, b"OK"

    def rcpt(self, recipient: str) -> tuple[int, bytes]:
        return (550 if self.reject else 250), b"response is deliberately never logged"

    def data(self, raw: bytes) -> tuple[int, bytes]:
        self.deliveries += 1
        if self.disconnect:
            raise smtplib.SMTPServerDisconnected("sensitive provider data")
        return 250, b"OK"

    def close(self) -> None:
        self.closed = True


class FakeIMAP:
    capabilities = (b"IMAP4rev1", b"MOVE", b"UIDPLUS")

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.appended = False
        self.validity = 4

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        return "OK", [b"OK"]

    def select(self, path: str, *, readonly: bool) -> tuple[str, list[bytes]]:
        self.calls.append(("SELECT", path, readonly))
        return "OK", [b"12"]

    def response(self, key: str) -> tuple[str, list[bytes]]:
        return key, [str(self.validity).encode()]

    def list(self) -> tuple[str, list[bytes]]:
        return "OK", [b'(\\HasNoChildren) "/" INBOX', b'(\\Sent) "/" "Sent Items"']

    def uid(self, command: str, *args: Any) -> tuple[str, builtins.list[Any]]:
        self.calls.append((command, *args))
        if command == "SEARCH":
            if "HEADER" in args:
                return "OK", [b"7" if self.appended else b""]
            return "OK", [b"1 2 3 4 5 6 7 8 9 10 11 12"]
        if command == "FETCH":
            if args[-1] == "(RFC822.SIZE)":
                return "OK", [b"1 (RFC822.SIZE 500)"]
            if args[-1] == "(UID FLAGS)":
                return "OK", [b"1 (UID 1 FLAGS (\\Seen \\Flagged))"]
            return "OK", [
                (f"1 (UID {args[0]} FLAGS (\\Seen))".encode(), b"Subject: Test\r\n\r\nBody")
            ]
        return "OK", [b"OK"]

    def append(self, *args: Any) -> tuple[str, builtins.list[bytes]]:
        self.calls.append(("APPEND", *args))
        self.appended = True
        return "OK", [b"OK"]

    def logout(self) -> tuple[str, builtins.list[bytes]]:
        return "BYE", [b"closed"]


def transport(**kwargs: Any) -> MailTransport:
    return MailTransport(allowed_hosts=("imap.example.com", "smtp.example.com"), **kwargs)


def test_special_use_and_modified_utf7_folder() -> None:
    folder = parse_folder_line(b'(\\HasNoChildren \\Sent) "/" "INBOX.Sent Items"')
    assert folder is not None and folder.kind == "sent"
    russian = parse_folder_line(b'(\\HasNoChildren) "/" "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"')
    assert russian is not None and russian.display_name == "Отправленные"
    assert parse_folder_line(b'(\\Noselect) "/" "Parent"') is None


def test_mime_html_fallback_is_inert_and_blocks_bad_attachments() -> None:
    raw = (
        b"From: Person <person@example.com>\r\nSubject: HTML\r\n"
        b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="test"\r\n\r\n'
        b"--test\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        b"<style>hidden</style><p>Hello <b>team</b></p><script>secret()</script>"
        b'<img src="https://tracking.example.com/pixel">\r\n'
        b"--test\r\nContent-Type: application/javascript\r\n"
        b'Content-Disposition: attachment; filename="bad.js"\r\n\r\nalert(1)\r\n'
        b"--test--\r\n"
    )
    parsed = parse_mail(raw)
    assert parsed.body_text == "Hello team"
    assert parsed.attachments == ()
    assert parsed.blocked_attachments == 1


def test_mime_build_is_stable_and_does_not_emit_bcc() -> None:
    values: dict[str, Any] = dict(
        email="employee@example.com",
        display_name="Employee",
        subject="Hello",
        to_addresses=["customer@example.com"],
        cc_addresses=[],
        body_text="Text",
        message_id="<stable@example.com>",
        sent_at=datetime(2026, 10, 20, tzinfo=UTC),
        in_reply_to="<parent@example.com>",
        references=["<parent@example.com>"],
    )
    raw = build_mail(**values)
    assert raw == build_mail(**values)
    assert b"Bcc:" not in raw
    assert parse_mail(raw).thread_key == "<parent@example.com>"


async def test_transport_rejects_untrusted_hosts_before_connecting() -> None:
    client = FakeSMTP()
    settings = account()
    settings.imap_host = "localhost"
    with pytest.raises(MailTransportError, match="not allowed"):
        await transport(smtp_factory=lambda *args, **kwargs: client).send(
            settings,
            {"username": "employee@example.com", "password": "test-only"},
            raw=b"Subject: hello\r\n\r\nText",
            recipients=("customer@example.com",),
        )
    assert not client.deliveries


@pytest.mark.parametrize("disconnect,reject", [(True, False), (False, True), (False, False)])
async def test_smtp_ambiguity_is_separate_from_safe_rejection(
    disconnect: bool, reject: bool
) -> None:
    client = FakeSMTP(disconnect=disconnect, reject=reject)
    call = transport(smtp_factory=lambda *args, **kwargs: client).send(
        account(),
        {"username": "employee@example.com", "password": "test-only"},
        raw=b"Subject: hello\r\n\r\nText",
        recipients=("customer@example.com",),
    )
    if disconnect:
        with pytest.raises(MailDeliveryUncertain, match="uncertain"):
            await call
    elif reject:
        with pytest.raises(MailTransportError, match="recipient was rejected"):
            await call
        assert client.deliveries == 0
    else:
        await call
        assert client.deliveries == 1
    assert client.closed


async def test_sync_is_paged_readonly_and_resets_uid_on_validity_change() -> None:
    client = FakeIMAP()
    mail = transport(imap_ssl_factory=lambda *args, **kwargs: client)
    snapshot = await mail.sync_folder(
        account(),
        {"username": "employee@example.com", "password": "test-only"},
        path="INBOX",
        uidvalidity=3,
        last_uid=999,
        sync_days=30,
        known_uids=(1,),
    )
    assert snapshot.uidvalidity == 4 and snapshot.last_uid == 10 and snapshot.has_more
    assert len(snapshot.messages) == 10
    assert ("SELECT", '"INBOX"', True) in client.calls
    assert any(call[0] == "SEARCH" and "SINCE" in call for call in client.calls)
    assert not any("BODY[]" in str(call) or "CLOSE" in call for call in client.calls)


async def test_sent_append_retry_searches_existing_message_id() -> None:
    client = FakeIMAP()
    mail = transport(imap_ssl_factory=lambda *args, **kwargs: client)
    values: dict[str, Any] = dict(
        path="Sent",
        raw=b"Message-ID: <test@example.com>\r\n\r\nBody",
        message_id="<test@example.com>",
    )
    credentials = {"username": "employee@example.com", "password": "test-only"}
    await mail.append_sent(account(), credentials, **values)
    await mail.append_sent(account(), credentials, **values)
    assert sum(call[0] == "APPEND" for call in client.calls) == 1


async def test_cancelled_handoff_holds_slot_until_socket_thread_finishes() -> None:
    import threading

    release = threading.Event()
    entered = threading.Event()
    mail = transport(max_concurrency=1)

    def blocked() -> None:
        entered.set()
        release.wait(2)

    task = asyncio.create_task(mail._run(blocked))
    await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert mail._slots.locked()
    release.set()
    for _ in range(20):
        if not mail._slots.locked():
            break
        await asyncio.sleep(0.01)
    assert not mail._slots.locked()


async def test_successful_smtp_is_not_retried_when_socket_cleanup_raises() -> None:
    class CloseFailureSMTP(FakeSMTP):
        def close(self) -> None:
            super().close()
            raise OSError("late socket cleanup error")

    client = CloseFailureSMTP()
    await transport(smtp_factory=lambda *args, **kwargs: client).send(
        account(),
        {"username": "employee@example.com", "password": "test-only"},
        raw=b"Subject: hello\r\n\r\nText",
        recipients=("customer@example.com",),
    )
    assert client.deliveries == 1


def test_mime_reply_to_is_separate_from_sender_identity() -> None:
    parsed = parse_mail(
        b"From: Form Robot <robot@example.com>\r\n"
        b"Reply-To: Customer <customer@example.com>, assistant@example.com\r\n"
        b"To: employee@example.com\r\nSubject: Contact form\r\n\r\nPlease reply to customer"
    )
    assert parsed.from_address == "robot@example.com"
    assert parsed.reply_to_addresses == ["customer@example.com", "assistant@example.com"]


@pytest.mark.parametrize("destination_has_message", [False, True])
async def test_missing_source_uid_requires_confirmed_destination(
    destination_has_message: bool,
) -> None:
    from app.mail.transport import MailIdentityChanged

    class MissingUIDIMAP(FakeIMAP):
        def uid(self, command: str, *args: Any) -> tuple[str, builtins.list[Any]]:
            if command == "SEARCH":
                self.calls.append((command, *args))
                return "OK", [b"9" if "HEADER" in args and destination_has_message else b""]
            return super().uid(command, *args)

    client = MissingUIDIMAP()
    call = transport(imap_ssl_factory=lambda *args, **kwargs: client).action(
        account(),
        {"username": "employee@example.com", "password": "test-only"},
        path="INBOX",
        target_path="Sent",
        uid=1,
        uidvalidity=4,
        is_read=True,
        is_starred=False,
        message_id="<stable@example.com>",
    )
    if destination_has_message:
        await call
    else:
        with pytest.raises(MailIdentityChanged, match="removed or moved"):
            await call
    assert not any(call[0] in {"STORE", "MOVE", "COPY"} for call in client.calls)


async def test_smtp_total_pre_data_deadline_stops_without_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moments = iter([0.0, 0.0, 0.0, 16.0])
    monkeypatch.setattr("app.mail.transport.time", SimpleNamespace(monotonic=lambda: next(moments)))
    client = FakeSMTP()
    with pytest.raises(MailTransportError, match="deadline reached before delivery"):
        await transport(smtp_factory=lambda *args, **kwargs: client).send(
            account(),
            {"username": "employee@example.com", "password": "test-only"},
            raw=b"Subject: Test\r\n\r\nBody",
            recipients=("customer@example.com",),
        )
    assert client.deliveries == 0 and client.closed

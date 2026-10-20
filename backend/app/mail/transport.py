"""Authenticated, bounded IMAP/SMTP for private employee mailboxes.

SMTP acknowledgement is separate from saving Sent: callers persist the result
before attempting IMAP APPEND, and never blindly repeat an ambiguous delivery.
"""

from __future__ import annotations

import asyncio
import base64
import imaplib
import re
import smtplib
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from app.config import get_settings

MAX_MESSAGE_BYTES = 32 * 1024 * 1024
MAX_OUTBOUND_BYTES = 30 * 1024 * 1024
PAGE_SIZE = 10
SOCKET_TIMEOUT_SECONDS = 5.0


class AccountSettings(Protocol):
    email: str
    imap_host: str
    imap_port: int
    imap_security: str
    smtp_host: str
    smtp_port: int
    smtp_security: str


class MailTransportError(RuntimeError):
    """Redacted failure safe for durable job records and the account screen."""


class MailDeliveryUncertain(MailTransportError):
    """SMTP DATA may have been accepted; manual reconciliation is required."""


class MailIdentityChanged(MailTransportError):
    """The queued operation no longer identifies the remote message safely."""


@dataclass(frozen=True, slots=True)
class RemoteFolder:
    name: str
    display_name: str
    kind: str


@dataclass(frozen=True, slots=True)
class RemoteMessage:
    uid: int
    raw: bytes
    flags: frozenset[str]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class FolderSnapshot:
    uidvalidity: int
    last_uid: int
    has_more: bool
    messages: tuple[RemoteMessage, ...]
    existing_uids: frozenset[int]
    flags: Mapping[int, frozenset[str]]


def _quoted(value: str) -> str:
    if any(character in value for character in ("\r", "\n", "\x00")):
        raise MailTransportError("Invalid mailbox value")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _decode_mailbox(value: str) -> str:
    """Decode IMAP modified UTF-7 for presentation; preserve wire names separately."""

    def decode(match: re.Match[str]) -> str:
        token = match.group(1)
        if not token:
            return "&"
        try:
            encoded = token.replace(",", "/")
            return base64.b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-16-be")
        except (ValueError, UnicodeError):
            return match.group(0)

    return re.sub(r"&([^-]*)-", decode, value)


def _folder_kind(name: str, flags: str) -> str:
    special = {
        "\\inbox": "inbox",
        "\\sent": "sent",
        "\\drafts": "drafts",
        "\\trash": "trash",
        "\\archive": "archive",
        "\\junk": "spam",
    }
    for flag in flags.casefold().split():
        if flag in special:
            return special[flag]
    leaf = _decode_mailbox(name).replace("/", ".").rsplit(".", 1)[-1].casefold()
    return {
        "inbox": "inbox",
        "sent": "sent",
        "sent items": "sent",
        "sent messages": "sent",
        "отправленные": "sent",
        "drafts": "drafts",
        "черновики": "drafts",
        "trash": "trash",
        "deleted items": "trash",
        "корзина": "trash",
        "archive": "archive",
        "архив": "archive",
        "junk": "spam",
        "spam": "spam",
        "спам": "spam",
    }.get(leaf, "custom")


def parse_folder_line(line: bytes) -> RemoteFolder | None:
    match = re.fullmatch(rb'\(([^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(.+)', line)
    if match is None:
        return None
    flags = match.group(1).decode("ascii", "replace")
    if "\\noselect" in flags.casefold():
        return None
    name = match.group(2).decode("ascii", "surrogateescape")
    if name.startswith('"') and name.endswith('"'):
        name = re.sub(r"\\(.)", r"\1", name[1:-1])
    if not name:
        return None
    return RemoteFolder(name, _decode_mailbox(name), _folder_kind(name, flags))


def _ok(response: tuple[str, Any]) -> Any:
    if response[0] != "OK":
        raise MailTransportError("Mail server rejected the operation")
    return response[1]


def _uidvalidity(client: imaplib.IMAP4) -> int:
    _, data = client.response("UIDVALIDITY")
    try:
        return int(data[0])
    except (IndexError, TypeError, ValueError):
        raise MailTransportError("Mail folder has no valid UIDVALIDITY") from None


def _search(client: imaplib.IMAP4, *criteria: str) -> list[int]:
    data = _ok(client.uid("SEARCH", *criteria))
    return [int(value) for value in (data[0] or b"").split()]


def _close_imap(client: imaplib.IMAP4) -> None:
    # CLOSE implicitly expunges *all* deleted messages and is intentionally avoided.
    try:
        client.logout()
    except (OSError, imaplib.IMAP4.error):
        try:
            client.shutdown()
        except (OSError, imaplib.IMAP4.error):
            pass


class MailTransport:
    def __init__(
        self,
        *,
        max_concurrency: int = 4,
        imap_factory: Callable[..., imaplib.IMAP4] = imaplib.IMAP4,
        imap_ssl_factory: Callable[..., imaplib.IMAP4_SSL] = imaplib.IMAP4_SSL,
        smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP,
        smtp_ssl_factory: Callable[..., smtplib.SMTP_SSL] = smtplib.SMTP_SSL,
        allowed_hosts: tuple[str, ...] | None = None,
    ) -> None:
        if not 1 <= max_concurrency <= 4:
            raise ValueError("Mail concurrency must be between one and four")
        self._slots = asyncio.Semaphore(max_concurrency)
        self._imap_factory = imap_factory
        self._imap_ssl_factory = imap_ssl_factory
        self._smtp_factory = smtp_factory
        self._smtp_ssl_factory = smtp_ssl_factory
        self._active: set[asyncio.Task[Any]] = set()
        self._allowed_hosts = frozenset(
            host.casefold().rstrip(".")
            for host in (
                get_settings().mail_allowed_hosts if allowed_hosts is None else allowed_hosts
            )
        )

    def _validate(self, account: AccountSettings) -> None:
        for host in (account.imap_host, account.smtp_host):
            if host.casefold().rstrip(".") not in self._allowed_hosts:
                raise MailTransportError("Mail host is not allowed by the administrator")
        if (account.imap_security, account.imap_port) not in {("ssl", 993), ("starttls", 143)}:
            raise MailTransportError("Unsupported encrypted IMAP port")
        if (account.smtp_security, account.smtp_port) not in {("ssl", 465), ("starttls", 587)}:
            raise MailTransportError("Unsupported encrypted SMTP port")

    async def _run[T](self, callback: Callable[[], T]) -> T:
        await self._slots.acquire()
        task = asyncio.create_task(asyncio.to_thread(callback))
        self._active.add(task)

        def done(completed: asyncio.Task[Any]) -> None:
            self._active.discard(completed)
            self._slots.release()
            # A job cancellation must not release admission while the socket thread
            # is still alive. Retrieve late failures without logging server details.
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)
        try:
            return await asyncio.shield(task)
        except (MailTransportError, asyncio.CancelledError):
            raise
        except (OSError, ValueError, imaplib.IMAP4.error, smtplib.SMTPException):
            raise MailTransportError("Mail server connection failed") from None

    def _imap(self, account: AccountSettings, credentials: Mapping[str, str]) -> imaplib.IMAP4:
        self._validate(account)
        if account.imap_security == "ssl":
            client: imaplib.IMAP4 = self._imap_ssl_factory(
                account.imap_host,
                account.imap_port,
                timeout=SOCKET_TIMEOUT_SECONDS,
                ssl_context=ssl.create_default_context(),
            )
        elif account.imap_security == "starttls":
            client = self._imap_factory(
                account.imap_host,
                account.imap_port,
                timeout=SOCKET_TIMEOUT_SECONDS,
            )
            try:
                _ok(client.starttls(ssl_context=ssl.create_default_context()))
            except BaseException:
                _close_imap(client)
                raise
        else:
            raise MailTransportError("Encrypted IMAP connection is required")
        try:
            _ok(client.login(credentials["username"], credentials["password"]))
        except BaseException:
            _close_imap(client)
            raise
        return client

    def _smtp(self, account: AccountSettings, credentials: Mapping[str, str]) -> smtplib.SMTP:
        self._validate(account)
        if account.smtp_security == "ssl":
            client: smtplib.SMTP = self._smtp_ssl_factory(
                account.smtp_host,
                account.smtp_port,
                timeout=SOCKET_TIMEOUT_SECONDS,
                context=ssl.create_default_context(),
            )
        elif account.smtp_security == "starttls":
            client = self._smtp_factory(
                account.smtp_host,
                account.smtp_port,
                timeout=SOCKET_TIMEOUT_SECONDS,
            )
        else:
            raise MailTransportError("Encrypted SMTP connection is required")
        try:
            client.ehlo()
            if account.smtp_security == "starttls":
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            client.login(credentials["username"], credentials["password"])
        except BaseException:
            client.close()
            raise
        return client

    async def check(self, account: AccountSettings, credentials: Mapping[str, str]) -> None:
        def check_sync() -> None:
            imap = self._imap(account, credentials)
            _close_imap(imap)
            smtp = self._smtp(account, credentials)
            smtp.close()

        await self._run(check_sync)

    async def folders(
        self,
        account: AccountSettings,
        credentials: Mapping[str, str],
    ) -> tuple[RemoteFolder, ...]:
        def list_sync() -> tuple[RemoteFolder, ...]:
            client = self._imap(account, credentials)
            try:
                lines = _ok(client.list())
                folders = [parse_folder_line(line) for line in lines if isinstance(line, bytes)]
                return tuple(folder for folder in folders if folder is not None)
            finally:
                _close_imap(client)

        return await self._run(list_sync)

    async def sync_folder(
        self,
        account: AccountSettings,
        credentials: Mapping[str, str],
        *,
        path: str,
        uidvalidity: int | None,
        last_uid: int,
        sync_days: int,
        known_uids: tuple[int, ...] = (),
    ) -> FolderSnapshot:
        def sync() -> FolderSnapshot:
            started = time.monotonic()
            client = self._imap(account, credentials)
            try:
                _ok(client.select(_quoted(path), readonly=True))
                current_validity = _uidvalidity(client)
                cursor = last_uid if current_validity == uidvalidity else 0
                existing = frozenset(_search(client, "ALL"))
                if cursor:
                    found = _search(client, "UID", f"{cursor + 1}:*")
                    candidates = sorted(uid for uid in found if uid > cursor)
                else:
                    date = datetime.now(UTC) - timedelta(days=sync_days)
                    month = (
                        "Jan",
                        "Feb",
                        "Mar",
                        "Apr",
                        "May",
                        "Jun",
                        "Jul",
                        "Aug",
                        "Sep",
                        "Oct",
                        "Nov",
                        "Dec",
                    )[date.month - 1]
                    since = f"{date.day:02d}-{month}-{date.year}"
                    candidates = sorted(_search(client, "SINCE", since))
                selected = candidates[:PAGE_SIZE]
                flags: dict[int, frozenset[str]] = {}
                # Fetch metadata for a bounded rotating portion of already cached
                # messages; BODY.PEEK never marks a letter read as a sync side effect.
                known = [uid for uid in known_uids if uid in existing and uid not in selected]
                if known:
                    data = _ok(client.uid("FETCH", ",".join(map(str, known)), "(UID FLAGS)"))
                    for item in data:
                        header = item[0] if isinstance(item, tuple) else item
                        if isinstance(header, bytes) and (
                            match := re.search(rb"UID (\d+)", header)
                        ):
                            flags[int(match.group(1))] = frozenset(
                                value.decode("ascii", "replace")
                                for value in imaplib.ParseFlags(header)
                            )
                messages: list[RemoteMessage] = []
                processed: list[int] = []
                for uid in selected:
                    if time.monotonic() - started >= 15.0:
                        break
                    size_data = _ok(client.uid("FETCH", str(uid), "(RFC822.SIZE)"))
                    size_header = b" ".join(item for item in size_data if isinstance(item, bytes))
                    match = re.search(rb"RFC822.SIZE (\d+)", size_header)
                    truncated = match is not None and int(match.group(1)) > MAX_MESSAGE_BYTES
                    fields = (
                        "(UID FLAGS BODY.PEEK[HEADER.FIELDS "
                        "(FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES)])"
                        if truncated
                        else "(UID FLAGS BODY.PEEK[])"
                    )
                    data = _ok(client.uid("FETCH", str(uid), fields))
                    for item in data:
                        if isinstance(item, tuple) and isinstance(item[1], bytes):
                            header, raw = item
                            if len(raw) <= MAX_MESSAGE_BYTES:
                                message_flags = frozenset(
                                    value.decode("ascii", "replace")
                                    for value in imaplib.ParseFlags(header)
                                )
                                messages.append(RemoteMessage(uid, raw, message_flags, truncated))
                            break
                    processed.append(uid)
                return FolderSnapshot(
                    current_validity,
                    max(processed, default=cursor),
                    len(candidates) > len(processed),
                    tuple(messages),
                    existing,
                    flags,
                )
            finally:
                _close_imap(client)

        return await self._run(sync)

    async def send(
        self,
        account: AccountSettings,
        credentials: Mapping[str, str],
        *,
        raw: bytes,
        recipients: tuple[str, ...],
    ) -> None:
        if len(raw) > MAX_OUTBOUND_BYTES:
            raise MailTransportError("Message exceeds the 30 MB sending limit")

        def send_sync() -> None:
            started = time.monotonic()

            def require_budget() -> None:
                if time.monotonic() - started >= 15.0:
                    raise MailTransportError("SMTP operation deadline reached before delivery")

            # Errors before DATA are safe to retry. Disconnects after DATA starts
            # cannot establish whether delivery happened and must never auto resend.
            client = self._smtp(account, credentials)
            try:
                require_budget()
                code, _ = client.mail(account.email)
                if code != 250:
                    raise MailTransportError("SMTP sender was rejected")
                for recipient in recipients:
                    require_budget()
                    code, _ = client.rcpt(recipient)
                    if code not in (250, 251):
                        raise MailTransportError("SMTP recipient was rejected")
                require_budget()
                try:
                    code, _ = client.data(raw)
                except (OSError, smtplib.SMTPException):
                    raise MailDeliveryUncertain("SMTP delivery result is uncertain") from None
                if code != 250:
                    raise MailTransportError("SMTP message was rejected")
            finally:
                try:
                    client.close()
                except OSError:
                    pass

        await self._run(send_sync)

    async def append_sent(
        self,
        account: AccountSettings,
        credentials: Mapping[str, str],
        *,
        path: str,
        raw: bytes,
        message_id: str,
    ) -> None:
        def append() -> None:
            client = self._imap(account, credentials)
            try:
                _ok(client.select(_quoted(path), readonly=True))
                if not _search(client, "HEADER", "Message-ID", _quoted(message_id)):
                    _ok(client.append(_quoted(path), r"(\Seen)", None, raw))
            finally:
                _close_imap(client)

        await self._run(append)

    async def action(
        self,
        account: AccountSettings,
        credentials: Mapping[str, str],
        *,
        path: str,
        uid: int,
        uidvalidity: int,
        is_read: bool,
        is_starred: bool,
        target_path: str | None = None,
        message_id: str | None = None,
    ) -> None:
        def apply() -> None:
            client = self._imap(account, credentials)
            try:
                _ok(client.select(_quoted(path), readonly=False))
                if _uidvalidity(client) != uidvalidity:
                    raise MailIdentityChanged("Folder identity changed; synchronize before editing")
                if uid not in _search(client, "UID", str(uid)):
                    if target_path and target_path != path and message_id:
                        _ok(client.select(_quoted(target_path), readonly=True))
                        if _search(client, "HEADER", "Message-ID", _quoted(message_id)):
                            return  # The previous MOVE completed before its reply was lost.
                    raise MailIdentityChanged("Message was removed or moved by another mail client")
                for flag, desired in ((r"\Seen", is_read), (r"\Flagged", is_starred)):
                    _ok(
                        client.uid(
                            "STORE",
                            str(uid),
                            "+FLAGS.SILENT" if desired else "-FLAGS.SILENT",
                            f"({flag})",
                        )
                    )
                if target_path and target_path != path:
                    capabilities = {
                        (cap.decode("ascii") if isinstance(cap, bytes) else cap).upper()
                        for cap in client.capabilities
                    }
                    if "MOVE" in capabilities:
                        _ok(client.uid("MOVE", str(uid), _quoted(target_path)))
                    else:
                        # COPY/EXPUNGE fallback cannot be made safe after lost replies:
                        # require UIDPLUS for targeted expunge, and a stable Message-ID
                        # to discover an already copied message before retrying.
                        if "UIDPLUS" not in capabilities or not message_id:
                            raise MailTransportError(
                                "Mail server does not support safe message moves"
                            )
                        _ok(client.select(_quoted(target_path), readonly=True))
                        copied = bool(_search(client, "HEADER", "Message-ID", _quoted(message_id)))
                        _ok(client.select(_quoted(path), readonly=False))
                        if not copied:
                            _ok(client.uid("COPY", str(uid), _quoted(target_path)))
                        _ok(client.uid("STORE", str(uid), "+FLAGS.SILENT", r"(\Deleted)"))
                        _ok(client.uid("EXPUNGE", str(uid)))
            finally:
                _close_imap(client)

        await self._run(apply)

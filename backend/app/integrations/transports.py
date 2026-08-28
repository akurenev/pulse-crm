"""Concrete network transports and adapter construction for CRM channels.

HTTP providers use a shared ``httpx.AsyncClient``.  The standard-library SMTP
and IMAP clients are blocking, so their calls cross a bounded ``to_thread``
handoff; at most four channel operations can occupy worker threads by default.

Credential envelopes contain UTF-8 JSON and are authenticated with AAD
``channel:{connection.id}``.  Supported credential shapes are intentionally
small and explicit::

    {"bot_token": "...", "webhook_secret": "..."}          # Telegram
    {"access_token": "...", "webhook_secret": "..."}       # MAX
    {
      "smtp": {"host": "...", "port": 587, "security": "starttls",
               "username": "...", "password": "...", "from_address": "..."},
      "imap": {"host": "...", "port": 993, "security": "ssl",
               "username": "...", "password": "...", "mailbox": "INBOX"}
    }

Non-secret connection settings may hold the same keys; encrypted values win.
Tokens, passwords, provider response bodies and decrypted credentials are
never included in exceptions or logs.
"""

from __future__ import annotations

import asyncio
import imaplib
import json
import smtplib
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any, TypeVar, cast
from urllib.parse import quote, urlparse

import httpx

from app.integrations.channels.base import AdapterHealth, ChannelAdapter, OutboundAttachment
from app.integrations.channels.email import EmailAdapter, EmailEnvelope
from app.integrations.channels.max import MaxAdapter
from app.integrations.channels.telegram import TelegramAdapter
from app.integrations.models import ChannelConnection, ChannelKind
from app.integrations.secrets import SecretCipher, SecretCipherError

TELEGRAM_API_ROOT = "https://api.telegram.org"
MAX_API_ROOT = "https://platform-api2.max.ru"
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
DEFAULT_HTTP_TIMEOUT_SECONDS = 15.0
TELEGRAM_PHOTO_TYPES = frozenset({"image/jpeg", "image/png"})
TELEGRAM_PHOTO_MAX_BYTES = 10 * 1024 * 1024
MAX_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif"})
MAX_UPLOAD_HOST_SUFFIXES = (".oneme.ru", ".okcdn.ru")

T = TypeVar("T")


class ChannelTransportError(RuntimeError):
    """A provider operation failed without exposing credential material."""


class ChannelCredentialsError(ValueError):
    """A channel credential envelope is missing, invalid or incompatible."""


class BoundedThreadHandoff:
    """Run blocking functions in asyncio's thread pool with bounded admission."""

    def __init__(self, max_concurrency: int = 4) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def run(self, callback: Callable[[], T]) -> T:
        async with self._semaphore:
            return await asyncio.to_thread(callback)


async def _response_bytes(response: httpx.Response, *, provider: str) -> bytes:
    try:
        response.raise_for_status()
    except httpx.HTTPError:
        raise ChannelTransportError(f"{provider} attachment download failed") from None
    content = response.content
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise ChannelTransportError(f"{provider} attachment exceeds the 20 MB limit")
    return content


def _json_object(response: httpx.Response, *, provider: str) -> Mapping[str, Any]:
    try:
        response.raise_for_status()
        value = response.json()
    except (httpx.HTTPError, ValueError, TypeError):
        raise ChannelTransportError(f"{provider} API request failed") from None
    if not isinstance(value, Mapping):
        raise ChannelTransportError(f"{provider} API returned an invalid response")
    return cast(Mapping[str, Any], value)


def _utc_from_unix(value: Any, *, milliseconds: bool = False) -> datetime:
    if isinstance(value, (int, float)):
        divisor = 1000 if milliseconds else 1
        try:
            return datetime.fromtimestamp(value / divisor, UTC)
        except (OSError, OverflowError, ValueError):
            pass
    return datetime.now(UTC)


class TelegramHTTPTransport:
    """Telegram Bot API transport with redacted failures."""

    def __init__(self, bot_token: str, client: httpx.AsyncClient) -> None:
        if not bot_token:
            raise ValueError("Telegram bot token must not be empty")
        self._bot_token = bot_token
        self._client = client

    def _method_url(self, method: str) -> str:
        return f"{TELEGRAM_API_ROOT}/bot{self._bot_token}/{method}"

    async def _call(
        self,
        method: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        data: Mapping[str, str] | None = None,
        files: Mapping[str, tuple[str, bytes, str]] | None = None,
    ) -> Mapping[str, Any]:
        try:
            response = await self._client.post(
                self._method_url(method),
                json=json_body,
                data=data,
                files=files,
            )
        except httpx.HTTPError:
            raise ChannelTransportError("Telegram API request failed") from None
        payload = _json_object(response, provider="Telegram")
        if payload.get("ok") is not True:
            raise ChannelTransportError("Telegram API rejected the request")
        return payload

    async def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        reply_to_message_id: str | None,
        attachments: tuple[OutboundAttachment, ...] = (),
    ) -> tuple[str, datetime]:
        if attachments:
            return await self._send_with_attachments(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=reply_to_message_id,
                attachments=attachments,
            )
        body: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to_message_id is not None:
            body["reply_parameters"] = {"message_id": reply_to_message_id}
        payload = await self._call("sendMessage", json_body=body)
        result = payload.get("result")
        if not isinstance(result, Mapping) or result.get("message_id") is None:
            raise ChannelTransportError("Telegram API returned an invalid message")
        return str(result["message_id"]), _utc_from_unix(result.get("date"))

    async def _send_with_attachments(
        self,
        *,
        chat_id: str,
        text: str,
        reply_to_message_id: str | None,
        attachments: tuple[OutboundAttachment, ...],
    ) -> tuple[str, datetime]:
        first_result: tuple[str, datetime] | None = None
        caption = text if len(text) <= 1024 else ""
        reply_for_attachment = reply_to_message_id
        if text and not caption:
            first_result = await self.send_message(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=reply_to_message_id,
            )
            reply_for_attachment = None

        for index, attachment in enumerate(attachments):
            is_photo = (
                attachment.content_type in TELEGRAM_PHOTO_TYPES
                and attachment.size_bytes <= TELEGRAM_PHOTO_MAX_BYTES
            )
            method = "sendPhoto" if is_photo else "sendDocument"
            field_name = "photo" if is_photo else "document"
            form: dict[str, str] = {"chat_id": chat_id}
            if index == 0 and caption:
                form["caption"] = caption
            if index == 0 and reply_for_attachment is not None:
                form["reply_parameters"] = json.dumps(
                    {"message_id": reply_for_attachment},
                    separators=(",", ":"),
                )
            payload = await self._call(
                method,
                data=form,
                files={
                    field_name: (
                        attachment.filename,
                        attachment.content,
                        attachment.content_type,
                    )
                },
            )
            result = payload.get("result")
            if not isinstance(result, Mapping) or result.get("message_id") is None:
                raise ChannelTransportError("Telegram API returned an invalid message")
            if first_result is None:
                first_result = (
                    str(result["message_id"]),
                    _utc_from_unix(result.get("date")),
                )

        if first_result is None:
            raise ChannelTransportError("Telegram API returned an invalid message")
        return first_result

    async def download_file(self, file_id: str) -> bytes:
        payload = await self._call("getFile", json_body={"file_id": file_id})
        result = payload.get("result")
        file_path = result.get("file_path") if isinstance(result, Mapping) else None
        if not isinstance(file_path, str) or not file_path or ".." in file_path.split("/"):
            raise ChannelTransportError("Telegram API returned an invalid file path")
        url = f"{TELEGRAM_API_ROOT}/file/bot{self._bot_token}/{file_path.lstrip('/')}"
        try:
            response = await self._client.get(url)
        except httpx.HTTPError:
            raise ChannelTransportError("Telegram attachment download failed") from None
        return await _response_bytes(response, provider="Telegram")

    async def healthcheck(self) -> AdapterHealth:
        try:
            payload = await self._call("getMe")
            result = payload.get("result")
            if not isinstance(result, Mapping) or result.get("id") is None:
                raise ChannelTransportError("Telegram API returned an invalid bot")
        except ChannelTransportError:
            return AdapterHealth(healthy=False, detail="Telegram API unavailable")
        return AdapterHealth(healthy=True)


class MaxHTTPTransport:
    """MAX Bot API transport using the required raw Authorization token header."""

    def __init__(self, access_token: str, client: httpx.AsyncClient) -> None:
        if not access_token:
            raise ValueError("MAX access token must not be empty")
        self._access_token = access_token
        self._client = client

    @property
    def _headers(self) -> Mapping[str, str]:
        return {"Authorization": self._access_token}

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        try:
            response = await self._client.request(
                method,
                f"{MAX_API_ROOT}{path}",
                headers=self._headers,
                params=params,
                json=json_body,
            )
        except httpx.HTTPError:
            raise ChannelTransportError("MAX API request failed") from None
        return _json_object(response, provider="MAX")

    async def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        reply_to_message_id: str | None,
        attachments: tuple[OutboundAttachment, ...] = (),
    ) -> tuple[str, datetime]:
        body: dict[str, Any] = {"text": text}
        if reply_to_message_id is not None:
            body["link"] = {"type": "reply", "mid": reply_to_message_id}
        if attachments:
            body["attachments"] = [
                await self._upload_attachment(attachment) for attachment in attachments
            ]
        payload = await self._request_json(
            "POST",
            "/messages",
            params={"chat_id": chat_id},
            json_body=body,
        )
        message = payload.get("message")
        if not isinstance(message, Mapping):
            raise ChannelTransportError("MAX API returned an invalid message")
        body_value = message.get("body")
        message_body = body_value if isinstance(body_value, Mapping) else {}
        message_id = (
            message.get("mid")
            or message.get("message_id")
            or message.get("id")
            or message_body.get("mid")
        )
        if not isinstance(message_id, (str, int)):
            raise ChannelTransportError("MAX API returned an invalid message")
        return str(message_id), _utc_from_unix(message.get("timestamp"), milliseconds=True)

    async def _upload_attachment(self, attachment: OutboundAttachment) -> dict[str, Any]:
        upload_type = "image" if attachment.content_type in MAX_IMAGE_TYPES else "file"
        slot = await self._request_json("POST", "/uploads", params={"type": upload_type})
        upload_url = slot.get("url")
        if not isinstance(upload_url, str) or not _is_allowed_max_upload_url(upload_url):
            raise ChannelTransportError("MAX API returned an invalid upload URL")
        try:
            response = await self._client.post(
                upload_url,
                files={
                    "data": (
                        attachment.filename,
                        attachment.content,
                        attachment.content_type,
                    )
                },
            )
        except httpx.HTTPError:
            raise ChannelTransportError("MAX attachment upload failed") from None
        uploaded = _json_object(response, provider="MAX")
        token = uploaded.get("token") or slot.get("token")
        if not isinstance(token, str) or not token:
            raise ChannelTransportError("MAX API returned an invalid attachment token")
        return {"type": upload_type, "payload": {"token": token}}

    async def download_file(self, file_id: str) -> bytes:
        """Download a MAX URL, or resolve a video token through ``/videos``.

        MAX exposes URLs in inbound attachment payloads and resolves video
        tokens through ``GET /videos/{token}``.  Arbitrary redirect targets are
        refused; only HTTPS URLs returned by MAX are followed.
        """

        download_url = file_id if _is_https_url(file_id) else None
        if download_url is None:
            payload = await self._request_json("GET", f"/videos/{quote(file_id, safe='')}")
            urls = payload.get("urls")
            if isinstance(urls, Mapping):
                for key in ("download", "mp4", "url", "external"):
                    candidate = urls.get(key)
                    if isinstance(candidate, str) and _is_https_url(candidate):
                        download_url = candidate
                        break
        if download_url is None:
            raise ChannelTransportError("MAX attachment has no HTTPS download URL")
        try:
            # MAX download URLs are commonly signed CDN URLs. Never forward the
            # bot token away from the API origin.
            response = await self._client.get(download_url)
        except httpx.HTTPError:
            raise ChannelTransportError("MAX attachment download failed") from None
        return await _response_bytes(response, provider="MAX")

    async def healthcheck(self) -> AdapterHealth:
        try:
            payload = await self._request_json("GET", "/me")
            if payload.get("user_id") is None:
                raise ChannelTransportError("MAX API returned an invalid bot")
        except ChannelTransportError:
            return AdapterHealth(healthy=False, detail="MAX API unavailable")
        return AdapterHealth(healthy=True)


def _is_https_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.hostname) and parsed.username is None


def _is_allowed_max_upload_url(value: str) -> bool:
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").casefold()
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and any(hostname.endswith(suffix) for suffix in MAX_UPLOAD_HOST_SUFFIXES)
    )


@dataclass(frozen=True, slots=True)
class SMTPConfig:
    host: str
    port: int = 587
    security: str = "starttls"
    username: str | None = None
    password: str | None = None
    from_address: str = ""
    subject: str = "Pulse CRM"
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if not self.host:
            raise ChannelCredentialsError("SMTP host is required")
        if not self.from_address:
            raise ChannelCredentialsError("SMTP from_address is required")
        if self.security not in {"ssl", "starttls", "plain"}:
            raise ChannelCredentialsError("SMTP security must be ssl, starttls or plain")
        if not 1 <= self.port <= 65_535:
            raise ChannelCredentialsError("SMTP port is invalid")
        if bool(self.username) != bool(self.password):
            raise ChannelCredentialsError("SMTP username and password must be supplied together")


class SMTPTransport:
    """SMTP sender executed outside the event loop."""

    def __init__(
        self,
        config: SMTPConfig,
        handoff: BoundedThreadHandoff,
        *,
        smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP,
        smtp_ssl_factory: Callable[..., smtplib.SMTP_SSL] = smtplib.SMTP_SSL,
    ) -> None:
        self._config = config
        self._handoff = handoff
        self._smtp_factory = smtp_factory
        self._smtp_ssl_factory = smtp_ssl_factory

    async def send_message(
        self,
        *,
        recipient: str,
        text: str,
        in_reply_to: str | None,
        attachments: tuple[OutboundAttachment, ...] = (),
    ) -> tuple[str, datetime]:
        message = EmailMessage()
        message["From"] = self._config.from_address
        message["To"] = recipient
        message["Subject"] = self._config.subject
        message_id = make_msgid()
        message["Message-ID"] = message_id
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
            message["References"] = in_reply_to
        message.set_content(text)
        for attachment in attachments:
            maintype, separator, subtype = attachment.content_type.partition("/")
            if not separator or not maintype or not subtype:
                raise ChannelTransportError("SMTP attachment has an invalid content type")
            message.add_attachment(
                attachment.content,
                maintype=maintype,
                subtype=subtype,
                filename=attachment.filename,
            )

        try:
            await self._handoff.run(lambda: self._send_sync(message))
        except (OSError, smtplib.SMTPException, TimeoutError):
            raise ChannelTransportError("SMTP send failed") from None
        return message_id, datetime.now(UTC)

    async def healthcheck(self) -> AdapterHealth:
        try:
            await self._handoff.run(self._healthcheck_sync)
        except (OSError, smtplib.SMTPException, TimeoutError):
            return AdapterHealth(healthy=False, detail="SMTP unavailable")
        return AdapterHealth(healthy=True)

    def _connect(self) -> smtplib.SMTP:
        config = self._config
        if config.security == "ssl":
            client: smtplib.SMTP = self._smtp_ssl_factory(
                config.host,
                config.port,
                timeout=config.timeout_seconds,
                context=ssl.create_default_context(),
            )
        else:
            client = self._smtp_factory(
                config.host,
                config.port,
                timeout=config.timeout_seconds,
            )
            client.ehlo()
            if config.security == "starttls":
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
        if config.username and config.password:
            client.login(config.username, config.password)
        return client

    def _send_sync(self, message: EmailMessage) -> None:
        client = self._connect()
        try:
            client.send_message(message)
        finally:
            _close_smtp(client)

    def _healthcheck_sync(self) -> None:
        client = self._connect()
        try:
            code, _ = client.noop()
            if code >= 400:
                raise smtplib.SMTPException("SMTP NOOP rejected")
        finally:
            _close_smtp(client)


def _close_smtp(client: smtplib.SMTP) -> None:
    try:
        client.quit()
    except (OSError, smtplib.SMTPException):
        client.close()


@dataclass(frozen=True, slots=True)
class IMAPConfig:
    host: str
    port: int = 993
    security: str = "ssl"
    username: str = ""
    password: str = ""
    mailbox: str = "INBOX"
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        if not self.host:
            raise ChannelCredentialsError("IMAP host is required")
        if not self.username or not self.password:
            raise ChannelCredentialsError("IMAP username and password are required")
        if self.security not in {"ssl", "starttls", "plain"}:
            raise ChannelCredentialsError("IMAP security must be ssl, starttls or plain")
        if not 1 <= self.port <= 65_535:
            raise ChannelCredentialsError("IMAP port is invalid")
        if not self.mailbox:
            raise ChannelCredentialsError("IMAP mailbox is required")


@dataclass(frozen=True, slots=True)
class IMAPPollBatch:
    uidvalidity: int
    envelopes: tuple[EmailEnvelope, ...]

    @property
    def last_uid(self) -> int | None:
        if not self.envelopes:
            return None
        return self.envelopes[-1].uid


class AsyncIMAPPoller:
    """Open a mailbox, fetch a bounded UID page, then close the connection."""

    def __init__(
        self,
        config: IMAPConfig,
        handoff: BoundedThreadHandoff,
        *,
        imap_factory: Callable[..., imaplib.IMAP4] = imaplib.IMAP4,
        imap_ssl_factory: Callable[..., imaplib.IMAP4_SSL] = imaplib.IMAP4_SSL,
    ) -> None:
        self._config = config
        self._handoff = handoff
        self._imap_factory = imap_factory
        self._imap_ssl_factory = imap_ssl_factory

    async def poll(self, *, after_uid: int | None = None, limit: int = 50) -> IMAPPollBatch:
        if after_uid is not None and after_uid < 0:
            raise ValueError("after_uid must not be negative")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        try:
            return await self._handoff.run(lambda: self._poll_sync(after_uid, limit))
        except (OSError, imaplib.IMAP4.error, TimeoutError, ValueError):
            raise ChannelTransportError("IMAP polling failed") from None

    async def healthcheck(self) -> AdapterHealth:
        try:
            await self._handoff.run(self._healthcheck_sync)
        except (OSError, imaplib.IMAP4.error, TimeoutError, ValueError):
            return AdapterHealth(healthy=False, detail="IMAP unavailable")
        return AdapterHealth(healthy=True)

    def _connect(self) -> imaplib.IMAP4:
        config = self._config
        if config.security == "ssl":
            client: imaplib.IMAP4 = self._imap_ssl_factory(
                config.host,
                config.port,
                ssl_context=ssl.create_default_context(),
                timeout=config.timeout_seconds,
            )
        else:
            client = self._imap_factory(
                config.host,
                config.port,
                timeout=config.timeout_seconds,
            )
            if config.security == "starttls":
                client.starttls(ssl_context=ssl.create_default_context())
        client.login(config.username, config.password)
        return client

    def _select(self, client: imaplib.IMAP4) -> int:
        status, _ = client.select(self._config.mailbox, readonly=True)
        if status != "OK":
            raise imaplib.IMAP4.error("mailbox selection failed")
        _, values = client.response("UIDVALIDITY")
        if not values or values[0] is None:
            raise imaplib.IMAP4.error("mailbox has no UIDVALIDITY")
        raw = values[0]
        if isinstance(raw, bytes):
            raw = raw.decode("ascii", errors="strict")
        return int(raw)

    def _poll_sync(self, after_uid: int | None, limit: int) -> IMAPPollBatch:
        client = self._connect()
        try:
            uidvalidity = self._select(client)
            start_uid = (after_uid or 0) + 1
            # imaplib requires ``None`` for the SEARCH charset when the
            # criteria are ASCII, though typeshed currently annotates this
            # generic UID argument as ``str`` only.
            status, values = cast(Any, client).uid("SEARCH", None, f"UID {start_uid}:*")
            if status != "OK":
                raise imaplib.IMAP4.error("UID search failed")
            raw_uids = values[0] if values else b""
            if isinstance(raw_uids, str):
                raw_uids = raw_uids.encode("ascii")
            uids = sorted(int(value) for value in raw_uids.split() if value.isdigit())
            if after_uid is not None:
                uids = [uid for uid in uids if uid > after_uid]

            envelopes: list[EmailEnvelope] = []
            for uid in uids[:limit]:
                status, parts = client.uid("FETCH", str(uid), "(BODY.PEEK[])")
                if status != "OK":
                    raise imaplib.IMAP4.error("UID fetch failed")
                raw_message = _imap_message_bytes(parts)
                envelopes.append(EmailEnvelope(uidvalidity, uid, raw_message))
            return IMAPPollBatch(uidvalidity, tuple(envelopes))
        finally:
            _close_imap(client)

    def _healthcheck_sync(self) -> None:
        client = self._connect()
        try:
            self._select(client)
        finally:
            _close_imap(client)


def _imap_message_bytes(parts: list[Any]) -> bytes:
    for part in parts:
        if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], bytes):
            return part[1]
    raise imaplib.IMAP4.error("UID fetch contained no message")


def _close_imap(client: imaplib.IMAP4) -> None:
    try:
        client.close()
    except imaplib.IMAP4.error:
        pass
    try:
        client.logout()
    except (OSError, imaplib.IMAP4.error):
        pass


class ChannelAdapterFactory:
    """Decrypt a ``ChannelConnection`` and construct its concrete adapter."""

    def __init__(
        self,
        cipher: SecretCipher,
        *,
        http_client: httpx.AsyncClient | None = None,
        blocking_handoff: BoundedThreadHandoff | None = None,
        http_timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._cipher = cipher
        self._handoff = blocking_handoff or BoundedThreadHandoff()
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(http_timeout_seconds),
            follow_redirects=False,
        )

    def build(self, connection: ChannelConnection) -> ChannelAdapter:
        config = self._connection_config(connection)
        kind = (
            connection.kind.value
            if isinstance(connection.kind, ChannelKind)
            else str(connection.kind)
        )

        if kind == ChannelKind.telegram.value:
            token = _required_string(config, "bot_token", "token")
            webhook_secret = _required_string(config, "webhook_secret")
            return TelegramAdapter(
                webhook_secret,
                TelegramHTTPTransport(token, self._http_client),
            )
        if kind == ChannelKind.max.value:
            token = _required_string(config, "access_token", "token")
            webhook_secret = _required_string(config, "webhook_secret")
            return MaxAdapter(webhook_secret, MaxHTTPTransport(token, self._http_client))
        if kind == ChannelKind.email.value:
            smtp_config = _smtp_config(_section(config, "smtp"))
            return EmailAdapter(SMTPTransport(smtp_config, self._handoff))
        raise ChannelCredentialsError("unsupported channel provider")

    def build_imap_poller(self, connection: ChannelConnection) -> AsyncIMAPPoller:
        kind = (
            connection.kind.value
            if isinstance(connection.kind, ChannelKind)
            else str(connection.kind)
        )
        if kind != ChannelKind.email.value:
            raise ChannelCredentialsError("IMAP polling is available only for email channels")
        config = self._connection_config(connection)
        return AsyncIMAPPoller(_imap_config(_section(config, "imap")), self._handoff)

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    def _connection_config(self, connection: ChannelConnection) -> Mapping[str, Any]:
        encrypted = connection.encrypted_credentials
        if encrypted is None:
            raise ChannelCredentialsError("channel credentials are not configured")
        if connection.credentials_key_id not in {None, self._cipher.key_id}:
            raise ChannelCredentialsError("channel credentials use an unavailable key")
        try:
            plaintext = self._cipher.decrypt(
                encrypted,
                associated_data=f"channel:{connection.id}".encode(),
            )
            secrets = json.loads(plaintext.decode("utf-8"))
        except (SecretCipherError, UnicodeDecodeError, json.JSONDecodeError):
            raise ChannelCredentialsError("channel credentials cannot be decrypted") from None
        if not isinstance(secrets, Mapping):
            raise ChannelCredentialsError("channel credentials must be a JSON object")
        settings = connection.settings if isinstance(connection.settings, Mapping) else {}
        return _merge_mappings(settings, secrets)


def _merge_mappings(base: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overrides.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_mappings(current, value)
        else:
            merged[key] = value
    return merged


def _section(config: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    result = {key: value for key, value in config.items() if key not in {"smtp", "imap"}}
    nested = config.get(name)
    if isinstance(nested, Mapping):
        result.update(nested)
    return result


def _required_string(config: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = config.get(key)
        if isinstance(value, str) and value:
            return value
    raise ChannelCredentialsError(f"required channel credential {keys[0]} is missing")


def _optional_string(config: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = config.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _integer(config: Mapping[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    if isinstance(value, bool):
        raise ChannelCredentialsError(f"{key} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ChannelCredentialsError(f"{key} must be an integer") from None


def _number(config: Mapping[str, Any], key: str, default: float) -> float:
    value = config.get(key, default)
    if isinstance(value, bool):
        raise ChannelCredentialsError(f"{key} must be a number")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ChannelCredentialsError(f"{key} must be a number") from None


def _smtp_config(config: Mapping[str, Any]) -> SMTPConfig:
    return SMTPConfig(
        host=_required_string(config, "host", "smtp_host"),
        port=_integer(config, "port", _integer(config, "smtp_port", 587)),
        security=_optional_string(config, "security", "smtp_security") or "starttls",
        username=_optional_string(config, "username", "smtp_username"),
        password=_optional_string(config, "password", "smtp_password"),
        from_address=_required_string(config, "from_address", "from_email", "smtp_from"),
        subject=_optional_string(config, "subject", "smtp_subject") or "Pulse CRM",
        timeout_seconds=_number(config, "timeout_seconds", 15.0),
    )


def _imap_config(config: Mapping[str, Any]) -> IMAPConfig:
    return IMAPConfig(
        host=_required_string(config, "host", "imap_host"),
        port=_integer(config, "port", _integer(config, "imap_port", 993)),
        security=_optional_string(config, "security", "imap_security") or "ssl",
        username=_required_string(config, "username", "imap_username"),
        password=_required_string(config, "password", "imap_password"),
        mailbox=_optional_string(config, "mailbox", "imap_mailbox") or "INBOX",
        timeout_seconds=_number(config, "timeout_seconds", 20.0),
    )


__all__ = [
    "AsyncIMAPPoller",
    "BoundedThreadHandoff",
    "ChannelAdapterFactory",
    "ChannelCredentialsError",
    "ChannelTransportError",
    "IMAPConfig",
    "IMAPPollBatch",
    "MaxHTTPTransport",
    "SMTPConfig",
    "SMTPTransport",
    "TelegramHTTPTransport",
]

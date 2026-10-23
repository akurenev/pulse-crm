"""MIME parsing to inert text and construction of stable outbound MIME bytes."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import format_datetime, getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from app.integrations.s3 import AttachmentValidationError, validate_attachment


class TextFromHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.blocked = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "head", "iframe", "object", "svg"}:
            self.blocked += 1
        if not self.blocked and tag in {"p", "br", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "head", "iframe", "object", "svg"}:
            self.blocked = max(0, self.blocked - 1)
        if not self.blocked and tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.blocked:
            self.parts.append(data)


def html_to_text(source: str) -> str:
    parser = TextFromHTML()
    parser.feed(source)
    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", "".join(parser.parts)).strip()


@dataclass(frozen=True, slots=True)
class ParsedAttachment:
    filename: str
    content_type: str
    content: bytes


@dataclass(frozen=True, slots=True)
class ParsedMail:
    subject: str
    from_address: str
    from_name: str | None
    to_addresses: list[str]
    cc_addresses: list[str]
    reply_to_addresses: list[str]
    body_text: str
    internet_message_id: str | None
    in_reply_to: str | None
    references: list[str]
    thread_key: str
    occurred_at: datetime
    attachments: tuple[ParsedAttachment, ...]
    blocked_attachments: int


def _text(part: Message) -> str:
    value = part.get_payload(decode=True)
    if not isinstance(value, bytes):
        return ""
    try:
        return value.decode(part.get_content_charset() or "utf-8", errors="replace")
    except LookupError:
        return value.decode("utf-8", errors="replace")


def parse_mail(raw: bytes) -> ParsedMail:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    sender_name, sender_address = parseaddr(str(message.get("From", "")))
    text: list[str] = []
    html: list[str] = []
    attachments: list[ParsedAttachment] = []
    blocked = 0
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if filename or disposition == "attachment":
            content = part.get_payload(decode=True)
            if not isinstance(content, bytes) or len(attachments) >= 30:
                blocked += 1
                continue
            try:
                validated = validate_attachment(
                    filename or "attachment",
                    part.get_content_type(),
                    content,
                )
            except AttachmentValidationError:
                blocked += 1
                continue
            attachments.append(
                ParsedAttachment(validated.filename, validated.content_type, content)
            )
        elif part.get_content_type() == "text/plain":
            text.append(_text(part))
        elif part.get_content_type() == "text/html":
            html.append(_text(part))
    body = "\n".join(text) if text else html_to_text("\n".join(html))
    try:
        occurred = parsedate_to_datetime(str(message.get("Date", "")))
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=UTC)
    except (TypeError, ValueError, OverflowError):
        occurred = datetime.now(UTC)
    message_id = str(message.get("Message-ID", "")).strip()[:998] or None
    in_reply_to = str(message.get("In-Reply-To", "")).strip()[:998] or None
    references = re.findall(r"<[^<>\s]+>", str(message.get("References", "")))[:100]
    root = next(iter(references), None) or in_reply_to or message_id
    if not root:
        root = "sha256:" + hashlib.sha256(raw).hexdigest()

    def addresses(key: str) -> list[str]:
        return [address[:320] for _, address in getaddresses(message.get_all(key, [])) if address][
            :100
        ]

    return ParsedMail(
        str(message.get("Subject", ""))[:998],
        sender_address[:320],
        sender_name[:320] or None,
        addresses("To"),
        addresses("Cc"),
        addresses("Reply-To"),
        body[:100_000],
        message_id,
        in_reply_to,
        references,
        root[:998],
        occurred.astimezone(UTC),
        tuple(attachments),
        blocked,
    )


def build_mail(
    *,
    email: str,
    display_name: str,
    subject: str,
    to_addresses: list[str],
    cc_addresses: list[str],
    body_text: str,
    message_id: str,
    sent_at: datetime,
    in_reply_to: str | None,
    references: list[str],
    attachments: tuple[ParsedAttachment, ...] = (),
) -> bytes:
    from email.utils import formataddr

    message = EmailMessage(policy=policy.SMTP)
    message["From"] = formataddr((display_name, email))
    message["To"] = ", ".join(to_addresses)
    if cc_addresses:
        message["Cc"] = ", ".join(cc_addresses)
    message["Subject"] = subject
    message["Message-ID"] = message_id
    timestamp = sent_at.replace(tzinfo=UTC) if sent_at.tzinfo is None else sent_at.astimezone(UTC)
    message["Date"] = format_datetime(timestamp)
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = " ".join(references)
    message.set_content(body_text)
    for attachment in attachments:
        main, _, sub = attachment.content_type.partition("/")
        message.add_attachment(
            attachment.content,
            maintype=main,
            subtype=sub,
            filename=attachment.filename,
        )
    # A deterministic boundary ensures Sent retries preserve the original MIME.
    if message.is_multipart():
        message.set_boundary("pulse-" + hashlib.sha256(message_id.encode()).hexdigest()[:40])
    return message.as_bytes()


def parsed_values(parsed: ParsedMail) -> dict[str, Any]:
    return {
        key: getattr(parsed, key)
        for key in (
            "subject",
            "from_address",
            "from_name",
            "to_addresses",
            "cc_addresses",
            "body_text",
            "internet_message_id",
            "in_reply_to",
            "references",
            "thread_key",
        )
    }

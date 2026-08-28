"""Authentication and durable acceptance for generic inbound webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.models import InboundEvent, InboundStatus

DEFAULT_REPLAY_WINDOW = timedelta(minutes=5)
IDEMPOTENCY_KEY_RE = re.compile(r"^[\x21-\x7e]{1,255}$")


class WebhookVerificationError(ValueError):
    """Raised when a public webhook cannot be authenticated safely."""


class IdempotencyConflictError(ValueError):
    """Raised when one idempotency key is reused for different content."""


@dataclass(frozen=True, slots=True)
class VerifiedWebhook:
    timestamp: datetime
    idempotency_key: str
    request_digest: str


@dataclass(frozen=True, slots=True)
class InboundAcceptance:
    event: InboundEvent
    created: bool


def webhook_signature(secret: bytes | str, timestamp: int, body: bytes) -> str:
    """Return the documented ``sha256=<hex>`` signature for an outbound test.

    Pulse signs the exact bytes ``<unix timestamp>.<raw request body>``.  Raw
    bytes are essential: parsing and re-serializing JSON would make valid
    signatures dependent on whitespace or key ordering.
    """

    secret_bytes = secret.encode() if isinstance(secret, str) else secret
    signed_payload = str(timestamp).encode("ascii") + b"." + body
    return "sha256=" + hmac.new(secret_bytes, signed_payload, hashlib.sha256).hexdigest()


def verify_generic_webhook(
    *,
    secret: bytes | str,
    body: bytes,
    signature: str,
    timestamp: str | int,
    idempotency_key: str,
    now: datetime | None = None,
    replay_window: timedelta = DEFAULT_REPLAY_WINDOW,
) -> VerifiedWebhook:
    """Verify HMAC, replay window and the syntactic idempotency key.

    Idempotency itself is enforced by :func:`accept_inbound_event`, backed by a
    database uniqueness constraint.  Keeping authentication pure lets an HTTP
    route reject invalid requests before opening a transaction.
    """

    secret_bytes = secret.encode() if isinstance(secret, str) else secret
    if not secret_bytes:
        raise WebhookVerificationError("webhook secret must not be empty")
    if replay_window <= timedelta(0):
        raise ValueError("replay_window must be positive")
    if not IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
        raise WebhookVerificationError("invalid Idempotency-Key")

    try:
        unix_timestamp = int(timestamp)
        request_time = datetime.fromtimestamp(unix_timestamp, UTC)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WebhookVerificationError("invalid webhook timestamp") from exc

    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if abs(current_time - request_time) > replay_window:
        raise WebhookVerificationError("webhook timestamp is outside the replay window")

    normalized_signature = signature.removeprefix("sha256=").lower()
    if len(normalized_signature) != 64:
        raise WebhookVerificationError("invalid webhook signature")
    try:
        bytes.fromhex(normalized_signature)
    except ValueError as exc:
        raise WebhookVerificationError("invalid webhook signature") from exc

    expected = webhook_signature(secret_bytes, unix_timestamp, body).removeprefix("sha256=")
    if not hmac.compare_digest(normalized_signature, expected):
        raise WebhookVerificationError("invalid webhook signature")

    return VerifiedWebhook(
        timestamp=request_time,
        idempotency_key=idempotency_key,
        request_digest=hashlib.sha256(body).hexdigest(),
    )


def decode_json_object(body: bytes) -> dict[str, Any]:
    """Decode a bounded webhook body after the HTTP layer enforced its limit."""

    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookVerificationError("request body must be valid JSON") from exc
    if not isinstance(decoded, dict):
        raise WebhookVerificationError("request body must be a JSON object")
    return decoded


def _insert_for(session: AsyncSession) -> Any:
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        return pg_insert(InboundEvent)
    if dialect_name == "sqlite":
        return sqlite_insert(InboundEvent)
    raise RuntimeError(f"unsupported database dialect for idempotent insert: {dialect_name}")


async def accept_inbound_event(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    source_key: str,
    verified: VerifiedWebhook,
    payload: dict[str, Any],
    channel_connection_id: uuid.UUID | None = None,
    external_event_id: str | None = None,
) -> InboundAcceptance:
    """Insert an authenticated event exactly once, without committing.

    Concurrent deliveries use one ``INSERT .. ON CONFLICT DO NOTHING``.  A
    retry with the same key and identical bytes returns the original record;
    reusing that key for different content is rejected explicitly.
    """

    if not source_key.strip():
        raise ValueError("source_key must not be empty")

    values = {
        "id": uuid.uuid4(),
        "workspace_id": workspace_id,
        "channel_connection_id": channel_connection_id,
        "source_key": source_key,
        "external_event_id": external_event_id,
        "idempotency_key": verified.idempotency_key,
        "request_digest": verified.request_digest,
        "payload": payload,
        "status": InboundStatus.accepted,
        "received_at": datetime.now(UTC),
    }
    statement = (
        _insert_for(session)
        .values(**values)
        # No conflict target is intentional: provider event IDs and generic
        # Idempotency-Keys are separate unique constraints, and either one can
        # prove that this delivery was already accepted.
        .on_conflict_do_nothing()
        .returning(InboundEvent.id)
    )
    inserted_id = (await session.execute(statement)).scalar_one_or_none()
    if inserted_id is not None:
        event = await session.get(InboundEvent, inserted_id)
        if event is None:  # pragma: no cover - database contract guard
            raise RuntimeError("inserted inbound event could not be loaded")
        return InboundAcceptance(event=event, created=True)

    event = await session.scalar(
        sa.select(InboundEvent).where(
            InboundEvent.workspace_id == workspace_id,
            InboundEvent.source_key == source_key,
            InboundEvent.idempotency_key == verified.idempotency_key,
        )
    )
    if event is None and external_event_id is not None:
        event = await session.scalar(
            sa.select(InboundEvent).where(
                InboundEvent.workspace_id == workspace_id,
                InboundEvent.source_key == source_key,
                InboundEvent.external_event_id == external_event_id,
            )
        )
    if event is None:  # pragma: no cover - indicates a broken database constraint
        raise RuntimeError("deduplicated inbound event could not be loaded")
    if event.request_digest != verified.request_digest:
        raise IdempotencyConflictError("Idempotency-Key was already used for another payload")
    return InboundAcceptance(event=event, created=False)

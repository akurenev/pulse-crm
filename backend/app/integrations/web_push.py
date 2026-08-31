"""Encrypted PWA subscriptions and privacy-safe Web Push delivery."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.channels.base import SendResult
from app.integrations.models import (
    NotificationAudience,
    NotificationDelivery,
    WebPushSubscription,
)
from app.integrations.secrets import SecretCipher, SecretCipherError
from app.models import DeliveryStatus, OutboxEvent, User
from app.services.jobs import SessionFactory

MAX_ACTIVE_SUBSCRIPTIONS_PER_USER = 10
MAX_WEB_PUSH_PAYLOAD_BYTES = 3_000
DEFAULT_WEB_PUSH_URL = "/"


class WebPushDeliveryError(RuntimeError):
    """A retryable provider error without subscription secrets in its message."""


@dataclass(frozen=True, slots=True)
class QueuedWebPushDelivery:
    delivery: NotificationDelivery
    created: bool


class _WebPushCall(Protocol):
    def __call__(
        self,
        *,
        subscription_info: dict[str, Any],
        data: str,
        vapid_private_key: str,
        vapid_claims: dict[str, str],
        ttl: int,
        request_timeout: float,
    ) -> Awaitable[object]: ...


class _NoRedirectSession:
    """Small aiohttp proxy that forbids provider-controlled redirect hops."""

    def __init__(self, session: Any) -> None:
        self._session = session

    def post(self, *args: Any, **kwargs: Any) -> Any:
        kwargs["allow_redirects"] = False
        return self._session.post(*args, **kwargs)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def subscription_aad(
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    endpoint_digest: str,
) -> bytes:
    return f"web-push-subscription:{workspace_id}:{user_id}:{endpoint_digest}".encode()


def _insert_for(session: AsyncSession) -> Any:
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        return pg_insert(WebPushSubscription)
    if dialect_name == "sqlite":
        return sqlite_insert(WebPushSubscription)
    raise RuntimeError(f"unsupported database dialect for web push: {dialect_name}")


def _delivery_insert_for(session: AsyncSession) -> Any:
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        return pg_insert(NotificationDelivery)
    if dialect_name == "sqlite":
        return sqlite_insert(NotificationDelivery)
    raise RuntimeError(f"unsupported database dialect for web push: {dialect_name}")


async def register_subscription(
    session: AsyncSession,
    *,
    cipher: SecretCipher,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    endpoint: str,
    expiration_time: datetime | None,
    p256dh: str,
    auth: str,
    now: datetime | None = None,
) -> WebPushSubscription:
    """Upsert a browser endpoint and cap active devices for the current employee."""

    current_time = _as_utc(now or datetime.now(UTC))
    if expiration_time is not None:
        expiration_time = _as_utc(expiration_time)
        if expiration_time <= current_time:
            raise ValueError("web push subscription is already expired")
    # Serialize registrations for the same employee so concurrent endpoint
    # upserts cannot bypass the per-user device cap.
    locked_user_id = await session.scalar(
        sa.select(User.id).where(User.id == user_id).with_for_update()
    )
    if locked_user_id is None:
        raise LookupError("web push subscription user not found")
    digest = endpoint_hash(endpoint)
    plaintext = json.dumps(
        {
            "endpoint": endpoint,
            "expiration_time": expiration_time.isoformat() if expiration_time else None,
            "keys": {"p256dh": p256dh, "auth": auth},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    encrypted = cipher.encrypt(
        plaintext,
        associated_data=subscription_aad(
            workspace_id=workspace_id,
            user_id=user_id,
            endpoint_digest=digest,
        ),
    )
    subscription_id = uuid.uuid4()
    statement = (
        _insert_for(session)
        .values(
            id=subscription_id,
            workspace_id=workspace_id,
            user_id=user_id,
            endpoint_hash=digest,
            encrypted_subscription=encrypted,
            encryption_key_id=cipher.key_id,
            expiration_time=expiration_time,
            is_active=True,
            disabled_at=None,
            last_success_at=None,
            last_error=None,
            created_at=current_time,
            updated_at=current_time,
        )
        .on_conflict_do_update(
            index_elements=[WebPushSubscription.endpoint_hash],
            set_={
                "workspace_id": workspace_id,
                "user_id": user_id,
                "encrypted_subscription": encrypted,
                "encryption_key_id": cipher.key_id,
                "expiration_time": expiration_time,
                "is_active": True,
                "disabled_at": None,
                "last_success_at": None,
                "last_error": None,
                "updated_at": current_time,
            },
        )
        .returning(WebPushSubscription.id)
    )
    stored_id = (await session.execute(statement)).scalar_one()

    await session.execute(
        sa.delete(WebPushSubscription)
        .where(
            WebPushSubscription.workspace_id == workspace_id,
            WebPushSubscription.user_id == user_id,
            WebPushSubscription.id != stored_id,
            sa.or_(
                WebPushSubscription.is_active.is_(False),
                sa.and_(
                    WebPushSubscription.expiration_time.is_not(None),
                    WebPushSubscription.expiration_time <= current_time,
                ),
            ),
        )
    )
    active_ids = list(
        (
            await session.scalars(
                sa.select(WebPushSubscription.id)
                .where(
                    WebPushSubscription.workspace_id == workspace_id,
                    WebPushSubscription.user_id == user_id,
                    WebPushSubscription.is_active.is_(True),
                )
                .order_by(
                    sa.case((WebPushSubscription.id == stored_id, 0), else_=1),
                    WebPushSubscription.updated_at.desc(),
                    WebPushSubscription.id.desc(),
                )
            )
        ).all()
    )
    stale_ids = active_ids[MAX_ACTIVE_SUBSCRIPTIONS_PER_USER:]
    if stale_ids:
        await session.execute(
            sa.delete(WebPushSubscription).where(WebPushSubscription.id.in_(stale_ids))
        )
    stored = await session.get(WebPushSubscription, stored_id)
    if stored is None:  # pragma: no cover - INSERT/RETURNING invariant
        raise RuntimeError("stored web push subscription could not be loaded")
    return stored


async def delete_subscription(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    endpoint: str,
) -> bool:
    result = await session.execute(
        sa.delete(WebPushSubscription).where(
            WebPushSubscription.workspace_id == workspace_id,
            WebPushSubscription.user_id == user_id,
            WebPushSubscription.endpoint_hash == endpoint_hash(endpoint),
        )
    )
    return bool(getattr(result, "rowcount", 0))


def _active_subscription_predicate(
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    now: datetime,
) -> Any:
    return sa.and_(
        WebPushSubscription.workspace_id == workspace_id,
        WebPushSubscription.user_id == user_id,
        WebPushSubscription.is_active.is_(True),
        sa.or_(
            WebPushSubscription.expiration_time.is_(None),
            WebPushSubscription.expiration_time > now,
        ),
    )


async def has_active_subscription(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    now: datetime | None = None,
) -> bool:
    current_time = now or datetime.now(UTC)
    return (
        await session.scalar(
            sa.select(WebPushSubscription.id)
            .where(
                _active_subscription_predicate(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    now=current_time,
                )
            )
            .limit(1)
        )
        is not None
    )


async def enqueue_web_push_delivery(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    subject: str | None,
    body: str,
    dedupe_key: str,
    scheduled_at: datetime | None = None,
    rule_id: uuid.UUID | None = None,
    template_id: uuid.UUID | None = None,
    target_entity_type: str | None = None,
    target_entity_id: uuid.UUID | None = None,
) -> QueuedWebPushDelivery:
    if (target_entity_type is None) != (target_entity_id is None) or (
        target_entity_type is not None and target_entity_type not in {"deal", "task"}
    ):
        raise ValueError("invalid web push target")
    due_at = scheduled_at or datetime.now(UTC)
    delivery_id = uuid.uuid4()
    inserted_id = (
        await session.execute(
            _delivery_insert_for(session)
            .values(
                id=delivery_id,
                workspace_id=workspace_id,
                rule_id=rule_id,
                template_id=template_id,
                audience=NotificationAudience.employee,
                channel="web_push",
                recipient_id=user_id,
                recipient_address=str(user_id),
                subject=subject,
                body=body,
                target_entity_type=target_entity_type,
                target_entity_id=target_entity_id,
                status=DeliveryStatus.pending,
                dedupe_key=dedupe_key,
                scheduled_at=due_at,
                attempts=0,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    NotificationDelivery.workspace_id,
                    NotificationDelivery.dedupe_key,
                ]
            )
            .returning(NotificationDelivery.id)
        )
    ).scalar_one_or_none()
    if inserted_id is None:
        existing = await session.scalar(
            sa.select(NotificationDelivery).where(
                NotificationDelivery.workspace_id == workspace_id,
                NotificationDelivery.dedupe_key == dedupe_key,
            )
        )
        if existing is None:  # pragma: no cover - unique constraint invariant
            raise RuntimeError("deduplicated web push delivery could not be loaded")
        return QueuedWebPushDelivery(delivery=existing, created=False)
    delivery = await session.get(NotificationDelivery, inserted_id)
    if delivery is None:  # pragma: no cover - INSERT/RETURNING invariant
        raise RuntimeError("queued web push delivery could not be loaded")
    session.add(
        OutboxEvent(
            workspace_id=workspace_id,
            event_type="notification.delivery.queued",
            aggregate_type="notification_delivery",
            aggregate_id=delivery.id,
            payload={
                "delivery_id": str(delivery.id),
                "channel": "web_push",
                "scheduled_at": due_at.isoformat(),
            },
            available_at=due_at,
        )
    )
    return QueuedWebPushDelivery(delivery=delivery, created=True)


async def mirror_delivered_in_app_notification(
    session: AsyncSession,
    *,
    delivery: NotificationDelivery,
    now: datetime,
) -> NotificationDelivery | None:
    if (
        delivery.channel != "in_app"
        or delivery.audience is not NotificationAudience.employee
        or delivery.recipient_id is None
    ):
        return None
    if not await has_active_subscription(
        session,
        workspace_id=delivery.workspace_id,
        user_id=delivery.recipient_id,
        now=now,
    ):
        return None
    queued = await enqueue_web_push_delivery(
        session,
        workspace_id=delivery.workspace_id,
        user_id=delivery.recipient_id,
        subject=delivery.subject,
        body=delivery.body,
        rule_id=delivery.rule_id,
        template_id=delivery.template_id,
        target_entity_type=delivery.target_entity_type,
        target_entity_id=delivery.target_entity_id,
        dedupe_key=f"web-push:in-app:{delivery.id}",
        scheduled_at=now,
    )
    return queued.delivery


def _truncate_utf8(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    return encoded[:maximum_bytes].decode("utf-8", errors="ignore")


def _web_push_url(delivery: NotificationDelivery) -> str:
    """Build only the UUID-based relative routes supported by the PWA."""

    if delivery.target_entity_id is None:
        return DEFAULT_WEB_PUSH_URL
    try:
        entity_id = uuid.UUID(str(delivery.target_entity_id))
    except ValueError:
        return DEFAULT_WEB_PUSH_URL
    if delivery.target_entity_type == "deal":
        return f"/deals?deal={entity_id}"
    if delivery.target_entity_type == "task":
        return f"/tasks?task={entity_id}"
    return DEFAULT_WEB_PUSH_URL


def build_web_push_payload(delivery: NotificationDelivery) -> str:
    title = _truncate_utf8(delivery.subject or "Pulse CRM", 240)
    body = _truncate_utf8(delivery.body, 2_600)
    safe_url = _web_push_url(delivery)
    while True:
        payload = json.dumps(
            {
                "title": title,
                "body": body,
                "url": safe_url,
                "tag": f"notification:{delivery.id}",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        overflow = len(payload.encode("utf-8")) - MAX_WEB_PUSH_PAYLOAD_BYTES
        if overflow <= 0:
            return payload
        body = _truncate_utf8(body, max(0, len(body.encode("utf-8")) - overflow - 8))


async def _default_webpush_call(
    *,
    subscription_info: dict[str, Any],
    data: str,
    vapid_private_key: str,
    vapid_claims: dict[str, str],
    ttl: int,
    request_timeout: float,
) -> object:
    pywebpush_module = importlib.import_module("pywebpush")
    aiohttp_module = importlib.import_module("aiohttp")
    call = cast(Callable[..., Awaitable[object]], pywebpush_module.webpush_async)
    async with aiohttp_module.ClientSession() as session:
        return await call(
            subscription_info=subscription_info,
            data=data,
            vapid_private_key=vapid_private_key,
            vapid_claims=vapid_claims,
            ttl=ttl,
            timeout=request_timeout,
            aiohttp_session=_NoRedirectSession(session),
        )


class WebPushSender:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        cipher: SecretCipher,
        vapid_private_key: str,
        vapid_subject: str,
        webpush_call: _WebPushCall = _default_webpush_call,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.cipher = cipher
        self.vapid_private_key = vapid_private_key
        self.vapid_subject = vapid_subject
        self.webpush_call = webpush_call
        self.now = now

    async def __call__(self, delivery: NotificationDelivery) -> SendResult:
        if (
            delivery.channel != "web_push"
            or delivery.audience is not NotificationAudience.employee
            or delivery.recipient_id is None
        ):
            raise WebPushDeliveryError("web push delivery has an invalid recipient")
        subscriptions = await self._active_subscriptions(
            workspace_id=delivery.workspace_id,
            user_id=delivery.recipient_id,
        )
        payload = build_web_push_payload(delivery)

        async def send_one(
            subscription: WebPushSubscription,
        ) -> tuple[WebPushSubscription, bool, bool, str | None]:
            try:
                info = self._decrypt_subscription(subscription)
            except (SecretCipherError, UnicodeDecodeError, ValueError):
                return (
                    subscription,
                    False,
                    False,
                    "encrypted subscription is invalid",
                )
            try:
                # pywebpush mutates claims with endpoint-specific aud/exp, so a
                # fresh mapping is mandatory for every device.
                await self.webpush_call(
                    subscription_info=info,
                    data=payload,
                    vapid_private_key=self.vapid_private_key,
                    vapid_claims={"sub": self.vapid_subject},
                    ttl=60,
                    request_timeout=10.0,
                )
            except Exception as exc:
                response = getattr(exc, "response", None)
                status_code = getattr(exc, "status_code", None) or getattr(
                    response,
                    "status_code",
                    getattr(response, "status", None),
                )
                permanent_provider_error = (
                    isinstance(status_code, int)
                    and 300 <= status_code < 500
                    and status_code not in {408, 425, 429}
                )
                if permanent_provider_error:
                    return (
                        subscription,
                        False,
                        False,
                        "subscription or VAPID credentials rejected by push provider",
                    )
                return (
                    subscription,
                    False,
                    True,
                    "push provider delivery failed",
                )
            return subscription, True, False, None

        outcomes = await asyncio.gather(*(send_one(item) for item in subscriptions))
        await self._record_subscription_results(outcomes)
        succeeded = sum(1 for _item, success, _retryable, _error in outcomes if success)
        retryable_failures = sum(
            1 for _item, _success, retryable, _error in outcomes if retryable
        )

        if retryable_failures and succeeded == 0:
            raise WebPushDeliveryError("web push provider delivery failed")
        return SendResult(
            provider_message_id=f"web-push:{delivery.id}:{succeeded}",
            sent_at=self.now(),
        )

    async def _active_subscriptions(
        self,
        *,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> tuple[WebPushSubscription, ...]:
        current_time = self.now()
        async with self.session_factory() as session:
            subscriptions = tuple(
                (
                    await session.scalars(
                        sa.select(WebPushSubscription)
                        .where(
                            _active_subscription_predicate(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                now=current_time,
                            )
                        )
                        .order_by(WebPushSubscription.created_at, WebPushSubscription.id)
                    )
                ).all()
            )
        return subscriptions

    def _decrypt_subscription(self, subscription: WebPushSubscription) -> dict[str, Any]:
        raw = self.cipher.decrypt(
            subscription.encrypted_subscription,
            associated_data=subscription_aad(
                workspace_id=subscription.workspace_id,
                user_id=subscription.user_id,
                endpoint_digest=subscription.endpoint_hash,
            ),
        )
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("invalid encrypted web push subscription")
        endpoint = parsed.get("endpoint")
        keys = parsed.get("keys")
        if not isinstance(endpoint, str) or not isinstance(keys, Mapping):
            raise ValueError("invalid encrypted web push subscription")
        p256dh = keys.get("p256dh")
        auth = keys.get("auth")
        if not isinstance(p256dh, str) or not isinstance(auth, str):
            raise ValueError("invalid encrypted web push subscription")
        return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}

    async def _record_subscription_results(
        self,
        outcomes: tuple[tuple[WebPushSubscription, bool, bool, str | None], ...]
        | list[tuple[WebPushSubscription, bool, bool, str | None]],
    ) -> None:
        current_time = self.now()
        async with self.session_factory() as session:
            async with session.begin():
                for subscription, succeeded, retryable, error in outcomes:
                    active = succeeded or retryable
                    values: dict[str, Any] = {
                        "is_active": active,
                        "disabled_at": None if active else current_time,
                        "last_error": error,
                        "updated_at": current_time,
                    }
                    if succeeded:
                        values["last_success_at"] = current_time
                    await session.execute(
                        sa.update(WebPushSubscription)
                        .where(
                            WebPushSubscription.id == subscription.id,
                            WebPushSubscription.workspace_id == subscription.workspace_id,
                            WebPushSubscription.user_id == subscription.user_id,
                        )
                        .values(**values)
                    )


type WebPushDeliverySender = Callable[[NotificationDelivery], Awaitable[SendResult]]


__all__ = [
    "MAX_ACTIVE_SUBSCRIPTIONS_PER_USER",
    "MAX_WEB_PUSH_PAYLOAD_BYTES",
    "QueuedWebPushDelivery",
    "DEFAULT_WEB_PUSH_URL",
    "WebPushDeliveryError",
    "WebPushDeliverySender",
    "WebPushSender",
    "build_web_push_payload",
    "delete_subscription",
    "endpoint_hash",
    "enqueue_web_push_delivery",
    "has_active_subscription",
    "mirror_delivered_in_app_notification",
    "register_subscription",
    "subscription_aad",
]

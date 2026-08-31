"""Authenticated PWA Web Push subscription endpoints."""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import get_session
from app.integrations.secrets import SecretCipher
from app.integrations.web_push import (
    TEST_PUSH_BODY,
    TEST_PUSH_SUBJECT,
    delete_subscription,
    enqueue_web_push_delivery,
    has_active_subscription,
    register_subscription,
)
from app.security import CurrentMutationUser, CurrentUser, SettingsDependency

router = APIRouter(prefix="/push", tags=["push"])

PUSH_PROVIDER_HOSTS = frozenset(
    {
        "fcm.googleapis.com",
        "push.services.mozilla.com",
        "updates.push.services.mozilla.com",
        "web.push.apple.com",
    }
)
PUSH_PROVIDER_HOST_SUFFIXES = (".notify.windows.com",)


class PushConfigRead(BaseModel):
    enabled: bool
    public_key: str | None


class PushSubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=512)
    auth: str = Field(min_length=1, max_length=256)

    @field_validator("p256dh")
    @classmethod
    def validate_p256dh(cls, value: str) -> str:
        decoded = _decode_base64url(value, field="p256dh")
        if len(decoded) != 65 or decoded[0] != 0x04:
            raise ValueError("p256dh must encode a 65-byte P-256 public key")
        try:
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decoded)
        except ValueError as exc:
            raise ValueError("p256dh must contain a valid P-256 public key") from exc
        return value

    @field_validator("auth")
    @classmethod
    def validate_auth(cls, value: str) -> str:
        if len(_decode_base64url(value, field="auth")) != 16:
            raise ValueError("auth must encode 16 bytes")
        return value


class PushSubscriptionWrite(BaseModel):
    endpoint: str = Field(min_length=1, max_length=4_096)
    expiration_time: datetime | None = None
    keys: PushSubscriptionKeys

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        endpoint = value.strip()
        parsed = urlsplit(endpoint)
        try:
            port = parsed.port
            host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        except (UnicodeError, ValueError) as exc:
            raise ValueError("push endpoint is invalid") from exc
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or parsed.fragment
        ):
            raise ValueError("push endpoint must be an HTTPS URL without credentials")
        if host not in PUSH_PROVIDER_HOSTS and not any(
            host.endswith(suffix) and host != suffix[1:]
            for suffix in PUSH_PROVIDER_HOST_SUFFIXES
        ):
            raise ValueError("push endpoint host is not an approved Web Push provider")
        return endpoint

    @field_validator("expiration_time")
    @classmethod
    def validate_expiration_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        if normalized <= datetime.now(UTC) + timedelta(seconds=5):
            raise ValueError("push subscription expiration_time must be in the future")
        return normalized


class PushSubscriptionDelete(BaseModel):
    endpoint: str = Field(min_length=1, max_length=4_096)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        return PushSubscriptionWrite.validate_endpoint(value)


class PushTestQueued(BaseModel):
    delivery_id: uuid.UUID


def _decode_base64url(value: str, *, field: str) -> bytes:
    raw = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", raw) is None:
        raise ValueError(f"{field} must be valid base64url")
    try:
        return base64.b64decode(
            raw + "=" * (-len(raw) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{field} must be valid base64url") from exc


def _is_enabled(settings: Settings, request: Request) -> bool:
    runtime = getattr(request.app.state, "integration_runtime", None)
    handlers = getattr(runtime, "handlers", None)
    supervisor = getattr(runtime, "supervisor", None)
    scheduler = getattr(runtime, "scheduler", None)
    return bool(
        settings.web_push_enabled
        and settings.job_runner_enabled
        and isinstance(
            getattr(request.app.state, "integration_secret_cipher", None),
            SecretCipher,
        )
        and getattr(handlers, "web_push_sender", None) is not None
        and getattr(supervisor, "running", False)
        and getattr(scheduler, "running", False)
    )


def _require_enabled(settings: Settings, request: Request) -> None:
    if not _is_enabled(settings, request):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "web_push_disabled", "message": "Web Push is not configured"},
        )


def _cipher(request: Request) -> SecretCipher:
    cipher = getattr(request.app.state, "integration_secret_cipher", None)
    if not isinstance(cipher, SecretCipher):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "web_push_disabled", "message": "Web Push is not configured"},
        )
    return cipher


@router.get("/config", response_model=PushConfigRead)
async def push_config(
    request: Request,
    _context: CurrentUser,
    settings: SettingsDependency,
) -> PushConfigRead:
    enabled = _is_enabled(settings, request)
    return PushConfigRead(
        enabled=enabled,
        public_key=settings.web_push_vapid_public_key if enabled else None,
    )


@router.post("/subscriptions", status_code=status.HTTP_204_NO_CONTENT)
async def upsert_push_subscription(
    payload: PushSubscriptionWrite,
    request: Request,
    context: CurrentMutationUser,
    settings: SettingsDependency,
    db: AsyncSession = Depends(get_session),
) -> Response:
    _require_enabled(settings, request)
    try:
        await register_subscription(
            db,
            cipher=_cipher(request),
            workspace_id=context.workspace_id,
            user_id=context.user_id,
            endpoint=payload.endpoint,
            expiration_time=payload.expiration_time,
            p256dh=payload.keys.p256dh,
            auth=payload.keys.auth,
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "invalid_web_push_subscription",
                "message": "Web Push subscription is expired or invalid",
            },
        ) from exc
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/subscriptions", status_code=status.HTTP_204_NO_CONTENT)
async def remove_push_subscription(
    payload: PushSubscriptionDelete,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> Response:
    await delete_subscription(
        db,
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        endpoint=payload.endpoint,
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/test",
    response_model=PushTestQueued,
    status_code=status.HTTP_202_ACCEPTED,
)
async def queue_test_push(
    request: Request,
    context: CurrentMutationUser,
    settings: SettingsDependency,
    db: AsyncSession = Depends(get_session),
) -> PushTestQueued:
    _require_enabled(settings, request)
    if not await has_active_subscription(
        db,
        workspace_id=context.workspace_id,
        user_id=context.user_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "web_push_subscription_missing",
                "message": "No active Web Push subscription",
            },
        )
    now = datetime.now(UTC)
    cooldown_seconds = 60
    bucket = int(now.timestamp()) // cooldown_seconds
    queued = await enqueue_web_push_delivery(
        db,
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        subject=TEST_PUSH_SUBJECT,
        body=TEST_PUSH_BODY,
        dedupe_key=(
            f"web-push:test:{context.workspace_id}:{context.user_id}:"
            f"{bucket}"
        ),
        scheduled_at=now,
    )
    if not queued.created:
        await db.rollback()
        retry_after = max(
            1,
            int((bucket + 1) * cooldown_seconds - now.timestamp()),
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "web_push_test_cooldown",
                "message": "Test push can be sent once per minute",
                "retry_after_seconds": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )
    await db.commit()
    return PushTestQueued(delivery_id=queued.delivery.id)


__all__ = ["router"]

"""Public webhook, bot-hook and HTML-form endpoints for later router mounting."""

from __future__ import annotations

import hashlib
import html
import json
import uuid
from typing import Any, cast

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.integrations.channels.base import (
    ChannelAdapter,
    ChannelPayloadError,
    ChannelVerificationError,
)
from app.integrations.forms import (
    FormIdempotencyConflict,
    FormOriginError,
    FormRateLimitError,
    FormSubmissionError,
    accept_form_submission,
)
from app.integrations.models import (
    ChannelConnection,
    ChannelKind,
    ConnectionStatus,
    Form,
    InboundEvent,
    WebhookEndpoint,
)
from app.integrations.secrets import SecretCipher, SecretCipherError
from app.integrations.webhooks import (
    IdempotencyConflictError,
    VerifiedWebhook,
    WebhookVerificationError,
    accept_inbound_event,
    decode_json_object,
    verify_generic_webhook,
)
from app.models import OutboxEvent

router = APIRouter(tags=["public-integrations"])
MAX_WEBHOOK_BODY_BYTES = 2 * 1024 * 1024


class GenericWebhookPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact: dict[str, Any] | None = None
    deal: dict[str, Any] | None = None
    message: dict[str, Any] | None = None
    custom_fields: dict[str, Any] = Field(default_factory=dict)


class AcceptanceRead(BaseModel):
    accepted: bool = True
    duplicate: bool = False
    event_id: uuid.UUID | None = None
    submission_id: uuid.UUID | None = None


def _secret_cipher(request: Request) -> SecretCipher:
    cipher = getattr(request.app.state, "integration_secret_cipher", None)
    if not isinstance(cipher, SecretCipher):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="integration secret cipher is not configured",
        )
    return cipher


def _channel_adapter(request: Request, connection: ChannelConnection) -> ChannelAdapter:
    factory = getattr(request.app.state, "channel_adapter_factory", None)
    if factory is not None and callable(getattr(factory, "build", None)):
        try:
            return cast(ChannelAdapter, factory.build(connection))
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="channel adapter is not ready",
            ) from exc
    adapters = getattr(request.app.state, "channel_adapters", {})
    adapter = adapters.get(connection.id) if isinstance(adapters, dict) else None
    if adapter is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="channel adapter is not ready",
        )
    return cast(ChannelAdapter, adapter)


async def _bounded_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_WEBHOOK_BODY_BYTES:
                raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from exc
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BODY_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    return body


def _add_inbound_outbox(session: AsyncSession, event: InboundEvent) -> None:
    session.add(
        OutboxEvent(
            workspace_id=event.workspace_id,
            event_type="inbound.event.accepted",
            aggregate_type="inbound_event",
            aggregate_id=event.id,
            payload={"inbound_event_id": str(event.id), "source_key": event.source_key},
        )
    )


@router.post(
    "/hooks/v1/generic/{slug}",
    response_model=AcceptanceRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generic_webhook(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> AcceptanceRead:
    endpoint = await db.scalar(
        sa.select(WebhookEndpoint).where(
            WebhookEndpoint.slug == slug,
            WebhookEndpoint.is_active.is_(True),
        )
    )
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="webhook not found")
    body = await _bounded_body(request)
    try:
        secret = _secret_cipher(request).decrypt(
            endpoint.encrypted_secret,
            associated_data=f"webhook:{endpoint.id}".encode(),
        )
        verified = verify_generic_webhook(
            secret=secret,
            body=body,
            signature=request.headers.get("x-pulse-signature", ""),
            timestamp=request.headers.get("x-pulse-timestamp", ""),
            idempotency_key=request.headers.get("idempotency-key", ""),
        )
        raw_payload = decode_json_object(body)
        payload = GenericWebhookPayload.model_validate(raw_payload)
        if not any((payload.contact, payload.deal, payload.message)):
            raise WebhookVerificationError("payload must contain contact, deal or message")
        acceptance = await accept_inbound_event(
            db,
            workspace_id=endpoint.workspace_id,
            source_key=f"generic:{endpoint.id}",
            verified=verified,
            payload=payload.model_dump(mode="json"),
        )
    except (SecretCipherError, WebhookVerificationError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    if acceptance.created:
        _add_inbound_outbox(db, acceptance.event)
    await db.commit()
    return AcceptanceRead(
        duplicate=not acceptance.created,
        event_id=acceptance.event.id,
    )


@router.post(
    "/hooks/v1/{provider}/{connection_id}",
    response_model=AcceptanceRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def provider_webhook(
    provider: ChannelKind,
    connection_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> AcceptanceRead:
    if provider is ChannelKind.email:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="webhook not found")
    connection = await db.scalar(
        sa.select(ChannelConnection).where(
            ChannelConnection.id == connection_id,
            ChannelConnection.kind == provider,
            ChannelConnection.status == ConnectionStatus.active,
        )
    )
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="connection not found")
    body = await _bounded_body(request)
    adapter = _channel_adapter(request, connection)
    try:
        await adapter.verify(dict(request.headers), body)
        payload = decode_json_object(body)
        normalized = adapter.normalize_inbound(payload)
    except (ChannelVerificationError, WebhookVerificationError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except ChannelPayloadError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    digest = hashlib.sha256(body).hexdigest()
    provider_dedupe_key = hashlib.sha256(normalized.event_id.encode("utf-8")).hexdigest()
    try:
        acceptance = await accept_inbound_event(
            db,
            workspace_id=connection.workspace_id,
            channel_connection_id=connection.id,
            source_key=f"{provider.value}:{connection.id}",
            external_event_id=normalized.event_id,
            verified=VerifiedWebhook(
                timestamp=normalized.occurred_at,
                idempotency_key=provider_dedupe_key,
                request_digest=digest,
            ),
            payload=payload,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if acceptance.created:
        _add_inbound_outbox(db, acceptance.event)
    await db.commit()
    return AcceptanceRead(duplicate=not acceptance.created, event_id=acceptance.event.id)


@router.get("/forms/{slug}", response_class=Response)
async def hosted_form(
    slug: str,
    db: AsyncSession = Depends(get_session),
) -> Response:
    form = await _load_form(db, slug)
    body = _render_form(form, action=f"/forms/{html.escape(slug)}/submit", document=True)
    return Response(content=body, media_type="text/html; charset=utf-8")


@router.get("/forms/{slug}/embed.js", response_class=Response)
async def form_embed_script(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> Response:
    form = await _load_form(db, slug)
    action = str(request.base_url).rstrip("/") + f"/forms/{slug}/submit"
    markup = _render_form(form, action=action, document=False)
    script = (
        "(()=>{const s=document.currentScript;const w=document.createElement('div');"
        f"w.innerHTML={json.dumps(markup, ensure_ascii=False)};"
        "s.parentNode.insertBefore(w,s.nextSibling);})();"
    )
    return Response(
        content=script,
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.post(
    "/forms/{slug}/submit",
    response_model=AcceptanceRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_form(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> AcceptanceRead:
    form = await _load_form(db, slug)
    if (request.headers.get("content-type") or "").startswith("application/json"):
        raw = await request.json()
        if not isinstance(raw, dict):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)
        payload: dict[str, Any] = raw
    else:
        form_data = await request.form()
        payload = {key: value for key, value in form_data.items() if isinstance(value, str)}
    idempotency_key = request.headers.get("idempotency-key") or str(
        payload.pop("_idempotency_key", "")
    )
    client_host = request.client.host if request.client else "unknown"
    fingerprint = f"{client_host}|{request.headers.get('user-agent', '')[:200]}"
    try:
        outcome = await accept_form_submission(
            db,
            form=form,
            payload=payload,
            origin=request.headers.get("origin"),
            hosted_origin=str(request.base_url).rstrip("/"),
            client_fingerprint=fingerprint,
            idempotency_key=idempotency_key,
        )
    except FormOriginError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except FormRateLimitError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
    except FormIdempotencyConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except FormSubmissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    if outcome.submission is not None and outcome.created:
        db.add(
            OutboxEvent(
                workspace_id=form.workspace_id,
                event_type="form.submission.accepted",
                aggregate_type="form_submission",
                aggregate_id=outcome.submission.id,
                payload={"submission_id": str(outcome.submission.id), "form_id": str(form.id)},
            )
        )
    await db.commit()
    return AcceptanceRead(
        duplicate=outcome.submission is not None and not outcome.created,
        submission_id=outcome.submission.id if outcome.submission else None,
    )


async def _load_form(db: AsyncSession, slug: str) -> Form:
    form = await db.scalar(sa.select(Form).where(Form.slug == slug, Form.is_active.is_(True)))
    if form is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="form not found")
    return form


def _render_form(form: Form, *, action: str, document: bool) -> str:
    controls: list[str] = []
    for definition in form.fields_schema:
        key = definition.get("key")
        if not isinstance(key, str):
            continue
        label = html.escape(str(definition.get("label") or key))
        escaped_key = html.escape(key, quote=True)
        required = " required" if definition.get("required") else ""
        field_type = str(definition.get("type") or "text")
        if field_type == "textarea":
            control = f'<textarea name="{escaped_key}"{required}></textarea>'
        elif field_type == "select" and isinstance(definition.get("options"), list):
            options = "".join(
                f'<option value="{html.escape(str(option), quote=True)}">'
                f"{html.escape(str(option))}</option>"
                for option in definition["options"]
            )
            control = f'<select name="{escaped_key}"{required}>{options}</select>'
        else:
            input_type = "email" if field_type == "email" else "text"
            control = f'<input type="{input_type}" name="{escaped_key}"{required}>'
        controls.append(f"<label><span>{label}</span>{control}</label>")
    nonce = str(uuid.uuid4())
    form_markup = (
        f'<form class="pulse-form" method="post" action="{html.escape(action, quote=True)}">'
        f"<h2>{html.escape(form.title)}</h2>"
        + "".join(controls)
        + (
            f'<label class="pulse-hp">Не заполняйте<input name="'
            f'{html.escape(form.honeypot_field, quote=True)}"></label>'
        )
        + f'<input type="hidden" name="_idempotency_key" value="{nonce}">'
        + '<button type="submit">Отправить</button></form>'
    )
    style = (
        "<style>.pulse-form{box-sizing:border-box;display:grid;gap:14px;max-width:560px;"
        "margin:24px auto;padding:24px;border:1px solid #e2e8f0;border-radius:16px;"
        "font:15px/1.45 system-ui;color:#172033;background:#fff}.pulse-form h2{margin:0 0 4px}"
        ".pulse-form label{display:grid;gap:6px}.pulse-form input,.pulse-form textarea,"
        ".pulse-form select{box-sizing:border-box;width:100%;padding:11px;border:1px solid #cbd5e1;"
        "border-radius:9px;font:inherit}.pulse-form button{padding:12px 16px;border:0;"
        "border-radius:9px;color:#fff;background:#5265e8;font:600 15px system-ui}"
        ".pulse-hp{position:absolute!important;"
        "left:-10000px!important}</style>"
    )
    content = style + form_markup
    if not document:
        return content
    return (
        '<!doctype html><html lang="ru"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(form.title)}</title><body>{content}</body></html>"
    )

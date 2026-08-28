"""Public HTML-form validation, idempotency and PostgreSQL rate limiting."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.models import (
    Form,
    FormRateLimitBucket,
    FormSubmission,
    FormSubmissionStatus,
)


class FormSubmissionError(ValueError):
    pass


class FormOriginError(FormSubmissionError):
    pass


class FormRateLimitError(FormSubmissionError):
    pass


class FormIdempotencyConflict(FormSubmissionError):
    pass


@dataclass(frozen=True, slots=True)
class FormSubmissionOutcome:
    submission: FormSubmission | None
    created: bool
    discarded_as_spam: bool = False


def normalize_origin(origin: str) -> str:
    parsed = urlsplit(origin.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise FormOriginError("invalid form origin")
    if parsed.username or parsed.password or parsed.path not in {"", "/"}:
        raise FormOriginError("invalid form origin")
    if parsed.query or parsed.fragment:
        raise FormOriginError("invalid form origin")
    hostname = parsed.hostname.casefold()
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port
    authority = hostname if port in {None, default_port} else f"{hostname}:{port}"
    return f"{parsed.scheme}://{authority}"


def ensure_allowed_origin(
    origin: str | None,
    allowed_origins: list[str],
    *,
    hosted_origin: str,
) -> None:
    if origin is None:
        raise FormOriginError("Origin header is required")
    normalized = normalize_origin(origin)
    allowed = {normalize_origin(value) for value in allowed_origins}
    allowed.add(normalize_origin(hosted_origin))
    if normalized not in allowed:
        raise FormOriginError("form origin is not allowed")


def validate_form_payload(form: Form, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate only the administrator-declared public fields."""

    normalized: dict[str, Any] = {}
    errors: dict[str, str] = {}
    definitions: dict[str, dict[str, Any]] = {}
    for definition in form.fields_schema:
        if not isinstance(definition, dict):
            continue
        key = definition.get("key")
        if isinstance(key, str):
            definitions[key] = definition
    for key, definition in definitions.items():
        raw = payload.get(key)
        required = bool(definition.get("required"))
        if raw is None or raw == "":
            if required:
                errors[key] = "required"
            continue
        field_type = str(definition.get("type") or "text")
        try:
            normalized[key] = _normalize_field(raw, field_type, definition)
        except (TypeError, ValueError, InvalidOperation):
            errors[key] = "invalid"
    if errors:
        raise FormSubmissionError(json.dumps({"invalid_fields": errors}, sort_keys=True))
    return normalized


def _normalize_field(raw: Any, field_type: str, definition: dict[str, Any]) -> Any:
    if field_type in {"text", "email", "phone", "textarea"}:
        value = str(raw).strip()
        max_length = min(int(definition.get("max_length") or 2_000), 10_000)
        if not value or len(value) > max_length:
            raise ValueError
        if field_type == "email" and ("@" not in value or value.startswith("@")):
            raise ValueError
        return value
    if field_type == "number":
        return str(Decimal(str(raw)))
    if field_type == "boolean":
        if raw in {True, "true", "1", "on"}:
            return True
        if raw in {False, "false", "0", "off"}:
            return False
        raise ValueError
    if field_type == "select":
        value = str(raw)
        options = definition.get("options")
        if not isinstance(options, list) or value not in options:
            raise ValueError
        return value
    raise ValueError


def _insert_for(session: AsyncSession, model: type[Any]) -> Any:
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        return pg_insert(model)
    if dialect_name == "sqlite":
        return sqlite_insert(model)
    raise RuntimeError(f"unsupported database dialect for atomic upsert: {dialect_name}")


async def consume_form_rate_limit(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    form_id: uuid.UUID,
    subject: str,
    now: datetime | None = None,
    limit: int = 10,
    window_seconds: int = 60,
) -> int:
    """Atomically increment one fixed-window rate-limit bucket."""

    if limit < 1 or not 1 <= window_seconds <= 86_400:
        raise ValueError("invalid rate-limit configuration")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    epoch = int(current.timestamp())
    window_epoch = epoch - (epoch % window_seconds)
    window_started_at = datetime.fromtimestamp(window_epoch, UTC)
    subject_hash = hashlib.sha256(subject.encode("utf-8")).hexdigest()
    statement = (
        _insert_for(session, FormRateLimitBucket)
        .values(
            workspace_id=workspace_id,
            form_id=form_id,
            subject_hash=subject_hash,
            window_started_at=window_started_at,
            expires_at=window_started_at + timedelta(seconds=window_seconds * 2),
            request_count=1,
        )
        .on_conflict_do_update(
            index_elements=[
                FormRateLimitBucket.form_id,
                FormRateLimitBucket.subject_hash,
                FormRateLimitBucket.window_started_at,
            ],
            set_={"request_count": FormRateLimitBucket.request_count + 1},
        )
        .returning(FormRateLimitBucket.request_count)
    )
    count = int((await session.execute(statement)).scalar_one())
    if count > limit:
        raise FormRateLimitError("form rate limit exceeded")
    return count


async def accept_form_submission(
    session: AsyncSession,
    *,
    form: Form,
    payload: dict[str, Any],
    origin: str | None,
    hosted_origin: str,
    client_fingerprint: str,
    idempotency_key: str,
    now: datetime | None = None,
    rate_limit: int = 10,
) -> FormSubmissionOutcome:
    """Validate and durably accept a form submission without committing."""

    if not form.is_active:
        raise FormSubmissionError("form is disabled")
    if not idempotency_key or len(idempotency_key) > 255:
        raise FormSubmissionError("invalid Idempotency-Key")
    ensure_allowed_origin(origin, form.allowed_origins, hosted_origin=hosted_origin)

    honeypot = payload.get(form.honeypot_field)
    if honeypot is not None and honeypot != "":
        # Return the same public success response without creating CRM data.
        return FormSubmissionOutcome(submission=None, created=False, discarded_as_spam=True)

    await consume_form_rate_limit(
        session,
        workspace_id=form.workspace_id,
        form_id=form.id,
        subject=client_fingerprint,
        now=now,
        limit=rate_limit,
    )
    clean_payload = validate_form_payload(form, payload)
    canonical = json.dumps(
        clean_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    submission_id = uuid.uuid4()
    statement = (
        _insert_for(session, FormSubmission)
        .values(
            id=submission_id,
            workspace_id=form.workspace_id,
            form_id=form.id,
            idempotency_key=idempotency_key,
            request_digest=digest,
            payload=clean_payload,
            status=FormSubmissionStatus.accepted,
        )
        .on_conflict_do_nothing(
            index_elements=[FormSubmission.form_id, FormSubmission.idempotency_key]
        )
        .returning(FormSubmission.id)
    )
    inserted_id = (await session.execute(statement)).scalar_one_or_none()
    if inserted_id is not None:
        submission = await session.get(FormSubmission, inserted_id)
        if submission is None:  # pragma: no cover - database contract guard
            raise RuntimeError("inserted form submission could not be loaded")
        return FormSubmissionOutcome(submission=submission, created=True)

    submission = await session.scalar(
        sa.select(FormSubmission).where(
            FormSubmission.form_id == form.id,
            FormSubmission.idempotency_key == idempotency_key,
        )
    )
    if submission is None:  # pragma: no cover
        raise RuntimeError("deduplicated form submission could not be loaded")
    if submission.request_digest != digest:
        raise FormIdempotencyConflict("Idempotency-Key was reused for another submission")
    return FormSubmissionOutcome(submission=submission, created=False)

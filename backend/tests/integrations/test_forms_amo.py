from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.amo import AmoEntity, AmoPage, apply_import_page
from app.integrations.forms import FormIdempotencyConflict, accept_form_submission
from app.integrations.models import Form, ImportJob, ImportStatus


class FakeWriter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def upsert(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        entity: AmoEntity,
        existing_internal_id: uuid.UUID | None,
        user_mapping: Mapping[str, str],
    ) -> uuid.UUID:
        del session, workspace_id, user_mapping
        self.calls.append(entity.external_id)
        return existing_internal_id or uuid.uuid4()


@pytest.mark.asyncio
async def test_form_submission_rate_limit_and_idempotency(
    db: AsyncSession, integration_domain: dict[str, object]
) -> None:
    workspace = integration_domain["workspace"]
    pipeline = integration_domain["pipeline"]
    stage = integration_domain["stage"]
    form = Form(
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        slug="public-contact-form",
        title="Оставить заявку",
        pipeline_id=pipeline.id,  # type: ignore[attr-defined]
        stage_id=stage.id,  # type: ignore[attr-defined]
        fields_schema=[
            {"key": "name", "label": "Имя", "type": "text", "required": True},
            {"key": "email", "label": "Email", "type": "email", "required": True},
        ],
        allowed_origins=["https://example.com"],
    )
    db.add(form)
    await db.flush()

    first = await accept_form_submission(
        db,
        form=form,
        payload={"name": "Анна", "email": "anna@example.com"},
        origin="https://example.com",
        hosted_origin="https://crm.example.com",
        client_fingerprint="127.0.0.1|browser",
        idempotency_key="form-event-1",
    )
    second = await accept_form_submission(
        db,
        form=form,
        payload={"name": "Анна", "email": "anna@example.com"},
        origin="https://example.com",
        hosted_origin="https://crm.example.com",
        client_fingerprint="127.0.0.1|browser",
        idempotency_key="form-event-1",
    )
    assert first.created is True
    assert second.created is False
    assert first.submission is not None and second.submission is not None
    assert first.submission.id == second.submission.id

    with pytest.raises(FormIdempotencyConflict):
        await accept_form_submission(
            db,
            form=form,
            payload={"name": "Иван", "email": "ivan@example.com"},
            origin="https://example.com",
            hosted_origin="https://crm.example.com",
            client_fingerprint="127.0.0.1|browser",
            idempotency_key="form-event-1",
        )


@pytest.mark.asyncio
async def test_repeated_amo_import_uses_external_map_without_duplicate_writer_call(
    db: AsyncSession, integration_domain: dict[str, object]
) -> None:
    workspace = integration_domain["workspace"]
    first_job = ImportJob(
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        provider="amocrm",
        status=ImportStatus.running,
        dry_run=False,
        entity_type="contacts",
    )
    db.add(first_job)
    await db.flush()
    entity = AmoEntity(
        entity_type="contacts",
        external_id="amo-contact-42",
        data={"name": "Анна", "email": "anna@example.com"},
        source_updated_at=datetime(2026, 8, 27, tzinfo=UTC),
    )
    page = AmoPage(entity_type="contacts", entities=[entity], next_cursor=None)
    first_writer = FakeWriter()
    first = await apply_import_page(db, job=first_job, page=page, writer=first_writer)
    assert first.created == 1
    assert first_writer.calls == ["amo-contact-42"]

    second_job = ImportJob(
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        provider="amocrm",
        status=ImportStatus.running,
        dry_run=False,
        entity_type="contacts",
    )
    db.add(second_job)
    await db.flush()
    second_writer = FakeWriter()
    second = await apply_import_page(db, job=second_job, page=page, writer=second_writer)
    assert second.unchanged == 1
    assert second_writer.calls == []

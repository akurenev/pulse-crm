from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI

from app.db import SessionLocal
from app.integrations.admin_api import router
from app.integrations.models import (
    AmoConnectionStatus,
    AmoCRMConnection,
    ChannelConnection,
    ImportJob,
    ImportStatus,
    WebhookEndpoint,
)
from app.integrations.s3 import AttachmentStorage
from app.integrations.secrets import SecretCipher
from app.models import (
    ActivityEvent,
    BackgroundJob,
    Membership,
    Pipeline,
    Role,
    Session,
    Source,
    Stage,
    User,
    Workspace,
)
from app.security import digest_token

API_ROOT = "/api/v1/admin/integrations"


class FakeAdminS3:
    def put_object(self, **kwargs: Any) -> None:
        del kwargs

    def delete_object(self, **kwargs: Any) -> None:
        del kwargs

    def generate_presigned_url(
        self, client_method: str, *, Params: dict[str, Any], ExpiresIn: int
    ) -> str:
        return f"https://s3.test/{client_method}/{Params['Key']}?expires={ExpiresIn}"


@dataclass(frozen=True, slots=True)
class AdminSeed:
    workspace_id: str
    pipeline_id: str
    stage_id: str
    source_id: str
    owner_id: str
    admin_id: str
    other_pipeline_id: str
    other_stage_id: str
    owner_token: str
    admin_token: str
    manager_token: str


@pytest_asyncio.fixture
async def admin_seed() -> AdminSeed:
    async with SessionLocal() as db:
        workspace = Workspace(name="Admin API", slug="admin-api")
        other_workspace = Workspace(name="Other", slug="other-admin-api")
        owner = User(email="owner-admin@example.com", full_name="Owner", password_hash="unused")
        admin = User(email="admin-api@example.com", full_name="Admin", password_hash="unused")
        manager = User(
            email="manager-admin@example.com", full_name="Manager", password_hash="unused"
        )
        db.add_all([workspace, other_workspace, owner, admin, manager])
        await db.flush()
        pipeline = Pipeline(workspace_id=workspace.id, name="Продажи")
        other_pipeline = Pipeline(workspace_id=other_workspace.id, name="Other")
        source = Source(workspace_id=workspace.id, key="webhook", name="Webhook")
        db.add_all([pipeline, other_pipeline, source])
        db.add_all(
            [
                Membership(workspace_id=workspace.id, user_id=owner.id, role=Role.owner),
                Membership(workspace_id=workspace.id, user_id=admin.id, role=Role.admin),
                Membership(workspace_id=workspace.id, user_id=manager.id, role=Role.manager),
            ]
        )
        await db.flush()
        stage = Stage(
            workspace_id=workspace.id,
            pipeline_id=pipeline.id,
            name="Новый лид",
            position=0,
        )
        other_stage = Stage(
            workspace_id=other_workspace.id,
            pipeline_id=other_pipeline.id,
            name="Other stage",
            position=0,
        )
        db.add_all([stage, other_stage])
        await db.flush()
        tokens = {
            "owner": "owner-integration-token",
            "admin": "admin-integration-token",
            "manager": "manager-integration-token",
        }
        expires_at = datetime.now(UTC) + timedelta(hours=1)
        for user, token in (
            (owner, tokens["owner"]),
            (admin, tokens["admin"]),
            (manager, tokens["manager"]),
        ):
            db.add(
                Session(
                    user_id=user.id,
                    workspace_id=workspace.id,
                    token_hash=digest_token(token),
                    csrf_token_hash=digest_token("unused-csrf"),
                    expires_at=expires_at,
                )
            )
        await db.commit()
        return AdminSeed(
            workspace_id=str(workspace.id),
            pipeline_id=str(pipeline.id),
            stage_id=str(stage.id),
            source_id=str(source.id),
            owner_id=str(owner.id),
            admin_id=str(admin.id),
            other_pipeline_id=str(other_pipeline.id),
            other_stage_id=str(other_stage.id),
            owner_token=tokens["owner"],
            admin_token=tokens["admin"],
            manager_token=tokens["manager"],
        )


@pytest_asyncio.fixture
async def admin_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    def fake_encrypt(
        self: SecretCipher, plaintext: bytes | str, *, associated_data: bytes
    ) -> bytes:
        del self
        raw = plaintext.encode() if isinstance(plaintext, str) else plaintext
        return b"sealed|" + associated_data + b"|" + raw

    monkeypatch.setattr(SecretCipher, "encrypt", fake_encrypt)
    app = FastAPI()
    app.state.integration_secret_cipher = SecretCipher(key=b"x" * 32, key_id="test-key")
    app.state.attachment_storage = AttachmentStorage(FakeAdminS3(), bucket="pulse-private")
    app.include_router(router, prefix="/api/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_channel_crud_is_admin_only_workspace_scoped_and_secret_safe(
    admin_client: httpx.AsyncClient, admin_seed: AdminSeed
) -> None:
    payload = {
        "kind": "telegram",
        "name": "Sales bot",
        "status": "active",
        "credentials": {"bot_token": "top-secret-token"},
        "settings": {"language": "ru"},
        "default_pipeline_id": admin_seed.pipeline_id,
        "default_stage_id": admin_seed.stage_id,
        "default_assignee_id": admin_seed.admin_id,
    }
    forbidden = await admin_client.post(
        f"{API_ROOT}/channels", json=payload, headers=auth(admin_seed.manager_token)
    )
    assert forbidden.status_code == 403

    wrong_workspace = await admin_client.post(
        f"{API_ROOT}/channels",
        json={
            **payload,
            "default_pipeline_id": admin_seed.other_pipeline_id,
            "default_stage_id": admin_seed.other_stage_id,
        },
        headers=auth(admin_seed.owner_token),
    )
    assert wrong_workspace.status_code == 404

    created = await admin_client.post(
        f"{API_ROOT}/channels", json=payload, headers=auth(admin_seed.owner_token)
    )
    assert created.status_code == 201, created.text
    channel = created.json()
    assert channel["has_credentials"] is True
    assert "credentials" not in channel
    assert "top-secret-token" not in created.text

    async with SessionLocal() as db:
        stored = await db.get(ChannelConnection, uuid.UUID(channel["id"]))
        assert stored is not None
        assert stored.encrypted_credentials is not None
        assert f"channel:{channel['id']}".encode() in stored.encrypted_credentials
        assert stored.credentials_key_id == "test-key"

    listed = await admin_client.get(f"{API_ROOT}/channels", headers=auth(admin_seed.admin_token))
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [channel["id"]]
    fetched = await admin_client.get(
        f"{API_ROOT}/channels/{channel['id']}", headers=auth(admin_seed.admin_token)
    )
    assert fetched.status_code == 200

    updated = await admin_client.patch(
        f"{API_ROOT}/channels/{channel['id']}",
        json={"expected_version": 1, "name": "Primary sales bot"},
        headers=auth(admin_seed.admin_token),
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    stale = await admin_client.patch(
        f"{API_ROOT}/channels/{channel['id']}",
        json={"expected_version": 1, "name": "Stale"},
        headers=auth(admin_seed.admin_token),
    )
    assert stale.status_code == 409
    deleted = await admin_client.delete(
        f"{API_ROOT}/channels/{channel['id']}?expected_version=2",
        headers=auth(admin_seed.owner_token),
    )
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_form_and_webhook_crud_returns_webhook_secret_only_once(
    admin_client: httpx.AsyncClient, admin_seed: AdminSeed
) -> None:
    form_created = await admin_client.post(
        f"{API_ROOT}/forms",
        headers=auth(admin_seed.owner_token),
        json={
            "slug": "public-lead-form",
            "title": "Оставить заявку",
            "pipeline_id": admin_seed.pipeline_id,
            "stage_id": admin_seed.stage_id,
            "assignee_id": admin_seed.admin_id,
            "source_id": admin_seed.source_id,
            "fields_schema": [{"key": "name", "type": "text", "required": True}],
            "allowed_origins": ["https://Example.com/"],
        },
    )
    assert form_created.status_code == 201, form_created.text
    form = form_created.json()
    assert form["allowed_origins"] == ["https://example.com"]
    form_updated = await admin_client.patch(
        f"{API_ROOT}/forms/{form['id']}",
        headers=auth(admin_seed.admin_token),
        json={"expected_version": 1, "is_active": False},
    )
    assert form_updated.status_code == 200
    assert form_updated.json()["version"] == 2
    form_list = await admin_client.get(f"{API_ROOT}/forms", headers=auth(admin_seed.admin_token))
    assert [item["id"] for item in form_list.json()] == [form["id"]]
    form_get = await admin_client.get(
        f"{API_ROOT}/forms/{form['id']}", headers=auth(admin_seed.admin_token)
    )
    assert form_get.status_code == 200

    webhook_created = await admin_client.post(
        f"{API_ROOT}/webhooks",
        headers=auth(admin_seed.owner_token),
        json={
            "slug": "orders-from-site",
            "name": "Orders from site",
            "pipeline_id": admin_seed.pipeline_id,
            "stage_id": admin_seed.stage_id,
            "assignee_id": admin_seed.admin_id,
            "source_id": admin_seed.source_id,
        },
    )
    assert webhook_created.status_code == 201, webhook_created.text
    webhook = webhook_created.json()
    assert len(webhook["secret"]) >= 32
    first_secret = webhook["secret"]
    webhook_get = await admin_client.get(
        f"{API_ROOT}/webhooks/{webhook['id']}", headers=auth(admin_seed.admin_token)
    )
    assert webhook_get.status_code == 200
    assert "secret" not in webhook_get.json()
    webhook_list = await admin_client.get(
        f"{API_ROOT}/webhooks", headers=auth(admin_seed.admin_token)
    )
    assert "secret" not in webhook_list.text

    async with SessionLocal() as db:
        stored = await db.get(WebhookEndpoint, uuid.UUID(webhook["id"]))
        assert stored is not None
        assert f"webhook:{webhook['id']}".encode() in stored.encrypted_secret
        assert first_secret.encode() in stored.encrypted_secret

    rotated = await admin_client.post(
        f"{API_ROOT}/webhooks/{webhook['id']}/rotate-secret",
        headers=auth(admin_seed.owner_token),
        json={"expected_version": 1},
    )
    assert rotated.status_code == 200
    assert rotated.json()["version"] == 2
    assert rotated.json()["secret"] != first_secret
    webhook_updated = await admin_client.patch(
        f"{API_ROOT}/webhooks/{webhook['id']}",
        headers=auth(admin_seed.admin_token),
        json={"expected_version": 2, "name": "Primary website orders"},
    )
    assert webhook_updated.status_code == 200
    assert webhook_updated.json()["version"] == 3

    assert (
        await admin_client.delete(
            f"{API_ROOT}/webhooks/{webhook['id']}?expected_version=3",
            headers=auth(admin_seed.owner_token),
        )
    ).status_code == 204
    assert (
        await admin_client.delete(
            f"{API_ROOT}/forms/{form['id']}?expected_version=2",
            headers=auth(admin_seed.owner_token),
        )
    ).status_code == 204


@pytest.mark.asyncio
async def test_notification_template_and_rule_crud_enforces_catalog_and_consent(
    admin_client: httpx.AsyncClient, admin_seed: AdminSeed
) -> None:
    template_response = await admin_client.post(
        f"{API_ROOT}/notification-templates",
        headers=auth(admin_seed.admin_token),
        json={
            "name": "New lead email",
            "channel": "email",
            "subject_template": "Новый лид {name}",
            "body_template": "Откройте сделку {deal_id}",
        },
    )
    assert template_response.status_code == 201, template_response.text
    template = template_response.json()
    template_get = await admin_client.get(
        f"{API_ROOT}/notification-templates/{template['id']}",
        headers=auth(admin_seed.admin_token),
    )
    assert template_get.status_code == 200
    template_updated = await admin_client.patch(
        f"{API_ROOT}/notification-templates/{template['id']}",
        headers=auth(admin_seed.admin_token),
        json={"expected_version": 1, "name": "Primary lead email"},
    )
    assert template_updated.status_code == 200
    assert template_updated.json()["version"] == 2
    template_list = await admin_client.get(
        f"{API_ROOT}/notification-templates", headers=auth(admin_seed.admin_token)
    )
    assert [item["id"] for item in template_list.json()] == [template["id"]]

    unsafe_client_rule = await admin_client.post(
        f"{API_ROOT}/notification-rules",
        headers=auth(admin_seed.admin_token),
        json={
            "template_id": template["id"],
            "name": "Unsafe client notice",
            "event_type": "lead.created",
            "audience": "client",
            "channel": "email",
            "require_client_consent": False,
        },
    )
    assert unsafe_client_rule.status_code == 422

    pipeline_rule_response = await admin_client.post(
        f"{API_ROOT}/notification-rules",
        headers=auth(admin_seed.admin_token),
        json={
            "template_id": template["id"],
            "name": "All stages in sales pipeline",
            "event_type": "lead.created",
            "audience": "employee",
            "channel": "email",
            "pipeline_id": admin_seed.pipeline_id,
            "stage_id": None,
            "is_enabled": True,
        },
    )
    assert pipeline_rule_response.status_code == 201, pipeline_rule_response.text
    pipeline_rule = pipeline_rule_response.json()
    assert pipeline_rule["pipeline_id"] == admin_seed.pipeline_id
    assert pipeline_rule["stage_id"] is None

    stage_without_pipeline = await admin_client.post(
        f"{API_ROOT}/notification-rules",
        headers=auth(admin_seed.admin_token),
        json={
            "template_id": template["id"],
            "name": "Invalid stage-only filter",
            "event_type": "lead.created",
            "audience": "employee",
            "channel": "email",
            "pipeline_id": None,
            "stage_id": admin_seed.stage_id,
        },
    )
    assert stage_without_pipeline.status_code == 422

    rule_response = await admin_client.post(
        f"{API_ROOT}/notification-rules",
        headers=auth(admin_seed.admin_token),
        json={
            "template_id": template["id"],
            "name": "New lead for admins",
            "event_type": "lead.created",
            "audience": "employee",
            "channel": "email",
            "pipeline_id": admin_seed.pipeline_id,
            "stage_id": admin_seed.stage_id,
            "is_enabled": True,
        },
    )
    assert rule_response.status_code == 201, rule_response.text
    rule = rule_response.json()
    assert rule["version"] == 1
    rule_updated = await admin_client.patch(
        f"{API_ROOT}/notification-rules/{rule['id']}",
        headers=auth(admin_seed.owner_token),
        json={"expected_version": 1, "delay_seconds": 300},
    )
    assert rule_updated.status_code == 200
    assert rule_updated.json()["version"] == 2
    rule_get = await admin_client.get(
        f"{API_ROOT}/notification-rules/{rule['id']}",
        headers=auth(admin_seed.admin_token),
    )
    assert rule_get.status_code == 200
    assert (
        await admin_client.get(
            f"{API_ROOT}/notification-rules", headers=auth(admin_seed.admin_token)
        )
    ).status_code == 200
    assert (
        await admin_client.delete(
            f"{API_ROOT}/notification-rules/{rule['id']}?expected_version=2",
            headers=auth(admin_seed.owner_token),
        )
    ).status_code == 204
    assert (
        await admin_client.delete(
            f"{API_ROOT}/notification-rules/{pipeline_rule['id']}?expected_version=1",
            headers=auth(admin_seed.owner_token),
        )
    ).status_code == 204
    assert (
        await admin_client.delete(
            f"{API_ROOT}/notification-templates/{template['id']}?expected_version=2",
            headers=auth(admin_seed.owner_token),
        )
    ).status_code == 204


@pytest.mark.asyncio
async def test_import_start_pause_resume_is_versioned_and_enqueues_pages(
    admin_client: httpx.AsyncClient, admin_seed: AdminSeed
) -> None:
    async with SessionLocal() as db:
        db.add(
            AmoCRMConnection(
                workspace_id=uuid.UUID(admin_seed.workspace_id),
                status=AmoConnectionStatus.connected,
                account_domain="test.amocrm.ru",
                client_id="test-client",
                redirect_uri="https://pulse.test/callback",
            )
        )
        await db.commit()
    started = await admin_client.post(
        f"{API_ROOT}/imports/start",
        headers=auth(admin_seed.owner_token),
        json={"entity_type": "contacts", "dry_run": True, "user_mapping": {}},
    )
    assert started.status_code == 202, started.text
    job = started.json()
    assert job["status"] == "running"
    assert job["version"] == 1

    paused = await admin_client.post(
        f"{API_ROOT}/imports/{job['id']}/pause",
        headers=auth(admin_seed.admin_token),
        json={"expected_version": 1},
    )
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    assert paused.json()["version"] == 2
    stale_pause = await admin_client.post(
        f"{API_ROOT}/imports/{job['id']}/pause",
        headers=auth(admin_seed.admin_token),
        json={"expected_version": 1},
    )
    assert stale_pause.status_code == 409

    resumed = await admin_client.post(
        f"{API_ROOT}/imports/{job['id']}/resume",
        headers=auth(admin_seed.owner_token),
        json={"expected_version": 2},
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "running"
    assert resumed.json()["version"] == 3
    listed = await admin_client.get(f"{API_ROOT}/imports", headers=auth(admin_seed.admin_token))
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == job["id"]
    fetched = await admin_client.get(
        f"{API_ROOT}/imports/{job['id']}", headers=auth(admin_seed.admin_token)
    )
    assert fetched.status_code == 200

    async with SessionLocal() as db:
        queued = await db.scalar(
            sa.select(sa.func.count())
            .select_from(BackgroundJob)
            .where(
                BackgroundJob.workspace_id == uuid.UUID(admin_seed.workspace_id),
                BackgroundJob.job_type == "amo_import.page",
            )
        )
        assert queued == 2


@pytest.mark.asyncio
async def test_import_report_download_is_admin_and_workspace_scoped(
    admin_client: httpx.AsyncClient, admin_seed: AdminSeed
) -> None:
    workspace_id = uuid.UUID(admin_seed.workspace_id)
    async with SessionLocal() as db:
        other_workspace = Workspace(name="Report other", slug="report-other")
        db.add(other_workspace)
        await db.flush()
        running = ImportJob(
            workspace_id=workspace_id,
            provider="amocrm",
            status=ImportStatus.running,
            dry_run=True,
            entity_type="contacts",
        )
        without_report = ImportJob(
            workspace_id=workspace_id,
            provider="amocrm",
            status=ImportStatus.succeeded,
            dry_run=True,
            entity_type="contacts",
            completed_at=datetime.now(UTC),
        )
        ready = ImportJob(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            provider="amocrm",
            status=ImportStatus.succeeded,
            dry_run=False,
            entity_type="all",
            completed_at=datetime.now(UTC),
        )
        ready.report_object_key = f"imports/{workspace_id}/{ready.id}/report.json"
        other = ImportJob(
            id=uuid.uuid4(),
            workspace_id=other_workspace.id,
            provider="amocrm",
            status=ImportStatus.succeeded,
            dry_run=False,
            entity_type="all",
            completed_at=datetime.now(UTC),
        )
        other.report_object_key = (
            f"imports/{other_workspace.id}/{other.id}/report.json"
        )
        db.add_all([running, without_report, ready, other])
        await db.commit()

    manager = await admin_client.get(
        f"{API_ROOT}/imports/{ready.id}/report",
        headers=auth(admin_seed.manager_token),
    )
    assert manager.status_code == 403

    incomplete = await admin_client.get(
        f"{API_ROOT}/imports/{running.id}/report",
        headers=auth(admin_seed.admin_token),
    )
    assert incomplete.status_code == 409
    missing = await admin_client.get(
        f"{API_ROOT}/imports/{without_report.id}/report",
        headers=auth(admin_seed.admin_token),
    )
    assert missing.status_code == 404
    cross_workspace = await admin_client.get(
        f"{API_ROOT}/imports/{other.id}/report",
        headers=auth(admin_seed.owner_token),
    )
    assert cross_workspace.status_code == 404

    downloaded = await admin_client.get(
        f"{API_ROOT}/imports/{ready.id}/report",
        headers=auth(admin_seed.owner_token),
    )
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.json() == {
        "url": f"https://s3.test/get_object/{ready.report_object_key}?expires=300",
        "expires_in": 300,
    }
    async with SessionLocal() as db:
        audit = await db.scalar(
            sa.select(ActivityEvent).where(
                ActivityEvent.workspace_id == workspace_id,
                ActivityEvent.entity_id == ready.id,
                ActivityEvent.event_type == "amo_import.report_download_link_issued",
            )
        )
        assert audit is not None
        assert audit.actor_id == uuid.UUID(admin_seed.owner_id)
        assert audit.payload == {"expires_in": 300}

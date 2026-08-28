from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from app.db import SessionLocal
from app.integrations.amo import AmoEntity, AmoPage, apply_import_page
from app.integrations.amocrm_live import (
    AmoV4Client,
    PulseAmoWriter,
    make_amo_import_handler,
    make_amo_import_report_handler,
)
from app.integrations.models import (
    AmoConnectionStatus,
    AmoCRMConnection,
    ContactPoint,
    ExternalEntityMap,
    ImportJob,
    ImportStatus,
)
from app.integrations.s3 import AttachmentStorage
from app.integrations.secrets import SecretCipher
from app.models import (
    ActivityEvent,
    BackgroundJob,
    Company,
    Contact,
    CustomFieldDefinition,
    Deal,
    DealContact,
    Membership,
    Pipeline,
    Role,
    Stage,
    Task,
    User,
    Workspace,
)
from app.services.jobs import ClaimedJob


class FlakyReportS3:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []
        self.fail_first_put = True

    def put_object(self, **kwargs: Any) -> None:
        self.puts.append(kwargs)
        if self.fail_first_put:
            self.fail_first_put = False
            raise RuntimeError("simulated acknowledgement loss")

    def delete_object(self, **kwargs: Any) -> None:
        del kwargs

    def generate_presigned_url(
        self, client_method: str, *, Params: dict[str, Any], ExpiresIn: int
    ) -> str:
        return f"https://s3.test/{client_method}/{Params['Key']}?expires={ExpiresIn}"


@pytest.mark.asyncio
async def test_v4_client_pages_official_resources_and_filters_common_notes() -> None:
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer access-token"
        path = request.url.path
        if path == "/api/v4/leads/pipelines":
            return httpx.Response(
                200,
                json={
                    "_embedded": {
                        "pipelines": [
                            {
                                "id": 10,
                                "name": "Основная",
                                "updated_at": 1_700_000_000,
                                "_embedded": {
                                    "statuses": [
                                        {"id": 100, "name": "Новый", "sort": 10},
                                        {"id": 142, "name": "Успешно", "sort": 20},
                                    ]
                                },
                            }
                        ]
                    }
                },
            )
        if path.endswith("/custom_fields"):
            parent = path.split("/")[3]
            return httpx.Response(
                200,
                json={"_embedded": {"custom_fields": [{"id": 500, "name": parent}]}},
            )
        if path.endswith("/notes"):
            return httpx.Response(
                200,
                json={
                    "_embedded": {
                        "notes": [
                            {"id": 1, "note_type": "common", "entity_id": 40},
                            {"id": 2, "note_type": "call_in", "entity_id": 40},
                        ]
                    }
                },
            )
        raise AssertionError(f"unexpected path {path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http_client:
        client = AmoV4Client(
            account_domain="sales.amocrm.ru",
            access_token="access-token",
            http_client=http_client,
        )
        pipelines = await client.fetch_page(entity_type="pipelines", cursor={}, limit=250)
        stages = await client.fetch_page(entity_type="stages", cursor={}, limit=250)
        custom_fields = await client.fetch_page(entity_type="custom_fields", cursor={}, limit=250)
        notes = await client.fetch_page(entity_type="notes", cursor={}, limit=250)

    assert [entity.external_id for entity in pipelines.entities] == ["10"]
    assert [entity.external_id for entity in stages.entities] == ["100", "142"]
    assert stages.entities[0].data["pipeline_id"] == 10
    assert custom_fields.entities[0].external_id == "deal:500"
    assert custom_fields.next_cursor == {"partition": 1, "page": 1}
    assert [entity.external_id for entity in notes.entities] == ["leads:1"]
    note_request = next(request for request in requests if request.url.path.endswith("/notes"))
    assert "filter[note_type]" not in note_request.url.params
    assert all(int(request.url.params["limit"]) <= 250 for request in requests)


@pytest.mark.asyncio
async def test_workspace_writer_imports_full_domain_and_reruns_without_duplicates() -> None:
    async with SessionLocal() as db:
        workspace = Workspace(name="Writer", slug="amocrm-writer")
        owner = User(email="writer@example.com", full_name="Writer", password_hash="unused")
        db.add_all([workspace, owner])
        await db.flush()
        db.add(Membership(workspace_id=workspace.id, user_id=owner.id, role=Role.owner))
        await db.flush()
        writer = PulseAmoWriter()

        async def apply(entity_type: str, entities: list[AmoEntity]) -> None:
            job = ImportJob(
                workspace_id=workspace.id,
                provider="amocrm",
                status=ImportStatus.running,
                dry_run=False,
                entity_type=entity_type,
                user_mapping={"7": str(owner.id)},
            )
            db.add(job)
            await db.flush()
            await apply_import_page(
                db,
                job=job,
                page=AmoPage(entity_type=entity_type, entities=entities, next_cursor=None),
                writer=writer,
            )

        await apply(
            "pipelines",
            [AmoEntity("pipelines", "10", {"id": 10, "name": "Продажи", "sort": 1})],
        )
        await apply(
            "stages",
            [
                AmoEntity(
                    "stages",
                    "100",
                    {
                        "id": 100,
                        "pipeline_id": 10,
                        "name": "Новый",
                        "sort": 10,
                        "color": "#99CCFF",
                    },
                )
            ],
        )
        await apply(
            "users",
            [AmoEntity("users", "7", {"id": 7, "name": "amoCRM user"})],
        )
        await apply(
            "custom_fields",
            [
                AmoEntity(
                    "custom_fields",
                    "deal:501",
                    {
                        "id": 501,
                        "pulse_parent_entity": "leads",
                        "name": "Номер заказа",
                        "type": "text",
                    },
                )
            ],
        )
        await apply(
            "companies",
            [AmoEntity("companies", "20", {"id": 20, "name": "ООО Слой"})],
        )
        await apply(
            "contacts",
            [
                AmoEntity(
                    "contacts",
                    "30",
                    {
                        "id": 30,
                        "first_name": "Анна",
                        "last_name": "Иванова",
                        "custom_fields_values": [
                            {
                                "field_code": "EMAIL",
                                "values": [{"value": "anna@example.com"}],
                            }
                        ],
                        "_embedded": {"companies": [{"id": 20}]},
                    },
                )
            ],
        )
        deal_data = {
            "id": 40,
            "name": "Поставка кофе",
            "pipeline_id": 10,
            "status_id": 100,
            "responsible_user_id": 7,
            "price": 125000,
            "custom_fields_values": [{"field_id": 501, "values": [{"value": "A-001"}]}],
            "_embedded": {
                "companies": [{"id": 20}],
                "contacts": [{"id": 30, "is_main": True}],
            },
        }
        await apply("deals", [AmoEntity("deals", "40", deal_data)])
        await apply(
            "tasks",
            [
                AmoEntity(
                    "tasks",
                    "50",
                    {
                        "id": 50,
                        "text": "Позвонить",
                        "responsible_user_id": 7,
                        "complete_till": 1_800_000_000,
                        "entity_type": "leads",
                        "entity_id": 40,
                        "is_completed": False,
                    },
                )
            ],
        )
        await apply(
            "notes",
            [
                AmoEntity(
                    "notes",
                    "leads:60",
                    {
                        "id": 60,
                        "pulse_parent_entity": "leads",
                        "entity_id": 40,
                        "note_type": "common",
                        "params": {"text": "Клиент ждёт КП"},
                    },
                )
            ],
        )
        await db.commit()

        assert await db.scalar(sa.select(sa.func.count()).select_from(Pipeline)) == 1
        assert await db.scalar(sa.select(sa.func.count()).select_from(Stage)) == 1
        assert await db.scalar(sa.select(sa.func.count()).select_from(CustomFieldDefinition)) == 1
        company = await db.scalar(sa.select(Company))
        contact = await db.scalar(sa.select(Contact))
        deal = await db.scalar(sa.select(Deal))
        task = await db.scalar(sa.select(Task))
        note = await db.scalar(
            sa.select(ActivityEvent).where(ActivityEvent.event_type == "amo_import.note")
        )
        assert company is not None and contact is not None and deal is not None
        assert task is not None and note is not None
        assert contact.company_id == company.id
        assert contact.primary_email == "anna@example.com"
        assert await db.scalar(sa.select(sa.func.count()).select_from(ContactPoint)) == 1
        assert deal.company_id == company.id
        assert deal.assignee_id == owner.id
        assert deal.custom_fields == {"amo_501": "A-001"}
        assert task.deal_id == deal.id
        assert note.entity_id == deal.id
        assert await db.scalar(sa.select(sa.func.count()).select_from(DealContact)) == 1

        updated_data = {**deal_data, "name": "Поставка кофе — обновлено"}
        await apply("deals", [AmoEntity("deals", "40", updated_data)])
        await db.commit()
        assert await db.scalar(sa.select(sa.func.count()).select_from(Deal)) == 1
        refreshed = await db.get(Deal, deal.id)
        assert refreshed is not None
        assert refreshed.title == "Поставка кофе — обновлено"


@pytest.mark.asyncio
async def test_all_import_handler_advances_entity_and_enqueues_next_page() -> None:
    cipher = SecretCipher(key=b"h" * 32, key_id="handler-test")
    connection_id = uuid.uuid4()
    async with SessionLocal() as db:
        workspace = Workspace(name="Handler", slug="amocrm-handler")
        owner = User(email="handler@example.com", full_name="Owner", password_hash="unused")
        db.add_all([workspace, owner])
        await db.flush()
        db.add(Membership(workspace_id=workspace.id, user_id=owner.id, role=Role.owner))
        db.add(
            AmoCRMConnection(
                id=connection_id,
                workspace_id=workspace.id,
                status=AmoConnectionStatus.connected,
                account_domain="handler.amocrm.ru",
                client_id="client",
                redirect_uri="https://pulse.example.com/callback",
                encrypted_client_secret=cipher.encrypt(
                    "secret",
                    associated_data=f"amocrm-connection:{connection_id}:client-secret".encode(),
                ),
                encrypted_access_token=cipher.encrypt(
                    "access",
                    associated_data=f"amocrm-connection:{connection_id}:access".encode(),
                ),
                encrypted_refresh_token=cipher.encrypt(
                    "refresh",
                    associated_data=f"amocrm-connection:{connection_id}:refresh".encode(),
                ),
                token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        import_job = ImportJob(
            workspace_id=workspace.id,
            provider="amocrm",
            status=ImportStatus.running,
            dry_run=False,
            entity_type="all",
            user_mapping={"7": str(owner.id)},
        )
        db.add(import_job)
        await db.commit()
        import_job_id = import_job.id

    def provider(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v4/leads/pipelines"
        return httpx.Response(
            200,
            json={"_embedded": {"pipelines": [{"id": 10, "name": "Основная", "sort": 1}]}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http_client:
        handler = make_amo_import_handler(cipher=cipher, http_client=http_client)
        await handler(
            ClaimedJob(
                id=uuid.uuid4(),
                job_type="amo_import.page",
                payload={"import_job_id": str(import_job_id)},
                attempts=1,
                max_attempts=5,
                lease_owner="test-worker",
            )
        )

    async with SessionLocal() as db:
        imported = await db.get(ImportJob, import_job_id)
        assert imported is not None
        assert imported.status is ImportStatus.running
        assert imported.cursor == {"entity_type": "stages"}
        assert imported.counts["created"] == 1
        assert await db.scalar(sa.select(sa.func.count()).select_from(Pipeline)) == 1
        next_job = await db.scalar(
            sa.select(BackgroundJob).where(BackgroundJob.job_type == "amo_import.page")
        )
        assert next_job is not None
        assert next_job.payload == {"import_job_id": str(import_job_id)}
        assert await db.scalar(sa.select(sa.func.count()).select_from(ExternalEntityMap)) == 1


@pytest.mark.asyncio
async def test_import_report_handler_is_deterministic_and_retry_safe() -> None:
    started_at = datetime(2026, 8, 28, 8, 0, tzinfo=UTC)
    completed_at = datetime(2026, 8, 28, 8, 5, tzinfo=UTC)
    async with SessionLocal() as db:
        workspace = Workspace(name="Report", slug="amocrm-report")
        db.add(workspace)
        await db.flush()
        import_job = ImportJob(
            workspace_id=workspace.id,
            provider="amocrm",
            status=ImportStatus.succeeded,
            dry_run=True,
            entity_type="all",
            counts={"would_create": 17, "unchanged": 3},
            started_at=started_at,
            completed_at=completed_at,
        )
        db.add(import_job)
        await db.commit()
        import_job_id = import_job.id
        workspace_id = workspace.id

    fake_s3 = FlakyReportS3()
    storage = AttachmentStorage(fake_s3, bucket="pulse-private")
    handler = make_amo_import_report_handler(storage=storage)
    claimed = ClaimedJob(
        id=uuid.uuid4(),
        job_type="amo_import.report",
        payload={"import_job_id": str(import_job_id)},
        attempts=1,
        max_attempts=5,
        lease_owner="report-worker",
    )

    with pytest.raises(RuntimeError, match="acknowledgement loss"):
        await handler(claimed)
    async with SessionLocal() as db:
        after_failure = await db.get(ImportJob, import_job_id)
        assert after_failure is not None
        assert after_failure.report_object_key is None

    await handler(claimed)
    await handler(claimed)

    expected_key = f"imports/{workspace_id}/{import_job_id}/report.json"
    assert len(fake_s3.puts) == 2
    assert fake_s3.puts[0]["Key"] == fake_s3.puts[1]["Key"] == expected_key
    assert fake_s3.puts[0]["Body"] == fake_s3.puts[1]["Body"]
    report = json.loads(fake_s3.puts[1]["Body"])
    assert report == {
        "schema_version": 1,
        "import_id": str(import_job_id),
        "workspace_id": str(workspace_id),
        "provider": "amocrm",
        "entity_type": "all",
        "dry_run": True,
        "counts": {"unchanged": 3, "would_create": 17},
        "timestamps": {
            "created_at": report["timestamps"]["created_at"],
            "started_at": "2026-08-28T08:00:00Z",
            "completed_at": "2026-08-28T08:05:00Z",
        },
    }
    async with SessionLocal() as db:
        completed = await db.get(ImportJob, import_job_id)
        assert completed is not None
        assert completed.report_object_key == expected_key
        assert completed.version == 2


@pytest.mark.asyncio
async def test_final_import_page_enqueues_one_report_job() -> None:
    cipher = SecretCipher(key=b"q" * 32, key_id="report-queue-test")
    connection_id = uuid.uuid4()
    async with SessionLocal() as db:
        workspace = Workspace(name="Report queue", slug="amocrm-report-queue")
        db.add(workspace)
        await db.flush()
        db.add(
            AmoCRMConnection(
                id=connection_id,
                workspace_id=workspace.id,
                status=AmoConnectionStatus.connected,
                account_domain="report-queue.amocrm.ru",
                client_id="client",
                redirect_uri="https://pulse.example.com/callback",
                encrypted_client_secret=cipher.encrypt(
                    "secret",
                    associated_data=f"amocrm-connection:{connection_id}:client-secret".encode(),
                ),
                encrypted_access_token=cipher.encrypt(
                    "access",
                    associated_data=f"amocrm-connection:{connection_id}:access".encode(),
                ),
                encrypted_refresh_token=cipher.encrypt(
                    "refresh",
                    associated_data=f"amocrm-connection:{connection_id}:refresh".encode(),
                ),
                token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        import_job = ImportJob(
            workspace_id=workspace.id,
            provider="amocrm",
            status=ImportStatus.running,
            dry_run=True,
            entity_type="pipelines",
        )
        db.add(import_job)
        await db.commit()
        import_job_id = import_job.id

    def provider(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v4/leads/pipelines"
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http_client:
        handler = make_amo_import_handler(cipher=cipher, http_client=http_client)
        claimed = ClaimedJob(
            id=uuid.uuid4(),
            job_type="amo_import.page",
            payload={"import_job_id": str(import_job_id)},
            attempts=1,
            max_attempts=5,
            lease_owner="page-worker",
        )
        await handler(claimed)
        await handler(claimed)

    async with SessionLocal() as db:
        completed = await db.get(ImportJob, import_job_id)
        assert completed is not None
        assert completed.status is ImportStatus.succeeded
        reports = list(
            (
                await db.scalars(
                    sa.select(BackgroundJob).where(
                        BackgroundJob.job_type == "amo_import.report"
                    )
                )
            ).all()
        )
        assert len(reports) == 1
        assert reports[0].dedupe_key == f"amo-import:{import_job_id}:report"

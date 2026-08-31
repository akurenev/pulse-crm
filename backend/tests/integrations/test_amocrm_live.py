from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from app.db import SessionLocal
from app.integrations.amo import AmoEntity, AmoImportError, AmoPage, apply_import_page
from app.integrations.amocrm_live import (
    AmoImportDependencyError,
    AmoV4Client,
    PulseAmoWriter,
    _tags,
    make_amo_import_handler,
    make_amo_import_report_handler,
)
from app.integrations.identity import MatchKind, match_contact_point
from app.integrations.models import (
    AmoConnectionStatus,
    AmoCRMConnection,
    ContactPoint,
    ContactPointKind,
    ExternalEntityMap,
    ImportJob,
    ImportStatus,
    PurchaseSchedule,
    PurchaseScheduleStatus,
)
from app.integrations.purchases import ensure_purchase_task
from app.integrations.s3 import AttachmentStorage
from app.integrations.secrets import SecretCipher
from app.main import app
from app.models import (
    ActivityEvent,
    BackgroundJob,
    Company,
    Contact,
    CustomFieldDefinition,
    Deal,
    DealContact,
    FieldEntity,
    FieldType,
    Membership,
    Pipeline,
    RealtimeEvent,
    Role,
    Stage,
    StageType,
    Task,
    User,
    Workspace,
)
from app.services.access import notification_target_access_allowed
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


def _csrf(auth: dict[str, Any]) -> dict[str, str]:
    return {"X-CSRF-Token": str(auth["csrf_token"])}


async def _invite_employee(
    owner_client: httpx.AsyncClient,
    owner_auth: dict[str, Any],
    *,
    email: str,
    full_name: str,
) -> tuple[httpx.AsyncClient, dict[str, Any]]:
    invitation = await owner_client.post(
        "/api/v1/invitations",
        headers=_csrf(owner_auth),
        json={"email": email, "role": "employee"},
    )
    assert invitation.status_code == 201, invitation.text
    employee_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    accepted = await employee_client.post(
        "/api/v1/auth/accept-invitation",
        json={
            "token": invitation.json()["token"],
            "full_name": full_name,
            "password": "employee import test password",
        },
    )
    assert accepted.status_code == 201, accepted.text
    return employee_client, accepted.json()


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
                                        {"id": 143, "name": "Закрыто", "sort": 30},
                                    ]
                                },
                            },
                            {
                                "id": 20,
                                "name": "Повторные продажи",
                                "updated_at": 1_700_000_001,
                                "_embedded": {
                                    "statuses": [
                                        {"id": 200, "name": "Новый", "sort": 10},
                                        {"id": 142, "name": "Успешно", "sort": 20},
                                        {"id": 143, "name": "Закрыто", "sort": 30},
                                    ]
                                },
                            },
                        ]
                    }
                },
            )
        if path == "/api/v4/companies":
            return httpx.Response(
                200,
                json={
                    "_embedded": {
                        "companies": [
                            {
                                "id": 20,
                                "name": "ООО Слой",
                                "_embedded": {"tags": [{"id": 1, "name": "Партнёр"}]},
                            }
                        ]
                    }
                },
            )
        if path == "/api/v4/contacts":
            return httpx.Response(
                200,
                json={
                    "_embedded": {
                        "contacts": [
                            {
                                "id": 30,
                                "name": "Анна",
                                "_embedded": {"tags": [{"id": 2, "name": "VIP"}]},
                            }
                        ]
                    }
                },
            )
        if path == "/api/v4/leads":
            return httpx.Response(
                200,
                json={
                    "_embedded": {
                        "leads": [
                            {
                                "id": 40,
                                "name": "Поставка",
                                "_embedded": {
                                    "tags": [{"id": 3, "name": "Оптовая", "color": "#fff"}]
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
        companies = await client.fetch_page(entity_type="companies", cursor={}, limit=250)
        contacts = await client.fetch_page(entity_type="contacts", cursor={}, limit=250)
        deals = await client.fetch_page(entity_type="deals", cursor={}, limit=250)
        custom_fields = await client.fetch_page(entity_type="custom_fields", cursor={}, limit=250)
        notes = await client.fetch_page(entity_type="notes", cursor={}, limit=250)

    assert [entity.external_id for entity in pipelines.entities] == ["10", "20"]
    assert [entity.external_id for entity in stages.entities] == [
        "10:100",
        "10:142",
        "10:143",
        "20:200",
        "20:142",
        "20:143",
    ]
    assert len({entity.external_id for entity in stages.entities}) == len(stages.entities)
    assert stages.entities[0].data["pipeline_id"] == 10
    assert companies.entities[0].data["_embedded"]["tags"][0]["name"] == "Партнёр"
    assert contacts.entities[0].data["_embedded"]["tags"][0]["name"] == "VIP"
    assert deals.entities[0].data["_embedded"]["tags"][0]["name"] == "Оптовая"
    deal_request = next(request for request in requests if request.url.path == "/api/v4/leads")
    assert deal_request.url.params["with"] == "contacts"
    assert custom_fields.entities[0].external_id == "deal:500"
    assert custom_fields.next_cursor == {"partition": 1, "page": 1}
    assert [entity.external_id for entity in notes.entities] == ["leads:1"]
    note_request = next(request for request in requests if request.url.path.endswith("/notes"))
    assert "filter[note_type]" not in note_request.url.params
    assert all(int(request.url.params["limit"]) <= 250 for request in requests)

    async with SessionLocal() as db:
        workspace = Workspace(name="Dry run", slug="amocrm-stage-dry-run")
        db.add(workspace)
        await db.flush()
        job = ImportJob(
            workspace_id=workspace.id,
            provider="amocrm",
            status=ImportStatus.running,
            dry_run=True,
            entity_type="stages",
        )
        db.add(job)
        await db.flush()
        result = await apply_import_page(db, job=job, page=stages, writer=PulseAmoWriter())
        assert result.would_create == 6
        assert result.done is True


@pytest.mark.asyncio
async def test_workspace_writer_imports_full_domain_and_reruns_without_duplicates() -> None:
    async with SessionLocal() as db:
        workspace = Workspace(name="Writer", slug="amocrm-writer")
        owner = User(email="writer@example.com", full_name="Writer", password_hash="unused")
        employee = User(
            email="writer-employee@example.com",
            full_name="Writer Employee",
            password_hash="unused",
        )
        db.add_all([workspace, owner, employee])
        await db.flush()
        db.add_all(
            [
                Membership(workspace_id=workspace.id, user_id=owner.id, role=Role.owner),
                Membership(
                    workspace_id=workspace.id,
                    user_id=employee.id,
                    role=Role.employee,
                ),
            ]
        )
        await db.flush()
        writer = PulseAmoWriter()

        async def apply(entity_type: str, entities: list[AmoEntity]) -> Any:
            job = ImportJob(
                workspace_id=workspace.id,
                provider="amocrm",
                status=ImportStatus.running,
                dry_run=False,
                entity_type=entity_type,
                user_mapping={"7": str(owner.id), "8": str(employee.id)},
            )
            db.add(job)
            await db.flush()
            return await apply_import_page(
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
                    "10:100",
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
            [
                AmoEntity(
                    "companies",
                    "20",
                    {
                        "id": 20,
                        "name": "ООО Слой",
                        "_embedded": {"tags": [{"id": 1, "name": "Партнёр"}]},
                    },
                )
            ],
        )
        await apply(
            "contacts",
            [
                AmoEntity(
                    "contacts",
                    "30",
                    {
                        "id": 30,
                        "responsible_user_id": 7,
                        "first_name": "Анна",
                        "last_name": "Иванова",
                        "custom_fields_values": [
                            {
                                "field_code": "EMAIL",
                                "values": [{"value": "anna@example.com"}],
                            }
                        ],
                        "_embedded": {
                            "companies": [{"id": 20}],
                            "tags": [
                                {"id": 2, "name": " VIP "},
                                {"id": 3, "name": "VIP"},
                            ],
                        },
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
                "tags": [{"id": 4, "name": "Оптовая"}],
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
        assert company.tags == ["Партнёр"]
        assert contact.company_id == company.id
        assert contact.primary_email == "anna@example.com"
        assert contact.assignee_id == owner.id
        assert contact.tags == ["VIP"]
        assert await db.scalar(sa.select(sa.func.count()).select_from(ContactPoint)) == 1
        assert deal.company_id == company.id
        assert deal.assignee_id == owner.id
        assert deal.tags == ["Оптовая"]
        assert deal.custom_fields == {"amo_501": "A-001"}
        assert task.deal_id == deal.id
        assert note.entity_id == deal.id
        assert await db.scalar(sa.select(sa.func.count()).select_from(DealContact)) == 1

        updated_data = {
            **deal_data,
            "name": "Поставка кофе — обновлено",
            "_embedded": {
                **deal_data["_embedded"],
                "tags": [{"id": 5, "name": "Повторная"}],
            },
        }
        await apply("deals", [AmoEntity("deals", "40", updated_data)])
        await db.commit()
        assert await db.scalar(sa.select(sa.func.count()).select_from(Deal)) == 1
        refreshed = await db.get(Deal, deal.id)
        assert refreshed is not None
        assert refreshed.title == "Поставка кофе — обновлено"
        assert refreshed.tags == ["Повторная"]

        mapping = await db.scalar(
            sa.select(ExternalEntityMap).where(
                ExternalEntityMap.workspace_id == workspace.id,
                ExternalEntityMap.entity_type == "deals",
                ExternalEntityMap.external_id == "40",
            )
        )
        assert mapping is not None
        legacy_payload = json.dumps(
            updated_data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        mapping.fingerprint = hashlib.sha256(legacy_payload).hexdigest()
        refreshed.tags = []
        await db.commit()

        backfill = await apply("deals", [AmoEntity("deals", "40", updated_data)])
        await db.commit()
        assert backfill.updated == 1
        backfilled = await db.get(Deal, deal.id)
        assert backfilled is not None
        assert backfilled.tags == ["Повторная"]

        access_cursor = int(
            await db.scalar(sa.select(sa.func.max(RealtimeEvent.id))) or 0
        )
        await writer.upsert(
            db,
            workspace_id=workspace.id,
            entity=AmoEntity(
                "contacts",
                "30",
                {
                    "id": 30,
                    "responsible_user_id": 8,
                    "first_name": "Анна",
                    "last_name": "Иванова",
                },
            ),
            existing_internal_id=contact.id,
            user_mapping={"7": str(owner.id), "8": str(employee.id)},
        )
        await writer.upsert(
            db,
            workspace_id=workspace.id,
            entity=AmoEntity(
                "deals",
                "40",
                {**updated_data, "responsible_user_id": 8},
            ),
            existing_internal_id=deal.id,
            user_mapping={"7": str(owner.id), "8": str(employee.id)},
        )
        await writer.upsert(
            db,
            workspace_id=workspace.id,
            entity=AmoEntity(
                "tasks",
                "50",
                {
                    "id": 50,
                    "text": "Позвонить",
                    "responsible_user_id": 8,
                    "complete_till": 1_800_000_000,
                    "entity_type": "leads",
                    "entity_id": 40,
                },
            ),
            existing_internal_id=task.id,
            user_mapping={"7": str(owner.id), "8": str(employee.id)},
        )
        await db.flush()
        assert contact.assignee_id == employee.id
        assert deal.assignee_id == employee.id
        assert task.assignee_id == employee.id
        access_events = list(
            (
                await db.scalars(
                    sa.select(RealtimeEvent).where(
                        RealtimeEvent.workspace_id == workspace.id,
                        RealtimeEvent.event_type == "access.changed",
                        RealtimeEvent.id > access_cursor,
                    )
                )
            ).all()
        )
        assert {
            (event.payload["resource"], event.payload["recipient_id"])
            for event in access_events
        } == {
            (resource, str(recipient_id))
            for resource in ("contact", "deal", "task")
            for recipient_id in (owner.id, employee.id)
        }


@pytest.mark.asyncio
async def test_deal_import_keeps_purchase_schedule_and_task_ownership_in_sync(
    client: httpx.AsyncClient,
    owner_auth: dict[str, Any],
) -> None:
    employee_a, employee_a_auth = await _invite_employee(
        client,
        owner_auth,
        email="amo-schedule-a@example.com",
        full_name="Amo Schedule A",
    )
    employee_b, employee_b_auth = await _invite_employee(
        client,
        owner_auth,
        email="amo-schedule-b@example.com",
        full_name="Amo Schedule B",
    )
    try:
        owner_headers = _csrf(owner_auth)
        workspace_id = uuid.UUID(str(owner_auth["workspace"]["id"]))
        owner_id = uuid.UUID(str(owner_auth["user"]["id"]))
        employee_a_id = uuid.UUID(str(employee_a_auth["user"]["id"]))
        employee_b_id = uuid.UUID(str(employee_b_auth["user"]["id"]))

        pipeline = (await client.get("/api/v1/pipelines")).json()[0]
        open_stage = next(
            stage for stage in pipeline["stages"] if stage["stage_type"] == "open"
        )
        old_contact = (
            await client.post(
                "/api/v1/contacts",
                headers=owner_headers,
                json={"first_name": "Old", "assignee_id": str(employee_a_id)},
            )
        ).json()
        new_contact = (
            await client.post(
                "/api/v1/contacts",
                headers=owner_headers,
                json={"first_name": "New", "assignee_id": str(employee_b_id)},
            )
        ).json()
        next_purchase_at = (datetime.now(UTC) + timedelta(days=10)).isoformat()

        async def create_scheduled_deal(title: str) -> dict[str, Any]:
            response = await client.post(
                "/api/v1/deals",
                headers=owner_headers,
                json={
                    "title": title,
                    "pipeline_id": pipeline["id"],
                    "stage_id": open_stage["id"],
                    "assignee_id": str(employee_a_id),
                    "contact_ids": [old_contact["id"]],
                    "next_purchase_at": next_purchase_at,
                },
            )
            assert response.status_code == 201, response.text
            return response.json()

        materialized_deal = await create_scheduled_deal("Mapped materialized")
        unmaterialized_deal = await create_scheduled_deal("Mapped unmaterialized")
        fallback_deal = await create_scheduled_deal("Unmapped materialized")
        repair_deal = await create_scheduled_deal("Repair stale schedule")

        async with SessionLocal() as db:
            db.add_all(
                [
                    ExternalEntityMap(
                        workspace_id=workspace_id,
                        provider="amocrm",
                        entity_type=entity_type,
                        external_id=external_id,
                        internal_id=uuid.UUID(internal_id),
                        fingerprint=fingerprint * 64,
                    )
                    for entity_type, external_id, internal_id, fingerprint in (
                        ("pipelines", "10", pipeline["id"], "a"),
                        ("stages", "10:100", open_stage["id"], "b"),
                        ("contacts", "30", old_contact["id"], "c"),
                        ("contacts", "31", new_contact["id"], "d"),
                    )
                ]
            )
            await db.flush()
            schedules = {
                schedule.deal_id: schedule
                for schedule in (
                    await db.scalars(
                        sa.select(PurchaseSchedule).where(
                            PurchaseSchedule.workspace_id == workspace_id,
                            PurchaseSchedule.status == PurchaseScheduleStatus.active,
                        )
                    )
                ).all()
            }
            materialized_result = await ensure_purchase_task(
                db,
                workspace_id=workspace_id,
                schedule_id=schedules[
                    uuid.UUID(materialized_deal["id"])
                ].id,
            )
            fallback_result = await ensure_purchase_task(
                db,
                workspace_id=workspace_id,
                schedule_id=schedules[uuid.UUID(fallback_deal["id"])].id,
            )
            repair_stored_deal = await db.get(
                Deal, uuid.UUID(repair_deal["id"])
            )
            assert repair_stored_deal is not None
            repair_stored_deal.assignee_id = employee_b_id
            materialized_task_id = materialized_result.task.id
            materialized_task_version = materialized_result.task.version
            fallback_task_id = fallback_result.task.id
            access_cursor = int(
                await db.scalar(sa.select(sa.func.max(RealtimeEvent.id))) or 0
            )
            await db.commit()

        writer = PulseAmoWriter()

        def imported_deal(
            external_id: str, title: str, responsible_user_id: int
        ) -> AmoEntity:
            return AmoEntity(
                "deals",
                external_id,
                {
                    "id": int(external_id),
                    "name": title,
                    "pipeline_id": 10,
                    "status_id": 100,
                    "responsible_user_id": responsible_user_id,
                    "_embedded": {
                        "contacts": [{"id": 31, "is_main": True}],
                    },
                },
            )

        async with SessionLocal() as db:
            await writer.upsert(
                db,
                workspace_id=workspace_id,
                entity=imported_deal("40", "Mapped materialized", 8),
                existing_internal_id=uuid.UUID(materialized_deal["id"]),
                user_mapping={"8": str(employee_b_id)},
            )
            await writer.upsert(
                db,
                workspace_id=workspace_id,
                entity=imported_deal("41", "Mapped unmaterialized", 8),
                existing_internal_id=uuid.UUID(unmaterialized_deal["id"]),
                user_mapping={"8": str(employee_b_id)},
            )
            await writer.upsert(
                db,
                workspace_id=workspace_id,
                entity=imported_deal("42", "Unmapped materialized", 999),
                existing_internal_id=uuid.UUID(fallback_deal["id"]),
                user_mapping={"8": str(employee_b_id)},
            )
            await writer.upsert(
                db,
                workspace_id=workspace_id,
                entity=imported_deal("43", "Repair stale schedule", 8),
                existing_internal_id=uuid.UUID(repair_deal["id"]),
                user_mapping={"8": str(employee_b_id)},
            )
            await db.commit()

        async with SessionLocal() as db:
            schedules = {
                schedule.deal_id: schedule
                for schedule in (
                    await db.scalars(
                        sa.select(PurchaseSchedule).where(
                            PurchaseSchedule.workspace_id == workspace_id,
                            PurchaseSchedule.status == PurchaseScheduleStatus.active,
                        )
                    )
                ).all()
            }
            mapped_schedule = schedules[uuid.UUID(materialized_deal["id"])]
            unmaterialized_schedule = schedules[
                uuid.UUID(unmaterialized_deal["id"])
            ]
            fallback_schedule = schedules[uuid.UUID(fallback_deal["id"])]
            repair_schedule = schedules[uuid.UUID(repair_deal["id"])]
            mapped_task = await db.get(Task, materialized_task_id)
            fallback_task = await db.get(Task, fallback_task_id)
            fallback_stored_deal = await db.get(
                Deal, uuid.UUID(fallback_deal["id"])
            )
            assert mapped_task is not None
            assert fallback_task is not None
            assert fallback_stored_deal is not None

            assert mapped_schedule.assignee_id == employee_b_id
            assert mapped_schedule.contact_id == uuid.UUID(new_contact["id"])
            assert mapped_task.assignee_id == employee_b_id
            assert mapped_task.contact_id == uuid.UUID(new_contact["id"])
            assert mapped_task.version == materialized_task_version + 1
            assert await db.scalar(
                sa.select(DealContact.contact_id).where(
                    DealContact.workspace_id == workspace_id,
                    DealContact.deal_id == uuid.UUID(materialized_deal["id"]),
                    DealContact.is_primary.is_(True),
                )
            ) == uuid.UUID(new_contact["id"])

            assert unmaterialized_schedule.assignee_id == employee_b_id
            assert unmaterialized_schedule.contact_id == uuid.UUID(new_contact["id"])
            assert unmaterialized_schedule.task_id is None

            assert fallback_stored_deal.assignee_id is None
            assert fallback_schedule.assignee_id == owner_id
            assert fallback_schedule.contact_id == uuid.UUID(new_contact["id"])
            assert fallback_task.assignee_id == owner_id
            assert fallback_task.contact_id == uuid.UUID(new_contact["id"])

            assert repair_schedule.assignee_id == employee_b_id
            assert repair_schedule.contact_id == uuid.UUID(new_contact["id"])
            assert repair_schedule.task_id is None

            mapped_task_event = await db.scalar(
                sa.select(ActivityEvent).where(
                    ActivityEvent.workspace_id == workspace_id,
                    ActivityEvent.event_type == "task.updated",
                    ActivityEvent.entity_id == materialized_task_id,
                )
            )
            assert mapped_task_event is not None
            assert mapped_task_event.payload == {
                "fields": ["assignee_id", "contact_id"],
                "deal_id": materialized_deal["id"],
            }
            access_events = list(
                (
                    await db.scalars(
                        sa.select(RealtimeEvent).where(
                            RealtimeEvent.workspace_id == workspace_id,
                            RealtimeEvent.event_type == "access.changed",
                            RealtimeEvent.id > access_cursor,
                        )
                    )
                ).all()
            )
            access_pairs = {
                (event.payload["resource"], event.payload["recipient_id"])
                for event in access_events
            }
            assert {
                ("task", str(employee_a_id)),
                ("task", str(employee_b_id)),
                ("task", str(owner_id)),
                ("purchase", str(employee_a_id)),
                ("purchase", str(employee_b_id)),
            } <= access_pairs

            assert not await notification_target_access_allowed(
                db,
                workspace_id=workspace_id,
                recipient_id=employee_a_id,
                target_entity_type="deal",
                target_entity_id=uuid.UUID(materialized_deal["id"]),
            )
            assert await notification_target_access_allowed(
                db,
                workspace_id=workspace_id,
                recipient_id=employee_b_id,
                target_entity_type="deal",
                target_entity_id=uuid.UUID(materialized_deal["id"]),
            )
            assert not await notification_target_access_allowed(
                db,
                workspace_id=workspace_id,
                recipient_id=employee_a_id,
                target_entity_type="task",
                target_entity_id=materialized_task_id,
            )
            assert await notification_target_access_allowed(
                db,
                workspace_id=workspace_id,
                recipient_id=employee_b_id,
                target_entity_type="task",
                target_entity_id=materialized_task_id,
            )
            assert not await notification_target_access_allowed(
                db,
                workspace_id=workspace_id,
                recipient_id=employee_a_id,
                target_entity_type="task",
                target_entity_id=fallback_task_id,
            )
            assert await notification_target_access_allowed(
                db,
                workspace_id=workspace_id,
                recipient_id=owner_id,
                target_entity_type="task",
                target_entity_id=fallback_task_id,
            )

        employee_a_deals = await employee_a.get("/api/v1/deals")
        employee_b_deals = await employee_b.get("/api/v1/deals")
        assert employee_a_deals.status_code == 200, employee_a_deals.text
        assert employee_b_deals.status_code == 200, employee_b_deals.text
        assert not {
            materialized_deal["id"],
            unmaterialized_deal["id"],
            fallback_deal["id"],
            repair_deal["id"],
        } & {item["id"] for item in employee_a_deals.json()["items"]}
        assert {
            materialized_deal["id"],
            unmaterialized_deal["id"],
            repair_deal["id"],
        } <= {item["id"] for item in employee_b_deals.json()["items"]}

        employee_a_tasks = await employee_a.get(
            "/api/v1/tasks", params={"include_completed": True}
        )
        employee_b_tasks = await employee_b.get(
            "/api/v1/tasks", params={"include_completed": True}
        )
        assert employee_a_tasks.status_code == 200, employee_a_tasks.text
        assert employee_b_tasks.status_code == 200, employee_b_tasks.text
        assert str(materialized_task_id) not in {
            item["id"] for item in employee_a_tasks.json()["items"]
        }
        assert str(fallback_task_id) not in {
            item["id"] for item in employee_a_tasks.json()["items"]
        }
        assert str(materialized_task_id) in {
            item["id"] for item in employee_b_tasks.json()["items"]
        }

        employee_a_dashboard = await employee_a.get("/api/v1/dashboard")
        employee_b_dashboard = await employee_b.get("/api/v1/dashboard")
        assert employee_a_dashboard.status_code == 200, employee_a_dashboard.text
        assert employee_b_dashboard.status_code == 200, employee_b_dashboard.text
        assert employee_a_dashboard.json()["upcoming_purchases_30d"] == 0
        assert employee_b_dashboard.json()["upcoming_purchases_30d"] == 3
    finally:
        await employee_a.aclose()
        await employee_b.aclose()


@pytest.mark.asyncio
async def test_unmapped_deal_schedule_fallback_rejects_non_privileged_employee() -> None:
    async with SessionLocal() as db:
        workspace = Workspace(name="Fallback", slug="amo-schedule-fallback")
        inactive_owner = User(
            email="inactive-owner@example.com",
            full_name="Inactive Owner",
            password_hash="unused",
            is_active=False,
        )
        inactive_admin = User(
            email="inactive-admin@example.com",
            full_name="Inactive Admin",
            password_hash="unused",
            is_active=False,
        )
        employee = User(
            email="active-employee@example.com",
            full_name="Active Employee",
            password_hash="unused",
        )
        db.add_all([workspace, inactive_owner, inactive_admin, employee])
        await db.flush()
        db.add_all(
            [
                Membership(
                    workspace_id=workspace.id,
                    user_id=inactive_owner.id,
                    role=Role.owner,
                ),
                Membership(
                    workspace_id=workspace.id,
                    user_id=inactive_admin.id,
                    role=Role.admin,
                ),
                Membership(
                    workspace_id=workspace.id,
                    user_id=employee.id,
                    role=Role.employee,
                ),
            ]
        )
        pipeline = Pipeline(workspace_id=workspace.id, name="Import", position=0)
        db.add(pipeline)
        await db.flush()
        stage = Stage(
            workspace_id=workspace.id,
            pipeline_id=pipeline.id,
            name="Open",
            position=0,
            stage_type=StageType.open,
        )
        db.add(stage)
        await db.flush()
        deal = Deal(
            workspace_id=workspace.id,
            pipeline_id=pipeline.id,
            stage_id=stage.id,
            title="Assigned before import",
            assignee_id=employee.id,
        )
        db.add(deal)
        await db.flush()
        schedule = PurchaseSchedule(
            workspace_id=workspace.id,
            deal_id=deal.id,
            assignee_id=employee.id,
            scheduled_for=datetime.now(UTC) + timedelta(days=10),
            remind_at=datetime.now(UTC) + timedelta(days=7),
        )
        db.add_all(
            [
                schedule,
                ExternalEntityMap(
                    workspace_id=workspace.id,
                    provider="amocrm",
                    entity_type="pipelines",
                    external_id="10",
                    internal_id=pipeline.id,
                    fingerprint="a" * 64,
                ),
                ExternalEntityMap(
                    workspace_id=workspace.id,
                    provider="amocrm",
                    entity_type="stages",
                    external_id="10:100",
                    internal_id=stage.id,
                    fingerprint="b" * 64,
                ),
            ]
        )
        await db.commit()
        workspace_id = workspace.id
        deal_id = deal.id
        schedule_id = schedule.id
        employee_id = employee.id

    async with SessionLocal() as db:
        with pytest.raises(
            AmoImportDependencyError,
            match="no active fallback assignee",
        ):
            await PulseAmoWriter().upsert(
                db,
                workspace_id=workspace_id,
                entity=AmoEntity(
                    "deals",
                    "40",
                    {
                        "id": 40,
                        "name": "Unmapped after import",
                        "pipeline_id": 10,
                        "status_id": 100,
                        "responsible_user_id": 999,
                    },
                ),
                existing_internal_id=deal_id,
                user_mapping={},
            )
        await db.rollback()

    async with SessionLocal() as db:
        stored_deal = await db.get(Deal, deal_id)
        stored_schedule = await db.get(PurchaseSchedule, schedule_id)
        assert stored_deal is not None
        assert stored_schedule is not None
        assert stored_deal.assignee_id == employee_id
        assert stored_deal.title == "Assigned before import"
        assert stored_schedule.assignee_id == employee_id
        assert stored_schedule.status == PurchaseScheduleStatus.active


@pytest.mark.asyncio
async def test_writer_does_not_reactivate_locally_deleted_custom_field() -> None:
    async with SessionLocal() as db:
        workspace = Workspace(name="Writer tombstone", slug="amocrm-writer-tombstone")
        db.add(workspace)
        await db.flush()
        field = CustomFieldDefinition(
            workspace_id=workspace.id,
            entity_type=FieldEntity.deal,
            key="amo_501",
            name="Архивный номер",
            field_type=FieldType.text,
            is_active=False,
        )
        job = ImportJob(
            workspace_id=workspace.id,
            provider="amocrm",
            status=ImportStatus.running,
            dry_run=False,
            entity_type="custom_fields",
        )
        db.add_all([field, job])
        await db.flush()
        db.add(
            ExternalEntityMap(
                workspace_id=workspace.id,
                import_job_id=job.id,
                provider="amocrm",
                entity_type="custom_fields",
                external_id="deal:501",
                internal_id=field.id,
                fingerprint="previous-import",
            )
        )
        await db.flush()

        result = await apply_import_page(
            db,
            job=job,
            page=AmoPage(
                entity_type="custom_fields",
                entities=[
                    AmoEntity(
                        "custom_fields",
                        "deal:501",
                        {
                            "id": 501,
                            "name": "Номер заказа из amoCRM",
                            "type": "text",
                            "is_deleted": False,
                        },
                    )
                ],
                next_cursor=None,
            ),
            writer=PulseAmoWriter(),
        )

        await db.refresh(field)
        assert result.updated == 1
        assert field.name == "Номер заказа из amoCRM"
        assert field.is_active is False


@pytest.mark.asyncio
async def test_writer_does_not_reactivate_locally_deleted_contact() -> None:
    async with SessionLocal() as db:
        workspace = Workspace(name="Contact tombstone", slug="amocrm-contact-tombstone")
        db.add(workspace)
        await db.flush()
        deleted_at = datetime.now(UTC)
        contact = Contact(
            workspace_id=workspace.id,
            first_name="Удалённый",
            primary_email="old@example.com",
            deleted_at=deleted_at,
        )
        job = ImportJob(
            workspace_id=workspace.id,
            provider="amocrm",
            status=ImportStatus.running,
            dry_run=False,
            entity_type="contacts",
        )
        db.add_all([contact, job])
        await db.flush()
        db.add(
            ExternalEntityMap(
                workspace_id=workspace.id,
                import_job_id=job.id,
                provider="amocrm",
                entity_type="contacts",
                external_id="30",
                internal_id=contact.id,
                fingerprint="previous-import",
            )
        )
        await db.flush()

        result = await apply_import_page(
            db,
            job=job,
            page=AmoPage(
                entity_type="contacts",
                entities=[
                    AmoEntity(
                        "contacts",
                        "30",
                        {
                            "id": 30,
                            "first_name": "Обновлённый",
                            "custom_fields_values": [
                                {
                                    "field_code": "EMAIL",
                                    "values": [{"value": "new@example.com"}],
                                }
                            ],
                        },
                    )
                ],
                next_cursor=None,
            ),
            writer=PulseAmoWriter(),
        )

        await db.refresh(contact)
        assert result.updated == 1
        assert contact.first_name == "Обновлённый"
        assert contact.deleted_at is not None
        match = await match_contact_point(
            db,
            workspace_id=workspace.id,
            kind=ContactPointKind.email,
            value="new@example.com",
        )
        contact_point_count = await db.scalar(
            sa.select(sa.func.count())
            .select_from(ContactPoint)
            .where(ContactPoint.contact_id == contact.id)
        )
        assert match.kind is MatchKind.none
        assert contact_point_count == 0


def test_amo_tags_are_normalized_deduplicated_and_bounded() -> None:
    rows = [
        {"name": " VIP "},
        {"name": "vip"},
        {"name": "   "},
        {"name": None},
        {"name": "x" * 101},
        *({"name": f"tag-{index}"} for index in range(150)),
    ]

    tags = _tags({"_embedded": {"tags": rows}})

    assert tags[:2] == ["VIP", "x" * 100]
    assert len(tags) == 100
    assert tags[-1] == "tag-97"


@pytest.mark.asyncio
async def test_shared_system_stage_ids_remain_scoped_to_their_pipelines() -> None:
    async with SessionLocal() as db:
        workspace = Workspace(name="Shared stages", slug="amocrm-shared-stages")
        db.add(workspace)
        await db.flush()
        writer = PulseAmoWriter()

        async def apply(entity_type: str, entities: list[AmoEntity]) -> Any:
            job = ImportJob(
                workspace_id=workspace.id,
                provider="amocrm",
                status=ImportStatus.running,
                dry_run=False,
                entity_type=entity_type,
            )
            db.add(job)
            await db.flush()
            return await apply_import_page(
                db,
                job=job,
                page=AmoPage(entity_type=entity_type, entities=entities, next_cursor=None),
                writer=writer,
            )

        await apply(
            "pipelines",
            [
                AmoEntity("pipelines", "10", {"id": 10, "name": "Основная", "sort": 1}),
                AmoEntity("pipelines", "20", {"id": 20, "name": "Повторная", "sort": 2}),
            ],
        )
        stages = [
            AmoEntity(
                "stages",
                "10:142",
                {
                    "id": 142,
                    "pipeline_id": 10,
                    "name": "Успешно",
                    "sort": 10_000,
                    "color": "#CCFF66",
                },
            ),
            AmoEntity(
                "stages",
                "20:142",
                {
                    "id": 142,
                    "pipeline_id": 20,
                    "name": "Успешно",
                    "sort": 10_000,
                    "color": "#CCFF66",
                },
            ),
        ]
        first_stage_result = await apply("stages", stages)
        second_stage_result = await apply("stages", stages)
        assert first_stage_result.created == 2
        assert second_stage_result.unchanged == 2

        await apply(
            "deals",
            [
                AmoEntity(
                    "deals",
                    "40",
                    {"id": 40, "name": "Сделка A", "pipeline_id": 10, "status_id": 142},
                ),
                AmoEntity(
                    "deals",
                    "50",
                    {"id": 50, "name": "Сделка B", "pipeline_id": 20, "status_id": 142},
                ),
            ],
        )
        await db.commit()

        stage_maps = list(
            (
                await db.scalars(
                    sa.select(ExternalEntityMap).where(
                        ExternalEntityMap.workspace_id == workspace.id,
                        ExternalEntityMap.entity_type == "stages",
                    )
                )
            ).all()
        )
        stage_ids = {mapping.external_id: mapping.internal_id for mapping in stage_maps}
        assert stage_ids["10:142"] != stage_ids["20:142"]
        assert await db.scalar(sa.select(sa.func.count()).select_from(Stage)) == 2
        imported_stages = list((await db.scalars(sa.select(Stage))).all())
        assert all(stage.stage_type is StageType.won for stage in imported_stages)

        deals = {deal.title: deal for deal in (await db.scalars(sa.select(Deal))).all()}
        assert deals["Сделка A"].stage_id == stage_ids["10:142"]
        assert deals["Сделка B"].stage_id == stage_ids["20:142"]


@pytest.mark.asyncio
async def test_composite_stage_import_reuses_legacy_bare_stage_mapping() -> None:
    async with SessionLocal() as db:
        workspace = Workspace(name="Legacy stages", slug="amocrm-legacy-stages")
        db.add(workspace)
        await db.flush()
        pipeline = Pipeline(workspace_id=workspace.id, name="Импорт", position=1)
        db.add(pipeline)
        await db.flush()
        stage = Stage(
            workspace_id=workspace.id,
            pipeline_id=pipeline.id,
            name="Старое название",
            position=10_000,
            stage_type=StageType.won,
        )
        db.add(stage)
        await db.flush()
        db.add_all(
            [
                ExternalEntityMap(
                    workspace_id=workspace.id,
                    provider="amocrm",
                    entity_type="pipelines",
                    external_id="10",
                    internal_id=pipeline.id,
                    fingerprint="a" * 64,
                ),
                ExternalEntityMap(
                    workspace_id=workspace.id,
                    provider="amocrm",
                    entity_type="stages",
                    external_id="142",
                    internal_id=stage.id,
                    fingerprint="b" * 64,
                ),
            ]
        )
        job = ImportJob(
            workspace_id=workspace.id,
            provider="amocrm",
            status=ImportStatus.running,
            dry_run=False,
            entity_type="stages",
        )
        db.add(job)
        await db.flush()

        result = await apply_import_page(
            db,
            job=job,
            page=AmoPage(
                entity_type="stages",
                entities=[
                    AmoEntity(
                        "stages",
                        "10:142",
                        {
                            "id": 142,
                            "pipeline_id": 10,
                            "name": "Успешно",
                            "sort": 10_000,
                            "color": "#CCFF66",
                        },
                    )
                ],
                next_cursor=None,
            ),
            writer=PulseAmoWriter(),
        )
        await db.commit()

        assert result.created == 1
        assert await db.scalar(sa.select(sa.func.count()).select_from(Stage)) == 1
        composite_map = await db.scalar(
            sa.select(ExternalEntityMap).where(
                ExternalEntityMap.workspace_id == workspace.id,
                ExternalEntityMap.entity_type == "stages",
                ExternalEntityMap.external_id == "10:142",
            )
        )
        assert composite_map is not None
        assert composite_map.internal_id == stage.id
        refreshed = await db.get(Stage, stage.id)
        assert refreshed is not None
        assert refreshed.name == "Успешно"
        assert refreshed.version == 2


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
                workspace_id=workspace.id,
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
        workspace_id=workspace_id,
    )

    with pytest.raises(AmoImportError, match="does not match"):
        await handler(
            ClaimedJob(
                id=uuid.uuid4(),
                job_type="amo_import.report",
                payload={"import_job_id": str(import_job_id)},
                attempts=1,
                max_attempts=5,
                lease_owner="cross-workspace-worker",
                workspace_id=uuid.uuid4(),
            )
        )
    assert fake_s3.puts == []

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
            workspace_id=workspace.id,
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
                    sa.select(BackgroundJob).where(BackgroundJob.job_type == "amo_import.report")
                )
            ).all()
        )
        assert len(reports) == 1
        assert reports[0].dedupe_key == f"amo-import:{import_job_id}:report"

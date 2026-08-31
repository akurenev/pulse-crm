from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa

from app.db import SessionLocal
from app.integrations.models import (
    ChannelConnection,
    ChannelKind,
    ContactPoint,
    ExternalEntityMap,
    ExternalIdentity,
    Form,
    NotificationAudience,
    NotificationDelivery,
    NotificationRule,
    NotificationTemplate,
    PurchaseSchedule,
    PurchaseScheduleStatus,
    WebhookEndpoint,
)
from app.integrations.purchases import ensure_purchase_task
from app.models import (
    ActivityEvent,
    BackgroundJob,
    Contact,
    CustomFieldDefinition,
    Deal,
    DeliveryStatus,
    JobStatus,
    Membership,
    OutboxEvent,
    Pipeline,
    RealtimeEvent,
    Role,
    Stage,
    Task,
    TaskStatus,
    User,
    Workspace,
)


def csrf(auth: dict[str, object]) -> dict[str, str]:
    return {"X-CSRF-Token": str(auth["csrf_token"])}


def as_uuid(value: object) -> uuid.UUID:
    return uuid.UUID(str(value))


@pytest.mark.asyncio
async def test_contact_company_and_task_crud(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    company = await client.post("/api/v1/companies", headers=headers, json={"name": "Acme"})
    assert company.status_code == 201
    company_id = company.json()["id"]

    contact = await client.post(
        "/api/v1/contacts",
        headers=headers,
        json={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "company_id": company_id,
            "primary_email": "ADA@example.com",
            "tags": ["vip"],
        },
    )
    assert contact.status_code == 201, contact.text
    assert contact.json()["company_id"] == company_id

    users = await client.get("/api/v1/users")
    owner_id = users.json()[0]["id"]
    due_at = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    task = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"title": "Call customer", "due_at": due_at, "assignee_id": owner_id},
    )
    assert task.status_code == 201, task.text
    completed = await client.patch(
        f"/api/v1/tasks/{task.json()['id']}",
        headers=headers,
        json={"expected_version": 1, "status": "completed"},
    )
    assert completed.status_code == 200
    assert completed.json()["completed_at"] is not None

    companies = await client.get("/api/v1/companies", params={"search": "acm"})
    contacts = await client.get("/api/v1/contacts", params={"search": "ada"})
    assert len(companies.json()["items"]) == 1
    assert len(contacts.json()["items"]) == 1


@pytest.mark.asyncio
async def test_contact_update_and_soft_delete_are_versioned(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    created = await client.post(
        "/api/v1/contacts",
        headers=headers,
        json={
            "first_name": "Original",
            "last_name": "Contact",
            "primary_email": "original@example.com",
        },
    )
    assert created.status_code == 201, created.text
    contact = created.json()

    updated = await client.patch(
        f"/api/v1/contacts/{contact['id']}",
        headers=headers,
        json={
            "expected_version": contact["version"],
            "first_name": "Updated",
            "primary_email": "updated@example.com",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["first_name"] == "Updated"
    assert updated.json()["version"] == contact["version"] + 1

    invalid_null = await client.patch(
        f"/api/v1/contacts/{contact['id']}",
        headers=headers,
        json={"expected_version": updated.json()["version"], "first_name": None},
    )
    assert invalid_null.status_code == 422

    contact_id = as_uuid(contact["id"])
    workspace_id = as_uuid(owner_auth["workspace"]["id"])
    async with SessionLocal() as db:
        db.add(
            ExternalIdentity(
                workspace_id=workspace_id,
                contact_id=contact_id,
                provider="test-provider",
                connection_scope="global",
                external_user_id="external-contact-1",
            )
        )
        await db.commit()

    stale_delete = await client.delete(
        f"/api/v1/contacts/{contact['id']}",
        headers=headers,
        params={"expected_version": contact["version"]},
    )
    assert stale_delete.status_code == 409
    assert stale_delete.json()["detail"]["code"] == "version_conflict"

    deleted = await client.delete(
        f"/api/v1/contacts/{contact['id']}",
        headers=headers,
        params={"expected_version": updated.json()["version"]},
    )
    assert deleted.status_code == 204, deleted.text

    loaded = await client.get(f"/api/v1/contacts/{contact['id']}")
    assert loaded.status_code == 404
    listed = await client.get("/api/v1/contacts", params={"search": "Updated"})
    assert listed.status_code == 200
    assert listed.json()["items"] == []
    repeated = await client.delete(
        f"/api/v1/contacts/{contact['id']}",
        headers=headers,
        params={"expected_version": updated.json()["version"] + 1},
    )
    assert repeated.status_code == 404

    async with SessionLocal() as db:
        stored = await db.get(Contact, contact_id)
        assert stored is not None
        assert stored.deleted_at is not None
        assert stored.version == updated.json()["version"] + 1
        assert (
            await db.scalar(
                sa.select(sa.func.count())
                .select_from(ContactPoint)
                .where(ContactPoint.contact_id == contact_id)
            )
            == 0
        )
        assert (
            await db.scalar(
                sa.select(sa.func.count())
                .select_from(ExternalIdentity)
                .where(ExternalIdentity.contact_id == contact_id)
            )
            == 0
        )
        assert (
            await db.scalar(
                sa.select(sa.func.count())
                .select_from(ActivityEvent)
                .where(
                    ActivityEvent.entity_id == contact_id,
                    ActivityEvent.event_type == "contact.deleted",
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_task_update_relations_and_delete_are_versioned(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    owner_id = (await client.get("/api/v1/users")).json()[0]["id"]
    company = await client.post(
        "/api/v1/companies", headers=headers, json={"name": "Task company"}
    )
    contact = await client.post(
        "/api/v1/contacts", headers=headers, json={"first_name": "Task contact"}
    )
    pipeline = (await client.get("/api/v1/pipelines")).json()[0]
    deal = await client.post(
        "/api/v1/deals",
        headers=headers,
        json={
            "title": "Task deal",
            "pipeline_id": pipeline["id"],
            "stage_id": pipeline["stages"][0]["id"],
        },
    )
    assert company.status_code == contact.status_code == deal.status_code == 201

    initial_due_at = datetime.now(UTC) + timedelta(days=2)
    created = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Initial task",
            "due_at": initial_due_at.isoformat(),
            "assignee_id": owner_id,
        },
    )
    assert created.status_code == 201, created.text
    task = created.json()
    new_due_at = initial_due_at + timedelta(days=1)
    new_remind_at = new_due_at - timedelta(hours=2)
    updated = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        headers=headers,
        json={
            "expected_version": task["version"],
            "title": "Updated task",
            "description": "Updated description",
            "task_type": "meeting",
            "due_at": new_due_at.isoformat(),
            "remind_at": new_remind_at.isoformat(),
            "deal_id": deal.json()["id"],
            "contact_id": contact.json()["id"],
            "company_id": company.json()["id"],
        },
    )
    assert updated.status_code == 200, updated.text
    updated_task = updated.json()
    assert updated_task["title"] == "Updated task"
    assert updated_task["task_type"] == "meeting"
    assert updated_task["deal_id"] == deal.json()["id"]
    assert updated_task["contact_id"] == contact.json()["id"]
    assert updated_task["company_id"] == company.json()["id"]
    assert updated_task["version"] == task["version"] + 1

    async with SessionLocal() as db:
        purchase_schedule = PurchaseSchedule(
            workspace_id=as_uuid(owner_auth["workspace"]["id"]),
            deal_id=as_uuid(deal.json()["id"]),
            contact_id=as_uuid(contact.json()["id"]),
            assignee_id=as_uuid(owner_id),
            task_id=as_uuid(task["id"]),
            scheduled_for=new_due_at,
            remind_at=new_remind_at,
        )
        db.add(purchase_schedule)
        await db.flush()
        reminder_event = OutboxEvent(
            workspace_id=purchase_schedule.workspace_id,
            event_type="purchase.due_soon",
            aggregate_type="purchase_schedule",
            aggregate_id=purchase_schedule.id,
            payload={"task_id": task["id"]},
            available_at=new_remind_at,
        )
        db.add(reminder_event)
        await db.commit()
        purchase_schedule_id = purchase_schedule.id
        reminder_event_id = reminder_event.id

    invalid_reminder = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        headers=headers,
        json={
            "expected_version": updated_task["version"],
            "remind_at": (new_due_at + timedelta(minutes=1)).isoformat(),
        },
    )
    assert invalid_reminder.status_code == 422

    for field in ("title", "task_type", "due_at", "assignee_id", "status"):
        invalid_null = await client.patch(
            f"/api/v1/tasks/{task['id']}",
            headers=headers,
            json={"expected_version": updated_task["version"], field: None},
        )
        assert invalid_null.status_code == 422, (field, invalid_null.text)

    stale_delete = await client.delete(
        f"/api/v1/tasks/{task['id']}",
        headers=headers,
        params={"expected_version": task["version"]},
    )
    assert stale_delete.status_code == 409
    assert stale_delete.json()["detail"]["code"] == "version_conflict"

    deleted = await client.delete(
        f"/api/v1/tasks/{task['id']}",
        headers=headers,
        params={"expected_version": updated_task["version"]},
    )
    assert deleted.status_code == 204, deleted.text
    tasks = await client.get(
        "/api/v1/tasks", params={"search": "Updated task", "include_completed": True}
    )
    assert tasks.status_code == 200
    assert tasks.json()["items"] == []

    task_id = as_uuid(task["id"])
    async with SessionLocal() as db:
        assert await db.get(Task, task_id) is None
        cancelled_schedule = await db.get(PurchaseSchedule, purchase_schedule_id)
        assert cancelled_schedule is not None
        assert cancelled_schedule.status is PurchaseScheduleStatus.cancelled
        assert cancelled_schedule.task_id is None
        assert cancelled_schedule.completed_at is not None
        cancelled_reminder = await db.get(OutboxEvent, reminder_event_id)
        assert cancelled_reminder is not None
        assert cancelled_reminder.processed_at is not None
        assert (
            await db.scalar(
                sa.select(sa.func.count())
                .select_from(ActivityEvent)
                .where(
                    ActivityEvent.entity_id == task_id,
                    ActivityEvent.event_type == "task.deleted",
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_contact_and_task_mutations_are_workspace_scoped(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    owner_id = as_uuid(owner_auth["user"]["id"])
    local_task = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Local task",
            "due_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "assignee_id": str(owner_id),
        },
    )
    assert local_task.status_code == 201, local_task.text
    async with SessionLocal() as db:
        other_workspace = Workspace(name="Other workspace", slug="other-workspace")
        db.add(other_workspace)
        await db.flush()
        other_contact = Contact(
            workspace_id=other_workspace.id,
            first_name="Other",
        )
        other_task = Task(
            workspace_id=other_workspace.id,
            title="Other task",
            due_at=datetime.now(UTC) + timedelta(days=1),
            assignee_id=owner_id,
        )
        db.add_all([other_contact, other_task])
        await db.commit()
        other_contact_id = other_contact.id
        other_task_id = other_task.id

    for method, path, kwargs in (
        (
            client.patch,
            f"/api/v1/contacts/{other_contact_id}",
            {"json": {"expected_version": 1, "first_name": "Leaked"}},
        ),
        (
            client.delete,
            f"/api/v1/contacts/{other_contact_id}",
            {"params": {"expected_version": 1}},
        ),
        (
            client.patch,
            f"/api/v1/tasks/{other_task_id}",
            {"json": {"expected_version": 1, "title": "Leaked"}},
        ),
        (
            client.delete,
            f"/api/v1/tasks/{other_task_id}",
            {"params": {"expected_version": 1}},
        ),
    ):
        response = await method(path, headers=headers, **kwargs)
        assert response.status_code == 404, response.text

    foreign_relation = await client.patch(
        f"/api/v1/tasks/{local_task.json()['id']}",
        headers=headers,
        json={
            "expected_version": local_task.json()["version"],
            "contact_id": str(other_contact_id),
        },
    )
    assert foreign_relation.status_code == 404

    async with SessionLocal() as db:
        stored_contact = await db.get(Contact, other_contact_id)
        stored_task = await db.get(Task, other_task_id)
        assert stored_contact is not None and stored_contact.deleted_at is None
        assert stored_contact.first_name == "Other"
        assert stored_task is not None and stored_task.title == "Other task"
        stored_local_task = await db.get(Task, as_uuid(local_task.json()["id"]))
        assert stored_local_task is not None and stored_local_task.contact_id is None
        assert stored_local_task.version == local_task.json()["version"]


@pytest.mark.asyncio
async def test_task_list_hides_closed_before_cursor_pagination(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    owner_id = (await client.get("/api/v1/users")).json()[0]["id"]
    created: dict[str, dict[str, object]] = {}
    for title, due_at in (
        ("Past open task", datetime.now(UTC) - timedelta(days=2)),
        ("Today open task", datetime.now(UTC)),
        ("Future open task", datetime.now(UTC) + timedelta(days=2)),
        ("Today completed task", datetime.now(UTC)),
        ("Today cancelled task", datetime.now(UTC)),
    ):
        response = await client.post(
            "/api/v1/tasks",
            headers=headers,
            json={"title": title, "due_at": due_at.isoformat(), "assignee_id": owner_id},
        )
        assert response.status_code == 201, response.text
        created[title] = response.json()

    completed = created["Today completed task"]
    response = await client.patch(
        f"/api/v1/tasks/{completed['id']}",
        headers=headers,
        json={"expected_version": completed["version"], "status": "completed"},
    )
    assert response.status_code == 200, response.text

    cancelled = created["Today cancelled task"]
    response = await client.patch(
        f"/api/v1/tasks/{cancelled['id']}",
        headers=headers,
        json={"expected_version": cancelled["version"], "status": "cancelled"},
    )
    assert response.status_code == 200, response.text

    first = await client.get("/api/v1/tasks", params={"limit": 2})
    assert first.status_code == 200, first.text
    assert len(first.json()["items"]) == 2
    assert first.json()["next_cursor"]
    assert all(item["status"] == "open" for item in first.json()["items"])

    second = await client.get(
        "/api/v1/tasks",
        params={"limit": 2, "cursor": first.json()["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    assert len(second.json()["items"]) == 1
    assert second.json()["next_cursor"] is None

    visible_titles = {
        item["title"] for item in [*first.json()["items"], *second.json()["items"]]
    }
    assert visible_titles == {"Past open task", "Today open task", "Future open task"}

    with_completed = await client.get(
        "/api/v1/tasks", params={"limit": 25, "include_completed": True}
    )
    assert with_completed.status_code == 200, with_completed.text
    assert {item["title"] for item in with_completed.json()["items"]} == set(created)

    completed_only = await client.get(
        "/api/v1/tasks", params={"status": "completed"}
    )
    assert completed_only.status_code == 200, completed_only.text
    assert [item["title"] for item in completed_only.json()["items"]] == [
        "Today completed task"
    ]

    cancelled_only = await client.get("/api/v1/tasks", params={"status": "cancelled"})
    assert cancelled_only.status_code == 200, cancelled_only.text
    assert [item["title"] for item in cancelled_only.json()["items"]] == [
        "Today cancelled task"
    ]


@pytest.mark.asyncio
async def test_task_list_defaults_to_pages_of_25(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    workspace_id = as_uuid(owner_auth["workspace"]["id"])
    owner_id = as_uuid(owner_auth["user"]["id"])
    async with SessionLocal() as db:
        db.add_all(
            [
                Task(
                    workspace_id=workspace_id,
                    title=f"Default page task {index}",
                    due_at=datetime.now(UTC) + timedelta(days=1),
                    assignee_id=owner_id,
                )
                for index in range(26)
            ]
        )
        await db.commit()

    first = await client.get("/api/v1/tasks")
    assert first.status_code == 200, first.text
    assert len(first.json()["items"]) == 25
    assert first.json()["next_cursor"]

    second = await client.get(
        "/api/v1/tasks", params={"cursor": first.json()["next_cursor"]}
    )
    assert second.status_code == 200, second.text
    assert len(second.json()["items"]) == 1
    assert second.json()["next_cursor"] is None

    searched = await client.get("/api/v1/tasks", params={"search": "task 25"})
    assert searched.status_code == 200, searched.text
    assert [item["title"] for item in searched.json()["items"]] == [
        "Default page task 25"
    ]


@pytest.mark.asyncio
async def test_task_scopes_filter_by_workspace_day_before_pagination(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    owner_id = (await client.get("/api/v1/users")).json()[0]["id"]
    for title, due_at in (
        ("Past task", datetime.now(UTC) - timedelta(days=2)),
        ("Today task", datetime.now(UTC)),
        ("Upcoming task", datetime.now(UTC) + timedelta(days=2)),
    ):
        response = await client.post(
            "/api/v1/tasks",
            headers=headers,
            json={"title": title, "due_at": due_at.isoformat(), "assignee_id": owner_id},
        )
        assert response.status_code == 201, response.text

    expected_by_scope = {
        "today": {"Today task"},
        "overdue": {"Past task", "Today task"},
        "upcoming": {"Upcoming task"},
    }
    for scope, expected_titles in expected_by_scope.items():
        response = await client.get(
            "/api/v1/tasks", params={"scope": scope, "limit": 25}
        )
        assert response.status_code == 200, response.text
        assert {item["title"] for item in response.json()["items"]} == expected_titles
        assert response.json()["next_cursor"] is None

    async with SessionLocal() as db:
        await db.execute(
            sa.update(Workspace)
            .where(Workspace.id == as_uuid(owner_auth["workspace"]["id"]))
            .values(timezone="Invalid/Timezone")
        )
        await db.commit()
    invalid_timezone = await client.get("/api/v1/tasks", params={"scope": "today"})
    assert invalid_timezone.status_code == 200, invalid_timezone.text


@pytest.mark.asyncio
async def test_company_and_contact_lists_support_cursor_pagination(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    company_ids: set[str] = set()
    contact_ids: set[str] = set()
    for index in range(3):
        company = await client.post(
            "/api/v1/companies",
            headers=headers,
            json={"name": f"Cursor test company {index}"},
        )
        contact = await client.post(
            "/api/v1/contacts",
            headers=headers,
            json={"first_name": "Cursor", "last_name": f"Test contact {index}"},
        )
        assert company.status_code == 201, company.text
        assert contact.status_code == 201, contact.text
        company_ids.add(company.json()["id"])
        contact_ids.add(contact.json()["id"])

    for collection, expected_ids in (
        ("companies", company_ids),
        ("contacts", contact_ids),
    ):
        first = await client.get(
            f"/api/v1/{collection}", params={"limit": 2, "search": "Cursor"}
        )
        assert first.status_code == 200, first.text
        assert len(first.json()["items"]) == 2
        assert first.json()["next_cursor"]

        second = await client.get(
            f"/api/v1/{collection}",
            params={
                "limit": 2,
                "search": "Cursor",
                "cursor": first.json()["next_cursor"],
            },
        )
        assert second.status_code == 200, second.text
        assert len(second.json()["items"]) == 1
        assert second.json()["next_cursor"] is None

        listed_ids = {
            item["id"] for item in [*first.json()["items"], *second.json()["items"]]
        }
        assert listed_ids == expected_ids


@pytest.mark.asyncio
async def test_deal_tags_round_trip_through_create_list_get_and_update(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    pipeline = (await client.get("/api/v1/pipelines")).json()[0]
    deal = await client.post(
        "/api/v1/deals",
        headers=headers,
        json={
            "title": "Tagged deal",
            "pipeline_id": pipeline["id"],
            "stage_id": pipeline["stages"][0]["id"],
            "tags": ["vip", "repeat"],
        },
    )
    assert deal.status_code == 201, deal.text
    assert deal.json()["tags"] == ["vip", "repeat"]

    deal_id = deal.json()["id"]
    loaded = await client.get(f"/api/v1/deals/{deal_id}")
    listed = await client.get("/api/v1/deals", params={"pipeline_id": pipeline["id"]})
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["tags"] == ["vip", "repeat"]
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["tags"] == ["vip", "repeat"]

    updated = await client.patch(
        f"/api/v1/deals/{deal_id}",
        headers=headers,
        json={"expected_version": 1, "tags": ["priority"]},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["tags"] == ["priority"]


@pytest.mark.asyncio
async def test_deal_assignee_update_is_versioned_and_workspace_scoped(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    workspace_id = as_uuid(owner_auth["workspace"]["id"])
    assignee = User(
        email="deal-assignee@example.com",
        full_name="Deal Assignee",
        password_hash="not-used",
    )
    outsider = User(
        email="deal-outsider@example.com",
        full_name="Deal Outsider",
        password_hash="not-used",
    )
    async with SessionLocal() as db:
        db.add_all([assignee, outsider])
        await db.flush()
        db.add(
            Membership(
                workspace_id=workspace_id,
                user_id=assignee.id,
                role=Role.manager,
            )
        )
        await db.commit()
        assignee_id = assignee.id
        outsider_id = outsider.id

    pipeline = (await client.get("/api/v1/pipelines")).json()[0]
    created = await client.post(
        "/api/v1/deals",
        headers=headers,
        json={
            "title": "Assigned deal",
            "pipeline_id": pipeline["id"],
            "stage_id": pipeline["stages"][0]["id"],
            "next_purchase_at": (datetime.now(UTC) + timedelta(days=10)).isoformat(),
        },
    )
    assert created.status_code == 201, created.text
    deal = created.json()
    deal_id = as_uuid(deal["id"])
    async with SessionLocal() as db:
        original_schedule = await db.scalar(
            sa.select(PurchaseSchedule).where(
                PurchaseSchedule.workspace_id == workspace_id,
                PurchaseSchedule.deal_id == deal_id,
                PurchaseSchedule.status == PurchaseScheduleStatus.active,
            )
        )
        assert original_schedule is not None
        original_schedule_id = original_schedule.id
        materialized = await ensure_purchase_task(
            db,
            workspace_id=workspace_id,
            schedule_id=original_schedule.id,
        )
        materialized_task_id = materialized.task.id
        materialized_task_version = materialized.task.version
        materialized_task_updated_at = materialized.task.updated_at
        await db.commit()

    assigned = await client.patch(
        f"/api/v1/deals/{deal['id']}",
        headers=headers,
        json={"expected_version": deal["version"], "assignee_id": str(assignee_id)},
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["assignee_id"] == str(assignee_id)
    assert assigned.json()["version"] == deal["version"] + 1

    rejected = await client.patch(
        f"/api/v1/deals/{deal['id']}",
        headers=headers,
        json={
            "expected_version": assigned.json()["version"],
            "assignee_id": str(outsider_id),
        },
    )
    assert rejected.status_code == 404

    async with SessionLocal() as db:
        stored = await db.get(Deal, deal_id)
        assert stored is not None
        assert stored.assignee_id == assignee_id
        assert stored.version == assigned.json()["version"]
        schedule = await db.scalar(
            sa.select(PurchaseSchedule).where(
                PurchaseSchedule.workspace_id == workspace_id,
                PurchaseSchedule.deal_id == deal_id,
                PurchaseSchedule.status == PurchaseScheduleStatus.active,
            )
        )
        assert schedule is not None
        assert schedule.id == original_schedule_id
        assert schedule.assignee_id == assignee_id
        materialized_task = await db.get(Task, materialized_task_id)
        assert materialized_task is not None
        assert materialized_task.status is TaskStatus.open
        assert materialized_task.assignee_id == assignee_id
        assert materialized_task.version == materialized_task_version + 1
        assert materialized_task.updated_at != materialized_task_updated_at
        assigned_event = await db.scalar(
            sa.select(ActivityEvent).where(
                ActivityEvent.workspace_id == workspace_id,
                ActivityEvent.entity_id == deal_id,
                ActivityEvent.event_type == "deal.assigned",
            )
        )
        assert assigned_event is not None
        assert assigned_event.payload["assignee_id"] == str(assignee_id)
        task_event = await db.scalar(
            sa.select(ActivityEvent).where(
                ActivityEvent.workspace_id == workspace_id,
                ActivityEvent.entity_id == materialized_task_id,
                ActivityEvent.event_type == "task.updated",
            )
        )
        assert task_event is not None
        assert task_event.payload == {
            "fields": ["assignee_id"],
            "deal_id": str(deal_id),
        }
        assert (
            await db.scalar(
                sa.select(sa.func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.workspace_id == workspace_id,
                    OutboxEvent.aggregate_id == materialized_task_id,
                    OutboxEvent.event_type == "task.updated",
                )
            )
            == 1
        )
        task_realtime = await db.scalar(
            sa.select(RealtimeEvent).where(
                RealtimeEvent.workspace_id == workspace_id,
                RealtimeEvent.event_type == "task.updated",
            )
        )
        assert task_realtime is not None
        assert task_realtime.payload["entity_id"] == str(materialized_task_id)


@pytest.mark.asyncio
async def test_deal_soft_delete_is_versioned_and_cancels_purchase_schedule(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    workspace_id = as_uuid(owner_auth["workspace"]["id"])
    owner_id = as_uuid(owner_auth["user"]["id"])
    pipeline = (await client.get("/api/v1/pipelines")).json()[0]
    created = await client.post(
        "/api/v1/deals",
        headers=headers,
        json={
            "title": "Disposable deal",
            "pipeline_id": pipeline["id"],
            "stage_id": pipeline["stages"][0]["id"],
            "next_purchase_at": (datetime.now(UTC) + timedelta(days=10)).isoformat(),
        },
    )
    assert created.status_code == 201, created.text
    deal = created.json()
    deal_id = as_uuid(deal["id"])

    async with SessionLocal() as db:
        schedule = await db.scalar(
            sa.select(PurchaseSchedule).where(
                PurchaseSchedule.workspace_id == workspace_id,
                PurchaseSchedule.deal_id == deal_id,
                PurchaseSchedule.status == PurchaseScheduleStatus.active,
            )
        )
        assert schedule is not None
        schedule_id = schedule.id
        materialized = await ensure_purchase_task(
            db,
            workspace_id=workspace_id,
            schedule_id=schedule.id,
        )
        task = materialized.task
        task_id = task.id
        task_version = task.version
        task_updated_at = task.updated_at
        reminder = OutboxEvent(
            workspace_id=workspace_id,
            event_type="purchase.due_soon",
            aggregate_type="purchase_schedule",
            aggregate_id=schedule.id,
            payload={"deal_id": str(deal_id)},
        )
        pending_task_due = OutboxEvent(
            workspace_id=workspace_id,
            event_type="task.due_soon",
            aggregate_type="task",
            aggregate_id=task.id,
            payload={"task_id": str(task.id)},
        )
        pending_task_overdue = OutboxEvent(
            workspace_id=workspace_id,
            event_type="task.overdue",
            aggregate_type="task",
            aggregate_id=task.id,
            payload={"task_id": str(task.id)},
        )
        expanded_task_due = OutboxEvent(
            workspace_id=workspace_id,
            event_type="task.due_soon",
            aggregate_type="task",
            aggregate_id=task.id,
            payload={"task_id": str(task.id)},
            processed_at=datetime.now(UTC),
        )
        db.add_all([reminder, pending_task_due, pending_task_overdue, expanded_task_due])
        await db.flush()
        notification = NotificationDelivery(
            workspace_id=workspace_id,
            audience=NotificationAudience.employee,
            channel="in_app",
            recipient_id=owner_id,
            recipient_address=str(owner_id),
            subject="Task reminder",
            body="Task is due",
            status=DeliveryStatus.pending,
            dedupe_key=f"rule:test:event:{expanded_task_due.id}:recipient:0",
            scheduled_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add(notification)
        await db.flush()
        notification_outbox = OutboxEvent(
            workspace_id=workspace_id,
            event_type="notification.delivery.queued",
            aggregate_type="notification_delivery",
            aggregate_id=notification.id,
            payload={"delivery_id": str(notification.id)},
        )
        db.add(notification_outbox)
        await db.flush()
        jobs = [
            BackgroundJob(
                workspace_id=workspace_id,
                job_type="monitor.task.due",
                payload={"task_id": str(task.id)},
                status=JobStatus.queued,
                dedupe_key=f"monitor.task.due:{task.id}:{task.remind_at.isoformat()}",
            ),
            BackgroundJob(
                workspace_id=workspace_id,
                job_type="monitor.task.overdue",
                payload={"task_id": str(task.id)},
                status=JobStatus.queued,
                dedupe_key=f"monitor.task.overdue:{task.id}:{task.due_at.isoformat()}",
            ),
            BackgroundJob(
                workspace_id=workspace_id,
                job_type="notification.expand",
                payload={"outbox_event_id": str(expanded_task_due.id)},
                status=JobStatus.queued,
                dedupe_key=f"outbox:{expanded_task_due.id}:notifications",
            ),
            BackgroundJob(
                workspace_id=workspace_id,
                job_type="notification.deliver",
                payload={"notification_delivery_id": str(notification.id)},
                status=JobStatus.queued,
                dedupe_key=f"notification-delivery:{notification.id}:send",
            ),
            BackgroundJob(
                workspace_id=workspace_id,
                job_type="outbox.dispatch",
                payload={"outbox_event_id": str(notification_outbox.id)},
                status=JobStatus.queued,
                dedupe_key=f"outbox:{notification_outbox.id}:dispatch",
            ),
        ]
        db.add_all(jobs)
        await db.commit()
        reminder_id = reminder.id
        pending_task_event_ids = [pending_task_due.id, pending_task_overdue.id]
        notification_id = notification.id
        notification_outbox_id = notification_outbox.id
        job_ids = [job.id for job in jobs]

    stale = await client.delete(
        f"/api/v1/deals/{deal['id']}",
        headers=headers,
        params={"expected_version": deal["version"] + 1},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "version_conflict"

    deleted = await client.delete(
        f"/api/v1/deals/{deal['id']}",
        headers=headers,
        params={"expected_version": deal["version"]},
    )
    assert deleted.status_code == 204, deleted.text
    assert (await client.get(f"/api/v1/deals/{deal['id']}")).status_code == 404
    listed = await client.get("/api/v1/deals", params={"pipeline_id": pipeline["id"]})
    assert listed.status_code == 200
    assert deal["id"] not in {item["id"] for item in listed.json()["items"]}
    repeated = await client.delete(
        f"/api/v1/deals/{deal['id']}",
        headers=headers,
        params={"expected_version": deal["version"] + 1},
    )
    assert repeated.status_code == 404

    async with SessionLocal() as db:
        stored = await db.get(Deal, deal_id)
        assert stored is not None
        assert stored.deleted_at is not None
        assert stored.version == deal["version"] + 1
        cancelled_schedule = await db.get(PurchaseSchedule, schedule_id)
        assert cancelled_schedule is not None
        assert cancelled_schedule.status is PurchaseScheduleStatus.cancelled
        assert cancelled_schedule.completed_at is not None
        cancelled_task = await db.get(Task, task_id)
        assert cancelled_task is not None
        assert cancelled_task.status is TaskStatus.cancelled
        assert cancelled_task.completed_at is None
        assert cancelled_task.version == task_version + 1
        assert cancelled_task.updated_at != task_updated_at
        processed_reminder = await db.get(OutboxEvent, reminder_id)
        assert processed_reminder is not None
        assert processed_reminder.processed_at is not None
        pending_task_events = list(
            (
                await db.scalars(
                    sa.select(OutboxEvent).where(OutboxEvent.id.in_(pending_task_event_ids))
                )
            ).all()
        )
        assert len(pending_task_events) == 2
        assert all(event.processed_at is not None for event in pending_task_events)
        cancelled_notification = await db.get(NotificationDelivery, notification_id)
        assert cancelled_notification is not None
        assert cancelled_notification.status is DeliveryStatus.failed
        assert cancelled_notification.last_error == (
            "source task cancelled because its deal was deleted"
        )
        processed_notification_outbox = await db.get(OutboxEvent, notification_outbox_id)
        assert processed_notification_outbox is not None
        assert processed_notification_outbox.processed_at is not None
        cancelled_jobs = list(
            (
                await db.scalars(sa.select(BackgroundJob).where(BackgroundJob.id.in_(job_ids)))
            ).all()
        )
        assert len(cancelled_jobs) == len(job_ids)
        assert all(job.status is JobStatus.succeeded for job in cancelled_jobs)
        task_cancelled_event = await db.scalar(
            sa.select(ActivityEvent).where(
                ActivityEvent.workspace_id == workspace_id,
                ActivityEvent.entity_id == task_id,
                ActivityEvent.event_type == "task.updated",
            )
        )
        assert task_cancelled_event is not None
        assert task_cancelled_event.payload == {
            "fields": ["status"],
            "deal_id": str(deal_id),
        }
        assert (
            await db.scalar(
                sa.select(sa.func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.workspace_id == workspace_id,
                    OutboxEvent.aggregate_id == task_id,
                    OutboxEvent.event_type == "task.updated",
                )
            )
            == 1
        )
        task_realtime = await db.scalar(
            sa.select(RealtimeEvent).where(
                RealtimeEvent.workspace_id == workspace_id,
                RealtimeEvent.event_type == "task.updated",
            )
        )
        assert task_realtime is not None
        assert task_realtime.payload["entity_id"] == str(task_id)
        assert (
            await db.scalar(
                sa.select(sa.func.count())
                .select_from(ActivityEvent)
                .where(
                    ActivityEvent.workspace_id == workspace_id,
                    ActivityEvent.entity_id == deal_id,
                    ActivityEvent.event_type == "deal.deleted",
                )
            )
            == 1
        )
        assert (
            await db.scalar(
                sa.select(sa.func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.workspace_id == workspace_id,
                    OutboxEvent.aggregate_id == deal_id,
                    OutboxEvent.event_type == "deal.deleted",
                )
            )
            == 1
        )
        assert (
            await db.scalar(
                sa.select(sa.func.count())
                .select_from(RealtimeEvent)
                .where(
                    RealtimeEvent.workspace_id == workspace_id,
                    RealtimeEvent.event_type == "deal.deleted",
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_deal_delete_is_workspace_scoped(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    async with SessionLocal() as db:
        other_workspace = Workspace(name="Other deal workspace", slug="other-deal-workspace")
        db.add(other_workspace)
        await db.flush()
        other_pipeline = Pipeline(
            workspace_id=other_workspace.id,
            name="Other pipeline",
            position=0,
        )
        db.add(other_pipeline)
        await db.flush()
        other_stage = Stage(
            workspace_id=other_workspace.id,
            pipeline_id=other_pipeline.id,
            name="Other stage",
            position=0,
        )
        db.add(other_stage)
        await db.flush()
        other_deal = Deal(
            workspace_id=other_workspace.id,
            pipeline_id=other_pipeline.id,
            stage_id=other_stage.id,
            title="Other deal",
        )
        db.add(other_deal)
        await db.commit()
        other_deal_id = other_deal.id

    response = await client.delete(
        f"/api/v1/deals/{other_deal_id}",
        headers=headers,
        params={"expected_version": 1},
    )
    assert response.status_code == 404

    async with SessionLocal() as db:
        stored = await db.get(Deal, other_deal_id)
        assert stored is not None
        assert stored.deleted_at is None
        assert stored.version == 1


@pytest.mark.asyncio
async def test_company_contact_and_deal_search_include_tags(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    company = await client.post(
        "/api/v1/companies",
        headers=headers,
        json={"name": "Northwind", "tags": ["Company-only-segment"]},
    )
    assert company.status_code == 201, company.text

    contact = await client.post(
        "/api/v1/contacts",
        headers=headers,
        json={"first_name": "Test", "last_name": "Person", "tags": ["Contact-only-segment"]},
    )
    assert contact.status_code == 201, contact.text

    pipeline = (await client.get("/api/v1/pipelines")).json()[0]
    sources = (await client.get("/api/v1/sources")).json()
    html_form_source = next(source for source in sources if source["key"] == "html_form")
    deal = await client.post(
        "/api/v1/deals",
        headers=headers,
        json={
            "title": "Synthetic order",
            "pipeline_id": pipeline["id"],
            "stage_id": pipeline["stages"][0]["id"],
            "source_id": html_form_source["id"],
            "tags": ["Deal-only-segment"],
            "custom_fields": {"subtitle": "Rare catalog request"},
        },
    )
    assert deal.status_code == 201, deal.text

    companies = await client.get("/api/v1/companies", params={"search": "company-ONLY"})
    contacts = await client.get("/api/v1/contacts", params={"search": "contact-ONLY"})
    deals = await client.get("/api/v1/deals", params={"search": "deal-ONLY"})
    deals_by_description = await client.get(
        "/api/v1/deals", params={"search": "catalog request"}
    )
    deals_by_source_key = await client.get("/api/v1/deals", params={"search": "html_form"})
    deals_by_source_name = await client.get("/api/v1/deals", params={"search": "HTML-форма"})

    assert [item["id"] for item in companies.json()["items"]] == [company.json()["id"]]
    assert [item["id"] for item in contacts.json()["items"]] == [contact.json()["id"]]
    assert [item["id"] for item in deals.json()["items"]] == [deal.json()["id"]]
    assert [item["id"] for item in deals_by_description.json()["items"]] == [deal.json()["id"]]
    assert [item["id"] for item in deals_by_source_key.json()["items"]] == [deal.json()["id"]]
    assert [item["id"] for item in deals_by_source_name.json()["items"]] == [deal.json()["id"]]


@pytest.mark.asyncio
async def test_required_fields_block_stage_transition_and_versions_conflict(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    pipeline_response = await client.get("/api/v1/pipelines")
    pipeline = pipeline_response.json()[0]
    initial_stage, target_stage = pipeline["stages"][:2]

    custom_field = await client.post(
        "/api/v1/custom-fields",
        headers=headers,
        json={
            "entity_type": "deal",
            "key": "order_number",
            "name": "Номер заказа",
            "field_type": "text",
        },
    )
    assert custom_field.status_code == 201, custom_field.text
    required = await client.put(
        f"/api/v1/stages/{target_stage['id']}/required-fields",
        headers=headers,
        json={"fields": [{"field_definition_id": custom_field.json()["id"]}]},
    )
    assert required.status_code == 200, required.text
    loaded_required = await client.get(
        f"/api/v1/stages/{target_stage['id']}/required-fields"
    )
    assert loaded_required.status_code == 200, loaded_required.text
    assert loaded_required.json() == required.json()

    deal = await client.post(
        "/api/v1/deals",
        headers=headers,
        json={
            "title": "First deal",
            "pipeline_id": pipeline["id"],
            "stage_id": initial_stage["id"],
        },
    )
    assert deal.status_code == 201, deal.text
    deal_id = deal.json()["id"]

    blocked = await client.patch(
        f"/api/v1/deals/{deal_id}/stage",
        headers=headers,
        json={"target_stage_id": target_stage["id"], "expected_version": 1},
    )
    assert blocked.status_code == 422
    assert blocked.json()["detail"]["code"] == "missing_required_fields"
    assert blocked.json()["detail"]["fields"][0]["key"] == "order_number"

    filled = await client.patch(
        f"/api/v1/deals/{deal_id}",
        headers=headers,
        json={"expected_version": 1, "custom_fields": {"order_number": "A-001"}},
    )
    assert filled.status_code == 200, filled.text
    assert filled.json()["version"] == 2

    moved = await client.patch(
        f"/api/v1/deals/{deal_id}/stage",
        headers=headers,
        json={"target_stage_id": target_stage["id"], "expected_version": 2},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["stage_id"] == target_stage["id"]
    assert moved.json()["version"] == 3

    stale = await client.patch(
        f"/api/v1/deals/{deal_id}",
        headers=headers,
        json={"expected_version": 1, "title": "stale update"},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "version_conflict"

    activity = await client.get(
        "/api/v1/activity", params={"entity_type": "deal", "entity_id": deal_id}
    )
    assert activity.status_code == 200
    assert {item["event_type"] for item in activity.json()["items"]} >= {
        "deal.created",
        "deal.updated",
        "deal.stage_changed",
    }


@pytest.mark.asyncio
async def test_custom_field_delete_hides_definition_and_keeps_deal_values(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    pipeline = (await client.get("/api/v1/pipelines")).json()[0]
    initial_stage, target_stage = pipeline["stages"][:2]
    custom_field = await client.post(
        "/api/v1/custom-fields",
        headers=headers,
        json={
            "entity_type": "deal",
            "key": "legacy_reference",
            "name": "Архивный номер",
            "field_type": "text",
        },
    )
    assert custom_field.status_code == 201, custom_field.text
    field_id = custom_field.json()["id"]

    required = await client.put(
        f"/api/v1/stages/{target_stage['id']}/required-fields",
        headers=headers,
        json={"fields": [{"field_definition_id": field_id}]},
    )
    assert required.status_code == 200, required.text
    deal = await client.post(
        "/api/v1/deals",
        headers=headers,
        json={
            "title": "Deal retaining deleted field value",
            "pipeline_id": pipeline["id"],
            "stage_id": initial_stage["id"],
            "custom_fields": {"legacy_reference": "ARCH-42"},
        },
    )
    assert deal.status_code == 201, deal.text

    deleted = await client.delete(f"/api/v1/custom-fields/{field_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text

    definitions = await client.get("/api/v1/custom-fields", params={"entity_type": "deal"})
    assert definitions.status_code == 200, definitions.text
    assert field_id not in {item["id"] for item in definitions.json()}
    remaining_required = await client.get(
        f"/api/v1/stages/{target_stage['id']}/required-fields"
    )
    assert remaining_required.status_code == 200, remaining_required.text
    assert remaining_required.json() == []
    loaded_deal = await client.get(f"/api/v1/deals/{deal.json()['id']}")
    assert loaded_deal.status_code == 200, loaded_deal.text
    assert loaded_deal.json()["custom_fields"]["legacy_reference"] == "ARCH-42"

    repeated = await client.delete(f"/api/v1/custom-fields/{field_id}", headers=headers)
    assert repeated.status_code == 404


@pytest.mark.asyncio
async def test_inactive_required_custom_field_does_not_block_stage_transition(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    pipeline = (await client.get("/api/v1/pipelines")).json()[0]
    initial_stage, target_stage = pipeline["stages"][:2]
    custom_field = await client.post(
        "/api/v1/custom-fields",
        headers=headers,
        json={
            "entity_type": "deal",
            "key": "inactive_requirement",
            "name": "Неактивное обязательное поле",
            "field_type": "text",
        },
    )
    assert custom_field.status_code == 201, custom_field.text
    required = await client.put(
        f"/api/v1/stages/{target_stage['id']}/required-fields",
        headers=headers,
        json={"fields": [{"field_definition_id": custom_field.json()["id"]}]},
    )
    assert required.status_code == 200, required.text
    deal = await client.post(
        "/api/v1/deals",
        headers=headers,
        json={
            "title": "Deal with stale inactive requirement",
            "pipeline_id": pipeline["id"],
            "stage_id": initial_stage["id"],
        },
    )
    assert deal.status_code == 201, deal.text

    async with SessionLocal() as db:
        await db.execute(
            sa.update(CustomFieldDefinition)
            .where(CustomFieldDefinition.id == as_uuid(custom_field.json()["id"]))
            .values(is_active=False)
        )
        await db.commit()

    rejected_requirement = await client.put(
        f"/api/v1/stages/{target_stage['id']}/required-fields",
        headers=headers,
        json={"fields": [{"field_definition_id": custom_field.json()["id"]}]},
    )
    assert rejected_requirement.status_code == 422, rejected_requirement.text

    moved = await client.patch(
        f"/api/v1/deals/{deal.json()['id']}/stage",
        headers=headers,
        json={"target_stage_id": target_stage["id"], "expected_version": 1},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["stage_id"] == target_stage["id"]


@pytest.mark.asyncio
async def test_contact_and_next_purchase_can_be_required_and_filled(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    pipeline = (await client.get("/api/v1/pipelines")).json()[0]
    initial_stage, target_stage = pipeline["stages"][:2]
    required = await client.put(
        f"/api/v1/stages/{target_stage['id']}/required-fields",
        headers=headers,
        json={
            "fields": [
                {"built_in_key": "contact_ids"},
                {"built_in_key": "next_purchase_at"},
            ]
        },
    )
    assert required.status_code == 200, required.text

    deal = await client.post(
        "/api/v1/deals",
        headers=headers,
        json={
            "title": "Repeat order",
            "pipeline_id": pipeline["id"],
            "stage_id": initial_stage["id"],
            "amount": 12500,
        },
    )
    assert deal.status_code == 201, deal.text
    deal_id = deal.json()["id"]

    blocked = await client.patch(
        f"/api/v1/deals/{deal_id}/stage",
        headers=headers,
        json={"target_stage_id": target_stage["id"], "expected_version": 1},
    )
    assert blocked.status_code == 422, blocked.text
    assert {field["key"] for field in blocked.json()["detail"]["fields"]} == {
        "contact_ids",
        "next_purchase_at",
    }

    contact = await client.post(
        "/api/v1/contacts",
        headers=headers,
        json={"first_name": "Анна", "last_name": "Орлова"},
    )
    assert contact.status_code == 201, contact.text
    updated = await client.patch(
        f"/api/v1/deals/{deal_id}",
        headers=headers,
        json={
            "expected_version": 1,
            "contact_ids": [contact.json()["id"]],
            "next_purchase_at": "2026-10-15T09:00:00+05:00",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["contact_ids"] == [contact.json()["id"]]
    assert updated.json()["primary_contact"]["first_name"] == "Анна"

    moved = await client.patch(
        f"/api/v1/deals/{deal_id}/stage",
        headers=headers,
        json={"target_stage_id": target_stage["id"], "expected_version": 2},
    )
    assert moved.status_code == 200, moved.text

    won_stage = next(stage for stage in pipeline["stages"] if stage["stage_type"] == "won")
    won = await client.patch(
        f"/api/v1/deals/{deal_id}/stage",
        headers=headers,
        json={"target_stage_id": won_stage["id"], "expected_version": 3},
    )
    assert won.status_code == 200, won.text
    purchases = await client.get(
        f"/api/v1/contacts/{contact.json()['id']}/purchases"
    )
    assert purchases.status_code == 200, purchases.text
    assert [item["id"] for item in purchases.json()["items"]] == [deal_id]


@pytest.mark.asyncio
async def test_notes_are_appended_to_entity_activity(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    pipeline = (await client.get("/api/v1/pipelines")).json()[0]
    deal = await client.post(
        "/api/v1/deals",
        headers=headers,
        json={
            "title": "Deal with a note",
            "pipeline_id": pipeline["id"],
            "stage_id": pipeline["stages"][0]["id"],
        },
    )
    assert deal.status_code == 201, deal.text
    note = await client.post(
        f"/api/v1/deals/{deal.json()['id']}/notes",
        headers=headers,
        json={"body": "  Клиент подтвердил заказ  "},
    )
    assert note.status_code == 201, note.text
    assert note.json()["event_type"] == "deal.note.created"
    assert note.json()["payload"]["body"] == "Клиент подтвердил заказ"

    activity = await client.get(
        "/api/v1/activity",
        params={"entity_type": "deal", "entity_id": deal.json()["id"]},
    )
    assert activity.status_code == 200, activity.text
    assert activity.json()["items"][0]["id"] == note.json()["id"]


@pytest.mark.asyncio
async def test_foreign_workspace_records_are_not_visible(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    import uuid

    from app.db import SessionLocal
    from app.models import Company, Workspace

    async with SessionLocal() as db:
        other_workspace = Workspace(name="Other", slug="other")
        db.add(other_workspace)
        await db.flush()
        foreign_company = Company(workspace_id=other_workspace.id, name="Hidden")
        db.add(foreign_company)
        await db.commit()
        foreign_id = uuid.UUID(str(foreign_company.id))

    response = await client.get(f"/api/v1/companies/{foreign_id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_pipeline_and_stage_management_versions_ordering_and_events(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    pipeline = (await client.get("/api/v1/pipelines")).json()[0]

    renamed_pipeline = await client.patch(
        f"/api/v1/pipelines/{pipeline['id']}",
        headers=headers,
        json={"name": "  Основные продажи  ", "expected_version": pipeline["version"]},
    )
    assert renamed_pipeline.status_code == 200, renamed_pipeline.text
    assert renamed_pipeline.json()["name"] == "Основные продажи"
    assert renamed_pipeline.json()["version"] == pipeline["version"] + 1

    stale_pipeline = await client.patch(
        f"/api/v1/pipelines/{pipeline['id']}",
        headers=headers,
        json={"name": "Устаревшее имя", "expected_version": pipeline["version"]},
    )
    assert stale_pipeline.status_code == 409
    assert stale_pipeline.json()["detail"]["code"] == "version_conflict"

    created_stage = await client.post(
        f"/api/v1/pipelines/{pipeline['id']}/stages",
        headers=headers,
        json={"name": "  Согласование  ", "color": "#aabbcc", "stage_type": "open"},
    )
    assert created_stage.status_code == 201, created_stage.text
    stage = created_stage.json()
    assert stage["name"] == "Согласование"
    assert stage["color"] == "#AABBCC"
    assert stage["stage_type"] == "open"
    assert stage["version"] == 1

    after_create = (await client.get("/api/v1/pipelines")).json()[0]
    assert after_create["version"] == renamed_pipeline.json()["version"] + 1
    assert [item["position"] for item in after_create["stages"]] == list(
        range(len(after_create["stages"]))
    )
    assert [item["stage_type"] for item in after_create["stages"]] == [
        "open",
        "open",
        "open",
        "won",
        "lost",
    ]

    renamed_stage = await client.patch(
        f"/api/v1/stages/{stage['id']}",
        headers=headers,
        json={
            "name": "  Договор  ",
            "color": "#112233",
            "expected_version": stage["version"],
        },
    )
    assert renamed_stage.status_code == 200, renamed_stage.text
    assert renamed_stage.json()["name"] == "Договор"
    assert renamed_stage.json()["color"] == "#112233"
    assert renamed_stage.json()["version"] == stage["version"] + 1

    stale_stage = await client.patch(
        f"/api/v1/stages/{stage['id']}",
        headers=headers,
        json={"name": "Старое", "expected_version": stage["version"]},
    )
    assert stale_stage.status_code == 409
    assert stale_stage.json()["detail"]["code"] == "version_conflict"

    workspace_id = as_uuid(owner_auth["workspace"]["id"])
    stage_id = as_uuid(stage["id"])
    async with SessionLocal() as db:
        db.add(
            ExternalEntityMap(
                workspace_id=workspace_id,
                provider="amocrm",
                entity_type="stages",
                external_id="amo-stage-42",
                internal_id=stage_id,
                fingerprint="f" * 64,
            )
        )
        await db.commit()

    stale_delete = await client.delete(
        f"/api/v1/stages/{stage['id']}",
        headers=headers,
        params={"expected_version": stage["version"]},
    )
    assert stale_delete.status_code == 409
    assert stale_delete.json()["detail"]["code"] == "version_conflict"

    deleted = await client.delete(
        f"/api/v1/stages/{stage['id']}",
        headers=headers,
        params={"expected_version": renamed_stage.json()["version"]},
    )
    assert deleted.status_code == 204, deleted.text
    after_delete = (await client.get("/api/v1/pipelines")).json()[0]
    assert after_delete["version"] == after_create["version"] + 1
    assert all(item["id"] != stage["id"] for item in after_delete["stages"])

    async with SessionLocal() as db:
        assert (
            await db.scalar(
                sa.select(ExternalEntityMap.id).where(
                    ExternalEntityMap.workspace_id == workspace_id,
                    ExternalEntityMap.internal_id == stage_id,
                )
            )
            is None
        )
        activity_count = await db.scalar(
            sa.select(sa.func.count())
            .select_from(ActivityEvent)
            .where(
                ActivityEvent.entity_id == stage_id,
                ActivityEvent.event_type.in_(
                    ["stage.created", "stage.updated", "stage.deleted"]
                ),
            )
        )
        outbox_count = await db.scalar(
            sa.select(sa.func.count())
            .select_from(OutboxEvent)
            .where(
                OutboxEvent.aggregate_id == stage_id,
                OutboxEvent.event_type.in_(
                    ["stage.created", "stage.updated", "stage.deleted"]
                ),
            )
        )
        realtime_count = await db.scalar(
            sa.select(sa.func.count())
            .select_from(RealtimeEvent)
            .where(
                RealtimeEvent.workspace_id == workspace_id,
                RealtimeEvent.event_type.in_(
                    ["stage.created", "stage.updated", "stage.deleted"]
                ),
            )
        )
    assert activity_count == outbox_count == realtime_count == 3


@pytest.mark.asyncio
async def test_stage_deletion_safety_and_integration_references(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    pipeline = (await client.get("/api/v1/pipelines")).json()[0]
    first_open, second_open = [
        stage for stage in pipeline["stages"] if stage["stage_type"] == "open"
    ]
    terminal = next(stage for stage in pipeline["stages"] if stage["stage_type"] == "won")

    terminal_delete = await client.delete(
        f"/api/v1/stages/{terminal['id']}",
        headers=headers,
        params={"expected_version": terminal["version"]},
    )
    assert terminal_delete.status_code == 409
    assert terminal_delete.json()["detail"]["code"] == "stage_not_open"

    deal = await client.post(
        "/api/v1/deals",
        headers=headers,
        json={
            "title": "Stage blocker",
            "pipeline_id": pipeline["id"],
            "stage_id": first_open["id"],
        },
    )
    assert deal.status_code == 201, deal.text
    occupied_delete = await client.delete(
        f"/api/v1/stages/{first_open['id']}",
        headers=headers,
        params={"expected_version": first_open["version"]},
    )
    assert occupied_delete.status_code == 409
    assert occupied_delete.json()["detail"]["code"] == "stage_in_use"
    assert {"deals", "deal_stage_history"}.issubset(
        occupied_delete.json()["detail"]["references"]
    )

    workspace_id = as_uuid(owner_auth["workspace"]["id"])
    pipeline_id = as_uuid(pipeline["id"])
    second_open_id = as_uuid(second_open["id"])
    async with SessionLocal() as db:
        template = NotificationTemplate(
            workspace_id=workspace_id,
            name="Stage reference template",
            channel="email",
            body_template="Test",
        )
        db.add(template)
        await db.flush()
        db.add_all(
            [
                ChannelConnection(
                    workspace_id=workspace_id,
                    kind=ChannelKind.email,
                    name="Stage routing",
                    default_pipeline_id=pipeline_id,
                    default_stage_id=second_open_id,
                ),
                NotificationRule(
                    workspace_id=workspace_id,
                    template_id=template.id,
                    name="Stage rule",
                    event_type="deal.stage_changed",
                    audience=NotificationAudience.employee,
                    channel="email",
                    pipeline_id=pipeline_id,
                    stage_id=second_open_id,
                ),
                Form(
                    workspace_id=workspace_id,
                    slug="stage-reference-form",
                    title="Stage form",
                    pipeline_id=pipeline_id,
                    stage_id=second_open_id,
                ),
                WebhookEndpoint(
                    workspace_id=workspace_id,
                    slug="stage-reference-webhook",
                    name="Stage webhook",
                    encrypted_secret=b"secret",
                    pipeline_id=pipeline_id,
                    stage_id=second_open_id,
                ),
            ]
        )
        await db.commit()

    integration_delete = await client.delete(
        f"/api/v1/stages/{second_open['id']}",
        headers=headers,
        params={"expected_version": second_open["version"]},
    )
    assert integration_delete.status_code == 409
    assert integration_delete.json()["detail"]["code"] == "stage_in_use"
    assert set(integration_delete.json()["detail"]["references"]) == {
        "channel_connections",
        "notification_rules",
        "forms",
        "webhook_endpoints",
    }
    pipeline_delete = await client.delete(
        f"/api/v1/pipelines/{pipeline['id']}",
        headers=headers,
        params={"expected_version": pipeline["version"]},
    )
    assert pipeline_delete.status_code == 409
    assert pipeline_delete.json()["detail"]["code"] == "pipeline_in_use"
    assert set(pipeline_delete.json()["detail"]["references"]) == {
        "deals",
        "deal_stage_history",
        "channel_connections",
        "notification_rules",
        "forms",
        "webhook_endpoints",
    }


@pytest.mark.asyncio
async def test_stage_delete_keeps_one_open_stage(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    pipeline = (await client.get("/api/v1/pipelines")).json()[0]
    first_open, last_open = [
        stage for stage in pipeline["stages"] if stage["stage_type"] == "open"
    ]

    deleted = await client.delete(
        f"/api/v1/stages/{first_open['id']}",
        headers=headers,
        params={"expected_version": first_open["version"]},
    )
    assert deleted.status_code == 204, deleted.text
    blocked = await client.delete(
        f"/api/v1/stages/{last_open['id']}",
        headers=headers,
        params={"expected_version": last_open["version"]},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "last_open_stage"


@pytest.mark.asyncio
async def test_pipeline_delete_versions_usage_last_active_and_amo_maps(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    default_pipeline = (await client.get("/api/v1/pipelines")).json()[0]
    disposable = await client.post(
        "/api/v1/pipelines",
        headers=headers,
        json={
            "name": "Disposable",
            "position": 1,
            "stages": [
                {"name": "Open", "position": 0, "stage_type": "open"},
                {"name": "Won", "position": 1, "stage_type": "won"},
            ],
        },
    )
    assert disposable.status_code == 201, disposable.text
    disposable_pipeline = disposable.json()

    workspace_id = as_uuid(owner_auth["workspace"]["id"])
    disposable_pipeline_id = as_uuid(disposable_pipeline["id"])
    async with SessionLocal() as db:
        maps = [
            ExternalEntityMap(
                workspace_id=workspace_id,
                provider="amocrm",
                entity_type="pipelines",
                external_id="amo-pipeline-disposable",
                internal_id=disposable_pipeline_id,
                fingerprint="p" * 64,
            )
        ]
        maps.extend(
            ExternalEntityMap(
                workspace_id=workspace_id,
                provider="amocrm",
                entity_type="stages",
                external_id=f"amo-stage-{index}",
                internal_id=as_uuid(stage["id"]),
                fingerprint=str(index) * 64,
            )
            for index, stage in enumerate(disposable_pipeline["stages"], start=1)
        )
        db.add_all(maps)
        await db.commit()

    stale = await client.delete(
        f"/api/v1/pipelines/{disposable_pipeline['id']}",
        headers=headers,
        params={"expected_version": disposable_pipeline["version"] + 1},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "version_conflict"

    deleted = await client.delete(
        f"/api/v1/pipelines/{disposable_pipeline['id']}",
        headers=headers,
        params={"expected_version": disposable_pipeline["version"]},
    )
    assert deleted.status_code == 204, deleted.text
    async with SessionLocal() as db:
        assert (
            await db.scalar(
                sa.select(sa.func.count())
                .select_from(ExternalEntityMap)
                .where(ExternalEntityMap.workspace_id == workspace_id)
            )
            == 0
        )
        assert await db.get(Pipeline, disposable_pipeline_id) is None
        assert not (
            await db.scalars(
                sa.select(Stage).where(Stage.pipeline_id == disposable_pipeline_id)
            )
        ).all()

    last_active = await client.delete(
        f"/api/v1/pipelines/{default_pipeline['id']}",
        headers=headers,
        params={"expected_version": default_pipeline["version"]},
    )
    assert last_active.status_code == 409
    assert last_active.json()["detail"]["code"] == "last_active_pipeline"

    second = await client.post(
        "/api/v1/pipelines",
        headers=headers,
        json={
            "name": "Keep alive",
            "position": 2,
            "stages": [{"name": "Open", "position": 0, "stage_type": "open"}],
        },
    )
    assert second.status_code == 201, second.text
    duplicate_name = await client.patch(
        f"/api/v1/pipelines/{default_pipeline['id']}",
        headers=headers,
        json={
            "name": second.json()["name"],
            "expected_version": default_pipeline["version"],
        },
    )
    assert duplicate_name.status_code == 409
    assert duplicate_name.json()["detail"]["code"] == "pipeline_name_conflict"
    deal = await client.post(
        "/api/v1/deals",
        headers=headers,
        json={
            "title": "Pipeline blocker",
            "pipeline_id": default_pipeline["id"],
            "stage_id": default_pipeline["stages"][0]["id"],
        },
    )
    assert deal.status_code == 201, deal.text
    in_use = await client.delete(
        f"/api/v1/pipelines/{default_pipeline['id']}",
        headers=headers,
        params={"expected_version": default_pipeline["version"]},
    )
    assert in_use.status_code == 409
    assert in_use.json()["detail"]["code"] == "pipeline_in_use"
    assert {"deals", "deal_stage_history"}.issubset(in_use.json()["detail"]["references"])


@pytest.mark.asyncio
async def test_pipeline_management_is_admin_only_and_workspace_scoped(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    async with SessionLocal() as db:
        other_workspace = Workspace(name="Foreign CRM", slug="foreign-crm")
        db.add(other_workspace)
        await db.flush()
        foreign_pipeline = Pipeline(
            workspace_id=other_workspace.id,
            name="Foreign pipeline",
            position=0,
        )
        db.add(foreign_pipeline)
        await db.flush()
        foreign_stage = Stage(
            workspace_id=other_workspace.id,
            pipeline_id=foreign_pipeline.id,
            name="Foreign stage",
            position=0,
        )
        db.add(foreign_stage)
        await db.commit()
        foreign_pipeline_id = uuid.UUID(str(foreign_pipeline.id))
        foreign_stage_id = uuid.UUID(str(foreign_stage.id))

    foreign_responses = [
        await client.patch(
            f"/api/v1/pipelines/{foreign_pipeline_id}",
            headers=headers,
            json={"name": "No access", "expected_version": 1},
        ),
        await client.post(
            f"/api/v1/pipelines/{foreign_pipeline_id}/stages",
            headers=headers,
            json={"name": "No access"},
        ),
        await client.patch(
            f"/api/v1/stages/{foreign_stage_id}",
            headers=headers,
            json={"name": "No access", "expected_version": 1},
        ),
        await client.delete(
            f"/api/v1/stages/{foreign_stage_id}",
            headers=headers,
            params={"expected_version": 1},
        ),
        await client.delete(
            f"/api/v1/pipelines/{foreign_pipeline_id}",
            headers=headers,
            params={"expected_version": 1},
        ),
    ]
    assert [response.status_code for response in foreign_responses] == [404] * 5

    pipeline = (await client.get("/api/v1/pipelines")).json()[0]
    invitation = await client.post(
        "/api/v1/invitations",
        headers=headers,
        json={"email": "pipeline-manager@example.com", "role": "manager"},
    )
    assert invitation.status_code == 201, invitation.text
    manager = await client.post(
        "/api/v1/auth/accept-invitation",
        json={
            "token": invitation.json()["token"],
            "full_name": "Pipeline Manager",
            "password": "a secure pipeline manager password",
        },
    )
    assert manager.status_code == 201, manager.text
    manager_headers = csrf(manager.json())
    stage = pipeline["stages"][0]
    manager_responses = [
        await client.patch(
            f"/api/v1/pipelines/{pipeline['id']}",
            headers=manager_headers,
            json={"name": "Denied", "expected_version": pipeline["version"]},
        ),
        await client.post(
            f"/api/v1/pipelines/{pipeline['id']}/stages",
            headers=manager_headers,
            json={"name": "Denied"},
        ),
        await client.patch(
            f"/api/v1/stages/{stage['id']}",
            headers=manager_headers,
            json={"name": "Denied", "expected_version": stage["version"]},
        ),
        await client.delete(
            f"/api/v1/stages/{stage['id']}",
            headers=manager_headers,
            params={"expected_version": stage["version"]},
        ),
        await client.delete(
            f"/api/v1/pipelines/{pipeline['id']}",
            headers=manager_headers,
            params={"expected_version": pipeline["version"]},
        ),
    ]
    assert [response.status_code for response in manager_responses] == [403] * 5
    assert all(response.json()["detail"] == "insufficient role" for response in manager_responses)


@pytest.mark.asyncio
async def test_stage_creation_validation_and_limit(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    headers = csrf(owner_auth)
    pipeline = (await client.get("/api/v1/pipelines")).json()[0]

    blank_pipeline = await client.patch(
        f"/api/v1/pipelines/{pipeline['id']}",
        headers=headers,
        json={"name": "   ", "expected_version": pipeline["version"]},
    )
    blank_stage = await client.post(
        f"/api/v1/pipelines/{pipeline['id']}/stages",
        headers=headers,
        json={"name": "   "},
    )
    terminal_stage = await client.post(
        f"/api/v1/pipelines/{pipeline['id']}/stages",
        headers=headers,
        json={"name": "Another won", "stage_type": "won"},
    )
    blank_pipeline_create = await client.post(
        "/api/v1/pipelines",
        headers=headers,
        json={
            "name": "   ",
            "stages": [{"name": "Open", "position": 0, "stage_type": "open"}],
        },
    )
    no_open_pipeline = await client.post(
        "/api/v1/pipelines",
        headers=headers,
        json={
            "name": "No open stage",
            "stages": [{"name": "Won", "position": 0, "stage_type": "won"}],
        },
    )
    assert {
        blank_pipeline.status_code,
        blank_stage.status_code,
        terminal_stage.status_code,
        blank_pipeline_create.status_code,
        no_open_pipeline.status_code,
    } == {422}

    full_pipeline = await client.post(
        "/api/v1/pipelines",
        headers=headers,
        json={
            "name": "Fifty stages",
            "position": 2,
            "stages": [
                {"name": f"Stage {index}", "position": index, "stage_type": "open"}
                for index in range(50)
            ],
        },
    )
    assert full_pipeline.status_code == 201, full_pipeline.text
    overflow = await client.post(
        f"/api/v1/pipelines/{full_pipeline.json()['id']}/stages",
        headers=headers,
        json={"name": "Stage 51"},
    )
    assert overflow.status_code == 409
    assert overflow.json()["detail"]["code"] == "pipeline_stage_limit"

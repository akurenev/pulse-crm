from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest


def csrf(auth: dict[str, object]) -> dict[str, str]:
    return {"X-CSRF-Token": str(auth["csrf_token"])}


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

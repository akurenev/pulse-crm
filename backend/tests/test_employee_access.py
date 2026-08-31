from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import Delete

from app.db import SessionLocal, engine, get_session
from app.integrations.models import PurchaseSchedule, PurchaseScheduleStatus
from app.integrations.purchases import ensure_purchase_task
from app.main import app
from app.models import Contact, DealContact
from app.services.access import notification_target_access_allowed


def csrf(auth: dict[str, Any]) -> dict[str, str]:
    return {"X-CSRF-Token": str(auth["csrf_token"])}


async def invite_employee(
    owner_client: httpx.AsyncClient,
    owner_auth: dict[str, Any],
    *,
    email: str,
    full_name: str,
) -> tuple[httpx.AsyncClient, dict[str, Any]]:
    invitation = await owner_client.post(
        "/api/v1/invitations",
        headers=csrf(owner_auth),
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
            "password": "employee test password",
        },
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["user"]["role"] == "employee"
    return employee_client, accepted.json()


@pytest.mark.asyncio
async def test_employee_cannot_mutate_reassigned_purchase_task_through_own_deal(
    client: httpx.AsyncClient,
    owner_auth: dict[str, Any],
) -> None:
    employee, employee_auth = await invite_employee(
        client,
        owner_auth,
        email="purchase-employee@example.com",
        full_name="Purchase Employee",
    )
    other_employee, other_auth = await invite_employee(
        client,
        owner_auth,
        email="purchase-other@example.com",
        full_name="Purchase Other",
    )
    try:
        owner_headers = csrf(owner_auth)
        employee_headers = csrf(employee_auth)
        workspace_id = uuid.UUID(str(owner_auth["workspace"]["id"]))
        employee_id = employee_auth["user"]["id"]
        other_employee_id = other_auth["user"]["id"]
        pipeline = (await client.get("/api/v1/pipelines")).json()[0]
        open_stage = next(
            stage for stage in pipeline["stages"] if stage["stage_type"] == "open"
        )
        next_purchase_at = (datetime.now(UTC) + timedelta(days=10)).isoformat()
        created = await client.post(
            "/api/v1/deals",
            headers=owner_headers,
            json={
                "title": "Purchase task access",
                "pipeline_id": pipeline["id"],
                "stage_id": open_stage["id"],
                "assignee_id": employee_id,
                "next_purchase_at": next_purchase_at,
            },
        )
        assert created.status_code == 201, created.text
        deal = created.json()

        async with SessionLocal() as db:
            schedule = await db.scalar(
                sa.select(PurchaseSchedule).where(
                    PurchaseSchedule.workspace_id == workspace_id,
                    PurchaseSchedule.deal_id == uuid.UUID(deal["id"]),
                    PurchaseSchedule.status == PurchaseScheduleStatus.active,
                )
            )
            assert schedule is not None
            materialized = await ensure_purchase_task(
                db,
                workspace_id=workspace_id,
                schedule_id=schedule.id,
            )
            task_id = materialized.task.id
            await db.commit()

        task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
        reassigned = await client.patch(
            f"/api/v1/tasks/{task_id}",
            headers=owner_headers,
            json={
                "expected_version": task["version"],
                "assignee_id": other_employee_id,
            },
        )
        assert reassigned.status_code == 200, reassigned.text

        rejected = await employee.patch(
            f"/api/v1/deals/{deal['id']}",
            headers=employee_headers,
            json={
                "expected_version": deal["version"],
                "next_purchase_at": next_purchase_at,
            },
        )
        assert rejected.status_code == 404, rejected.text

        stored_deal = await client.get(f"/api/v1/deals/{deal['id']}")
        assert stored_deal.status_code == 200, stored_deal.text
        assert stored_deal.json()["version"] == deal["version"]
        stored_task = await client.get(f"/api/v1/tasks/{task_id}")
        assert stored_task.status_code == 200, stored_task.text
        assert stored_task.json()["assignee_id"] == other_employee_id
        assert stored_task.json()["version"] == reassigned.json()["version"]
    finally:
        await employee.aclose()
        await other_employee.aclose()


@pytest.mark.asyncio
async def test_employee_deal_contact_update_preserves_hidden_foreign_link(
    client: httpx.AsyncClient,
    owner_auth: dict[str, Any],
) -> None:
    employee, employee_auth = await invite_employee(
        client,
        owner_auth,
        email="contact-link-employee@example.com",
        full_name="Contact Link Employee",
    )
    try:
        owner_headers = csrf(owner_auth)
        employee_headers = csrf(employee_auth)
        employee_id = employee_auth["user"]["id"]
        owner_id = owner_auth["user"]["id"]

        pipeline = (await client.get("/api/v1/pipelines")).json()[0]
        open_stage = next(
            stage for stage in pipeline["stages"] if stage["stage_type"] == "open"
        )
        own_contact = (
            await client.post(
                "/api/v1/contacts",
                headers=owner_headers,
                json={"first_name": "Own", "assignee_id": employee_id},
            )
        ).json()
        foreign_contact = (
            await client.post(
                "/api/v1/contacts",
                headers=owner_headers,
                json={"first_name": "Foreign", "assignee_id": owner_id},
            )
        ).json()
        deal = (
            await client.post(
                "/api/v1/deals",
                headers=owner_headers,
                json={
                    "title": "Mixed contact visibility",
                    "pipeline_id": pipeline["id"],
                    "stage_id": open_stage["id"],
                    "assignee_id": employee_id,
                    "contact_ids": [foreign_contact["id"], own_contact["id"]],
                },
            )
        ).json()

        updated = await employee.patch(
            f"/api/v1/deals/{deal['id']}",
            headers=employee_headers,
            json={"expected_version": deal["version"], "contact_ids": []},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["contact_ids"] == []
        assert updated.json()["primary_contact"] is None
        assert foreign_contact["id"] not in updated.text

        owner_view = await client.get(f"/api/v1/deals/{deal['id']}")
        assert owner_view.status_code == 200, owner_view.text
        assert owner_view.json()["contact_ids"] == [foreign_contact["id"]]
        assert owner_view.json()["primary_contact"]["id"] == foreign_contact["id"]
    finally:
        await employee.aclose()


@pytest.mark.asyncio
async def test_employee_deal_contact_update_preserves_deleted_primary_link(
    client: httpx.AsyncClient,
    owner_auth: dict[str, Any],
) -> None:
    employee, employee_auth = await invite_employee(
        client,
        owner_auth,
        email="deleted-link-employee@example.com",
        full_name="Deleted Link Employee",
    )
    try:
        owner_headers = csrf(owner_auth)
        employee_headers = csrf(employee_auth)
        employee_id = employee_auth["user"]["id"]
        workspace_id = uuid.UUID(str(owner_auth["workspace"]["id"]))

        pipeline = (await client.get("/api/v1/pipelines")).json()[0]
        open_stage = next(
            stage for stage in pipeline["stages"] if stage["stage_type"] == "open"
        )
        deleted_contact = (
            await client.post(
                "/api/v1/contacts",
                headers=owner_headers,
                json={"first_name": "Deleted", "assignee_id": employee_id},
            )
        ).json()
        replacement_contact = (
            await client.post(
                "/api/v1/contacts",
                headers=owner_headers,
                json={"first_name": "Replacement", "assignee_id": employee_id},
            )
        ).json()
        deal = (
            await client.post(
                "/api/v1/deals",
                headers=owner_headers,
                json={
                    "title": "Deleted primary contact",
                    "pipeline_id": pipeline["id"],
                    "stage_id": open_stage["id"],
                    "assignee_id": employee_id,
                    "contact_ids": [deleted_contact["id"]],
                },
            )
        ).json()

        deleted = await client.delete(
            f"/api/v1/contacts/{deleted_contact['id']}",
            headers=owner_headers,
            params={"expected_version": deleted_contact["version"]},
        )
        assert deleted.status_code == 204, deleted.text

        updated = await employee.patch(
            f"/api/v1/deals/{deal['id']}",
            headers=employee_headers,
            json={
                "expected_version": deal["version"],
                "contact_ids": [replacement_contact["id"]],
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["contact_ids"] == [replacement_contact["id"]]

        async with SessionLocal() as db:
            links = list(
                (
                    await db.scalars(
                        sa.select(DealContact)
                        .where(
                            DealContact.workspace_id == workspace_id,
                            DealContact.deal_id == uuid.UUID(deal["id"]),
                        )
                        .order_by(DealContact.created_at)
                    )
                ).all()
            )
        assert {str(link.contact_id) for link in links} == {
            deleted_contact["id"],
            replacement_contact["id"],
        }
        assert sum(link.is_primary for link in links) == 1
        assert next(
            link for link in links if str(link.contact_id) == deleted_contact["id"]
        ).is_primary
    finally:
        await employee.aclose()


@pytest.mark.asyncio
async def test_employee_deal_contact_update_rechecks_primary_after_scoped_delete(
    client: httpx.AsyncClient,
    owner_auth: dict[str, Any],
) -> None:
    employee, employee_auth = await invite_employee(
        client,
        owner_auth,
        email="primary-race-employee@example.com",
        full_name="Primary Race Employee",
    )
    other_employee, other_auth = await invite_employee(
        client,
        owner_auth,
        email="primary-race-other@example.com",
        full_name="Primary Race Other",
    )
    delete_reached = asyncio.Event()
    continue_delete = asyncio.Event()
    try:
        owner_headers = csrf(owner_auth)
        employee_headers = csrf(employee_auth)
        employee_id = employee_auth["user"]["id"]
        other_employee_id = uuid.UUID(other_auth["user"]["id"])
        workspace_id = uuid.UUID(str(owner_auth["workspace"]["id"]))

        pipeline = (await client.get("/api/v1/pipelines")).json()[0]
        open_stage = next(
            stage for stage in pipeline["stages"] if stage["stage_type"] == "open"
        )
        original_contact = (
            await client.post(
                "/api/v1/contacts",
                headers=owner_headers,
                json={"first_name": "Original", "assignee_id": employee_id},
            )
        ).json()
        replacement_contact = (
            await client.post(
                "/api/v1/contacts",
                headers=owner_headers,
                json={"first_name": "Replacement", "assignee_id": employee_id},
            )
        ).json()
        deal = (
            await client.post(
                "/api/v1/deals",
                headers=owner_headers,
                json={
                    "title": "Primary reassignment race",
                    "pipeline_id": pipeline["id"],
                    "stage_id": open_stage["id"],
                    "assignee_id": employee_id,
                    "contact_ids": [original_contact["id"]],
                },
            )
        ).json()
        original_contact_id = uuid.UUID(original_contact["id"])

        class ReassignBeforeDeleteSession(AsyncSession):
            reassigned = False

            async def execute(
                self, statement: Any, *args: Any, **kwargs: Any
            ) -> Any:
                is_link_delete = (
                    isinstance(statement, Delete)
                    and statement.table.name == DealContact.__tablename__
                )
                if is_link_delete and not self.reassigned:
                    self.reassigned = True
                    delete_reached.set()
                    await continue_delete.wait()
                    await super().execute(
                        sa.update(Contact)
                        .where(
                            Contact.id == original_contact_id,
                            Contact.workspace_id == workspace_id,
                        )
                        .values(assignee_id=other_employee_id)
                    )
                return await super().execute(statement, *args, **kwargs)

        barrier_sessions = async_sessionmaker(
            engine,
            class_=ReassignBeforeDeleteSession,
            expire_on_commit=False,
            autoflush=False,
        )

        async def barrier_session() -> AsyncIterator[AsyncSession]:
            async with barrier_sessions() as db:
                try:
                    yield db
                except Exception:
                    await db.rollback()
                    raise

        app.dependency_overrides[get_session] = barrier_session
        update_request = asyncio.create_task(
            employee.patch(
                f"/api/v1/deals/{deal['id']}",
                headers=employee_headers,
                json={
                    "expected_version": deal["version"],
                    "contact_ids": [replacement_contact["id"]],
                },
            )
        )
        try:
            await asyncio.wait_for(delete_reached.wait(), timeout=2)
            continue_delete.set()
            updated = await asyncio.wait_for(update_request, timeout=2)
        finally:
            continue_delete.set()
            if not update_request.done():
                update_request.cancel()
            app.dependency_overrides.pop(get_session, None)

        assert updated.status_code == 200, updated.text
        assert updated.json()["contact_ids"] == [replacement_contact["id"]]

        async with SessionLocal() as db:
            links = list(
                (
                    await db.scalars(
                        sa.select(DealContact).where(
                            DealContact.workspace_id == workspace_id,
                            DealContact.deal_id == uuid.UUID(deal["id"]),
                        )
                    )
                ).all()
            )
        assert {str(link.contact_id) for link in links} == {
            original_contact["id"],
            replacement_contact["id"],
        }
        assert sum(link.is_primary for link in links) == 1
        assert next(
            link for link in links if str(link.contact_id) == original_contact["id"]
        ).is_primary
    finally:
        app.dependency_overrides.pop(get_session, None)
        continue_delete.set()
        await employee.aclose()
        await other_employee.aclose()


@pytest.mark.asyncio
async def test_employee_crm_access_is_owner_scoped_and_idor_safe(
    client: httpx.AsyncClient,
    owner_auth: dict[str, Any],
) -> None:
    employee, employee_auth = await invite_employee(
        client,
        owner_auth,
        email="employee-one@example.com",
        full_name="Employee One",
    )
    other_employee, other_auth = await invite_employee(
        client,
        owner_auth,
        email="employee-two@example.com",
        full_name="Employee Two",
    )
    try:
        employee_id = employee_auth["user"]["id"]
        other_employee_id = other_auth["user"]["id"]
        owner_headers = csrf(owner_auth)
        employee_headers = csrf(employee_auth)

        employee_users = await employee.get("/api/v1/users")
        assert employee_users.status_code == 200, employee_users.text
        assert [user["id"] for user in employee_users.json()] == [employee_id]
        forbidden_invitation = await employee.post(
            "/api/v1/invitations",
            headers=employee_headers,
            json={"email": "third-employee@example.com", "role": "employee"},
        )
        assert forbidden_invitation.status_code == 403

        pipeline = (await client.get("/api/v1/pipelines")).json()[0]
        open_stage = next(stage for stage in pipeline["stages"] if stage["stage_type"] == "open")
        won_stage = next(stage for stage in pipeline["stages"] if stage["stage_type"] == "won")

        own_company = (
            await client.post(
                "/api/v1/companies", headers=owner_headers, json={"name": "Own company"}
            )
        ).json()
        linked_company = (
            await client.post(
                "/api/v1/companies", headers=owner_headers, json={"name": "Linked company"}
            )
        ).json()
        foreign_company = (
            await client.post(
                "/api/v1/companies", headers=owner_headers, json={"name": "Foreign company"}
            )
        ).json()

        explicit_contact = (
            await client.post(
                "/api/v1/contacts",
                headers=owner_headers,
                json={
                    "first_name": "Explicit",
                    "company_id": own_company["id"],
                    "assignee_id": employee_id,
                },
            )
        ).json()
        linked_contact = (
            await client.post(
                "/api/v1/contacts",
                headers=owner_headers,
                json={
                    "first_name": "Linked",
                    "company_id": linked_company["id"],
                    "assignee_id": employee_id,
                },
            )
        ).json()
        foreign_contact = (
            await client.post(
                "/api/v1/contacts",
                headers=owner_headers,
                json={
                    "first_name": "Foreign",
                    "company_id": foreign_company["id"],
                    "assignee_id": other_employee_id,
                },
            )
        ).json()

        own_deal = (
            await client.post(
                "/api/v1/deals",
                headers=owner_headers,
                json={
                    "title": "Own deal",
                    "pipeline_id": pipeline["id"],
                    "stage_id": open_stage["id"],
                    "company_id": linked_company["id"],
                    "contact_ids": [foreign_contact["id"], linked_contact["id"]],
                    "assignee_id": employee_id,
                },
            )
        ).json()
        own_won_deal = (
            await client.post(
                "/api/v1/deals",
                headers=owner_headers,
                json={
                    "title": "Own won deal",
                    "pipeline_id": pipeline["id"],
                    "stage_id": won_stage["id"],
                    "contact_ids": [linked_contact["id"]],
                    "assignee_id": employee_id,
                },
            )
        ).json()
        foreign_deal = (
            await client.post(
                "/api/v1/deals",
                headers=owner_headers,
                json={
                    "title": "Foreign deal",
                    "pipeline_id": pipeline["id"],
                    "stage_id": won_stage["id"],
                    "company_id": foreign_company["id"],
                    "contact_ids": [foreign_contact["id"]],
                    "assignee_id": other_employee_id,
                },
            )
        ).json()

        due_at = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        own_task = (
            await client.post(
                "/api/v1/tasks",
                headers=owner_headers,
                json={
                    "title": "Own task",
                    "due_at": due_at,
                    "assignee_id": employee_id,
                    "deal_id": own_deal["id"],
                },
            )
        ).json()
        foreign_task = (
            await client.post(
                "/api/v1/tasks",
                headers=owner_headers,
                json={
                    "title": "Foreign task",
                    "due_at": due_at,
                    "assignee_id": other_employee_id,
                    "deal_id": foreign_deal["id"],
                },
            )
        ).json()
        redacted_reference_task = (
            await client.post(
                "/api/v1/tasks",
                headers=owner_headers,
                json={
                    "title": "Own task with foreign references",
                    "due_at": due_at,
                    "assignee_id": employee_id,
                    "deal_id": foreign_deal["id"],
                    "contact_id": foreign_contact["id"],
                    "company_id": foreign_company["id"],
                },
            )
        ).json()

        companies = await employee.get("/api/v1/companies")
        assert {item["id"] for item in companies.json()["items"]} == {
            own_company["id"],
            linked_company["id"],
        }
        assert (
            await employee.get(f"/api/v1/companies/{foreign_company['id']}")
        ).status_code == 404
        assert (
            await employee.post(
                "/api/v1/companies",
                headers=employee_headers,
                json={"name": "Forbidden"},
            )
        ).status_code == 403
        assert (
            await employee.patch(
                f"/api/v1/companies/{own_company['id']}",
                headers=employee_headers,
                json={"expected_version": own_company["version"], "name": "Forbidden"},
            )
        ).status_code == 403

        contacts = await employee.get("/api/v1/contacts")
        assert {item["id"] for item in contacts.json()["items"]} == {
            explicit_contact["id"],
            linked_contact["id"],
        }
        assert (
            await employee.get(f"/api/v1/contacts/{foreign_contact['id']}")
        ).status_code == 404
        created_contact = await employee.post(
            "/api/v1/contacts",
            headers=employee_headers,
            json={"first_name": "Created by employee", "company_id": own_company["id"]},
        )
        assert created_contact.status_code == 201, created_contact.text
        assert created_contact.json()["assignee_id"] == employee_id
        assert (
            await employee.post(
                "/api/v1/contacts",
                headers=employee_headers,
                json={"first_name": "Invalid assignee", "assignee_id": other_employee_id},
            )
        ).status_code == 403
        updated_contact = await employee.patch(
            f"/api/v1/contacts/{explicit_contact['id']}",
            headers=employee_headers,
            json={"expected_version": explicit_contact["version"], "last_name": "Updated"},
        )
        assert updated_contact.status_code == 200, updated_contact.text
        assert (
            await employee.patch(
                f"/api/v1/contacts/{explicit_contact['id']}",
                headers=employee_headers,
                json={
                    "expected_version": updated_contact.json()["version"],
                    "assignee_id": other_employee_id,
                },
            )
        ).status_code == 403
        assert (
            await employee.patch(
                f"/api/v1/contacts/{foreign_contact['id']}",
                headers=employee_headers,
                json={"expected_version": foreign_contact["version"], "last_name": "IDOR"},
            )
        ).status_code == 404
        assert (
            await employee.delete(
                f"/api/v1/contacts/{explicit_contact['id']}",
                headers=employee_headers,
                params={"expected_version": updated_contact.json()["version"]},
            )
        ).status_code == 403

        deals = await employee.get("/api/v1/deals")
        assert {item["id"] for item in deals.json()["items"]} == {
            own_deal["id"],
            own_won_deal["id"],
        }
        loaded_own_deal = await employee.get(f"/api/v1/deals/{own_deal['id']}")
        assert loaded_own_deal.status_code == 200, loaded_own_deal.text
        assert loaded_own_deal.json()["contact_ids"] == [linked_contact["id"]]
        assert loaded_own_deal.json()["primary_contact"]["id"] == linked_contact["id"]
        assert foreign_contact["id"] not in loaded_own_deal.text
        assert (await employee.get(f"/api/v1/deals/{foreign_deal['id']}")).status_code == 404
        created_deal = await employee.post(
            "/api/v1/deals",
            headers=employee_headers,
            json={
                "title": "Created by employee",
                "pipeline_id": pipeline["id"],
                "stage_id": open_stage["id"],
                "contact_ids": [explicit_contact["id"]],
            },
        )
        assert created_deal.status_code == 201, created_deal.text
        assert created_deal.json()["assignee_id"] == employee_id
        assert (
            await employee.post(
                "/api/v1/deals",
                headers=employee_headers,
                json={
                    "title": "Invalid assignee",
                    "pipeline_id": pipeline["id"],
                    "stage_id": open_stage["id"],
                    "assignee_id": other_employee_id,
                },
            )
        ).status_code == 403
        assert (
            await employee.post(
                "/api/v1/deals",
                headers=employee_headers,
                json={
                    "title": "Foreign contact IDOR",
                    "pipeline_id": pipeline["id"],
                    "stage_id": open_stage["id"],
                    "contact_ids": [foreign_contact["id"]],
                },
            )
        ).status_code == 404
        updated_deal = await employee.patch(
            f"/api/v1/deals/{own_deal['id']}",
            headers=employee_headers,
            json={"expected_version": own_deal["version"], "title": "Updated own deal"},
        )
        assert updated_deal.status_code == 200, updated_deal.text
        assert (
            await employee.patch(
                f"/api/v1/deals/{own_deal['id']}",
                headers=employee_headers,
                json={
                    "expected_version": updated_deal.json()["version"],
                    "assignee_id": other_employee_id,
                },
            )
        ).status_code == 403
        assert (
            await employee.patch(
                f"/api/v1/deals/{foreign_deal['id']}",
                headers=employee_headers,
                json={"expected_version": foreign_deal["version"], "title": "IDOR"},
            )
        ).status_code == 404
        transitioned = await employee.patch(
            f"/api/v1/deals/{own_deal['id']}/stage",
            headers=employee_headers,
            json={
                "expected_version": updated_deal.json()["version"],
                "target_stage_id": won_stage["id"],
            },
        )
        assert transitioned.status_code == 200, transitioned.text
        assert (
            await employee.patch(
                f"/api/v1/deals/{foreign_deal['id']}/stage",
                headers=employee_headers,
                json={
                    "expected_version": foreign_deal["version"],
                    "target_stage_id": open_stage["id"],
                },
            )
        ).status_code == 404
        assert (
            await employee.delete(
                f"/api/v1/deals/{own_deal['id']}",
                headers=employee_headers,
                params={"expected_version": transitioned.json()["version"]},
            )
        ).status_code == 403

        tasks = await employee.get("/api/v1/tasks")
        assert {item["id"] for item in tasks.json()["items"]} == {
            own_task["id"],
            redacted_reference_task["id"],
        }
        redacted_list_item = next(
            item
            for item in tasks.json()["items"]
            if item["id"] == redacted_reference_task["id"]
        )
        assert redacted_list_item["deal_id"] is None
        assert redacted_list_item["contact_id"] is None
        assert redacted_list_item["company_id"] is None
        redacted_detail = await employee.get(
            f"/api/v1/tasks/{redacted_reference_task['id']}"
        )
        assert redacted_detail.status_code == 200, redacted_detail.text
        assert redacted_detail.json()["deal_id"] is None
        assert redacted_detail.json()["contact_id"] is None
        assert redacted_detail.json()["company_id"] is None
        redacted_title_update = await employee.patch(
            f"/api/v1/tasks/{redacted_reference_task['id']}",
            headers=employee_headers,
            json={
                "expected_version": redacted_reference_task["version"],
                "title": "Foreign relations stay hidden",
            },
        )
        assert redacted_title_update.status_code == 200, redacted_title_update.text
        assert redacted_title_update.json()["deal_id"] is None
        assert redacted_title_update.json()["contact_id"] is None
        assert redacted_title_update.json()["company_id"] is None
        redacted_version = redacted_title_update.json()["version"]
        for relation_field, visible_replacement in (
            ("deal_id", own_deal["id"]),
            ("contact_id", linked_contact["id"]),
            ("company_id", own_company["id"]),
        ):
            for replacement in (None, visible_replacement):
                forbidden_relation_update = await employee.patch(
                    f"/api/v1/tasks/{redacted_reference_task['id']}",
                    headers=employee_headers,
                    json={
                        "expected_version": redacted_version,
                        relation_field: replacement,
                    },
                )
                assert forbidden_relation_update.status_code == 404
        owner_redacted_task = await client.get(
            f"/api/v1/tasks/{redacted_reference_task['id']}"
        )
        assert owner_redacted_task.status_code == 200, owner_redacted_task.text
        assert owner_redacted_task.json()["deal_id"] == foreign_deal["id"]
        assert owner_redacted_task.json()["contact_id"] == foreign_contact["id"]
        assert owner_redacted_task.json()["company_id"] == foreign_company["id"]
        assert (await employee.get(f"/api/v1/tasks/{foreign_task['id']}")).status_code == 404
        created_task = await employee.post(
            "/api/v1/tasks",
            headers=employee_headers,
            json={
                "title": "Created by employee",
                "due_at": due_at,
                "assignee_id": employee_id,
                "deal_id": own_deal["id"],
                "contact_id": linked_contact["id"],
            },
        )
        assert created_task.status_code == 201, created_task.text
        assert (
            await employee.post(
                "/api/v1/tasks",
                headers=employee_headers,
                json={
                    "title": "Invalid assignee",
                    "due_at": due_at,
                    "assignee_id": other_employee_id,
                },
            )
        ).status_code == 403
        assert (
            await employee.post(
                "/api/v1/tasks",
                headers=employee_headers,
                json={
                    "title": "Foreign deal IDOR",
                    "due_at": due_at,
                    "assignee_id": employee_id,
                    "deal_id": foreign_deal["id"],
                },
            )
        ).status_code == 404
        assert (
            await employee.post(
                "/api/v1/tasks",
                headers=employee_headers,
                json={
                    "title": "Foreign contact IDOR",
                    "due_at": due_at,
                    "assignee_id": employee_id,
                    "contact_id": foreign_contact["id"],
                },
            )
        ).status_code == 404
        updated_task = await employee.patch(
            f"/api/v1/tasks/{own_task['id']}",
            headers=employee_headers,
            json={"expected_version": own_task["version"], "title": "Updated own task"},
        )
        assert updated_task.status_code == 200, updated_task.text
        assert (
            await employee.patch(
                f"/api/v1/tasks/{own_task['id']}",
                headers=employee_headers,
                json={
                    "expected_version": updated_task.json()["version"],
                    "assignee_id": other_employee_id,
                },
            )
        ).status_code == 403
        assert (
            await employee.patch(
                f"/api/v1/tasks/{foreign_task['id']}",
                headers=employee_headers,
                json={"expected_version": foreign_task["version"], "title": "IDOR"},
            )
        ).status_code == 404
        assert (
            await employee.delete(
                f"/api/v1/tasks/{own_task['id']}",
                headers=employee_headers,
                params={"expected_version": updated_task.json()["version"]},
            )
        ).status_code == 403

        purchases = await employee.get(
            f"/api/v1/contacts/{linked_contact['id']}/purchases"
        )
        assert purchases.status_code == 200, purchases.text
        assert {item["id"] for item in purchases.json()["items"]} == {
            own_deal["id"],
            own_won_deal["id"],
        }
        assert (
            await employee.get(f"/api/v1/contacts/{foreign_contact['id']}/purchases")
        ).status_code == 404

        assert (
            await employee.post(
                f"/api/v1/deals/{own_deal['id']}/notes",
                headers=employee_headers,
                json={"body": "Own deal note"},
            )
        ).status_code == 201
        assert (
            await employee.post(
                f"/api/v1/deals/{foreign_deal['id']}/notes",
                headers=employee_headers,
                json={"body": "Foreign deal note"},
            )
        ).status_code == 404
        assert (
            await employee.post(
                f"/api/v1/contacts/{linked_contact['id']}/notes",
                headers=employee_headers,
                json={"body": "Visible contact note"},
            )
        ).status_code == 201
        assert (
            await employee.post(
                f"/api/v1/companies/{own_company['id']}/notes",
                headers=employee_headers,
                json={"body": "Forbidden company note"},
            )
        ).status_code == 403

        activity = await employee.get("/api/v1/activity", params={"limit": 100})
        assert activity.status_code == 200, activity.text
        activity_items = activity.json()["items"]
        visible_activity_ids = {item["entity_id"] for item in activity_items}
        note_payloads = {
            (item["event_type"], item["entity_id"]): item["payload"]
            for item in activity_items
            if item["event_type"] in {"deal.note.created", "contact.note.created"}
        }
        assert note_payloads[("deal.note.created", own_deal["id"])] == {
            "body": "Own deal note"
        }
        assert note_payloads[("contact.note.created", linked_contact["id"])] == {
            "body": "Visible contact note"
        }
        assert all(
            item["payload"] == {}
            for item in activity_items
            if item["event_type"] not in {"deal.note.created", "contact.note.created"}
        )
        assert foreign_contact["id"] not in visible_activity_ids
        assert foreign_deal["id"] not in visible_activity_ids
        assert foreign_task["id"] not in visible_activity_ids

        async with SessionLocal() as db:
            employee_uuid = uuid.UUID(employee_id)
            owner_uuid = uuid.UUID(str(owner_auth["user"]["id"]))
            assert await notification_target_access_allowed(
                db,
                workspace_id=uuid.UUID(str(owner_auth["workspace"]["id"])),
                recipient_id=employee_uuid,
                target_entity_type="deal",
                target_entity_id=uuid.UUID(own_deal["id"]),
            )
            assert not await notification_target_access_allowed(
                db,
                workspace_id=uuid.UUID(str(owner_auth["workspace"]["id"])),
                recipient_id=employee_uuid,
                target_entity_type="deal",
                target_entity_id=uuid.UUID(foreign_deal["id"]),
            )
            assert await notification_target_access_allowed(
                db,
                workspace_id=uuid.UUID(str(owner_auth["workspace"]["id"])),
                recipient_id=employee_uuid,
                target_entity_type="task",
                target_entity_id=uuid.UUID(own_task["id"]),
            )
            assert not await notification_target_access_allowed(
                db,
                workspace_id=uuid.UUID(str(owner_auth["workspace"]["id"])),
                recipient_id=employee_uuid,
                target_entity_type="contact",
                target_entity_id=uuid.UUID(explicit_contact["id"]),
            )
            assert await notification_target_access_allowed(
                db,
                workspace_id=uuid.UUID(str(owner_auth["workspace"]["id"])),
                recipient_id=owner_uuid,
                target_entity_type=None,
                target_entity_id=None,
            )
            assert not await notification_target_access_allowed(
                db,
                workspace_id=uuid.UUID(str(owner_auth["workspace"]["id"])),
                recipient_id=None,
                target_entity_type="deal",
                target_entity_id=uuid.UUID(own_deal["id"]),
            )
    finally:
        await employee.aclose()
        await other_employee.aclose()

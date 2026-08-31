from __future__ import annotations

import uuid

import httpx
import pytest
import sqlalchemy as sa

from app.db import SessionLocal
from app.main import app
from app.models import ActivityEvent, Company, Contact, Deal, Workspace


def csrf(auth: dict[str, object]) -> dict[str, str]:
    return {"X-CSRF-Token": str(auth["csrf_token"])}


@pytest.mark.asyncio
async def test_company_soft_delete_is_versioned_and_detaches_active_records(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
) -> None:
    headers = csrf(owner_auth)
    owner_id = owner_auth["user"]["id"]  # type: ignore[index]
    company_response = await client.post(
        "/api/v1/companies", headers=headers, json={"name": "Delete test company"}
    )
    assert company_response.status_code == 201, company_response.text
    company = company_response.json()
    contact_response = await client.post(
        "/api/v1/contacts",
        headers=headers,
        json={
            "first_name": "Linked",
            "last_name": "Contact",
            "company_id": company["id"],
            "assignee_id": owner_id,
        },
    )
    assert contact_response.status_code == 201, contact_response.text
    contact = contact_response.json()
    pipeline = (await client.get("/api/v1/pipelines")).json()[0]
    deal_response = await client.post(
        "/api/v1/deals",
        headers=headers,
        json={
            "title": "Linked deal",
            "pipeline_id": pipeline["id"],
            "stage_id": pipeline["stages"][0]["id"],
            "company_id": company["id"],
            "assignee_id": owner_id,
        },
    )
    assert deal_response.status_code == 201, deal_response.text
    deal = deal_response.json()

    stale = await client.delete(
        f"/api/v1/companies/{company['id']}",
        headers=headers,
        params={"expected_version": company["version"] + 1},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "version_conflict"

    deleted = await client.delete(
        f"/api/v1/companies/{company['id']}",
        headers=headers,
        params={"expected_version": company["version"]},
    )
    assert deleted.status_code == 204, deleted.text

    assert (await client.get(f"/api/v1/companies/{company['id']}")).status_code == 404
    companies = await client.get("/api/v1/companies", params={"search": "Delete test"})
    assert companies.status_code == 200
    assert companies.json()["items"] == []
    loaded_contact = await client.get(f"/api/v1/contacts/{contact['id']}")
    loaded_deal = await client.get(f"/api/v1/deals/{deal['id']}")
    assert loaded_contact.status_code == 200
    assert loaded_contact.json()["company_id"] is None
    assert loaded_contact.json()["version"] == contact["version"] + 1
    assert loaded_deal.status_code == 200
    assert loaded_deal.json()["company_id"] is None
    assert loaded_deal.json()["version"] == deal["version"] + 1

    repeated = await client.delete(
        f"/api/v1/companies/{company['id']}",
        headers=headers,
        params={"expected_version": company["version"] + 1},
    )
    assert repeated.status_code == 404

    company_id = uuid.UUID(company["id"])
    async with SessionLocal() as db:
        stored = await db.get(Company, company_id)
        assert stored is not None
        assert stored.deleted_at is not None
        assert stored.version == company["version"] + 1
        stored_contact = await db.get(Contact, uuid.UUID(contact["id"]))
        stored_deal = await db.get(Deal, uuid.UUID(deal["id"]))
        assert stored_contact is not None and stored_contact.company_id is None
        assert stored_deal is not None and stored_deal.company_id is None
        assert (
            await db.scalar(
                sa.select(sa.func.count())
                .select_from(ActivityEvent)
                .where(
                    ActivityEvent.entity_id == company_id,
                    ActivityEvent.event_type == "company.deleted",
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_company_delete_is_role_and_workspace_scoped(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
) -> None:
    headers = csrf(owner_auth)
    company = await client.post(
        "/api/v1/companies", headers=headers, json={"name": "Owner company"}
    )
    assert company.status_code == 201, company.text

    invitation = await client.post(
        "/api/v1/invitations",
        headers=headers,
        json={"email": "company-employee@example.com", "role": "employee"},
    )
    assert invitation.status_code == 201, invitation.text
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as employee:
        accepted = await employee.post(
            "/api/v1/auth/accept-invitation",
            json={
                "token": invitation.json()["token"],
                "full_name": "Company Employee",
                "password": "company employee password",
            },
        )
        assert accepted.status_code == 201, accepted.text
        forbidden = await employee.delete(
            f"/api/v1/companies/{company.json()['id']}",
            headers=csrf(accepted.json()),
            params={"expected_version": company.json()["version"]},
        )
        assert forbidden.status_code == 403

    async with SessionLocal() as db:
        other_workspace = Workspace(name="Other company workspace", slug="other-company")
        db.add(other_workspace)
        await db.flush()
        foreign_company = Company(workspace_id=other_workspace.id, name="Foreign company")
        db.add(foreign_company)
        await db.commit()
        foreign_id = foreign_company.id

    hidden = await client.delete(
        f"/api/v1/companies/{foreign_id}",
        headers=headers,
        params={"expected_version": 1},
    )
    assert hidden.status_code == 404
    loaded = await client.get(f"/api/v1/companies/{company.json()['id']}")
    assert loaded.status_code == 200

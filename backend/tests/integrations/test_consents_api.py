from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import sqlalchemy as sa

from app.db import SessionLocal
from app.integrations.models import ConsentStatus, ContactChannelConsent
from app.models import ActivityEvent, Contact, OutboxEvent, RealtimeEvent, Workspace


async def _create_contact(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
    *,
    name: str = "Анна",
) -> dict[str, object]:
    response = await client.post(
        "/api/v1/contacts",
        headers={"X-CSRF-Token": str(owner_auth["csrf_token"])},
        json={"first_name": name},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _grant_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "channel": "email",
        "address": "  Customer@Example.COM ",
        "source": "manual",
        "evidence": {
            "captured_by": "owner",
            "confirmation": "written request",
        },
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_grant_list_revoke_and_idempotent_regrant(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
) -> None:
    contact = await _create_contact(client, owner_auth)
    contact_id = str(contact["id"])
    csrf = {"X-CSRF-Token": str(owner_auth["csrf_token"])}

    granted = await client.post(
        f"/api/v1/contacts/{contact_id}/consents",
        headers=csrf,
        json=_grant_payload(),
    )
    assert granted.status_code == 200, granted.text
    consent = granted.json()
    assert consent["channel"] == "email"
    assert consent["address"] == "Customer@Example.COM"
    assert consent["normalized_address"] == "customer@example.com"
    assert consent["purpose"] == "notifications"
    assert consent["status"] == "granted"
    assert consent["source"] == "manual"
    assert consent["granted_at"] is not None
    assert consent["revoked_at"] is None

    duplicate = await client.post(
        f"/api/v1/contacts/{contact_id}/consents",
        headers=csrf,
        json=_grant_payload(
            address="customer@example.com",
            evidence={"captured_by": "duplicate request"},
        ),
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["id"] == consent["id"]
    assert duplicate.json()["evidence"] == consent["evidence"]

    listed = await client.get(f"/api/v1/contacts/{contact_id}/consents")
    assert listed.status_code == 200, listed.text
    assert listed.json() == [consent]

    revoked = await client.post(
        f"/api/v1/contacts/{contact_id}/consents/{consent['id']}/revoke",
        headers=csrf,
        json={
            "source": "manual",
            "evidence": {"reason": "customer request", "ticket": "SUP-42"},
        },
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["id"] == consent["id"]
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["revoked_at"] is not None

    regranted = await client.post(
        f"/api/v1/contacts/{contact_id}/consents",
        headers=csrf,
        json=_grant_payload(
            address="CUSTOMER@example.com",
            source="html_form",
            evidence={"form_submission_id": "form-84", "checkbox": True},
        ),
    )
    assert regranted.status_code == 200, regranted.text
    assert regranted.json()["id"] == consent["id"]
    assert regranted.json()["status"] == "granted"
    assert regranted.json()["source"] == "html_form"
    assert regranted.json()["revoked_at"] is None

    repeated_regrant = await client.post(
        f"/api/v1/contacts/{contact_id}/consents",
        headers=csrf,
        json=_grant_payload(
            address="customer@example.com",
            source="webhook",
            evidence={"request_id": "duplicate-regrant"},
        ),
    )
    assert repeated_regrant.status_code == 200, repeated_regrant.text
    assert repeated_regrant.json()["id"] == consent["id"]
    assert repeated_regrant.json()["source"] == "html_form"

    async with SessionLocal() as db:
        consent_count = await db.scalar(
            sa.select(sa.func.count()).select_from(ContactChannelConsent)
        )
        activity_types = list(
            (
                await db.scalars(
                    sa.select(ActivityEvent.event_type).where(
                        ActivityEvent.event_type.in_(
                            ["contact.consent.granted", "contact.consent.revoked"]
                        )
                    )
                )
            ).all()
        )
        outbox_types = list(
            (
                await db.scalars(
                    sa.select(OutboxEvent.event_type).where(
                        OutboxEvent.event_type.in_(
                            ["contact.consent.granted", "contact.consent.revoked"]
                        )
                    )
                )
            ).all()
        )
        realtime_types = list(
            (
                await db.scalars(
                    sa.select(RealtimeEvent.event_type).where(
                        RealtimeEvent.event_type.in_(
                            ["contact.consent.granted", "contact.consent.revoked"]
                        )
                    )
                )
            ).all()
        )
    assert consent_count == 1
    assert activity_types.count("contact.consent.granted") == 2
    assert activity_types.count("contact.consent.revoked") == 1
    assert sorted(outbox_types) == sorted(activity_types)
    assert sorted(realtime_types) == sorted(activity_types)


@pytest.mark.asyncio
async def test_consent_validation_and_workspace_isolation(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
) -> None:
    contact = await _create_contact(client, owner_auth)
    contact_id = str(contact["id"])
    csrf = {"X-CSRF-Token": str(owner_auth["csrf_token"])}

    unsupported = await client.post(
        f"/api/v1/contacts/{contact_id}/consents",
        headers=csrf,
        json=_grant_payload(channel="sms"),
    )
    assert unsupported.status_code == 422
    invalid_email = await client.post(
        f"/api/v1/contacts/{contact_id}/consents",
        headers=csrf,
        json=_grant_payload(address="not-an-email"),
    )
    assert invalid_email.status_code == 422
    missing_evidence = await client.post(
        f"/api/v1/contacts/{contact_id}/consents",
        headers=csrf,
        json=_grant_payload(evidence={}),
    )
    assert missing_evidence.status_code == 422
    invalid_source = await client.post(
        f"/api/v1/contacts/{contact_id}/consents",
        headers=csrf,
        json=_grant_payload(source="Untrusted source!"),
    )
    assert invalid_source.status_code == 422

    async with SessionLocal() as db:
        other_workspace = Workspace(name="Other", slug="consent-other")
        db.add(other_workspace)
        await db.flush()
        other_contact = Contact(workspace_id=other_workspace.id, first_name="Чужой контакт")
        db.add(other_contact)
        await db.flush()
        other_consent = ContactChannelConsent(
            workspace_id=other_workspace.id,
            contact_id=other_contact.id,
            channel="email",
            address="other@example.com",
            normalized_address="other@example.com",
            purpose="notifications",
            status=ConsentStatus.granted,
            source="manual",
            evidence={"confirmation": "other workspace"},
            granted_at=datetime.now(UTC),
        )
        db.add(other_consent)
        await db.commit()

    hidden_list = await client.get(f"/api/v1/contacts/{other_contact.id}/consents")
    assert hidden_list.status_code == 404
    hidden_grant = await client.post(
        f"/api/v1/contacts/{other_contact.id}/consents",
        headers=csrf,
        json=_grant_payload(),
    )
    assert hidden_grant.status_code == 404
    hidden_revoke = await client.post(
        f"/api/v1/contacts/{contact_id}/consents/{other_consent.id}/revoke",
        headers=csrf,
        json={"source": "manual", "evidence": {"reason": "must stay hidden"}},
    )
    assert hidden_revoke.status_code == 404

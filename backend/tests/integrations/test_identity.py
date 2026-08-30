from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest

from app.db import SessionLocal
from app.integrations.identity import (
    IdentityNormalizationError,
    MatchKind,
    match_contact_point,
    match_external_identity,
    normalize_email_address,
    normalize_phone_number,
)
from app.integrations.models import ContactPointKind, ExternalIdentity
from app.models import Contact


def test_identity_normalization() -> None:
    assert normalize_email_address(" Anna@Example.COM ") == "anna@example.com"
    assert normalize_phone_number("8 (000) 000-00-01") == "+70000000001"
    assert normalize_phone_number("+44 20 7946 0958") == "+442079460958"
    with pytest.raises(IdentityNormalizationError):
        normalize_phone_number("123")


@pytest.mark.asyncio
async def test_identity_matching_ignores_soft_deleted_contacts(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    created = await client.post(
        "/api/v1/contacts",
        headers={"X-CSRF-Token": str(owner_auth["csrf_token"])},
        json={"first_name": "Deleted", "primary_email": "deleted@example.com"},
    )
    assert created.status_code == 201, created.text

    async with SessionLocal() as db:
        contact = await db.get(Contact, uuid.UUID(created.json()["id"]))
        assert contact is not None
        contact.deleted_at = datetime.now(UTC)
        db.add(
            ExternalIdentity(
                workspace_id=contact.workspace_id,
                contact_id=contact.id,
                provider="test-provider",
                connection_scope="global",
                external_user_id="deleted-external-id",
            )
        )
        await db.commit()
        workspace_id = contact.workspace_id

    async with SessionLocal() as db:
        point_match = await match_contact_point(
            db,
            workspace_id=workspace_id,
            kind=ContactPointKind.email,
            value="deleted@example.com",
        )
        identity_match = await match_external_identity(
            db,
            workspace_id=workspace_id,
            provider="test-provider",
            external_user_id="deleted-external-id",
            channel_connection_id=None,
        )

    assert point_match.kind is MatchKind.none
    assert identity_match.kind is MatchKind.none

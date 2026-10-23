from __future__ import annotations

import uuid

import httpx
import pytest
import sqlalchemy as sa

from app.db import SessionLocal
from app.integrations.models import ContactPoint


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", ["contacts", "companies"])
async def test_communication_lists_keep_order_and_support_editing_and_search(
    client: httpx.AsyncClient, owner_auth: dict[str, object], collection: str
) -> None:
    headers = {"X-CSRF-Token": str(owner_auth["csrf_token"])}
    payload = (
        {"first_name": "Test Person"} if collection == "contacts" else {"name": "Test Company"}
    )
    primary_email = "primary_email" if collection == "contacts" else "email"
    primary_phone = "primary_phone" if collection == "contacts" else "phone"
    created = await client.post(
        f"/api/v1/{collection}",
        headers=headers,
        json={
            **payload,
            "emails": ["first@example.com", "second@example.com", "first@example.com"],
            "phones": ["+1 202 555 0101", "+1 202 555 0102"],
        },
    )
    assert created.status_code == 201, created.text
    record = created.json()
    assert record[primary_email] == "first@example.com"
    assert record[primary_phone] == "+1 202 555 0101"
    assert record["emails"] == ["first@example.com", "second@example.com"]
    if collection == "contacts":
        async with SessionLocal() as db:
            points = list(
                (
                    await db.scalars(
                        sa.select(ContactPoint).where(
                            ContactPoint.contact_id == uuid.UUID(record["id"])
                        )
                    )
                ).all()
            )
            assert {point.normalized_value for point in points} == {
                "first@example.com",
                "second@example.com",
                "+12025550101",
                "+12025550102",
            }
            assert {point.normalized_value for point in points if point.is_primary} == {
                "first@example.com",
                "+12025550101",
            }
    for term in ("second@example.com", "+1 202 555 0102"):
        searched = await client.get(f"/api/v1/{collection}", params={"search": term})
        assert searched.status_code == 200, searched.text
        assert [item["id"] for item in searched.json()["items"]] == [record["id"]]

    updated = await client.patch(
        f"/api/v1/{collection}/{record['id']}",
        headers=headers,
        json={
            "expected_version": record["version"],
            "emails": ["second@example.com"],
            "phones": ["+1 202 555 0102", "+1 202 555 0101"],
        },
    )
    assert updated.status_code == 200, updated.text
    record = updated.json()
    assert record["emails"] == ["second@example.com"]
    assert record[primary_email] == "second@example.com"
    assert record[primary_phone] == "+1 202 555 0102"
    cleared = await client.patch(
        f"/api/v1/{collection}/{record['id']}",
        headers=headers,
        json={
            "expected_version": record["version"],
            "emails": [],
            "phones": [],
            primary_email: "second@example.com",
            primary_phone: "+1 202 555 0102",
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["emails"] == []
    assert cleared.json()["phones"] == []
    assert cleared.json()[primary_email] is None
    assert cleared.json()[primary_phone] is None
    loaded = await client.get(f"/api/v1/{collection}/{record['id']}")
    assert loaded.status_code == 200, loaded.text
    for field in ("emails", "phones", primary_email, primary_phone, "version"):
        assert loaded.json()[field] == cleared.json()[field]
    if collection == "contacts":
        async with SessionLocal() as db:
            assert (
                await db.scalar(
                    sa.select(sa.func.count())
                    .select_from(ContactPoint)
                    .where(ContactPoint.contact_id == uuid.UUID(record["id"]))
                )
                == 0
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", ["contacts", "companies"])
async def test_legacy_primary_edits_preserve_secondary_values(
    client: httpx.AsyncClient, owner_auth: dict[str, object], collection: str
) -> None:
    headers = {"X-CSRF-Token": str(owner_auth["csrf_token"])}
    payload = (
        {"first_name": "Legacy Person"} if collection == "contacts" else {"name": "Legacy Company"}
    )
    email_field = "primary_email" if collection == "contacts" else "email"
    created = await client.post(
        f"/api/v1/{collection}",
        headers=headers,
        json={**payload, email_field: "first@example.com"},
    )
    assert created.status_code == 201, created.text
    record = created.json()
    assert record["emails"] == ["first@example.com"]
    for changes, expected in (
        (
            {"emails": ["first@example.com", "extra@example.com"]},
            ["first@example.com", "extra@example.com"],
        ),
        (
            {email_field: "replacement@example.com"},
            ["replacement@example.com", "extra@example.com"],
        ),
        ({"tags": ["Edited"]}, ["replacement@example.com", "extra@example.com"]),
        ({email_field: None}, ["extra@example.com"]),
        (
            {"emails": ["list@example.com"], email_field: "legacy@example.com"},
            ["list@example.com", "legacy@example.com"],
        ),
    ):
        updated = await client.patch(
            f"/api/v1/{collection}/{record['id']}",
            headers=headers,
            json={"expected_version": record["version"], **changes},
        )
        assert updated.status_code == 200, updated.text
        record = updated.json()
        assert record["emails"] == expected
        assert record[email_field] == expected[0]
    for field in ("emails", "phones"):
        rejected = await client.patch(
            f"/api/v1/{collection}/{record['id']}",
            headers=headers,
            json={"expected_version": record["version"], field: None},
        )
        assert rejected.status_code == 422, rejected.text


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", ["contacts", "companies"])
@pytest.mark.parametrize(
    "invalid",
    [
        {"emails": ["invalid-email"]},
        {"emails": [f"test{index}@example.com" for index in range(21)]},
        {"phones": ["+1" + "0" * 63]},
        {"phones": ["+12025550101"] * 21},
    ],
)
async def test_communication_lists_validate_every_value_and_limit(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
    collection: str,
    invalid: dict[str, list[str]],
) -> None:
    payload = (
        {"first_name": "Test Person"} if collection == "contacts" else {"name": "Test Company"}
    )
    response = await client.post(
        f"/api/v1/{collection}",
        headers={"X-CSRF-Token": str(owner_auth["csrf_token"])},
        json={**payload, **invalid},
    )
    assert response.status_code == 422, response.text

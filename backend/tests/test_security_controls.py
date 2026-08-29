from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
import sqlalchemy as sa
from fastapi import HTTPException

from app.api.events import _public_event_payload
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.main import app
from app.models import ActivityEvent, CursorAccessBucket, RealtimeEvent, User, Workspace
from app.security import require_crm_export_enabled
from app.services.data_access import consume_cursor_page_budget


@pytest.mark.asyncio
async def test_export_policy_is_disabled_by_default_and_owner_only(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
) -> None:
    status = await client.get("/api/v1/admin/security/export-policy")
    assert status.status_code == 200, status.text
    assert status.json() == {"enabled": False, "allowed_role": "owner"}
    assert status.headers["cache-control"] == "no-store"
    assert status.headers["pragma"] == "no-cache"

    invitation = await client.post(
        "/api/v1/invitations",
        headers={"X-CSRF-Token": str(owner_auth["csrf_token"])},
        json={"email": "export-manager@example.com", "role": "manager"},
    )
    accepted = await client.post(
        "/api/v1/auth/accept-invitation",
        json={
            "token": invitation.json()["token"],
            "full_name": "Export Manager",
            "password": "secure export manager password",
        },
    )
    assert accepted.status_code == 201, accepted.text

    forbidden = await client.get("/api/v1/admin/security/export-policy")
    assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_export_guard_requires_explicit_server_opt_in() -> None:
    marker = object()
    with pytest.raises(HTTPException) as exc_info:
        await require_crm_export_enabled(  # type: ignore[arg-type]
            marker,
            Settings(crm_export_enabled=False),
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "crm_export_disabled"

    allowed = await require_crm_export_enabled(  # type: ignore[arg-type]
        marker,
        Settings(crm_export_enabled=True),
    )
    assert allowed is marker


@pytest.mark.asyncio
async def test_export_policy_reports_effective_opt_in(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
) -> None:
    del owner_auth
    app.dependency_overrides[get_settings] = lambda: Settings(crm_export_enabled=True)
    try:
        response = await client.get("/api/v1/admin/security/export-policy")
    finally:
        app.dependency_overrides.pop(get_settings, None)
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is True


def test_realtime_payload_removes_sensitive_fields_and_scopes_notifications() -> None:
    user_id = uuid.uuid4()
    business_event = RealtimeEvent(
        workspace_id=uuid.uuid4(),
        event_type="contact.consent.granted",
        payload={
            "entity_type": "contact",
            "entity_id": str(uuid.uuid4()),
            "address": "customer@example.com",
            "evidence": {"confirmation": "private"},
        },
    )
    public = _public_event_payload(business_event, user_id=user_id)
    assert public == {
        "entity_type": "contact",
        "entity_id": business_event.payload["entity_id"],
    }

    private_notification = RealtimeEvent(
        workspace_id=business_event.workspace_id,
        event_type="notification.delivered",
        payload={
            "delivery_id": str(uuid.uuid4()),
            "recipient_id": str(uuid.uuid4()),
            "subject": "Private subject",
            "body": "Private body",
        },
    )
    assert _public_event_payload(private_notification, user_id=user_id) is None

    missing_recipient = RealtimeEvent(
        workspace_id=business_event.workspace_id,
        event_type="notification.delivered",
        payload={"delivery_id": str(uuid.uuid4())},
    )
    assert _public_event_payload(missing_recipient, user_id=user_id) is None

    own_notification = RealtimeEvent(
        workspace_id=business_event.workspace_id,
        event_type="notification.delivered",
        payload={**private_notification.payload, "recipient_id": str(user_id)},
    )
    assert _public_event_payload(own_notification, user_id=user_id) == {
        "delivery_id": own_notification.payload["delivery_id"]
    }


@pytest.mark.asyncio
async def test_cursor_budget_does_not_count_first_pages_and_audits_one_crossing(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.data_access.get_settings",
        lambda: Settings(cursor_page_budget=2, cursor_page_window_seconds=900),
    )
    csrf = str(owner_auth["csrf_token"])
    for index in range(2):
        response = await client.post(
            "/api/v1/companies",
            headers={"X-CSRF-Token": csrf},
            json={"name": f"Budget company {index}"},
        )
        assert response.status_code == 201, response.text

    # Ordinary UI refreshes of the first page are deliberately unmetered.
    for _ in range(5):
        first = await client.get("/api/v1/companies", params={"limit": 1})
        assert first.status_code == 200, first.text
    cursor = first.json()["next_cursor"]
    assert cursor

    assert (
        await client.get("/api/v1/companies", params={"limit": 1, "cursor": cursor})
    ).status_code == 200
    assert (
        await client.get("/api/v1/companies", params={"limit": 1, "cursor": cursor})
    ).status_code == 200
    blocked = await client.get(
        "/api/v1/companies", params={"limit": 1, "cursor": cursor}
    )
    assert blocked.status_code == 429, blocked.text
    assert blocked.json()["detail"]["code"] == "cursor_page_budget_exceeded"
    assert int(blocked.headers["retry-after"]) > 0

    from app.db import SessionLocal

    async with SessionLocal() as db:
        buckets = list((await db.scalars(sa.select(CursorAccessBucket))).all())
        assert len(buckets) == 1
        assert buckets[0].resource == "companies"
        events = list(
            (
                await db.scalars(
                    sa.select(ActivityEvent).where(
                        ActivityEvent.event_type == "data_access.cursor_budget_exceeded"
                    )
                )
            ).all()
        )
        assert len(events) == 1
        assert events[0].payload["resource"] == "companies"


@pytest.mark.asyncio
async def test_cursor_budget_resets_in_place_after_fixed_window(
) -> None:
    # A real workspace/user pair keeps SQLite foreign-key semantics honest.
    async with SessionLocal() as db:
        workspace = Workspace(name="Budget", slug="budget")
        user = User(
            email="budget@example.com",
            full_name="Budget User",
            password_hash="not-used",
        )
        db.add_all([workspace, user])
        await db.flush()

        first_time = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
        first_count, first_window = await consume_cursor_page_budget(
            db,
            workspace_id=workspace.id,
            user_id=user.id,
            resource="contacts",
            window_seconds=60,
            now=first_time,
        )
        second_count, _ = await consume_cursor_page_budget(
            db,
            workspace_id=workspace.id,
            user_id=user.id,
            resource="contacts",
            window_seconds=60,
            now=first_time,
        )
        reset_count, reset_window = await consume_cursor_page_budget(
            db,
            workspace_id=workspace.id,
            user_id=user.id,
            resource="contacts",
            window_seconds=60,
            now=first_time.replace(minute=1),
        )
        await db.commit()

        assert (first_count, second_count, reset_count) == (1, 2, 1)
        assert reset_window > first_window
        assert await db.scalar(sa.select(sa.func.count()).select_from(CursorAccessBucket)) == 1

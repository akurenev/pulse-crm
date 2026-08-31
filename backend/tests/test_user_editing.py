from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from app.api.auth import _active_owner_count
from app.db import SessionLocal
from app.main import app
from app.models import ActivityEvent, Membership, RealtimeEvent, Role, User


def csrf(auth: dict[str, Any]) -> dict[str, str]:
    return {"X-CSRF-Token": str(auth["csrf_token"])}


async def invite_and_accept(
    owner_client: httpx.AsyncClient,
    owner_auth: dict[str, Any],
    *,
    email: str,
    full_name: str,
    role: str,
) -> tuple[httpx.AsyncClient, dict[str, Any]]:
    invitation = await owner_client.post(
        "/api/v1/invitations",
        headers=csrf(owner_auth),
        json={"email": email, "role": role},
    )
    assert invitation.status_code == 201, invitation.text
    member_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    accepted = await member_client.post(
        "/api/v1/auth/accept-invitation",
        json={
            "token": invitation.json()["token"],
            "full_name": full_name,
            "password": "member test password",
        },
    )
    assert accepted.status_code == 201, accepted.text
    return member_client, accepted.json()


@pytest.mark.asyncio
async def test_owner_updates_member_with_optimistic_lock_and_live_acl(
    client: httpx.AsyncClient,
    owner_auth: dict[str, Any],
) -> None:
    member_client, member_auth = await invite_and_accept(
        client,
        owner_auth,
        email="manager-edit@example.com",
        full_name="Manager Before",
        role="manager",
    )
    try:
        member = next(
            user
            for user in (await client.get("/api/v1/users")).json()
            if user["id"] == member_auth["user"]["id"]
        )
        assert member["version"] == 1

        blank_name = await client.patch(
            f"/api/v1/users/{member['id']}",
            headers=csrf(owner_auth),
            json={"expected_version": 1, "full_name": "   "},
        )
        assert blank_name.status_code == 422

        updated = await client.patch(
            f"/api/v1/users/{member['id']}",
            headers=csrf(owner_auth),
            json={
                "expected_version": member["version"],
                "full_name": "Employee After",
                "role": "employee",
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json() == {
            **member,
            "full_name": "Employee After",
            "role": "employee",
            "version": 2,
        }

        renamed = await client.patch(
            f"/api/v1/users/{member['id']}",
            headers=csrf(owner_auth),
            json={"expected_version": 2, "full_name": "  Employee Renamed  "},
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["full_name"] == "Employee Renamed"
        assert renamed.json()["version"] == 3

        stale = await client.patch(
            f"/api/v1/users/{member['id']}",
            headers=csrf(owner_auth),
            json={"expected_version": 1, "full_name": "Stale Write"},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "version_conflict"

        # Authentication reloads Membership on each request, so an already
        # open manager session immediately receives employee-scoped results.
        visible_users = await member_client.get("/api/v1/users")
        assert visible_users.status_code == 200, visible_users.text
        assert [user["id"] for user in visible_users.json()] == [member["id"]]

        async with SessionLocal() as db:
            audits = list(
                (
                    await db.scalars(
                        sa.select(ActivityEvent)
                        .where(ActivityEvent.event_type == "user.updated")
                        .order_by(ActivityEvent.occurred_at.desc())
                    )
                ).all()
            )
            access_events = list(
                (
                    await db.scalars(
                        sa.select(RealtimeEvent)
                        .where(RealtimeEvent.event_type == "access.changed")
                        .order_by(RealtimeEvent.created_at.desc())
                    )
                ).all()
            )
        target_audits = [event for event in audits if str(event.entity_id) == member["id"]]
        access_event = next(
            (
                event
                for event in access_events
                if event.payload.get("recipient_id") == member["id"]
            ),
            None,
        )
        assert len(target_audits) == 2
        assert any(event.payload["role_changed"] is True for event in target_audits)
        assert any(event.payload["role_changed"] is False for event in target_audits)
        target_access_events = [
            event
            for event in access_events
            if event.payload.get("recipient_id") == member["id"]
        ]
        assert len(target_access_events) == 2
        assert access_event is not None
        assert access_event.payload == {"recipient_id": member["id"], "resource": "all"}
    finally:
        await member_client.aclose()


@pytest.mark.asyncio
async def test_user_role_update_guards_owner_and_admin_boundaries(
    client: httpx.AsyncClient,
    owner_auth: dict[str, Any],
) -> None:
    admin_client, admin_auth = await invite_and_accept(
        client,
        owner_auth,
        email="admin-edit@example.com",
        full_name="Workspace Admin",
        role="admin",
    )
    manager_client, manager_auth = await invite_and_accept(
        client,
        owner_auth,
        email="manager-guard@example.com",
        full_name="Guarded Manager",
        role="manager",
    )
    try:
        users = (await client.get("/api/v1/users")).json()
        owner = next(user for user in users if user["id"] == owner_auth["user"]["id"])
        manager = next(user for user in users if user["id"] == manager_auth["user"]["id"])

        self_demote = await client.patch(
            f"/api/v1/users/{owner['id']}",
            headers=csrf(owner_auth),
            json={"expected_version": owner["version"], "role": "manager"},
        )
        assert self_demote.status_code == 409
        assert self_demote.json()["detail"]["code"] == "self_role_change_forbidden"

        owner_edit = await admin_client.patch(
            f"/api/v1/users/{owner['id']}",
            headers=csrf(admin_auth),
            json={"expected_version": owner["version"], "full_name": "Forbidden"},
        )
        assert owner_edit.status_code == 403

        elevation = await admin_client.patch(
            f"/api/v1/users/{manager['id']}",
            headers=csrf(admin_auth),
            json={"expected_version": manager["version"], "role": "admin"},
        )
        assert elevation.status_code == 403

        allowed = await admin_client.patch(
            f"/api/v1/users/{manager['id']}",
            headers=csrf(admin_auth),
            json={
                "expected_version": manager["version"],
                "full_name": "Updated by Admin",
                "role": "employee",
            },
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["role"] == "employee"
    finally:
        await admin_client.aclose()
        await manager_client.aclose()


@pytest.mark.asyncio
async def test_inactive_owner_does_not_satisfy_active_owner_invariant(
    owner_auth: dict[str, Any],
) -> None:
    workspace_id = uuid.UUID(str(owner_auth["workspace"]["id"]))
    async with SessionLocal() as db:
        inactive_owner = User(
            email="inactive-owner@example.com",
            full_name="Inactive Owner",
            password_hash="unused",
            is_active=False,
        )
        db.add(inactive_owner)
        await db.flush()
        db.add(
            Membership(
                workspace_id=workspace_id,
                user_id=inactive_owner.id,
                role=Role.owner,
            )
        )
        await db.commit()

        assert await _active_owner_count(db, workspace_id) == 1

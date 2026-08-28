from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_bootstrap_is_one_time_and_creates_session(
    client: httpx.AsyncClient, owner_payload: dict[str, str]
) -> None:
    denied = await client.post("/api/v1/auth/bootstrap", json=owner_payload)
    assert denied.status_code == 403

    created = await client.post(
        "/api/v1/auth/bootstrap",
        headers={"X-Bootstrap-Token": "test-bootstrap-token"},
        json=owner_payload,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["user"]["role"] == "owner"
    assert body["workspace"]["currency"] == "RUB"
    assert body["csrf_token"]
    assert "pulse_session" in client.cookies

    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "owner@example.com"
    assert me.json()["csrf_token"]

    restored_mutation = await client.post(
        "/api/v1/companies",
        headers={"X-CSRF-Token": me.json()["csrf_token"]},
        json={"name": "Created after reload"},
    )
    assert restored_mutation.status_code == 201

    duplicate = await client.post(
        "/api/v1/auth/bootstrap",
        headers={"X-Bootstrap-Token": "test-bootstrap-token"},
        json={**owner_payload, "workspace_slug": "other"},
    )
    assert duplicate.status_code == 409


@pytest.mark.asyncio
async def test_cookie_mutations_require_csrf(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    rejected = await client.post("/api/v1/companies", json={"name": "Acme"})
    assert rejected.status_code == 403

    accepted = await client.post(
        "/api/v1/companies",
        headers={"X-CSRF-Token": str(owner_auth["csrf_token"])},
        json={"name": "Acme"},
    )
    assert accepted.status_code == 201


@pytest.mark.asyncio
async def test_invitation_acceptance_and_manager_permissions(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    invite = await client.post(
        "/api/v1/invitations",
        headers={"X-CSRF-Token": str(owner_auth["csrf_token"])},
        json={"email": "manager@example.com", "role": "manager"},
    )
    assert invite.status_code == 201, invite.text

    accepted = await client.post(
        "/api/v1/auth/accept-invitation",
        json={
            "token": invite.json()["token"],
            "full_name": "Manager User",
            "password": "a different secure password",
        },
    )
    assert accepted.status_code == 201, accepted.text
    manager_auth = accepted.json()
    forbidden = await client.post(
        "/api/v1/invitations",
        headers={"X-CSRF-Token": manager_auth["csrf_token"]},
        json={"email": "other@example.com", "role": "manager"},
    )
    assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_login_rejects_bad_password_and_returns_new_session(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
) -> None:
    client.cookies.clear()
    bad = await client.post(
        "/api/v1/auth/login",
        json={"email": "OWNER@example.com", "password": "not the password"},
    )
    assert bad.status_code == 401
    good = await client.post(
        "/api/v1/auth/login",
        json={"email": "OWNER@example.com", "password": "correct horse battery staple"},
    )
    assert good.status_code == 200
    assert good.json()["csrf_token"]


@pytest.mark.asyncio
async def test_owner_can_invite_admin(
    client: httpx.AsyncClient, owner_auth: dict[str, object]
) -> None:
    response = await client.post(
        "/api/v1/invitations",
        headers={"X-CSRF-Token": str(owner_auth["csrf_token"])},
        json={"email": "admin@example.com", "role": "admin"},
    )
    assert response.status_code == 201
    assert response.json()["role"] == "admin"

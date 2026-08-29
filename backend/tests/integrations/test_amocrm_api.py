from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI

from app.db import SessionLocal
from app.integrations.amocrm_api import normalize_amocrm_referer, router
from app.integrations.amocrm_live import AmoTokenManager
from app.integrations.models import (
    AmoConnectionStatus,
    AmoCRMConnection,
    AmoOAuthState,
)
from app.integrations.secrets import SecretCipher
from app.models import Membership, Role, Session, User, Workspace
from app.security import digest_token

API_ROOT = "/api/v1/admin/integrations/amocrm"


@dataclass(frozen=True, slots=True)
class OAuthSeed:
    workspace_id: uuid.UUID
    owner_token: str
    manager_token: str


@pytest_asyncio.fixture
async def oauth_seed() -> OAuthSeed:
    async with SessionLocal() as db:
        workspace = Workspace(name="amoCRM OAuth", slug="amocrm-oauth")
        owner = User(email="amo-owner@example.com", full_name="Owner", password_hash="unused")
        manager = User(email="amo-manager@example.com", full_name="Manager", password_hash="unused")
        db.add_all([workspace, owner, manager])
        await db.flush()
        db.add_all(
            [
                Membership(workspace_id=workspace.id, user_id=owner.id, role=Role.owner),
                Membership(workspace_id=workspace.id, user_id=manager.id, role=Role.manager),
            ]
        )
        owner_token = "amocrm-owner-token"
        manager_token = "amocrm-manager-token"
        expires_at = datetime.now(UTC) + timedelta(hours=1)
        db.add_all(
            [
                Session(
                    user_id=owner.id,
                    workspace_id=workspace.id,
                    token_hash=digest_token(owner_token),
                    csrf_token_hash=digest_token("unused"),
                    expires_at=expires_at,
                ),
                Session(
                    user_id=manager.id,
                    workspace_id=workspace.id,
                    token_hash=digest_token(manager_token),
                    csrf_token_hash=digest_token("unused"),
                    expires_at=expires_at,
                ),
            ]
        )
        await db.commit()
        return OAuthSeed(
            workspace_id=workspace.id,
            owner_token=owner_token,
            manager_token=manager_token,
        )


@pytest_asyncio.fixture
async def oauth_client() -> AsyncIterator[
    tuple[httpx.AsyncClient, SecretCipher, list[dict[str, str]]]
]:
    cipher = SecretCipher(key=b"o" * 32, key_id="oauth-test")
    exchanges: list[dict[str, str]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://example.amocrm.ru/oauth2/access_token"
        payload = json.loads(request.content)
        assert isinstance(payload, dict)
        exchanges.append(payload)
        return httpx.Response(
            200,
            json={
                "token_type": "Bearer",
                "expires_in": 86_400,
                "access_token": "provider-access-token",
                "refresh_token": "provider-refresh-token",
                "account_id": 12345,
            },
        )

    provider_client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    app = FastAPI()
    app.state.integration_secret_cipher = cipher
    app.state.amocrm_http_client = provider_client
    app.include_router(router, prefix="/api/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client, cipher, exchanges
    await provider_client.aclose()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def start_oauth(
    client: httpx.AsyncClient,
    token: str,
    *,
    referers: list[str] | None = None,
) -> tuple[str, dict[str, object]]:
    response = await client.post(
        f"{API_ROOT}/oauth/start",
        headers=auth(token),
        json={
            "client_id": "integration-client-id",
            "client_secret": "integration-client-secret",
            "redirect_uri": "https://pulse.example.com/api/v1/admin/integrations/amocrm/oauth/callback",
            "allowed_referers": referers or ["example.amocrm.ru"],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    parsed = urlsplit(body["authorization_url"])
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://www.amocrm.ru/oauth"
    query = parse_qs(parsed.query)
    assert query["client_id"] == ["integration-client-id"]
    assert query["mode"] == ["post_message"]
    return query["state"][0], body


@pytest.mark.asyncio
async def test_oauth_state_is_digest_only_one_time_and_connection_is_secret_safe(
    oauth_client: tuple[httpx.AsyncClient, SecretCipher, list[dict[str, str]]],
    oauth_seed: OAuthSeed,
) -> None:
    client, cipher, exchanges = oauth_client
    forbidden = await client.post(
        f"{API_ROOT}/oauth/start",
        headers=auth(oauth_seed.manager_token),
        json={
            "client_id": "id",
            "client_secret": "secret",
            "redirect_uri": "https://pulse.example.com/callback",
            "allowed_referers": ["example.amocrm.ru"],
        },
    )
    assert forbidden.status_code == 403

    raw_state, _ = await start_oauth(client, oauth_seed.owner_token)
    async with SessionLocal() as db:
        stored_state = await db.scalar(sa.select(AmoOAuthState))
        assert stored_state is not None
        assert stored_state.state_digest == hashlib.sha256(raw_state.encode()).hexdigest()
        assert raw_state.encode() not in stored_state.encrypted_client_secret
        assert b"integration-client-secret" not in stored_state.encrypted_client_secret

    callback = await client.get(
        f"{API_ROOT}/oauth/callback",
        params={
            "code": "one-time-code",
            "state": raw_state,
            "referer": "https://example.amocrm.ru/",
        },
    )
    assert callback.status_code == 200, callback.text
    assert "pulse:amocrm-oauth" in callback.text
    assert "status:'ok'" in callback.text
    nonce = callback.text.split('nonce="', 1)[1].split('"', 1)[0]
    assert f"script-src 'nonce-{nonce}'" in callback.headers["content-security-policy"]
    assert callback.headers["cache-control"] == "no-store"
    assert "provider-access-token" not in callback.text
    assert "provider-refresh-token" not in callback.text
    assert "integration-client-secret" not in callback.text
    assert exchanges == [
        {
            "client_id": "integration-client-id",
            "client_secret": "integration-client-secret",
            "grant_type": "authorization_code",
            "code": "one-time-code",
            "redirect_uri": "https://pulse.example.com/api/v1/admin/integrations/amocrm/oauth/callback",
        }
    ]

    replay = await client.get(
        f"{API_ROOT}/oauth/callback",
        params={
            "code": "replayed-code",
            "state": raw_state,
            "referer": "example.amocrm.ru",
        },
    )
    assert replay.status_code == 400
    assert len(exchanges) == 1

    read = await client.get(f"{API_ROOT}/connection", headers=auth(oauth_seed.owner_token))
    assert read.status_code == 200
    assert "encrypted" not in read.text
    assert "secret" not in read.text
    connection_id = uuid.UUID(read.json()["id"])
    async with SessionLocal() as db:
        connection = await db.get(AmoCRMConnection, connection_id)
        assert connection is not None
        assert connection.encrypted_access_token is not None
        assert (
            cipher.decrypt(
                connection.encrypted_access_token,
                associated_data=f"amocrm-connection:{connection.id}:access".encode(),
            ).decode()
            == "provider-access-token"
        )

    disconnected = await client.post(
        f"{API_ROOT}/disconnect",
        headers=auth(oauth_seed.owner_token),
        json={"expected_version": read.json()["version"]},
    )
    assert disconnected.status_code == 204
    async with SessionLocal() as db:
        connection = await db.get(AmoCRMConnection, connection_id)
        assert connection is not None
        assert connection.status is AmoConnectionStatus.disconnected
        assert connection.encrypted_client_secret is None
        assert connection.encrypted_access_token is None
        assert connection.encrypted_refresh_token is None


@pytest.mark.asyncio
async def test_referer_allowlist_rejection_consumes_state(
    oauth_client: tuple[httpx.AsyncClient, SecretCipher, list[dict[str, str]]],
    oauth_seed: OAuthSeed,
) -> None:
    client, _, exchanges = oauth_client
    raw_state, _ = await start_oauth(client, oauth_seed.owner_token)
    rejected = await client.get(
        f"{API_ROOT}/oauth/callback",
        params={
            "code": "code",
            "state": raw_state,
            "referer": "other-team.amocrm.ru",
        },
    )
    assert rejected.status_code == 400
    assert exchanges == []
    reused = await client.get(
        f"{API_ROOT}/oauth/callback",
        params={
            "code": "code",
            "state": raw_state,
            "referer": "example.amocrm.ru",
        },
    )
    assert reused.status_code == 400


def test_referer_validation_blocks_ssrf_hosts() -> None:
    assert normalize_amocrm_referer("https://example.amocrm.ru/") == "example.amocrm.ru"
    for invalid in (
        "amocrm.ru",
        "amocrm.ru.attacker.example",
        "https://user@example.amocrm.ru",
        "https://example.amocrm.ru:444",
        "http://example.amocrm.ru",
        "http://127.0.0.1",
    ):
        with pytest.raises(ValueError):
            normalize_amocrm_referer(invalid)


@pytest.mark.asyncio
async def test_token_manager_rotates_refresh_token(
    oauth_seed: OAuthSeed,
) -> None:
    cipher = SecretCipher(key=b"r" * 32, key_id="refresh-test")
    connection_id = uuid.uuid4()
    async with SessionLocal() as db:
        connection = AmoCRMConnection(
            id=connection_id,
            workspace_id=oauth_seed.workspace_id,
            status=AmoConnectionStatus.connected,
            account_domain="example.amocrm.ru",
            client_id="client-id",
            redirect_uri="https://pulse.example.com/callback",
            encrypted_client_secret=cipher.encrypt(
                "client-secret",
                associated_data=f"amocrm-connection:{connection_id}:client-secret".encode(),
            ),
            encrypted_access_token=cipher.encrypt(
                "expired-access",
                associated_data=f"amocrm-connection:{connection_id}:access".encode(),
            ),
            encrypted_refresh_token=cipher.encrypt(
                "old-refresh",
                associated_data=f"amocrm-connection:{connection_id}:refresh".encode(),
            ),
            token_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        db.add(connection)
        await db.commit()

    refresh_payloads: list[dict[str, str]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        refresh_payloads.append(payload)
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as provider_client:
        manager = AmoTokenManager(cipher=cipher, http_client=provider_client)
        snapshot = await manager.snapshot(oauth_seed.workspace_id)
    assert snapshot.access_token == "new-access"
    assert snapshot.refresh_token == "rotated-refresh"
    assert refresh_payloads[0]["grant_type"] == "refresh_token"
    assert refresh_payloads[0]["refresh_token"] == "old-refresh"

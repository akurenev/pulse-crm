"""Owner/admin endpoints for the amoCRM OAuth connection lifecycle."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit

import httpx
import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.integrations.amocrm_live import AmoAPIError, AmoTokenSet, exchange_oauth_code
from app.integrations.models import (
    AmoConnectionStatus,
    AmoCRMConnection,
    AmoOAuthState,
)
from app.integrations.secrets import SecretCipher
from app.security import CurrentAdmin
from app.services.events import record_domain_event

router = APIRouter(prefix="/admin/integrations/amocrm", tags=["amocrm-integration"])

AMO_AUTHORIZATION_URL = "https://www.amocrm.ru/oauth"
OAUTH_STATE_TTL = timedelta(minutes=10)
MAX_OAUTH_CODE_LENGTH = 4_096


class AmoOAuthStart(BaseModel):
    client_id: str = Field(min_length=1, max_length=255)
    client_secret: SecretStr
    redirect_uri: AnyHttpUrl
    allowed_referers: list[str] = Field(min_length=1, max_length=100)

    @field_validator("client_id")
    @classmethod
    def validate_client_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("client id must not be blank")
        return value

    @field_validator("client_secret")
    @classmethod
    def validate_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("client secret must not be blank")
        return value

    @field_validator("redirect_uri")
    @classmethod
    def validate_redirect_uri(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.scheme != "https":
            raise ValueError("amoCRM redirect URI must use HTTPS")
        return value

    @field_validator("allowed_referers")
    @classmethod
    def validate_referers(cls, values: list[str]) -> list[str]:
        normalised = [normalize_amocrm_referer(value) for value in values]
        if len(set(normalised)) != len(normalised):
            raise ValueError("allowed amoCRM referers must be unique")
        return normalised


class AmoOAuthStartRead(BaseModel):
    authorization_url: str
    expires_at: datetime


class AmoConnectionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: AmoConnectionStatus
    account_domain: str
    account_id: str | None
    client_id: str
    redirect_uri: str
    token_expires_at: datetime | None
    connected_at: datetime | None
    disconnected_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class AmoDisconnect(BaseModel):
    expected_version: int = Field(ge=1)


def normalize_amocrm_referer(value: str) -> str:
    """Return a safe amoCRM account host suitable for composing API URLs."""

    raw = value.strip()
    if not raw:
        raise ValueError("amoCRM referer must not be blank")
    parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower().rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise ValueError("amoCRM referer is invalid") from exc
    if (
        parsed.scheme != "https"
        or not host.endswith(".amocrm.ru")
        or host == "amocrm.ru"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise ValueError("referer must be an amoCRM account on *.amocrm.ru")
    return host


def _state_digest(raw_state: str) -> str:
    return hashlib.sha256(raw_state.encode()).hexdigest()


def _state_aad(state_id: uuid.UUID) -> bytes:
    return f"amocrm-oauth-state:{state_id}".encode()


def _connection_aad(connection_id: uuid.UUID, field: str) -> bytes:
    return f"amocrm-connection:{connection_id}:{field}".encode()


def _cipher(request: Request) -> SecretCipher:
    value = getattr(request.app.state, "integration_secret_cipher", None)
    if not isinstance(value, SecretCipher):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="integration secret cipher is not configured",
        )
    return value


def _injected_http_client(request: Request) -> httpx.AsyncClient | None:
    value = getattr(request.app.state, "amocrm_http_client", None)
    return value if isinstance(value, httpx.AsyncClient) else None


def _connection_read(connection: AmoCRMConnection) -> AmoConnectionRead:
    return AmoConnectionRead.model_validate(connection)


def _oauth_popup_response(*, succeeded: bool, status_code: int = 200) -> HTMLResponse:
    outcome = "ok" if succeeded else "error"
    title = "amoCRM подключена" if succeeded else "Не удалось подключить amoCRM"
    nonce = secrets.token_urlsafe(24)
    return HTMLResponse(
        content=(
            '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            f'<title>{title}</title></head><body><p>{title}</p><script nonce="{nonce}">'
            "if(window.opener){window.opener.postMessage("
            f"{{type:'pulse:amocrm-oauth',status:'{outcome}'}}"
            ",window.location.origin);}window.close();</script></body></html>"
        ),
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; "
                f"script-src 'nonce-{nonce}'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/oauth/start",
    response_model=AmoOAuthStartRead,
    status_code=status.HTTP_201_CREATED,
)
async def start_oauth(
    payload: AmoOAuthStart,
    request: Request,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> AmoOAuthStartRead:
    cipher = _cipher(request)
    raw_state = secrets.token_urlsafe(32)
    state_id = uuid.uuid4()
    expires_at = datetime.now(UTC) + OAUTH_STATE_TTL
    oauth_state = AmoOAuthState(
        id=state_id,
        workspace_id=context.workspace_id,
        initiated_by_id=context.user_id,
        state_digest=_state_digest(raw_state),
        client_id=payload.client_id.strip(),
        redirect_uri=str(payload.redirect_uri),
        encrypted_client_secret=cipher.encrypt(
            payload.client_secret.get_secret_value(),
            associated_data=_state_aad(state_id),
        ),
        credentials_key_id=cipher.key_id,
        allowed_referers=payload.allowed_referers,
        expires_at=expires_at,
    )
    db.add(oauth_state)
    await db.commit()
    query = urlencode(
        {
            "client_id": oauth_state.client_id,
            "state": raw_state,
            "mode": "post_message",
        }
    )
    return AmoOAuthStartRead(
        authorization_url=f"{AMO_AUTHORIZATION_URL}?{query}",
        expires_at=expires_at,
    )


async def _consume_state(
    db: AsyncSession,
    *,
    raw_state: str,
) -> AmoOAuthState:
    now = datetime.now(UTC)
    statement = (
        sa.update(AmoOAuthState)
        .where(
            AmoOAuthState.state_digest == _state_digest(raw_state),
            AmoOAuthState.consumed_at.is_(None),
            AmoOAuthState.expires_at > now,
        )
        .values(consumed_at=now, updated_at=now)
        .returning(AmoOAuthState)
    )
    oauth_state = (await db.execute(statement)).scalar_one_or_none()
    await db.commit()
    if oauth_state is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OAuth state is invalid, expired or already used",
        )
    return oauth_state


async def _exchange_with_optional_client(
    request: Request,
    *,
    account_domain: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> AmoTokenSet:
    injected = _injected_http_client(request)
    if injected is not None:
        return await exchange_oauth_code(
            injected,
            account_domain=account_domain,
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=redirect_uri,
        )
    async with httpx.AsyncClient() as client:
        return await exchange_oauth_code(
            client,
            account_domain=account_domain,
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=redirect_uri,
        )


@router.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(
    request: Request,
    state_value: str = Query(alias="state", min_length=20, max_length=512),
    code: str | None = Query(default=None, min_length=1, max_length=MAX_OAUTH_CODE_LENGTH),
    referer: str | None = Query(default=None, min_length=1, max_length=2_048),
    error: str | None = Query(default=None, max_length=255),
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    cipher = _cipher(request)
    oauth_state = await _consume_state(db, raw_state=state_value)
    if error is not None or code is None or referer is None:
        return _oauth_popup_response(succeeded=False, status_code=400)
    try:
        account_domain = normalize_amocrm_referer(referer)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="amoCRM referer is not allowed") from exc
    if account_domain not in oauth_state.allowed_referers:
        raise HTTPException(status_code=400, detail="amoCRM referer is not allowed")
    try:
        client_secret = cipher.decrypt(
            oauth_state.encrypted_client_secret,
            associated_data=_state_aad(oauth_state.id),
        ).decode()
        tokens = await _exchange_with_optional_client(
            request,
            account_domain=account_domain,
            client_id=oauth_state.client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=oauth_state.redirect_uri,
        )
    except (AmoAPIError, UnicodeDecodeError, ValueError):
        return _oauth_popup_response(
            succeeded=False,
            status_code=status.HTTP_502_BAD_GATEWAY,
        )

    now = datetime.now(UTC)
    connection = await db.scalar(
        sa.select(AmoCRMConnection).where(AmoCRMConnection.workspace_id == oauth_state.workspace_id)
    )
    if connection is None:
        connection = AmoCRMConnection(
            id=uuid.uuid4(),
            workspace_id=oauth_state.workspace_id,
            status=AmoConnectionStatus.connected,
            account_domain=account_domain,
            client_id=oauth_state.client_id,
            redirect_uri=oauth_state.redirect_uri,
        )
        db.add(connection)
    else:
        connection.version += 1
    connection.status = AmoConnectionStatus.connected
    connection.account_domain = account_domain
    connection.account_id = tokens.account_id
    connection.client_id = oauth_state.client_id
    connection.redirect_uri = oauth_state.redirect_uri
    connection.encrypted_client_secret = cipher.encrypt(
        client_secret,
        associated_data=_connection_aad(connection.id, "client-secret"),
    )
    connection.encrypted_access_token = cipher.encrypt(
        tokens.access_token,
        associated_data=_connection_aad(connection.id, "access"),
    )
    connection.encrypted_refresh_token = cipher.encrypt(
        tokens.refresh_token,
        associated_data=_connection_aad(connection.id, "refresh"),
    )
    connection.credentials_key_id = cipher.key_id
    connection.token_expires_at = now + timedelta(seconds=tokens.expires_in)
    connection.connected_at = now
    connection.disconnected_at = None
    record_domain_event(
        db,
        workspace_id=oauth_state.workspace_id,
        event_type="amocrm.connected",
        entity_type="amocrm_connection",
        entity_id=connection.id,
        actor_id=oauth_state.initiated_by_id,
        payload={"account_domain": account_domain},
    )
    await db.commit()
    return _oauth_popup_response(succeeded=True)


@router.get("/connection", response_model=AmoConnectionRead)
async def read_connection(
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> AmoConnectionRead:
    connection = await db.scalar(
        sa.select(AmoCRMConnection).where(AmoCRMConnection.workspace_id == context.workspace_id)
    )
    if connection is None:
        raise HTTPException(status_code=404, detail="amoCRM connection not found")
    return _connection_read(connection)


@router.post("/disconnect", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(
    payload: AmoDisconnect,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> Response:
    now = datetime.now(UTC)
    connection = (
        await db.execute(
            sa.update(AmoCRMConnection)
            .where(
                AmoCRMConnection.workspace_id == context.workspace_id,
                AmoCRMConnection.version == payload.expected_version,
            )
            .values(
                status=AmoConnectionStatus.disconnected,
                encrypted_client_secret=None,
                encrypted_access_token=None,
                encrypted_refresh_token=None,
                credentials_key_id=None,
                token_expires_at=None,
                disconnected_at=now,
                version=payload.expected_version + 1,
                updated_at=now,
            )
            .returning(AmoCRMConnection)
        )
    ).scalar_one_or_none()
    if connection is None:
        existing = await db.scalar(
            sa.select(AmoCRMConnection.id).where(
                AmoCRMConnection.workspace_id == context.workspace_id
            )
        )
        if existing is None:
            raise HTTPException(status_code=404, detail="amoCRM connection not found")
        raise HTTPException(
            status_code=409,
            detail={"code": "version_conflict", "message": "record was modified"},
        )
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="amocrm.disconnected",
        entity_type="amocrm_connection",
        entity_id=connection.id,
        actor_id=context.user_id,
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["normalize_amocrm_referer", "router"]

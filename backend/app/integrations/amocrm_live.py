"""Live amoCRM OAuth token lifecycle, API v4 paging and Pulse domain import.

The module deliberately keeps HTTP calls outside database transactions.  It
never logs access tokens, refresh tokens, OAuth codes or response bodies.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.integrations.amo import (
    AMO_IMPORT_SEQUENCE,
    MAX_IMPORT_PAGE_SIZE,
    AmoEntity,
    AmoImportError,
    AmoPage,
    ImportEntityWriter,
    apply_import_page,
    fail_import,
)
from app.integrations.identity import (
    IdentityNormalizationError,
    normalize_email_address,
    normalize_phone_number,
)
from app.integrations.models import (
    AmoConnectionStatus,
    AmoCRMConnection,
    ContactPoint,
    ContactPointKind,
    ExternalEntityMap,
    ImportJob,
    ImportStatus,
)
from app.integrations.s3 import AttachmentStorage
from app.integrations.secrets import SecretCipher
from app.models import (
    ActivityEvent,
    BackgroundJob,
    Company,
    Contact,
    CustomFieldDefinition,
    Deal,
    DealContact,
    FieldEntity,
    FieldType,
    Membership,
    Pipeline,
    Role,
    Source,
    Stage,
    StageType,
    Task,
    TaskStatus,
    User,
)
from app.services.jobs import ClaimedJob, JobHandler, SessionFactory

AMO_PROVIDER = "amocrm"
AMO_IMPORT_JOB_TYPE = "amo_import.page"
AMO_IMPORT_REPORT_JOB_TYPE = "amo_import.report"
AMO_API_TIMEOUT_SECONDS = 12.0
TOKEN_REFRESH_LEEWAY = timedelta(seconds=90)


class AmoAPIError(RuntimeError):
    """Sanitised API error which never contains a provider response body."""

    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class AmoImportDependencyError(AmoImportError):
    pass


@dataclass(frozen=True, slots=True)
class AmoTokenSet:
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str = "Bearer"
    account_id: str | None = None


@dataclass(frozen=True, slots=True)
class AmoConnectionSnapshot:
    id: uuid.UUID
    workspace_id: uuid.UUID
    account_domain: str
    client_id: str
    redirect_uri: str
    client_secret: str
    access_token: str
    refresh_token: str
    token_expires_at: datetime
    version: int


def _connection_aad(connection_id: uuid.UUID, field: str) -> bytes:
    return f"amocrm-connection:{connection_id}:{field}".encode()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_token_payload(response: httpx.Response) -> AmoTokenSet:
    try:
        payload = response.json()
    except ValueError as exc:
        raise AmoAPIError("amoCRM returned an invalid OAuth response", retryable=False) from exc
    if not isinstance(payload, dict):
        raise AmoAPIError("amoCRM returned an invalid OAuth response", retryable=False)
    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    expires = payload.get("expires_in")
    if not isinstance(access, str) or not access:
        raise AmoAPIError("amoCRM OAuth response has no access token", retryable=False)
    if not isinstance(refresh, str) or not refresh:
        raise AmoAPIError("amoCRM OAuth response has no refresh token", retryable=False)
    if not isinstance(expires, int) or expires <= 0:
        raise AmoAPIError("amoCRM OAuth response has an invalid expiry", retryable=False)
    account_id = payload.get("account_id")
    return AmoTokenSet(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires,
        token_type=str(payload.get("token_type") or "Bearer"),
        account_id=str(account_id) if account_id is not None else None,
    )


async def exchange_oauth_code(
    http_client: httpx.AsyncClient,
    *,
    account_domain: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> AmoTokenSet:
    return await _request_tokens(
        http_client,
        account_domain=account_domain,
        payload={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
    )


async def refresh_oauth_tokens(
    http_client: httpx.AsyncClient,
    *,
    account_domain: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    redirect_uri: str,
) -> AmoTokenSet:
    return await _request_tokens(
        http_client,
        account_domain=account_domain,
        payload={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "redirect_uri": redirect_uri,
        },
    )


async def _request_tokens(
    http_client: httpx.AsyncClient,
    *,
    account_domain: str,
    payload: Mapping[str, str],
) -> AmoTokenSet:
    try:
        response = await http_client.post(
            f"https://{account_domain}/oauth2/access_token",
            json=dict(payload),
            headers={"Accept": "application/json"},
            timeout=AMO_API_TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException as exc:
        raise AmoAPIError("amoCRM OAuth request timed out", retryable=True) from exc
    except httpx.RequestError as exc:
        raise AmoAPIError("amoCRM OAuth request failed", retryable=True) from exc
    if response.status_code >= 400:
        retryable = response.status_code == 429 or response.status_code >= 500
        raise AmoAPIError(
            f"amoCRM OAuth request returned HTTP {response.status_code}",
            retryable=retryable,
            status_code=response.status_code,
        )
    return _parse_token_payload(response)


class AmoTokenManager:
    """Loads and rotates encrypted tokens without holding a DB transaction over HTTP."""

    def __init__(
        self,
        *,
        cipher: SecretCipher,
        http_client: httpx.AsyncClient,
        session_factory: SessionFactory = SessionLocal,
    ) -> None:
        self.cipher = cipher
        self.http_client = http_client
        self.session_factory = session_factory
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}

    async def snapshot(
        self, workspace_id: uuid.UUID, *, force_refresh: bool = False
    ) -> AmoConnectionSnapshot:
        lock = self._locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            snapshot = await self._load(workspace_id)
            now = datetime.now(UTC)
            if (
                not force_refresh
                and _as_utc(snapshot.token_expires_at) > now + TOKEN_REFRESH_LEEWAY
            ):
                return snapshot
            tokens = await refresh_oauth_tokens(
                self.http_client,
                account_domain=snapshot.account_domain,
                client_id=snapshot.client_id,
                client_secret=snapshot.client_secret,
                refresh_token=snapshot.refresh_token,
                redirect_uri=snapshot.redirect_uri,
            )
            async with self.session_factory() as session:
                async with session.begin():
                    connection = await session.scalar(
                        sa.select(AmoCRMConnection)
                        .where(
                            AmoCRMConnection.id == snapshot.id,
                            AmoCRMConnection.workspace_id == workspace_id,
                            AmoCRMConnection.status == AmoConnectionStatus.connected,
                        )
                        .with_for_update()
                    )
                    if connection is None:
                        raise AmoAPIError("amoCRM connection is unavailable", retryable=False)
                    connection.encrypted_access_token = self.cipher.encrypt(
                        tokens.access_token,
                        associated_data=_connection_aad(connection.id, "access"),
                    )
                    connection.encrypted_refresh_token = self.cipher.encrypt(
                        tokens.refresh_token,
                        associated_data=_connection_aad(connection.id, "refresh"),
                    )
                    connection.token_expires_at = now + timedelta(seconds=tokens.expires_in)
                    connection.account_id = tokens.account_id or connection.account_id
                    connection.credentials_key_id = self.cipher.key_id
                    connection.version += 1
            return await self._load(workspace_id)

    async def _load(self, workspace_id: uuid.UUID) -> AmoConnectionSnapshot:
        async with self.session_factory() as session:
            connection = await session.scalar(
                sa.select(AmoCRMConnection).where(
                    AmoCRMConnection.workspace_id == workspace_id,
                    AmoCRMConnection.status == AmoConnectionStatus.connected,
                )
            )
        if (
            connection is None
            or connection.encrypted_client_secret is None
            or connection.encrypted_access_token is None
            or connection.encrypted_refresh_token is None
            or connection.token_expires_at is None
        ):
            raise AmoAPIError("amoCRM connection is not configured", retryable=False)
        try:
            client_secret = self.cipher.decrypt(
                connection.encrypted_client_secret,
                associated_data=_connection_aad(connection.id, "client-secret"),
            ).decode()
            access_token = self.cipher.decrypt(
                connection.encrypted_access_token,
                associated_data=_connection_aad(connection.id, "access"),
            ).decode()
            refresh_token = self.cipher.decrypt(
                connection.encrypted_refresh_token,
                associated_data=_connection_aad(connection.id, "refresh"),
            ).decode()
        except (UnicodeDecodeError, ValueError) as exc:
            raise AmoAPIError("amoCRM credentials cannot be decrypted", retryable=False) from exc
        return AmoConnectionSnapshot(
            id=connection.id,
            workspace_id=connection.workspace_id,
            account_domain=connection.account_domain,
            client_id=connection.client_id,
            redirect_uri=connection.redirect_uri,
            client_secret=client_secret,
            access_token=access_token,
            refresh_token=refresh_token,
            token_expires_at=connection.token_expires_at,
            version=connection.version,
        )


class AmoV4Client:
    """Small async amoCRM API v4 client implementing the import page contract."""

    def __init__(
        self,
        *,
        account_domain: str,
        access_token: str,
        http_client: httpx.AsyncClient,
    ) -> None:
        self.account_domain = account_domain
        self.access_token = access_token
        self.http_client = http_client

    async def fetch_page(
        self,
        *,
        entity_type: str,
        cursor: Mapping[str, Any],
        limit: int,
    ) -> AmoPage:
        limit = min(max(1, limit), MAX_IMPORT_PAGE_SIZE)
        if entity_type == "stages":
            return await self._fetch_stages(cursor, limit)
        if entity_type == "custom_fields":
            return await self._fetch_partitioned(
                entity_type=entity_type,
                cursor=cursor,
                limit=limit,
                partitions=("leads", "contacts", "companies"),
                suffix="custom_fields",
            )
        if entity_type == "notes":
            return await self._fetch_partitioned(
                entity_type=entity_type,
                cursor=cursor,
                limit=limit,
                partitions=("leads", "contacts", "companies"),
                suffix="notes",
                ordinary_notes_only=True,
            )
        endpoint, embedded_key = {
            "pipelines": ("/api/v4/leads/pipelines", "pipelines"),
            "users": ("/api/v4/users", "users"),
            "companies": ("/api/v4/companies", "companies"),
            "contacts": ("/api/v4/contacts", "contacts"),
            "deals": ("/api/v4/leads", "leads"),
            "tasks": ("/api/v4/tasks", "tasks"),
        }.get(entity_type, (None, None))
        if endpoint is None or embedded_key is None:
            raise ValueError(f"unsupported amoCRM API entity type: {entity_type}")
        params: dict[str, Any] = {
            "page": _cursor_int(cursor, "page", 1),
            "limit": limit,
        }
        if entity_type == "tasks":
            params["filter[is_completed]"] = 0
        if entity_type == "deals":
            params["with"] = "contacts"
        payload = await self._get(endpoint, params=params)
        rows = _embedded_rows(payload, embedded_key)
        if entity_type == "tasks":
            rows = [row for row in rows if not bool(row.get("is_completed"))]
        entities = [_amo_entity(entity_type, row) for row in rows]
        return AmoPage(
            entity_type=entity_type,
            entities=entities,
            next_cursor=_next_page_cursor(payload, params["page"]),
        )

    async def _fetch_stages(self, cursor: Mapping[str, Any], limit: int) -> AmoPage:
        page_number = _cursor_int(cursor, "page", 1)
        offset = _cursor_int(cursor, "offset", 0)
        payload = await self._get(
            "/api/v4/leads/pipelines",
            params={"page": page_number, "limit": min(limit, 50)},
        )
        flattened: list[dict[str, Any]] = []
        for pipeline in _embedded_rows(payload, "pipelines"):
            pipeline_id = pipeline.get("id")
            for status in _embedded_rows(pipeline, "statuses"):
                item = dict(status)
                item["pipeline_id"] = pipeline_id
                flattened.append(item)
        selected = flattened[offset : offset + limit]
        if offset + limit < len(flattened):
            next_cursor: Mapping[str, Any] | None = {
                "page": page_number,
                "offset": offset + limit,
            }
        elif _has_next(payload):
            next_cursor = {"page": page_number + 1, "offset": 0}
        else:
            next_cursor = None
        return AmoPage(
            entity_type="stages",
            entities=[_amo_entity("stages", row) for row in selected],
            next_cursor=next_cursor,
        )

    async def _fetch_partitioned(
        self,
        *,
        entity_type: str,
        cursor: Mapping[str, Any],
        limit: int,
        partitions: Sequence[str],
        suffix: str,
        extra_params: Mapping[str, Any] | None = None,
        ordinary_notes_only: bool = False,
    ) -> AmoPage:
        partition_index = _cursor_int(cursor, "partition", 0)
        if partition_index >= len(partitions):
            return AmoPage(entity_type=entity_type, entities=[], next_cursor=None)
        partition = partitions[partition_index]
        page_number = _cursor_int(cursor, "page", 1)
        params: dict[str, Any] = {"page": page_number, "limit": limit}
        params.update(extra_params or {})
        payload = await self._get(f"/api/v4/{partition}/{suffix}", params=params)
        rows = _embedded_rows(payload, suffix)
        if ordinary_notes_only:
            rows = [row for row in rows if row.get("note_type") == "common"]
        normalised: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["pulse_parent_entity"] = partition
            normalised.append(item)
        if _has_next(payload):
            next_cursor: Mapping[str, Any] | None = {
                "partition": partition_index,
                "page": page_number + 1,
            }
        elif partition_index + 1 < len(partitions):
            next_cursor = {"partition": partition_index + 1, "page": 1}
        else:
            next_cursor = None
        return AmoPage(
            entity_type=entity_type,
            entities=[_amo_entity(entity_type, row) for row in normalised],
            next_cursor=next_cursor,
        )

    async def _get(self, path: str, *, params: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            response = await self.http_client.get(
                f"https://{self.account_domain}{path}",
                params=dict(params),
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.access_token}",
                },
                timeout=AMO_API_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException as exc:
            raise AmoAPIError("amoCRM API request timed out", retryable=True) from exc
        except httpx.RequestError as exc:
            raise AmoAPIError("amoCRM API request failed", retryable=True) from exc
        if response.status_code == 204:
            return {}
        if response.status_code >= 400:
            retryable = (
                response.status_code in {401, 408, 409, 425, 429} or response.status_code >= 500
            )
            raise AmoAPIError(
                f"amoCRM API returned HTTP {response.status_code}",
                retryable=retryable,
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AmoAPIError("amoCRM API returned invalid JSON", retryable=False) from exc
        if not isinstance(payload, dict):
            raise AmoAPIError("amoCRM API returned an invalid document", retryable=False)
        return cast(Mapping[str, Any], payload)


def _cursor_int(cursor: Mapping[str, Any], key: str, default: int) -> int:
    value = cursor.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AmoAPIError(f"invalid amoCRM import cursor field: {key}", retryable=False)
    return cast(int, value)


def _embedded_rows(payload: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    embedded = payload.get("_embedded", {})
    if not isinstance(embedded, Mapping):
        return []
    rows = embedded.get(key, [])
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _has_next(payload: Mapping[str, Any]) -> bool:
    links = payload.get("_links", {})
    return isinstance(links, Mapping) and isinstance(links.get("next"), Mapping)


def _next_page_cursor(payload: Mapping[str, Any], current_page: int) -> Mapping[str, Any] | None:
    return {"page": current_page + 1} if _has_next(payload) else None


def _amo_entity(entity_type: str, row: Mapping[str, Any]) -> AmoEntity:
    external_id: str
    if entity_type == "custom_fields":
        parent = _field_parent_key(str(row.get("pulse_parent_entity", "")))
        external_id = f"{parent}:{row.get('id')}"
    elif entity_type == "notes":
        parent = str(row.get("pulse_parent_entity", ""))
        external_id = f"{parent}:{row.get('id')}"
    else:
        external_id = str(row.get("id"))
    if not external_id or external_id.endswith(":None") or external_id == "None":
        raise AmoAPIError("amoCRM entity has no identifier", retryable=False)
    updated_at = row.get("updated_at") or row.get("created_at")
    source_updated_at = (
        datetime.fromtimestamp(updated_at, tz=UTC)
        if isinstance(updated_at, int) and not isinstance(updated_at, bool)
        else None
    )
    return AmoEntity(
        entity_type=entity_type,
        external_id=external_id,
        data=dict(row),
        source_updated_at=source_updated_at,
    )


def _field_parent_key(value: str) -> str:
    return {"leads": "deal", "contacts": "contact", "companies": "company"}.get(value, value)


class PulseAmoWriter(ImportEntityWriter):
    """Workspace-scoped idempotent writer from amoCRM entities to Pulse models."""

    async def upsert(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        entity: AmoEntity,
        existing_internal_id: uuid.UUID | None,
        user_mapping: Mapping[str, str],
    ) -> uuid.UUID:
        handlers = {
            "pipelines": self._pipeline,
            "stages": self._stage,
            "users": self._user_mapping,
            "custom_fields": self._custom_field,
            "companies": self._company,
            "contacts": self._contact,
            "deals": self._deal,
            "tasks": self._task,
            "notes": self._note,
        }
        handler = handlers.get(entity.entity_type)
        if handler is None:
            raise AmoImportError(f"unsupported amoCRM writer entity: {entity.entity_type}")
        return await handler(
            session,
            workspace_id=workspace_id,
            entity=entity,
            existing_internal_id=existing_internal_id,
            user_mapping=user_mapping,
        )

    async def _pipeline(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        entity: AmoEntity,
        existing_internal_id: uuid.UUID | None,
        user_mapping: Mapping[str, str],
    ) -> uuid.UUID:
        del user_mapping
        model = await _scoped_existing(session, Pipeline, workspace_id, existing_internal_id)
        data = entity.data
        name = _required_text(data.get("name"), "Воронка amoCRM")[:160]
        if model is None:
            collision = await session.scalar(
                sa.select(Pipeline.id).where(
                    Pipeline.workspace_id == workspace_id,
                    Pipeline.name == name,
                )
            )
            if collision is not None:
                name = f"{name[:125]} · amoCRM {entity.external_id}"[:160]
            model = Pipeline(workspace_id=workspace_id, name=name)
            session.add(model)
        model.name = name
        model.position = _int_value(data.get("sort"), 0)
        model.is_active = not bool(data.get("is_archive"))
        if existing_internal_id is not None:
            model.version += 1
        await session.flush()
        return model.id

    async def _stage(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        entity: AmoEntity,
        existing_internal_id: uuid.UUID | None,
        user_mapping: Mapping[str, str],
    ) -> uuid.UUID:
        del user_mapping
        pipeline_id = await _mapped_internal_id(
            session,
            workspace_id=workspace_id,
            entity_type="pipelines",
            external_id=str(entity.data.get("pipeline_id")),
        )
        if pipeline_id is None:
            raise AmoImportDependencyError("amoCRM stage references an unimported pipeline")
        model = await _scoped_existing(session, Stage, workspace_id, existing_internal_id)
        if model is None:
            position = _int_value(entity.data.get("sort"), 0)
            while await session.scalar(
                sa.select(Stage.id).where(
                    Stage.pipeline_id == pipeline_id,
                    Stage.position == position,
                )
            ):
                position += 1
            model = Stage(
                workspace_id=workspace_id,
                pipeline_id=pipeline_id,
                name="",
                position=position,
            )
            session.add(model)
        model.pipeline_id = pipeline_id
        model.name = _required_text(entity.data.get("name"), "Этап amoCRM")[:120]
        color = str(entity.data.get("color") or "#64748B")
        model.color = color if len(color) == 7 and color.startswith("#") else "#64748B"
        external_numeric_id = _int_value(entity.data.get("id"), 0)
        model.stage_type = (
            StageType.won
            if external_numeric_id == 142
            else StageType.lost
            if external_numeric_id == 143
            else StageType.open
        )
        await session.flush()
        return model.id

    async def _user_mapping(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        entity: AmoEntity,
        existing_internal_id: uuid.UUID | None,
        user_mapping: Mapping[str, str],
    ) -> uuid.UUID:
        del existing_internal_id
        mapped = user_mapping.get(entity.external_id)
        if mapped:
            try:
                user_id = uuid.UUID(mapped)
            except ValueError as exc:
                raise AmoImportError("amoCRM user mapping contains an invalid UUID") from exc
            membership = await session.scalar(
                sa.select(Membership.id).where(
                    Membership.workspace_id == workspace_id,
                    Membership.user_id == user_id,
                )
            )
            if membership is None:
                raise AmoImportError("amoCRM user mapping points outside the workspace")
            return user_id
        return uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"pulse:amocrm:{workspace_id}:user:{entity.external_id}",
        )

    async def _custom_field(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        entity: AmoEntity,
        existing_internal_id: uuid.UUID | None,
        user_mapping: Mapping[str, str],
    ) -> uuid.UUID:
        del user_mapping
        target_name = entity.external_id.split(":", 1)[0]
        try:
            target = FieldEntity(target_name)
        except ValueError as exc:
            raise AmoImportError("amoCRM custom field has an invalid entity type") from exc
        model = await _scoped_existing(
            session, CustomFieldDefinition, workspace_id, existing_internal_id
        )
        external_field_id = entity.external_id.split(":", 1)[-1]
        key = f"amo_{external_field_id}"[:64]
        if model is None:
            model = CustomFieldDefinition(
                workspace_id=workspace_id,
                entity_type=target,
                key=key,
                name="",
                field_type=FieldType.text,
            )
            session.add(model)
        model.entity_type = target
        model.key = key
        model.name = _required_text(entity.data.get("name"), f"Поле {external_field_id}")[:120]
        model.field_type = _amo_field_type(str(entity.data.get("type") or "text"))
        model.options = _field_options(entity.data)
        model.is_active = not bool(entity.data.get("is_deleted"))
        await session.flush()
        return model.id

    async def _company(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        entity: AmoEntity,
        existing_internal_id: uuid.UUID | None,
        user_mapping: Mapping[str, str],
    ) -> uuid.UUID:
        del user_mapping
        model = await _scoped_existing(session, Company, workspace_id, existing_internal_id)
        if model is None:
            model = Company(workspace_id=workspace_id, name="")
            session.add(model)
        fields = await _custom_field_values(
            session, workspace_id, "company", entity.data.get("custom_fields_values")
        )
        model.name = _required_text(entity.data.get("name"), "Компания amoCRM")[:240]
        model.phone = _first_amo_field_value(entity.data, "PHONE", max_length=64)
        model.email = _first_amo_field_value(entity.data, "EMAIL", max_length=320)
        model.website = _first_amo_field_value(entity.data, "WEB", max_length=512)
        model.tags = _tags(entity.data)
        model.custom_fields = fields
        if existing_internal_id is not None:
            model.version += 1
        await session.flush()
        return model.id

    async def _contact(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        entity: AmoEntity,
        existing_internal_id: uuid.UUID | None,
        user_mapping: Mapping[str, str],
    ) -> uuid.UUID:
        del user_mapping
        model = await _scoped_existing(session, Contact, workspace_id, existing_internal_id)
        if model is None:
            model = Contact(workspace_id=workspace_id, first_name="")
            session.add(model)
        embedded = entity.data.get("_embedded", {})
        companies = embedded.get("companies", []) if isinstance(embedded, Mapping) else []
        company_id: uuid.UUID | None = None
        if isinstance(companies, list) and companies and isinstance(companies[0], Mapping):
            company_id = await _mapped_internal_id(
                session,
                workspace_id=workspace_id,
                entity_type="companies",
                external_id=str(companies[0].get("id")),
            )
        first = str(entity.data.get("first_name") or "").strip()
        last = str(entity.data.get("last_name") or "").strip()
        if not first:
            name_parts = _required_text(entity.data.get("name"), "Контакт amoCRM").split(maxsplit=1)
            first = name_parts[0]
            last = last or (name_parts[1] if len(name_parts) > 1 else "")
        emails = _all_amo_field_values(entity.data, "EMAIL", max_length=320)
        phones = _all_amo_field_values(entity.data, "PHONE", max_length=64)
        model.company_id = company_id
        model.first_name = first[:120]
        model.last_name = last[:120]
        model.primary_email = emails[0] if emails else None
        model.primary_phone = phones[0] if phones else None
        model.emails = emails
        model.phones = phones
        model.tags = _tags(entity.data)
        model.custom_fields = await _custom_field_values(
            session, workspace_id, "contact", entity.data.get("custom_fields_values")
        )
        if existing_internal_id is not None:
            model.version += 1
        await session.flush()
        await _sync_imported_contact_points(session, model)
        return model.id

    async def _deal(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        entity: AmoEntity,
        existing_internal_id: uuid.UUID | None,
        user_mapping: Mapping[str, str],
    ) -> uuid.UUID:
        pipeline_id = await _mapped_internal_id(
            session,
            workspace_id=workspace_id,
            entity_type="pipelines",
            external_id=str(entity.data.get("pipeline_id")),
        )
        stage_id = await _mapped_internal_id(
            session,
            workspace_id=workspace_id,
            entity_type="stages",
            external_id=str(entity.data.get("status_id")),
        )
        if pipeline_id is None or stage_id is None:
            raise AmoImportDependencyError("amoCRM deal references an unimported pipeline or stage")
        model = await _scoped_existing(session, Deal, workspace_id, existing_internal_id)
        if model is None:
            model = Deal(
                workspace_id=workspace_id,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                title="",
            )
            session.add(model)
        company_id = await _first_embedded_mapping(
            session,
            workspace_id,
            entity.data,
            embedded_key="companies",
            mapped_entity_type="companies",
        )
        assignee_id = await _assignee_id(
            session,
            workspace_id=workspace_id,
            external_user_id=entity.data.get("responsible_user_id"),
            user_mapping=user_mapping,
            required=False,
        )
        source = await session.scalar(
            sa.select(Source).where(Source.workspace_id == workspace_id, Source.key == "amo_import")
        )
        if source is None:
            source = Source(
                workspace_id=workspace_id,
                key="amo_import",
                name="Импорт amoCRM",
            )
            session.add(source)
            await session.flush()
        model.pipeline_id = pipeline_id
        model.stage_id = stage_id
        model.company_id = company_id
        model.assignee_id = assignee_id
        model.source_id = source.id
        model.title = _required_text(entity.data.get("name"), "Сделка amoCRM")[:240]
        model.amount = _decimal_value(entity.data.get("price"))
        model.currency = "RUB"
        model.custom_fields = await _custom_field_values(
            session, workspace_id, "deal", entity.data.get("custom_fields_values")
        )
        updated_at = entity.source_updated_at
        if updated_at is not None:
            model.last_activity_at = updated_at
        if existing_internal_id is not None:
            model.version += 1
        await session.flush()
        await self._sync_deal_contacts(session, workspace_id, model.id, entity.data)
        return model.id

    async def _sync_deal_contacts(
        self,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        deal_id: uuid.UUID,
        data: Mapping[str, Any],
    ) -> None:
        embedded = data.get("_embedded", {})
        contacts = embedded.get("contacts", []) if isinstance(embedded, Mapping) else []
        if not isinstance(contacts, list):
            return
        for index, item in enumerate(contacts):
            if not isinstance(item, Mapping):
                continue
            contact_id = await _mapped_internal_id(
                session,
                workspace_id=workspace_id,
                entity_type="contacts",
                external_id=str(item.get("id")),
            )
            if contact_id is None:
                continue
            exists = await session.scalar(
                sa.select(DealContact.id).where(
                    DealContact.workspace_id == workspace_id,
                    DealContact.deal_id == deal_id,
                    DealContact.contact_id == contact_id,
                )
            )
            if exists is None:
                session.add(
                    DealContact(
                        workspace_id=workspace_id,
                        deal_id=deal_id,
                        contact_id=contact_id,
                        is_primary=bool(item.get("is_main")) or index == 0,
                    )
                )

    async def _task(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        entity: AmoEntity,
        existing_internal_id: uuid.UUID | None,
        user_mapping: Mapping[str, str],
    ) -> uuid.UUID:
        model = await _scoped_existing(session, Task, workspace_id, existing_internal_id)
        assignee_id = await _assignee_id(
            session,
            workspace_id=workspace_id,
            external_user_id=entity.data.get("responsible_user_id"),
            user_mapping=user_mapping,
            required=True,
        )
        if assignee_id is None:
            raise AmoImportDependencyError("workspace has no active assignee for amoCRM tasks")
        due_value = entity.data.get("complete_till")
        due_at = (
            datetime.fromtimestamp(due_value, tz=UTC)
            if isinstance(due_value, int) and not isinstance(due_value, bool)
            else datetime.now(UTC)
        )
        if model is None:
            model = Task(
                workspace_id=workspace_id,
                assignee_id=assignee_id,
                due_at=due_at,
                title="",
            )
            session.add(model)
        model.assignee_id = assignee_id
        model.due_at = due_at
        model.title = _required_text(entity.data.get("text"), "Задача amoCRM")[:240]
        model.description = str(entity.data.get("text") or "")[:4_000] or None
        model.task_type = f"amocrm:{entity.data.get('task_type_id', 'task')}"[:64]
        model.status = TaskStatus.open
        model.deal_id = None
        model.contact_id = None
        model.company_id = None
        related_external = entity.data.get("entity_id")
        related_type = str(entity.data.get("entity_type") or "").lower()
        mapping = {
            "leads": ("deals", "deal_id"),
            "lead": ("deals", "deal_id"),
            "contacts": ("contacts", "contact_id"),
            "contact": ("contacts", "contact_id"),
            "companies": ("companies", "company_id"),
            "company": ("companies", "company_id"),
        }.get(related_type)
        if mapping and related_external is not None:
            internal_id = await _mapped_internal_id(
                session,
                workspace_id=workspace_id,
                entity_type=mapping[0],
                external_id=str(related_external),
            )
            if internal_id is not None:
                setattr(model, mapping[1], internal_id)
        if existing_internal_id is not None:
            model.version += 1
        await session.flush()
        return model.id

    async def _note(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        entity: AmoEntity,
        existing_internal_id: uuid.UUID | None,
        user_mapping: Mapping[str, str],
    ) -> uuid.UUID:
        del existing_internal_id, user_mapping
        parent = str(entity.data.get("pulse_parent_entity") or "")
        mapped_type = {
            "leads": "deals",
            "contacts": "contacts",
            "companies": "companies",
        }.get(parent)
        singular_type = {
            "deals": "deal",
            "contacts": "contact",
            "companies": "company",
        }.get(mapped_type or "", "amo_note")
        parent_external_id = entity.data.get("entity_id")
        parent_id = (
            await _mapped_internal_id(
                session,
                workspace_id=workspace_id,
                entity_type=mapped_type,
                external_id=str(parent_external_id),
            )
            if mapped_type and parent_external_id is not None
            else None
        )
        params = entity.data.get("params", {})
        text = params.get("text") if isinstance(params, Mapping) else None
        model = ActivityEvent(
            workspace_id=workspace_id,
            event_type="amo_import.note",
            entity_type=singular_type,
            entity_id=parent_id or uuid.uuid4(),
            payload={
                "text": str(text or "")[:20_000],
                "amo_note_id": entity.external_id,
                "amo_parent_id": str(parent_external_id or ""),
            },
        )
        if entity.source_updated_at is not None:
            model.occurred_at = entity.source_updated_at
        session.add(model)
        await session.flush()
        return model.id


async def _scoped_existing(
    session: AsyncSession,
    model_type: type[Any],
    workspace_id: uuid.UUID,
    internal_id: uuid.UUID | None,
) -> Any | None:
    if internal_id is None:
        return None
    return await session.scalar(
        sa.select(model_type).where(
            model_type.id == internal_id,
            model_type.workspace_id == workspace_id,
        )
    )


async def _mapped_internal_id(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    entity_type: str | None,
    external_id: str,
) -> uuid.UUID | None:
    if entity_type is None or external_id in {"", "None"}:
        return None
    return cast(
        uuid.UUID | None,
        await session.scalar(
            sa.select(ExternalEntityMap.internal_id).where(
                ExternalEntityMap.workspace_id == workspace_id,
                ExternalEntityMap.provider == AMO_PROVIDER,
                ExternalEntityMap.entity_type == entity_type,
                ExternalEntityMap.external_id == external_id,
            )
        ),
    )


async def _first_embedded_mapping(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    data: Mapping[str, Any],
    *,
    embedded_key: str,
    mapped_entity_type: str,
) -> uuid.UUID | None:
    embedded = data.get("_embedded", {})
    rows = embedded.get(embedded_key, []) if isinstance(embedded, Mapping) else []
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], Mapping):
        return None
    return await _mapped_internal_id(
        session,
        workspace_id=workspace_id,
        entity_type=mapped_entity_type,
        external_id=str(rows[0].get("id")),
    )


async def _assignee_id(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    external_user_id: Any,
    user_mapping: Mapping[str, str],
    required: bool,
) -> uuid.UUID | None:
    mapped = user_mapping.get(str(external_user_id)) if external_user_id is not None else None
    if mapped:
        try:
            user_id = uuid.UUID(mapped)
        except ValueError as exc:
            raise AmoImportError("amoCRM user mapping contains an invalid UUID") from exc
        valid = await session.scalar(
            sa.select(Membership.user_id).where(
                Membership.workspace_id == workspace_id,
                Membership.user_id == user_id,
            )
        )
        if valid is not None:
            return valid
        raise AmoImportError("amoCRM user mapping points outside the workspace")
    if not required:
        return None
    return cast(
        uuid.UUID | None,
        await session.scalar(
            sa.select(User.id)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.workspace_id == workspace_id, User.is_active.is_(True))
            .order_by(
                sa.case(
                    (Membership.role == Role.owner, 0),
                    (Membership.role == Role.admin, 1),
                    else_=2,
                ),
                User.created_at,
            )
            .limit(1)
        ),
    )


def _required_text(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text or fallback


def _int_value(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _decimal_value(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _tags(data: Mapping[str, Any]) -> list[str]:
    embedded = data.get("_embedded", {})
    rows = embedded.get("tags", []) if isinstance(embedded, Mapping) else []
    if not isinstance(rows, list):
        return []
    return [
        str(row.get("name"))[:100] for row in rows if isinstance(row, Mapping) and row.get("name")
    ]


def _amo_custom_rows(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = data.get("custom_fields_values", [])
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _all_amo_field_values(
    data: Mapping[str, Any], field_code: str, *, max_length: int
) -> list[str]:
    result: list[str] = []
    for row in _amo_custom_rows(data):
        if str(row.get("field_code") or "").upper() != field_code:
            continue
        values = row.get("values", [])
        if not isinstance(values, list):
            continue
        for item in values:
            if isinstance(item, Mapping) and item.get("value") not in {None, ""}:
                text = str(item["value"]).strip()[:max_length]
                if text and text not in result:
                    result.append(text)
    return result


def _first_amo_field_value(
    data: Mapping[str, Any], field_code: str, *, max_length: int
) -> str | None:
    values = _all_amo_field_values(data, field_code, max_length=max_length)
    return values[0] if values else None


async def _custom_field_values(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    target: str,
    raw_rows: Any,
) -> dict[str, Any]:
    if not isinstance(raw_rows, list):
        return {}
    external_ids = [
        f"{target}:{row.get('field_id')}"
        for row in raw_rows
        if isinstance(row, Mapping) and row.get("field_id") is not None
    ]
    if not external_ids:
        return {}
    mappings = list(
        (
            await session.execute(
                sa.select(ExternalEntityMap.external_id, CustomFieldDefinition.key)
                .join(
                    CustomFieldDefinition,
                    CustomFieldDefinition.id == ExternalEntityMap.internal_id,
                )
                .where(
                    ExternalEntityMap.workspace_id == workspace_id,
                    ExternalEntityMap.provider == AMO_PROVIDER,
                    ExternalEntityMap.entity_type == "custom_fields",
                    ExternalEntityMap.external_id.in_(external_ids),
                    CustomFieldDefinition.workspace_id == workspace_id,
                )
            )
        ).all()
    )
    keys = {external_id: key for external_id, key in mappings}
    result: dict[str, Any] = {}
    for row in raw_rows:
        if not isinstance(row, Mapping):
            continue
        key = keys.get(f"{target}:{row.get('field_id')}")
        values = row.get("values", [])
        if key is None or not isinstance(values, list):
            continue
        cleaned = [item.get("value") for item in values if isinstance(item, Mapping)]
        cleaned = [value for value in cleaned if value is not None]
        if cleaned:
            result[key] = cleaned[0] if len(cleaned) == 1 else cleaned
    return result


async def _sync_imported_contact_points(session: AsyncSession, contact: Contact) -> None:
    await session.execute(
        sa.delete(ContactPoint).where(
            ContactPoint.workspace_id == contact.workspace_id,
            ContactPoint.contact_id == contact.id,
        )
    )
    points: list[ContactPoint] = []
    seen: set[tuple[ContactPointKind, str]] = set()
    for kind, values in (
        (ContactPointKind.email, contact.emails),
        (ContactPointKind.phone, contact.phones),
    ):
        for value in values:
            try:
                normalized = (
                    normalize_email_address(value)
                    if kind is ContactPointKind.email
                    else normalize_phone_number(value)
                )
            except IdentityNormalizationError:
                continue
            identity = (kind, normalized)
            if identity in seen:
                continue
            seen.add(identity)
            points.append(
                ContactPoint(
                    workspace_id=contact.workspace_id,
                    contact_id=contact.id,
                    kind=kind,
                    value=value,
                    normalized_value=normalized,
                    is_primary=not any(point.kind is kind for point in points),
                )
            )
    session.add_all(points)
    await session.flush()


def _amo_field_type(value: str) -> FieldType:
    if value in {"numeric", "monetary", "price"}:
        return FieldType.number
    if value in {"date", "date_time", "birthday"}:
        return FieldType.date
    if value in {"checkbox", "legal_entity"}:
        return FieldType.boolean
    if value in {"select", "multiselect", "radiobutton"}:
        return FieldType.select
    return FieldType.text


def _field_options(data: Mapping[str, Any]) -> list[str]:
    enums = data.get("enums", [])
    if not isinstance(enums, list):
        return []
    return [
        str(item.get("value"))[:200]
        for item in enums
        if isinstance(item, Mapping) and item.get("value") is not None
    ]


def _active_entity(job: ImportJob) -> str:
    if job.entity_type != "all":
        if job.entity_type not in AMO_IMPORT_SEQUENCE:
            raise AmoImportError("import job has an unsupported entity type")
        return job.entity_type
    value = job.cursor.get("entity_type") if isinstance(job.cursor, Mapping) else None
    if value is None:
        return AMO_IMPORT_SEQUENCE[0]
    if value not in AMO_IMPORT_SEQUENCE:
        raise AmoImportError("all-entity import cursor is invalid")
    return str(value)


def _page_cursor(job: ImportJob) -> dict[str, Any]:
    cursor = dict(job.cursor or {})
    cursor.pop("entity_type", None)
    return cursor


def _advance_page(job: ImportJob, page: AmoPage) -> AmoPage:
    if job.entity_type != "all":
        return page
    next_cursor: Mapping[str, Any] | None
    if page.next_cursor is not None:
        next_cursor = {"entity_type": page.entity_type, **dict(page.next_cursor)}
    else:
        index = AMO_IMPORT_SEQUENCE.index(page.entity_type)
        next_cursor = (
            {"entity_type": AMO_IMPORT_SEQUENCE[index + 1]}
            if index + 1 < len(AMO_IMPORT_SEQUENCE)
            else None
        )
    return AmoPage(
        entity_type=page.entity_type,
        entities=page.entities,
        next_cursor=next_cursor,
    )


def _safe_import_error(error: BaseException) -> str:
    if isinstance(error, AmoAPIError):
        return str(error)[:1_000]
    if isinstance(error, AmoImportError):
        return str(error)[:1_000]
    return f"{type(error).__name__}: amoCRM import page failed"[:1_000]


def _report_timestamp(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat().replace("+00:00", "Z") if value is not None else None


def build_amo_import_report(job: ImportJob) -> bytes:
    """Create canonical report bytes without credentials or source entity bodies."""

    document = {
        "schema_version": 1,
        "import_id": str(job.id),
        "workspace_id": str(job.workspace_id),
        "provider": job.provider,
        "entity_type": job.entity_type,
        "dry_run": job.dry_run,
        "counts": {key: int(value) for key, value in sorted(dict(job.counts).items())},
        "timestamps": {
            "created_at": _report_timestamp(job.created_at),
            "started_at": _report_timestamp(job.started_at),
            "completed_at": _report_timestamp(job.completed_at),
        },
    }
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def make_amo_import_handler(
    *,
    cipher: SecretCipher,
    http_client: httpx.AsyncClient,
    session_factory: SessionFactory = SessionLocal,
    writer: ImportEntityWriter | None = None,
) -> JobHandler:
    """Return the single ``amo_import.page`` handler registered by app runtime."""

    token_manager = AmoTokenManager(
        cipher=cipher,
        http_client=http_client,
        session_factory=session_factory,
    )
    entity_writer = writer or PulseAmoWriter()

    async def handle(claimed: ClaimedJob) -> None:
        raw_job_id = claimed.payload.get("import_job_id")
        try:
            import_job_id = uuid.UUID(str(raw_job_id))
        except (ValueError, TypeError) as exc:
            raise AmoImportError("amoCRM background job has an invalid import id") from exc

        async with session_factory() as session:
            import_job = await session.get(ImportJob, import_job_id)
            if import_job is None:
                raise AmoImportError("amoCRM import job does not exist")
            if import_job.status in {ImportStatus.paused, ImportStatus.succeeded}:
                return
            if import_job.status == ImportStatus.failed:
                return
            workspace_id = import_job.workspace_id
            import_version = import_job.version
            active_entity = _active_entity(import_job)
            cursor = _page_cursor(import_job)

        try:
            snapshot = await token_manager.snapshot(workspace_id)
            client = AmoV4Client(
                account_domain=snapshot.account_domain,
                access_token=snapshot.access_token,
                http_client=http_client,
            )
            try:
                page = await client.fetch_page(
                    entity_type=active_entity,
                    cursor=cursor,
                    limit=MAX_IMPORT_PAGE_SIZE,
                )
            except AmoAPIError as exc:
                if exc.status_code != 401:
                    raise
                snapshot = await token_manager.snapshot(workspace_id, force_refresh=True)
                page = await AmoV4Client(
                    account_domain=snapshot.account_domain,
                    access_token=snapshot.access_token,
                    http_client=http_client,
                ).fetch_page(
                    entity_type=active_entity,
                    cursor=cursor,
                    limit=MAX_IMPORT_PAGE_SIZE,
                )
            async with session_factory() as session:
                async with session.begin():
                    locked_job = await session.scalar(
                        sa.select(ImportJob)
                        .where(
                            ImportJob.id == import_job_id,
                            ImportJob.workspace_id == workspace_id,
                        )
                        .with_for_update()
                    )
                    if locked_job is None:
                        raise AmoImportError("amoCRM import job disappeared")
                    if locked_job.status in {
                        ImportStatus.paused,
                        ImportStatus.failed,
                        ImportStatus.succeeded,
                    }:
                        return
                    if locked_job.version != import_version:
                        return
                    result = await apply_import_page(
                        session,
                        job=locked_job,
                        page=_advance_page(locked_job, page),
                        writer=entity_writer,
                    )
                    if not result.done:
                        session.add(
                            BackgroundJob(
                                job_type=AMO_IMPORT_JOB_TYPE,
                                payload={"import_job_id": str(locked_job.id)},
                                dedupe_key=f"amo-import:{locked_job.id}:page:{locked_job.version}",
                            )
                        )
                    else:
                        session.add(
                            BackgroundJob(
                                job_type=AMO_IMPORT_REPORT_JOB_TYPE,
                                payload={"import_job_id": str(locked_job.id)},
                                dedupe_key=f"amo-import:{locked_job.id}:report",
                                max_attempts=5,
                            )
                        )
        except Exception as exc:
            retryable = isinstance(exc, AmoAPIError) and exc.retryable
            final_attempt = claimed.attempts >= claimed.max_attempts
            async with session_factory() as session:
                async with session.begin():
                    failed_job = await session.scalar(
                        sa.select(ImportJob).where(ImportJob.id == import_job_id).with_for_update()
                    )
                    if failed_job is not None and failed_job.status in {
                        ImportStatus.pending,
                        ImportStatus.running,
                    }:
                        failed_job.last_error = _safe_import_error(exc)
                        if not retryable or final_attempt:
                            fail_import(failed_job, failed_job.last_error)
            if retryable and not final_attempt:
                raise

    return handle


def make_amo_import_report_handler(
    *,
    storage: AttachmentStorage | None,
    session_factory: SessionFactory = SessionLocal,
) -> JobHandler:
    """Return an idempotent handler that persists a completed import report in S3."""

    async def handle(claimed: ClaimedJob) -> None:
        raw_job_id = claimed.payload.get("import_job_id")
        try:
            import_job_id = uuid.UUID(str(raw_job_id))
        except (ValueError, TypeError) as exc:
            raise AmoImportError("amoCRM report job has an invalid import id") from exc
        if storage is None:
            raise RuntimeError("S3 storage is not configured for amoCRM import reports")

        async with session_factory() as session:
            import_job = await session.scalar(
                sa.select(ImportJob).where(ImportJob.id == import_job_id)
            )
            if import_job is None:
                raise AmoImportError("amoCRM import job does not exist")
            if import_job.status is not ImportStatus.succeeded:
                raise AmoImportError("amoCRM import is not completed")
            expected_key = storage.import_report_key(import_job.workspace_id, import_job.id)
            if import_job.report_object_key is not None:
                if import_job.report_object_key != expected_key:
                    raise AmoImportError("amoCRM import report key is invalid")
                return
            workspace_id = import_job.workspace_id
            content = build_amo_import_report(import_job)

        object_key = await storage.store_import_report(
            workspace_id=workspace_id,
            import_job_id=import_job_id,
            content=content,
        )

        async with session_factory() as session:
            async with session.begin():
                updated = await session.execute(
                    sa.update(ImportJob)
                    .where(
                        ImportJob.id == import_job_id,
                        ImportJob.workspace_id == workspace_id,
                        ImportJob.status == ImportStatus.succeeded,
                        ImportJob.report_object_key.is_(None),
                    )
                    .values(
                        report_object_key=object_key,
                        version=ImportJob.version + 1,
                        updated_at=datetime.now(UTC),
                    )
                )
                if not int(getattr(updated, "rowcount", 0) or 0):
                    current_key = await session.scalar(
                        sa.select(ImportJob.report_object_key).where(
                            ImportJob.id == import_job_id,
                            ImportJob.workspace_id == workspace_id,
                        )
                    )
                    if current_key != object_key:
                        raise AmoImportError("amoCRM import report state changed")

    return handle


__all__ = [
    "AMO_IMPORT_JOB_TYPE",
    "AMO_IMPORT_REPORT_JOB_TYPE",
    "AmoAPIError",
    "AmoConnectionSnapshot",
    "AmoTokenManager",
    "AmoTokenSet",
    "AmoV4Client",
    "PulseAmoWriter",
    "build_amo_import_report",
    "exchange_oauth_code",
    "make_amo_import_handler",
    "make_amo_import_report_handler",
    "refresh_oauth_tokens",
]

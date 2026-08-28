"""Resumable, idempotent amoCRM import contracts and page application."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.models import ExternalEntityMap, ImportJob, ImportStatus

AMO_IMPORT_SEQUENCE = (
    "pipelines",
    "stages",
    "users",
    "custom_fields",
    "companies",
    "contacts",
    "deals",
    "tasks",
    "notes",
)
AMO_RESOURCE_ENTITY_TYPES = frozenset(AMO_IMPORT_SEQUENCE)
AMO_ENTITY_TYPES = AMO_RESOURCE_ENTITY_TYPES | {"all"}
MAX_IMPORT_PAGE_SIZE = 250


class AmoImportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AmoEntity:
    entity_type: str
    external_id: str
    data: Mapping[str, Any]
    source_updated_at: datetime | None = None

    def fingerprint(self) -> str:
        canonical = json.dumps(
            self.data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class AmoPage:
    entity_type: str
    entities: Sequence[AmoEntity]
    next_cursor: Mapping[str, Any] | None

    def __post_init__(self) -> None:
        if self.entity_type not in AMO_RESOURCE_ENTITY_TYPES:
            raise ValueError(f"unsupported amoCRM entity type: {self.entity_type}")
        if len(self.entities) > MAX_IMPORT_PAGE_SIZE:
            raise ValueError(f"amoCRM page exceeds {MAX_IMPORT_PAGE_SIZE} entities")
        if any(entity.entity_type != self.entity_type for entity in self.entities):
            raise ValueError("page contains a different entity type")


@dataclass(frozen=True, slots=True)
class ImportJobSnapshot:
    id: uuid.UUID
    workspace_id: uuid.UUID
    entity_type: str
    cursor: Mapping[str, Any]
    user_mapping: Mapping[str, str]
    dry_run: bool


@dataclass(frozen=True, slots=True)
class ImportPageResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    would_create: int = 0
    would_update: int = 0
    done: bool = False


class AmoClient(Protocol):
    async def fetch_page(
        self,
        *,
        entity_type: str,
        cursor: Mapping[str, Any],
        limit: int,
    ) -> AmoPage: ...


class ImportEntityWriter(Protocol):
    async def upsert(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        entity: AmoEntity,
        existing_internal_id: uuid.UUID | None,
        user_mapping: Mapping[str, str],
    ) -> uuid.UUID: ...


def snapshot_job(job: ImportJob) -> ImportJobSnapshot:
    if job.entity_type not in AMO_ENTITY_TYPES:
        raise AmoImportError("import job has no supported entity type")
    return ImportJobSnapshot(
        id=job.id,
        workspace_id=job.workspace_id,
        entity_type=job.entity_type,
        cursor=dict(job.cursor),
        user_mapping=dict(job.user_mapping),
        dry_run=job.dry_run,
    )


async def fetch_import_page(client: AmoClient, snapshot: ImportJobSnapshot) -> AmoPage:
    """Fetch outside a database transaction to avoid holding locks over HTTP."""

    page = await client.fetch_page(
        entity_type=snapshot.entity_type,
        cursor=snapshot.cursor,
        limit=MAX_IMPORT_PAGE_SIZE,
    )
    if page.entity_type != snapshot.entity_type:
        raise AmoImportError("amoCRM returned an unexpected entity type")
    return page


async def apply_import_page(
    session: AsyncSession,
    *,
    job: ImportJob,
    page: AmoPage,
    writer: ImportEntityWriter,
) -> ImportPageResult:
    """Apply one already-fetched page in a short caller-owned transaction."""

    if job.status not in {ImportStatus.pending, ImportStatus.running}:
        raise AmoImportError("only pending or running imports can apply a page")
    if job.entity_type not in {page.entity_type, "all"}:
        raise AmoImportError("import page does not match the job entity type")

    external_ids = [entity.external_id for entity in page.entities]
    if len(set(external_ids)) != len(external_ids):
        raise AmoImportError("amoCRM page contains duplicate external identifiers")
    existing_maps = {}
    if external_ids:
        mappings = list(
            (
                await session.scalars(
                    sa.select(ExternalEntityMap).where(
                        ExternalEntityMap.workspace_id == job.workspace_id,
                        ExternalEntityMap.provider == job.provider,
                        ExternalEntityMap.entity_type == page.entity_type,
                        ExternalEntityMap.external_id.in_(external_ids),
                    )
                )
            ).all()
        )
        existing_maps = {mapping.external_id: mapping for mapping in mappings}

    counters = {
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "would_create": 0,
        "would_update": 0,
    }
    for entity in page.entities:
        mapping = existing_maps.get(entity.external_id)
        fingerprint = entity.fingerprint()
        if mapping is not None and mapping.fingerprint == fingerprint:
            counters["unchanged"] += 1
            continue
        if job.dry_run:
            counters["would_update" if mapping else "would_create"] += 1
            continue

        internal_id = await writer.upsert(
            session,
            workspace_id=job.workspace_id,
            entity=entity,
            existing_internal_id=mapping.internal_id if mapping else None,
            user_mapping=dict(job.user_mapping),
        )
        if mapping is None:
            mapping = ExternalEntityMap(
                workspace_id=job.workspace_id,
                import_job_id=job.id,
                provider=job.provider,
                entity_type=page.entity_type,
                external_id=entity.external_id,
                internal_id=internal_id,
                source_updated_at=entity.source_updated_at,
                fingerprint=fingerprint,
            )
            session.add(mapping)
            existing_maps[entity.external_id] = mapping
            counters["created"] += 1
        else:
            mapping.internal_id = internal_id
            mapping.import_job_id = job.id
            mapping.source_updated_at = entity.source_updated_at
            mapping.fingerprint = fingerprint
            counters["updated"] += 1

    job.status = ImportStatus.succeeded if page.next_cursor is None else ImportStatus.running
    job.cursor = dict(page.next_cursor or {})
    counts = dict(job.counts)
    for name, count in counters.items():
        counts[name] = int(counts.get(name, 0)) + count
    job.counts = counts
    job.started_at = job.started_at or datetime.now(UTC)
    job.completed_at = datetime.now(UTC) if page.next_cursor is None else None
    job.last_error = None
    job.version += 1
    await session.flush()
    return ImportPageResult(**counters, done=page.next_cursor is None)


def pause_import(job: ImportJob) -> None:
    if job.status in {ImportStatus.pending, ImportStatus.running}:
        job.status = ImportStatus.paused
        job.version += 1


def resume_import(job: ImportJob) -> None:
    if job.status not in {ImportStatus.paused, ImportStatus.failed}:
        raise AmoImportError("only paused or failed imports can be resumed")
    job.status = ImportStatus.running
    job.last_error = None
    job.version += 1


def fail_import(job: ImportJob, error: BaseException | str) -> None:
    job.status = ImportStatus.failed
    job.last_error = str(error)[:4_000]
    job.version += 1

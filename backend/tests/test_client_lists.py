from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

import httpx
import pytest
import sqlalchemy as sa

from app.db import engine
from app.models import Company, Contact, Deal, Task


@contextmanager
def _capture_statements() -> Iterator[list[str]]:
    statements: list[str] = []

    def before_cursor_execute(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    sa.event.listen(engine.sync_engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield statements
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", before_cursor_execute)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("collection", "payload", "table_name"),
    [
        ("contacts", {"first_name": "List query contact"}, "contacts"),
        ("companies", {"name": "List query company"}, "companies"),
    ],
)
async def test_client_list_uses_one_entity_select_without_n_plus_one(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
    collection: str,
    payload: dict[str, str],
    table_name: str,
) -> None:
    created = await client.post(
        f"/api/v1/{collection}",
        headers={"X-CSRF-Token": str(owner_auth["csrf_token"])},
        json=payload,
    )
    assert created.status_code == 201, created.text

    with _capture_statements() as statements:
        response = await client.get(f"/api/v1/{collection}", params={"limit": 25})

    assert response.status_code == 200, response.text
    entity_selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
        and f"FROM {table_name}" in statement
    ]
    assert len(entity_selects) == 1


def test_client_models_index_cursor_list_order() -> None:
    expected = ("workspace_id", "deleted_at", "created_at", "id")
    for model, index_name in (
        (Company, "ix_companies_workspace_deleted_created_id"),
        (Contact, "ix_contacts_workspace_deleted_created_id"),
    ):
        table = cast(sa.Table, model.__table__)
        indexes = {
            str(index.name): tuple(column.name for column in index.columns)
            for index in table.indexes
        }
        assert indexes[index_name] == expected


def test_task_model_indexes_status_cursor_list_order() -> None:
    table = cast(sa.Table, Task.__table__)
    indexes = {
        str(index.name): tuple(column.name for column in index.columns)
        for index in table.indexes
    }
    assert indexes["ix_tasks_workspace_status_created_id"] == (
        "workspace_id",
        "status",
        "created_at",
        "id",
    )


def test_deal_model_indexes_stage_cursor_list_order() -> None:
    table = cast(sa.Table, Deal.__table__)
    indexes = {
        str(index.name): tuple(column.name for column in index.columns)
        for index in table.indexes
    }
    assert indexes["ix_deals_workspace_pipeline_stage_deleted_created_id"] == (
        "workspace_id",
        "pipeline_id",
        "stage_id",
        "deleted_at",
        "created_at",
        "id",
    )

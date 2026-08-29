"""index cursor-paginated CRM lists

Revision ID: c4f18a2d7e90
Revises: 83b4d45e9c21
Create Date: 2026-08-29 15:40:00.000000
"""

from collections.abc import Mapping, Sequence
from typing import cast

from sqlalchemy import text
from sqlalchemy.engine import Connection

from alembic import op

revision: str = "c4f18a2d7e90"
down_revision: str | None = "83b4d45e9c21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = (
    (
        "ix_companies_workspace_deleted_created_id",
        "companies",
        ("workspace_id", "deleted_at", "created_at", "id"),
    ),
    (
        "ix_contacts_workspace_deleted_created_id",
        "contacts",
        ("workspace_id", "deleted_at", "created_at", "id"),
    ),
    (
        "ix_tasks_workspace_status_created_id",
        "tasks",
        ("workspace_id", "status", "created_at", "id"),
    ),
    (
        "ix_deals_workspace_pipeline_stage_deleted_created_id",
        "deals",
        ("workspace_id", "pipeline_id", "stage_id", "deleted_at", "created_at", "id"),
    ),
)

_POSTGRESQL_INDEX_STATE = text(
    """
    SELECT
        target_table.relname AS target_table_name,
        target_namespace.nspname AS target_schema_name,
        index_relation.relkind AS relation_kind,
        indexed_table.relname AS indexed_table_name,
        access_method.amname AS access_method,
        index_data.indisvalid AS is_valid,
        index_data.indisready AS is_ready,
        index_data.indislive AS is_live,
        index_data.indisunique AS is_unique,
        index_data.indisprimary AS is_primary,
        index_data.indisexclusion AS is_exclusion,
        index_data.indnkeyatts AS key_column_count,
        index_data.indnatts AS total_column_count,
        pg_catalog.pg_get_expr(index_data.indpred, index_data.indrelid) AS predicate,
        CASE
            WHEN index_data.indexrelid IS NULL THEN NULL
            ELSE ARRAY(
                SELECT pg_catalog.pg_get_indexdef(
                    index_data.indexrelid,
                    positions.key_position,
                    false
                )
                FROM pg_catalog.generate_series(
                    1,
                    index_data.indnkeyatts::integer
                ) AS positions(key_position)
                ORDER BY positions.key_position
            )
        END AS column_definitions,
        pg_catalog.pg_get_indexdef(index_data.indexrelid) AS index_definition
    FROM (
        SELECT pg_catalog.to_regclass(CAST(:table_name AS text)) AS oid
    ) AS target
    LEFT JOIN pg_catalog.pg_class AS target_table
        ON target_table.oid = target.oid
    LEFT JOIN pg_catalog.pg_namespace AS target_namespace
        ON target_namespace.oid = target_table.relnamespace
    LEFT JOIN pg_catalog.pg_class AS index_relation
        ON index_relation.relnamespace = target_namespace.oid
        AND index_relation.relname = :index_name
    LEFT JOIN pg_catalog.pg_index AS index_data
        ON index_data.indexrelid = index_relation.oid
    LEFT JOIN pg_catalog.pg_class AS indexed_table
        ON indexed_table.oid = index_data.indrelid
    LEFT JOIN pg_catalog.pg_am AS access_method
        ON access_method.oid = index_relation.relam
    """
)


def _create_index(
    name: str,
    table_name: str,
    columns: Sequence[str],
    *,
    concurrently: bool,
) -> None:
    op.create_index(
        name,
        table_name,
        columns,
        unique=False,
        postgresql_concurrently=concurrently,
    )


def _create_indexes(*, concurrently: bool) -> None:
    for name, table_name, columns in _INDEXES:
        _create_index(
            name,
            table_name,
            columns,
            concurrently=concurrently,
        )


def _drop_indexes(*, concurrently: bool) -> None:
    for name, table_name, _columns in reversed(_INDEXES):
        op.drop_index(
            name,
            table_name=table_name,
            if_exists=concurrently,
            postgresql_concurrently=concurrently,
        )


def _load_postgresql_index_state(
    connection: Connection,
    *,
    name: str,
    table_name: str,
) -> Mapping[str, object]:
    return dict(
        connection.execute(
            _POSTGRESQL_INDEX_STATE,
            {"index_name": name, "table_name": table_name},
        )
        .mappings()
        .one()
    )


def _definition_conflicts(
    state: Mapping[str, object],
    *,
    table_name: str,
    columns: Sequence[str],
) -> list[str]:
    relation_kind = state["relation_kind"]
    if relation_kind != "i":
        return [f'existing relation is not an ordinary index (relkind={relation_kind!r})']

    conflicts: list[str] = []
    if state["indexed_table_name"] != table_name:
        conflicts.append(f'it belongs to table {state["indexed_table_name"]!r}')
    if state["access_method"] != "btree":
        conflicts.append(f'access method is {state["access_method"]!r}, not "btree"')
    if bool(state["is_unique"]):
        conflicts.append("it is unique")
    if bool(state["is_primary"]):
        conflicts.append("it backs a primary key")
    if bool(state["is_exclusion"]):
        conflicts.append("it backs an exclusion constraint")
    if state["predicate"] is not None:
        conflicts.append(f'it has predicate {state["predicate"]!r}')

    key_column_count = state["key_column_count"]
    total_column_count = state["total_column_count"]
    if key_column_count != len(columns) or total_column_count != len(columns):
        conflicts.append(
            "its key/include column counts are "
            f"{key_column_count!r}/{total_column_count!r}, expected {len(columns)}/{len(columns)}"
        )

    raw_column_definitions = state["column_definitions"]
    actual_column_definitions = tuple(
        cast(Sequence[str], raw_column_definitions)
        if raw_column_definitions is not None
        else ()
    )
    if actual_column_definitions != tuple(columns):
        conflicts.append(
            f"its columns are {actual_column_definitions!r}, expected {tuple(columns)!r}"
        )
    return conflicts


def _reconcile_postgresql_indexes(connection: Connection) -> None:
    for name, table_name, columns in _INDEXES:
        state = _load_postgresql_index_state(
            connection,
            name=name,
            table_name=table_name,
        )
        if state["target_table_name"] is None:
            raise RuntimeError(
                f'Cannot create index "{name}": target table "{table_name}" does not exist'
            )

        if state["relation_kind"] is None:
            _create_index(name, table_name, columns, concurrently=True)
            continue

        conflicts = _definition_conflicts(
            state,
            table_name=table_name,
            columns=columns,
        )
        if conflicts:
            existing_definition = state["index_definition"]
            raise RuntimeError(
                f'Cannot create index "{name}": an existing relation has a conflicting '
                f'definition ({"; ".join(conflicts)}). Existing definition: '
                f"{existing_definition!r}"
            )

        if all(bool(state[key]) for key in ("is_valid", "is_ready", "is_live")):
            continue

        op.drop_index(
            name,
            table_name=table_name,
            if_exists=True,
            postgresql_concurrently=True,
        )
        _create_index(name, table_name, columns, concurrently=True)


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            _reconcile_postgresql_indexes(op.get_bind())
    else:
        _create_indexes(concurrently=False)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            _drop_indexes(concurrently=True)
    else:
        _drop_indexes(concurrently=False)

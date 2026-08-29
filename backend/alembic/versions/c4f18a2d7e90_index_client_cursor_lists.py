"""index cursor-paginated CRM lists

Revision ID: c4f18a2d7e90
Revises: 83b4d45e9c21
Create Date: 2026-08-29 15:40:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c4f18a2d7e90"
down_revision: str | None = "83b4d45e9c21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = (
    (
        "ix_companies_workspace_deleted_created_id",
        "companies",
        ["workspace_id", "deleted_at", "created_at", "id"],
    ),
    (
        "ix_contacts_workspace_deleted_created_id",
        "contacts",
        ["workspace_id", "deleted_at", "created_at", "id"],
    ),
    (
        "ix_tasks_workspace_status_created_id",
        "tasks",
        ["workspace_id", "status", "created_at", "id"],
    ),
    (
        "ix_deals_workspace_pipeline_stage_deleted_created_id",
        "deals",
        ["workspace_id", "pipeline_id", "stage_id", "deleted_at", "created_at", "id"],
    ),
)


def _create_indexes(*, concurrently: bool) -> None:
    for name, table_name, columns in _INDEXES:
        op.create_index(
            name,
            table_name,
            columns,
            unique=False,
            postgresql_concurrently=concurrently,
        )


def _drop_indexes(*, concurrently: bool) -> None:
    for name, table_name, _columns in reversed(_INDEXES):
        op.drop_index(
            name,
            table_name=table_name,
            postgresql_concurrently=concurrently,
        )


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            _create_indexes(concurrently=True)
    else:
        _create_indexes(concurrently=False)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            _drop_indexes(concurrently=True)
    else:
        _drop_indexes(concurrently=False)

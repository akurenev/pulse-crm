"""scope background jobs to a workspace

Revision ID: 83b4d45e9c21
Revises: 6b7f81efc342
Create Date: 2026-08-29 09:35:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "83b4d45e9c21"
down_revision: str | None = "6b7f81efc342"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("background_jobs", sa.Column("workspace_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_background_jobs_workspace_id_workspaces"),
        "background_jobs",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Preserve ownership for jobs queued before this column existed.  Global
    # cleanup/recovery jobs intentionally remain NULL and are not exposed by
    # workspace administration APIs.
    op.execute(
        """
        UPDATE background_jobs AS job
        SET workspace_id = workspace.id
        FROM workspaces AS workspace
        WHERE job.workspace_id IS NULL
          AND job.payload ->> 'workspace_id' = workspace.id::text
        """
    )
    references = (
        ("import_job_id", "import_jobs"),
        ("outbox_event_id", "outbox_events"),
        ("message_id", "messages"),
        ("inbound_event_id", "inbound_events"),
        ("form_submission_id", "form_submissions"),
        ("notification_delivery_id", "notification_deliveries"),
        ("purchase_schedule_id", "purchase_schedules"),
        ("channel_connection_id", "channel_connections"),
        ("task_id", "tasks"),
        ("deal_id", "deals"),
    )
    for payload_key, table_name in references:
        op.execute(
            f"""
            UPDATE background_jobs AS job
            SET workspace_id = entity.workspace_id
            FROM {table_name} AS entity
            WHERE job.workspace_id IS NULL
              AND job.payload ->> '{payload_key}' = entity.id::text
            """
        )

    op.create_index(
        "ix_background_jobs_workspace_updated",
        "background_jobs",
        ["workspace_id", "updated_at"],
        unique=False,
    )
    op.create_table(
        "cursor_access_buckets",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("resource", sa.String(length=64), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_cursor_access_buckets_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_cursor_access_buckets_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cursor_access_buckets")),
        sa.UniqueConstraint(
            "workspace_id",
            "user_id",
            "resource",
            name="uq_cursor_access_bucket_scope",
        ),
    )
    op.create_index(
        "ix_cursor_access_buckets_workspace",
        "cursor_access_buckets",
        ["workspace_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_cursor_access_buckets_workspace", table_name="cursor_access_buckets")
    op.drop_table("cursor_access_buckets")
    op.drop_index("ix_background_jobs_workspace_updated", table_name="background_jobs")
    op.drop_constraint(
        op.f("fk_background_jobs_workspace_id_workspaces"),
        "background_jobs",
        type_="foreignkey",
    )
    op.drop_column("background_jobs", "workspace_id")

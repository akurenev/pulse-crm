"""add note attachments

Revision ID: d4e7b1a92c35
Revises: a6c38f21d904
Create Date: 2026-08-31 18:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4e7b1a92c35"
down_revision: str | None = "a6c38f21d904"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "note_attachments",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("activity_event_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["activity_event_id"],
            ["activity_events.id"],
            name=op.f("fk_note_attachments_activity_event_id_activity_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_note_attachments_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_note_attachments")),
        sa.UniqueConstraint("object_key", name="uq_note_attachment_object_key"),
        sa.UniqueConstraint(
            "activity_event_id",
            "position",
            name="uq_note_attachment_activity_position",
        ),
    )
    op.create_index(
        "ix_note_attachments_workspace_activity",
        "note_attachments",
        ["workspace_id", "activity_event_id", "position", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_note_attachments_workspace_activity",
        table_name="note_attachments",
    )
    op.drop_table("note_attachments")

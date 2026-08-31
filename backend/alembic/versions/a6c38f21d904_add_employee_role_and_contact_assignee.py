"""add employee role and contact assignee

Revision ID: a6c38f21d904
Revises: b2d9a6f4c781
Create Date: 2026-08-31 15:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a6c38f21d904"
down_revision: str | None = "b2d9a6f4c781"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Role is stored as a non-native enum (VARCHAR). ``employee`` is one
    # character longer than the previous longest value, ``manager``.
    op.alter_column(
        "memberships",
        "role",
        existing_type=sa.String(length=7),
        type_=sa.String(length=8),
        existing_nullable=False,
    )
    op.alter_column(
        "invitations",
        "role",
        existing_type=sa.String(length=7),
        type_=sa.String(length=8),
        existing_nullable=False,
    )
    op.add_column("contacts", sa.Column("assignee_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_contacts_assignee_id_users"),
        "contacts",
        "users",
        ["assignee_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_contacts_workspace_assignee_deleted_created_id",
        "contacts",
        ["workspace_id", "assignee_id", "deleted_at", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    employee_roles_exist = sa.text(
        "SELECT (EXISTS (SELECT 1 FROM memberships WHERE role = 'employee') "
        "OR EXISTS (SELECT 1 FROM invitations WHERE role = 'employee'))"
    )
    if op.get_context().as_sql:
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                "IF EXISTS (SELECT 1 FROM memberships WHERE role = 'employee') "
                "OR EXISTS (SELECT 1 FROM invitations WHERE role = 'employee') THEN "
                "RAISE EXCEPTION 'cannot downgrade while employee roles exist; "
                "remap memberships and remove employee invitations first'; "
                "END IF; END $$"
            )
        )
    elif bool(op.get_bind().scalar(employee_roles_exist)):
        raise RuntimeError(
            "cannot downgrade while employee roles exist; "
            "remap memberships and remove employee invitations first"
        )
    op.drop_index(
        "ix_contacts_workspace_assignee_deleted_created_id",
        table_name="contacts",
    )
    op.drop_constraint(
        op.f("fk_contacts_assignee_id_users"),
        "contacts",
        type_="foreignkey",
    )
    op.drop_column("contacts", "assignee_id")
    op.alter_column(
        "invitations",
        "role",
        existing_type=sa.String(length=8),
        type_=sa.String(length=7),
        existing_nullable=False,
    )
    op.alter_column(
        "memberships",
        "role",
        existing_type=sa.String(length=8),
        type_=sa.String(length=7),
        existing_nullable=False,
    )

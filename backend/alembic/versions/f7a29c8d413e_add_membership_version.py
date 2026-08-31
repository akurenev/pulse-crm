"""add membership version

Revision ID: f7a29c8d413e
Revises: d4e7b1a92c35
Create Date: 2026-08-31 19:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f7a29c8d413e"
down_revision: str | None = "d4e7b1a92c35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memberships",
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("memberships", "version")

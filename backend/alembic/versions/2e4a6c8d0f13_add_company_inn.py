"""add INN to companies

Revision ID: 2e4a6c8d0f13
Revises: f7a29c8d413e
Create Date: 2026-09-10 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "2e4a6c8d0f13"
down_revision: str | None = "f7a29c8d413e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("inn", sa.String(length=12), nullable=True))


def downgrade() -> None:
    op.drop_column("companies", "inn")

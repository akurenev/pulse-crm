"""add tags to deals

Revision ID: 6b7f81efc342
Revises: 9f13c7b2a461
Create Date: 2026-08-29 09:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "6b7f81efc342"
down_revision: str | None = "9f13c7b2a461"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deals",
        sa.Column(
            "tags",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
    )
    op.alter_column("deals", "tags", server_default=None)


def downgrade() -> None:
    op.drop_column("deals", "tags")

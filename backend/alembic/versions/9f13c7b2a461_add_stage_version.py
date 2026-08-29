"""add optimistic-locking version to stages

Revision ID: 9f13c7b2a461
Revises: fc49598d464b
Create Date: 2026-08-28 16:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9f13c7b2a461"
down_revision: str | None = "fc49598d464b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "stages",
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("stages", "version")

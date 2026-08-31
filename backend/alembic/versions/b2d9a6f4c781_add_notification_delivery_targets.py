"""add safe notification delivery targets

Revision ID: b2d9a6f4c781
Revises: e8a9d62f1b37
Create Date: 2026-08-31 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b2d9a6f4c781"
down_revision: str | None = "e8a9d62f1b37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notification_deliveries",
        sa.Column("target_entity_type", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "notification_deliveries",
        sa.Column("target_entity_id", sa.Uuid(), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_notification_deliveries_notification_delivery_target_pair"),
        "notification_deliveries",
        "(target_entity_type IS NULL AND target_entity_id IS NULL) OR "
        "(target_entity_type IS NOT NULL AND "
        "target_entity_type IN ('deal', 'task') AND target_entity_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_notification_deliveries_notification_delivery_target_pair"),
        "notification_deliveries",
        type_="check",
    )
    op.drop_column("notification_deliveries", "target_entity_id")
    op.drop_column("notification_deliveries", "target_entity_type")

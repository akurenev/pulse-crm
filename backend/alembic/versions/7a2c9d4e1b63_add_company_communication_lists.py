"""Add company communication lists and preserve existing primary values.

Revision ID: 7a2c9d4e1b63
Revises: 4d6f8a0b2c15
Create Date: 2026-10-23 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "7a2c9d4e1b63"
down_revision: str | None = "4d6f8a0b2c15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    for field in ("phones", "emails"):
        op.add_column(
            "companies",
            sa.Column(field, json_type, server_default=sa.text("'[]'"), nullable=False),
        )
    dialect = op.get_bind().dialect.name
    array_function = "jsonb_build_array" if dialect == "postgresql" else "json_array"
    for primary, values in (("phone", "phones"), ("email", "emails")):
        op.execute(
            sa.text(
                f"UPDATE companies SET {values} = {array_function}({primary}) "
                f"WHERE {primary} IS NOT NULL AND {primary} <> ''"
            )
        )
        first_value = (
            f"{values} ->> 0" if dialect == "postgresql" else f"json_extract({values}, '$[0]')"
        )
        op.execute(sa.text(f"UPDATE companies SET {primary} = {first_value}"))
    # The former contact API allowed a separate primary and a secondary-only
    # list. Put that primary first before the UI starts editing the whole list.
    for primary, values in (("primary_phone", "phones"), ("primary_email", "emails")):
        if dialect == "postgresql":
            expression = (
                f"jsonb_build_array({primary}) || COALESCE("
                f"(SELECT jsonb_agg(value ORDER BY position) "
                f"FROM jsonb_array_elements_text(contacts.{values}) "
                f"WITH ORDINALITY AS entries(value, position) "
                f"WHERE value <> contacts.{primary}), '[]'::jsonb)"
            )
            first_value = f"{values} ->> 0"
        else:
            expression = (
                f"(SELECT json_group_array(value) FROM "
                f"(SELECT contacts.{primary} AS value, -1 AS position UNION ALL "
                f"SELECT value, key AS position FROM json_each(contacts.{values}) "
                f"WHERE value <> contacts.{primary} ORDER BY position))"
            )
            first_value = f"json_extract({values}, '$[0]')"
        op.execute(
            sa.text(
                f"UPDATE contacts SET {values} = {expression} "
                f"WHERE {primary} IS NOT NULL AND {primary} <> ''"
            )
        )
        op.execute(sa.text(f"UPDATE contacts SET {primary} = {first_value}"))


def downgrade() -> None:
    op.drop_column("companies", "emails")
    op.drop_column("companies", "phones")

from __future__ import annotations

import importlib.util
from io import StringIO
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "7a2c9d4e1b63_add_company_communication_lists.py"
    )
    spec = importlib.util.spec_from_file_location("communication_lists_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_migration_preserves_legacy_values_and_is_reversible() -> None:
    migration = _load_migration()
    assert migration.down_revision == "4d6f8a0b2c15"
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    companies = sa.Table(
        "companies",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String),
        sa.Column("phone", sa.String),
    )
    contacts = sa.Table(
        "contacts",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("primary_email", sa.String),
        sa.Column("primary_phone", sa.String),
        sa.Column("emails", sa.JSON, nullable=False),
        sa.Column("phones", sa.JSON, nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            companies.insert(),
            [
                {"id": 1, "email": "office@example.com", "phone": "+12025550101"},
                {"id": 2, "email": None, "phone": ""},
            ],
        )
        connection.execute(
            contacts.insert(),
            [
                {
                    "id": 1,
                    "primary_email": "first@example.com",
                    "primary_phone": "+12025550101",
                    "emails": ["second@example.com", "first@example.com", "third@example.com"],
                    "phones": [],
                },
                {
                    "id": 2,
                    "primary_email": None,
                    "primary_phone": None,
                    "emails": ["only@example.com"],
                    "phones": [],
                },
            ],
        )
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
        migrated_companies = sa.Table("companies", sa.MetaData(), autoload_with=connection)
        company_rows = (
            connection.execute(sa.select(migrated_companies).order_by(migrated_companies.c.id))
            .mappings()
            .all()
        )
        assert company_rows[0]["emails"] == ["office@example.com"]
        assert company_rows[0]["phones"] == ["+12025550101"]
        assert company_rows[1]["emails"] == []
        assert company_rows[1]["phones"] == []
        assert company_rows[1]["phone"] is None
        contact_rows = (
            connection.execute(sa.select(contacts).order_by(contacts.c.id)).mappings().all()
        )
        assert contact_rows[0]["emails"] == [
            "first@example.com",
            "second@example.com",
            "third@example.com",
        ]
        assert contact_rows[0]["phones"] == ["+12025550101"]
        assert contact_rows[1]["primary_email"] == "only@example.com"
        assert contact_rows[1]["primary_phone"] is None
        with Operations.context(context):
            migration.downgrade()
        assert {column["name"] for column in sa.inspect(connection).get_columns("companies")} == {
            "id",
            "email",
            "phone",
        }
        assert connection.execute(sa.select(companies.c.email)).scalars().all() == [
            "office@example.com",
            None,
        ]


def test_migration_generates_postgresql_jsonb_backfill_sql() -> None:
    migration = _load_migration()
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()
    sql = output.getvalue()
    for field in ("phones", "emails"):
        assert f"ADD COLUMN {field} JSONB DEFAULT '[]' NOT NULL" in sql
        assert f"jsonb_array_elements_text(contacts.{field}) WITH ORDINALITY" in sql
        assert f"UPDATE contacts SET primary_{field[:-1]} = {field} ->> 0" in sql
        assert f"DROP COLUMN {field}" in sql
    assert "UPDATE companies SET phones = jsonb_build_array(phone)" in sql
    assert "UPDATE companies SET emails = jsonb_build_array(email)" in sql
    assert "json_each" not in sql

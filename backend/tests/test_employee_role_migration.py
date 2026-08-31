from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.models import Contact, Invitation, Membership


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "a6c38f21d904_add_employee_role_and_contact_assignee.py"
    )
    spec = importlib.util.spec_from_file_location("employee_role_migration", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()


def test_employee_role_migration_follows_current_head() -> None:
    assert migration.down_revision == "b2d9a6f4c781"


def test_employee_schema_metadata_has_role_capacity_and_contact_index() -> None:
    assert Membership.__table__.c.role.type.length == 8
    assert Invitation.__table__.c.role.type.length == 8
    assert Contact.__table__.c.assignee_id.nullable is True
    targets = {
        foreign_key.target_fullname
        for foreign_key in Contact.__table__.c.assignee_id.foreign_keys
    }
    assert targets == {
        "users.id"
    }
    index = next(
        item
        for item in Contact.__table__.indexes
        if item.name == "ix_contacts_workspace_assignee_deleted_created_id"
    )
    assert [column.name for column in index.columns] == [
        "workspace_id",
        "assignee_id",
        "deleted_at",
        "created_at",
        "id",
    ]


def test_employee_role_downgrade_refuses_lossy_role_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destructive_calls: list[str] = []
    monkeypatch.setattr(migration.op, "get_context", lambda: SimpleNamespace(as_sql=False))
    monkeypatch.setattr(
        migration.op,
        "get_bind",
        lambda: SimpleNamespace(scalar=lambda _statement: True),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_index",
        lambda *_args, **_kwargs: destructive_calls.append("drop_index"),
    )

    with pytest.raises(RuntimeError, match="cannot downgrade while employee roles exist"):
        migration.downgrade()

    assert destructive_calls == []


def test_employee_role_downgrade_can_narrow_after_manual_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    altered: list[tuple[str, str]] = []
    monkeypatch.setattr(migration.op, "get_context", lambda: SimpleNamespace(as_sql=False))
    monkeypatch.setattr(
        migration.op,
        "get_bind",
        lambda: SimpleNamespace(scalar=lambda _statement: False),
    )
    monkeypatch.setattr(migration.op, "drop_index", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(migration.op, "drop_constraint", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(migration.op, "drop_column", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(migration.op, "f", lambda value: value)
    monkeypatch.setattr(
        migration.op,
        "alter_column",
        lambda table, column, **_kwargs: altered.append((table, column)),
    )

    migration.downgrade()

    assert altered == [("invitations", "role"), ("memberships", "role")]

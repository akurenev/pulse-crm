from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "f7a29c8d413e_add_membership_version.py"
    )
    spec = importlib.util.spec_from_file_location("membership_version_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_membership_version_migration_follows_note_attachments_and_is_reversible(
    monkeypatch,
) -> None:
    migration = _load_migration()
    added: list[tuple[str, sa.Column]] = []
    dropped: list[tuple[str, str]] = []
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, column: added.append((table, column)),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda table, column: dropped.append((table, column)),
    )

    migration.upgrade()
    migration.downgrade()

    assert migration.down_revision == "d4e7b1a92c35"
    assert len(added) == 1
    table, column = added[0]
    assert table == "memberships"
    assert column.name == "version"
    assert isinstance(column.type, sa.Integer)
    assert column.nullable is False
    assert str(column.server_default.arg) == "1"
    assert dropped == [("memberships", "version")]

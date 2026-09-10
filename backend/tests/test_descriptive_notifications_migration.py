from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "c08f5a7d1e92_make_default_notifications_descriptive.py"
    )
    spec = importlib.util.spec_from_file_location("descriptive_notifications_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_descriptive_notifications_migration_follows_current_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    statements: list[object] = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()

    assert migration.down_revision == "2e4a6c8d0f13"
    assert len(statements) == len(migration.EVENT_PRESENTATION) + 2
    rendered = [str(statement) for statement in statements]
    assert any("UPDATE notification_templates" in statement for statement in rendered)
    assert all("UPDATE notification_deliveries" in statement for statement in rendered[2:])
    assert all("body = :legacy_body" in statement for statement in rendered[2:])

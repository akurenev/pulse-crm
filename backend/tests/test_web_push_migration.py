from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa


def _load_migration() -> ModuleType:
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "e8a9d62f1b37_add_web_push_subscriptions.py"
    )
    spec = importlib.util.spec_from_file_location("web_push_migration", migration_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_web_push_migration_follows_current_head_and_creates_secure_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    tables: list[tuple[object, ...]] = []
    indexes: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(migration.op, "f", lambda value: value)
    monkeypatch.setattr(migration.op, "create_table", lambda *args: tables.append(args))
    monkeypatch.setattr(
        migration.op,
        "create_index",
        lambda *args, **kwargs: indexes.append((args, kwargs)),
    )

    migration.upgrade()

    assert migration.down_revision == "c4f18a2d7e90"
    assert len(tables) == 1
    assert tables[0][0] == "web_push_subscriptions"
    columns = {item.name: item for item in tables[0][1:] if isinstance(item, sa.Column)}
    assert set(columns) >= {
        "workspace_id",
        "user_id",
        "endpoint_hash",
        "encrypted_subscription",
        "encryption_key_id",
        "is_active",
    }
    assert isinstance(columns["encrypted_subscription"].type, sa.LargeBinary)
    assert not columns["endpoint_hash"].nullable
    constraints = [item for item in tables[0][1:] if isinstance(item, sa.UniqueConstraint)]
    assert any(item.name == "uq_web_push_subscription_endpoint_hash" for item in constraints)
    assert indexes == [
        (
            (
                "ix_web_push_subscriptions_workspace_user_active",
                "web_push_subscriptions",
                ["workspace_id", "user_id", "is_active"],
            ),
            {"unique": False},
        )
    ]

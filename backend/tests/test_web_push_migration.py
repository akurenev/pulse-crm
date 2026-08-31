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


def _load_target_migration() -> ModuleType:
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "b2d9a6f4c781_add_notification_delivery_targets.py"
    )
    spec = importlib.util.spec_from_file_location("notification_target_migration", migration_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_web_push_subscription_migration_creates_secure_lookup(
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


def test_notification_target_migration_follows_web_push_head_and_is_reversible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_target_migration()
    added_columns: list[tuple[str, sa.Column[object]]] = []
    checks: list[tuple[object, ...]] = []
    dropped_constraints: list[tuple[tuple[object, ...], dict[str, object]]] = []
    dropped_columns: list[tuple[str, str]] = []
    monkeypatch.setattr(migration.op, "f", lambda value: value)
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, column: added_columns.append((table, column)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda *args: checks.append(args),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda *args, **kwargs: dropped_constraints.append((args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda table, column: dropped_columns.append((table, column)),
    )

    migration.upgrade()
    migration.downgrade()

    assert migration.down_revision == "e8a9d62f1b37"
    assert [(table, column.name) for table, column in added_columns] == [
        ("notification_deliveries", "target_entity_type"),
        ("notification_deliveries", "target_entity_id"),
    ]
    assert isinstance(added_columns[0][1].type, sa.String)
    assert isinstance(added_columns[1][1].type, sa.Uuid)
    assert all(column.nullable for _table, column in added_columns)
    assert checks == [
        (
            "ck_notification_deliveries_notification_delivery_target_pair",
            "notification_deliveries",
            "(target_entity_type IS NULL AND target_entity_id IS NULL) OR "
            "(target_entity_type IS NOT NULL AND "
            "target_entity_type IN ('deal', 'task') AND target_entity_id IS NOT NULL)",
        )
    ]
    assert dropped_constraints == [
        (
            (
                "ck_notification_deliveries_notification_delivery_target_pair",
                "notification_deliveries",
            ),
            {"type_": "check"},
        )
    ]
    assert dropped_columns == [
        ("notification_deliveries", "target_entity_id"),
        ("notification_deliveries", "target_entity_type"),
    ]

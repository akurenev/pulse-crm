from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def _load_migration() -> ModuleType:
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "c4f18a2d7e90_index_client_cursor_lists.py"
    )
    spec = importlib.util.spec_from_file_location("index_cursor_migration", migration_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()


def _index_state(
    table_name: str,
    columns: tuple[str, ...],
    *,
    valid: bool = True,
) -> dict[str, object]:
    return {
        "target_table_name": table_name,
        "target_schema_name": "public",
        "relation_kind": "i",
        "indexed_table_name": table_name,
        "access_method": "btree",
        "is_valid": valid,
        "is_ready": valid,
        "is_live": True,
        "is_unique": False,
        "is_primary": False,
        "is_exclusion": False,
        "key_column_count": len(columns),
        "total_column_count": len(columns),
        "predicate": None,
        "column_definitions": list(columns),
        "index_definition": f"CREATE INDEX test ON public.{table_name} USING btree (...)",
    }


def _matching_state(
    _connection: object,
    *,
    name: str,
    table_name: str,
) -> dict[str, object]:
    columns = next(columns for index, _table, columns in migration._INDEXES if index == name)
    return _index_state(table_name, columns)


def _record_index_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    list[tuple[tuple[object, ...], dict[str, object]]],
    list[tuple[tuple[object, ...], dict[str, object]]],
]:
    created: list[tuple[tuple[object, ...], dict[str, object]]] = []
    dropped: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        migration.op,
        "create_index",
        lambda *args, **kwargs: created.append((args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_index",
        lambda *args, **kwargs: dropped.append((args, kwargs)),
    )
    return created, dropped


def test_postgresql_reconcile_skips_matching_valid_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created, dropped = _record_index_operations(monkeypatch)
    monkeypatch.setattr(migration, "_load_postgresql_index_state", _matching_state)

    migration._reconcile_postgresql_indexes(object())

    assert created == []
    assert dropped == []


def test_postgresql_reconcile_repairs_matching_invalid_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created, dropped = _record_index_operations(monkeypatch)
    first_name, first_table, first_columns = migration._INDEXES[0]

    def load_state(
        _connection: object,
        *,
        name: str,
        table_name: str,
    ) -> dict[str, object]:
        columns = next(columns for index, _table, columns in migration._INDEXES if index == name)
        return _index_state(table_name, columns, valid=name != first_name)

    monkeypatch.setattr(migration, "_load_postgresql_index_state", load_state)

    migration._reconcile_postgresql_indexes(object())

    assert dropped == [
        (
            (first_name,),
            {
                "table_name": first_table,
                "if_exists": True,
                "postgresql_concurrently": True,
            },
        )
    ]
    assert created == [
        (
            (first_name, first_table, first_columns),
            {"unique": False, "postgresql_concurrently": True},
        )
    ]


def test_postgresql_reconcile_creates_only_missing_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created, dropped = _record_index_operations(monkeypatch)
    first_name, first_table, first_columns = migration._INDEXES[0]

    def load_state(
        _connection: object,
        *,
        name: str,
        table_name: str,
    ) -> dict[str, object]:
        columns = next(columns for index, _table, columns in migration._INDEXES if index == name)
        state = _index_state(table_name, columns)
        if name == first_name:
            state.update(
                relation_kind=None,
                indexed_table_name=None,
                access_method=None,
                column_definitions=None,
                index_definition=None,
            )
        return state

    monkeypatch.setattr(migration, "_load_postgresql_index_state", load_state)

    migration._reconcile_postgresql_indexes(object())

    assert dropped == []
    assert created == [
        (
            (first_name, first_table, first_columns),
            {"unique": False, "postgresql_concurrently": True},
        )
    ]


@pytest.mark.parametrize(
    ("state_update", "message"),
    [
        ({"column_definitions": ["workspace_id", "created_at"]}, "its columns are"),
        ({"relation_kind": "r", "index_definition": None}, "not an ordinary index"),
        ({"predicate": "deleted_at IS NULL"}, "it has predicate"),
    ],
)
def test_postgresql_reconcile_rejects_conflicting_relation(
    monkeypatch: pytest.MonkeyPatch,
    state_update: dict[str, object],
    message: str,
) -> None:
    created, dropped = _record_index_operations(monkeypatch)
    first_name, first_table, first_columns = migration._INDEXES[0]

    def load_state(
        _connection: object,
        *,
        name: str,
        table_name: str,
    ) -> dict[str, object]:
        state = _index_state(table_name, first_columns)
        state.update(state_update)
        return state

    monkeypatch.setattr(migration, "_load_postgresql_index_state", load_state)

    with pytest.raises(RuntimeError, match=f'{first_name}.*conflicting definition.*{message}'):
        migration._reconcile_postgresql_indexes(object())

    assert created == []
    assert dropped == []


def test_sqlite_upgrade_keeps_non_concurrent_index_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created, dropped = _record_index_operations(monkeypatch)
    monkeypatch.setattr(
        migration.op,
        "get_bind",
        lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite")),
    )
    monkeypatch.setattr(
        migration.op,
        "get_context",
        lambda: pytest.fail("SQLite upgrade must not request an autocommit block"),
    )

    migration.upgrade()

    assert dropped == []
    assert [operation[0][:2] for operation in created] == [
        (name, table_name) for name, table_name, _columns in migration._INDEXES
    ]
    assert all(
        operation[1] == {"unique": False, "postgresql_concurrently": False}
        for operation in created
    )


def test_postgresql_upgrade_runs_reconciliation_in_autocommit_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    connection = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    @contextmanager
    def autocommit_block() -> Iterator[None]:
        calls.append("enter")
        yield
        calls.append("exit")

    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    monkeypatch.setattr(
        migration.op,
        "get_context",
        lambda: SimpleNamespace(autocommit_block=autocommit_block),
    )
    monkeypatch.setattr(
        migration,
        "_reconcile_postgresql_indexes",
        lambda bind: calls.append("reconcile") if bind is connection else pytest.fail(),
    )

    migration.upgrade()

    assert calls == ["enter", "reconcile", "exit"]

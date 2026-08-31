from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from app.models import NoteAttachment


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[1] / "alembic" / "versions" / "d4e7b1a92c35_add_note_attachments.py"
    )
    spec = importlib.util.spec_from_file_location("note_attachment_migration", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()


def test_note_attachment_migration_follows_employee_access_revision() -> None:
    assert migration.down_revision == "a6c38f21d904"


def test_note_attachment_schema_is_distinct_from_message_attachments() -> None:
    columns = NoteAttachment.__table__.c
    assert "activity_event_id" in columns
    assert "message_id" not in columns
    assert "object_key" in columns
    assert columns.workspace_id.nullable is False
    assert columns.activity_event_id.nullable is False
    assert {
        foreign_key.target_fullname for foreign_key in columns.activity_event_id.foreign_keys
    } == {"activity_events.id"}
    index = next(
        item
        for item in NoteAttachment.__table__.indexes
        if item.name == "ix_note_attachments_workspace_activity"
    )
    assert [column.name for column in index.columns] == [
        "workspace_id",
        "activity_event_id",
        "position",
        "id",
    ]

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from fastapi.responses import StreamingResponse

from app.api import events as events_api
from app.db import SessionLocal
from app.models import RealtimeEvent, Workspace


@dataclass
class _EventContext:
    workspace_id: uuid.UUID
    user_id: uuid.UUID


class _DisconnectAfterFirstPass:
    def __init__(self) -> None:
        self._checks = 0

    async def is_disconnected(self) -> bool:
        self._checks += 1
        return self._checks > 1


async def _response_chunks(response: StreamingResponse) -> list[str]:
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else bytes(chunk).decode())
    return chunks


def _event_ids(chunks: list[str]) -> list[int]:
    return [int(chunk.splitlines()[0].removeprefix("id: ")) for chunk in chunks]


@pytest.mark.asyncio
async def test_fresh_subscriber_starts_at_workspace_tail_and_only_receives_new_events() -> None:
    async with SessionLocal() as db:
        workspace = Workspace(name="Realtime workspace", slug="realtime-workspace")
        other_workspace = Workspace(name="Other workspace", slug="other-workspace")
        db.add_all([workspace, other_workspace])
        await db.flush()

        historical = RealtimeEvent(
            workspace_id=workspace.id,
            event_type="deal.updated",
            payload={"entity_type": "deal", "entity_id": str(uuid.uuid4())},
        )
        foreign_historical = RealtimeEvent(
            workspace_id=other_workspace.id,
            event_type="deal.updated",
            payload={"entity_type": "deal", "entity_id": str(uuid.uuid4())},
        )
        db.add_all([historical, foreign_historical])
        await db.commit()

    initial_cursor = await events_api._initial_event_cursor(
        workspace.id,
        after_id=None,
        last_event_id=None,
    )
    assert initial_cursor == historical.id
    assert initial_cursor != foreign_historical.id

    response = await events_api.realtime_events(
        _DisconnectAfterFirstPass(),  # type: ignore[arg-type]
        _EventContext(workspace_id=workspace.id, user_id=uuid.uuid4()),  # type: ignore[arg-type]
        after_id=None,
        last_event_id=None,
    )

    async with SessionLocal() as db:
        live_event = RealtimeEvent(
            workspace_id=workspace.id,
            event_type="deal.updated",
            payload={"entity_type": "deal", "entity_id": str(uuid.uuid4())},
        )
        foreign_live_event = RealtimeEvent(
            workspace_id=other_workspace.id,
            event_type="deal.updated",
            payload={"entity_type": "deal", "entity_id": str(uuid.uuid4())},
        )
        db.add_all([live_event, foreign_live_event])
        await db.commit()

    assert _event_ids(await _response_chunks(response)) == [live_event.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor_source", ["query", "header"])
async def test_explicit_cursor_replays_only_missed_workspace_events(cursor_source: str) -> None:
    async with SessionLocal() as db:
        workspace = Workspace(name="Reconnect workspace", slug="reconnect-workspace")
        other_workspace = Workspace(name="Foreign workspace", slug="foreign-workspace")
        db.add_all([workspace, other_workspace])
        await db.flush()

        acknowledged = RealtimeEvent(
            workspace_id=workspace.id,
            event_type="deal.updated",
            payload={"entity_type": "deal", "entity_id": str(uuid.uuid4())},
        )
        foreign_event = RealtimeEvent(
            workspace_id=other_workspace.id,
            event_type="deal.updated",
            payload={"entity_type": "deal", "entity_id": str(uuid.uuid4())},
        )
        first_missed = RealtimeEvent(
            workspace_id=workspace.id,
            event_type="task.updated",
            payload={"entity_type": "task", "entity_id": str(uuid.uuid4())},
        )
        second_missed = RealtimeEvent(
            workspace_id=workspace.id,
            event_type="contact.updated",
            payload={"entity_type": "contact", "entity_id": str(uuid.uuid4())},
        )
        db.add_all([acknowledged, foreign_event, first_missed, second_missed])
        await db.commit()

    response = await events_api.realtime_events(
        _DisconnectAfterFirstPass(),  # type: ignore[arg-type]
        _EventContext(workspace_id=workspace.id, user_id=uuid.uuid4()),  # type: ignore[arg-type]
        after_id=acknowledged.id if cursor_source == "query" else None,
        last_event_id=acknowledged.id if cursor_source == "header" else None,
    )

    assert _event_ids(await _response_chunks(response)) == [first_missed.id, second_missed.id]

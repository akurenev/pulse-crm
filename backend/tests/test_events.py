from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.responses import StreamingResponse
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import events as events_api
from app.db import SessionLocal
from app.models import (
    Deal,
    Membership,
    Pipeline,
    RealtimeEvent,
    Role,
    Session,
    Stage,
    StageType,
    User,
    Workspace,
)
from app.security import AuthContext


async def _create_context(
    db: AsyncSession,
    workspace: Workspace,
    *,
    role: Role = Role.owner,
    suffix: str = "owner",
) -> AuthContext:
    user = User(
        email=f"realtime-{suffix}@example.com",
        full_name=f"Realtime {suffix}",
        password_hash="not-used-in-tests",
    )
    db.add(user)
    await db.flush()
    membership = Membership(workspace_id=workspace.id, user_id=user.id, role=role)
    session = Session(
        workspace_id=workspace.id,
        user_id=user.id,
        token_hash=uuid.uuid4().hex,
        csrf_token_hash=uuid.uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add_all([membership, session])
    await db.flush()
    return AuthContext(
        user=user,
        workspace=workspace,
        membership=membership,
        session=session,
        via_cookie=True,
    )


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
        context = await _create_context(db, workspace)

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
        context,
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
        context = await _create_context(db, workspace, suffix=f"owner-{cursor_source}")

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
        context,
        after_id=acknowledged.id if cursor_source == "query" else None,
        last_event_id=acknowledged.id if cursor_source == "header" else None,
    )

    assert _event_ids(await _response_chunks(response)) == [first_missed.id, second_missed.id]


@pytest.mark.asyncio
async def test_access_change_is_opaque_and_recipient_scoped() -> None:
    async with SessionLocal() as db:
        workspace = Workspace(name="Scoped workspace", slug="scoped-workspace")
        db.add(workspace)
        await db.flush()
        recipient = await _create_context(
            db, workspace, role=Role.employee, suffix="employee-recipient"
        )
        other = await _create_context(
            db, workspace, role=Role.employee, suffix="employee-other"
        )
        access_event = RealtimeEvent(
            workspace_id=workspace.id,
            event_type="access.changed",
            payload={
                "recipient_id": str(recipient.user_id),
                "resource": "deal",
            },
        )
        db.add(access_event)
        await db.commit()

    recipient_response = await events_api.realtime_events(
        _DisconnectAfterFirstPass(),  # type: ignore[arg-type]
        recipient,
        after_id=0,
        last_event_id=None,
    )
    recipient_chunks = await _response_chunks(recipient_response)
    assert _event_ids(recipient_chunks) == [access_event.id]
    assert 'data: {"resource":"deal"}' in recipient_chunks[0]
    assert str(recipient.user_id) not in recipient_chunks[0]

    other_response = await events_api.realtime_events(
        _DisconnectAfterFirstPass(),  # type: ignore[arg-type]
        other,
        after_id=0,
        last_event_id=None,
    )
    assert await _response_chunks(other_response) == []


@pytest.mark.asyncio
async def test_stream_reloads_role_and_emits_opaque_cache_reset() -> None:
    async with SessionLocal() as db:
        workspace = Workspace(name="Role workspace", slug="role-workspace")
        db.add(workspace)
        await db.flush()
        context = await _create_context(db, workspace, role=Role.manager, suffix="manager")
        await db.commit()

    response = await events_api.realtime_events(
        _DisconnectAfterFirstPass(),  # type: ignore[arg-type]
        context,
        after_id=None,
        last_event_id=None,
    )

    async with SessionLocal() as db:
        membership = await db.get(Membership, context.membership.id)
        assert membership is not None
        membership.role = Role.employee
        db.add(
            RealtimeEvent(
                workspace_id=workspace.id,
                event_type="deal.updated",
                payload={"entity_type": "deal", "entity_id": str(uuid.uuid4())},
            )
        )
        await db.commit()

    chunks = await _response_chunks(response)
    assert chunks == ['event: access.changed\ndata: {"resource":"all"}\n\n']


@pytest.mark.asyncio
async def test_stream_closes_after_session_revocation() -> None:
    async with SessionLocal() as db:
        workspace = Workspace(name="Revoked workspace", slug="revoked-workspace")
        db.add(workspace)
        await db.flush()
        context = await _create_context(db, workspace, suffix="revoked")
        await db.commit()

    response = await events_api.realtime_events(
        _DisconnectAfterFirstPass(),  # type: ignore[arg-type]
        context,
        after_id=None,
        last_event_id=None,
    )
    async with SessionLocal() as db:
        session = await db.get(Session, context.session.id)
        assert session is not None
        session.revoked_at = datetime.now(UTC)
        await db.commit()

    assert await _response_chunks(response) == []


@pytest.mark.asyncio
async def test_employee_event_acl_batches_replayed_entity_checks() -> None:
    async with SessionLocal() as db:
        workspace = Workspace(name="Batch workspace", slug="batch-workspace")
        db.add(workspace)
        await db.flush()
        context = await _create_context(
            db, workspace, role=Role.employee, suffix="batch-employee"
        )
        pipeline = Pipeline(workspace_id=workspace.id, name="Batch pipeline", position=0)
        db.add(pipeline)
        await db.flush()
        stage = Stage(
            workspace_id=workspace.id,
            pipeline_id=pipeline.id,
            name="Batch stage",
            color="#4B96F8",
            position=0,
            stage_type=StageType.open,
        )
        db.add(stage)
        await db.flush()
        own_deal = Deal(
            workspace_id=workspace.id,
            pipeline_id=pipeline.id,
            stage_id=stage.id,
            assignee_id=context.user_id,
            title="Visible batch deal",
        )
        foreign_deal = Deal(
            workspace_id=workspace.id,
            pipeline_id=pipeline.id,
            stage_id=stage.id,
            title="Foreign batch deal",
        )
        db.add_all([own_deal, foreign_deal])
        await db.flush()
        replay = [
            RealtimeEvent(
                workspace_id=workspace.id,
                event_type="deal.updated",
                payload={
                    "entity_type": "deal",
                    "entity_id": str(own_deal.id if index % 2 == 0 else foreign_deal.id),
                },
            )
            for index in range(100)
        ]
        db.add_all(replay)
        await db.flush()

        query_count = 0

        def count_query(*_args: object) -> None:
            nonlocal query_count
            query_count += 1

        bind = db.sync_session.get_bind()
        sa_event.listen(bind, "before_cursor_execute", count_query)
        try:
            visible = await events_api._employee_visible_event_ids(db, replay, context)
        finally:
            sa_event.remove(bind, "before_cursor_execute", count_query)

        assert visible == {event.id for index, event in enumerate(replay) if index % 2 == 0}
        assert query_count == 1

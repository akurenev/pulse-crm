from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations import messaging
from app.integrations.messaging import (
    ConversationRoute,
    MessagingNotFoundError,
    OriginConversationError,
    list_deal_messages,
    queue_outbound_message,
)
from app.integrations.models import (
    ChannelConnection,
    ChannelKind,
    ConnectionStatus,
    Conversation,
)


@pytest.mark.asyncio
async def test_deal_reply_uses_its_origin_channel_and_is_queued(
    db: AsyncSession, integration_domain: dict[str, object]
) -> None:
    workspace = integration_domain["workspace"]
    user = integration_domain["user"]
    pipeline = integration_domain["pipeline"]
    stage = integration_domain["stage"]
    contact = integration_domain["contact"]
    deal = integration_domain["deal"]
    connection = ChannelConnection(
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        kind=ChannelKind.telegram,
        name="Sales bot",
        status=ConnectionStatus.active,
        default_pipeline_id=pipeline.id,  # type: ignore[attr-defined]
        default_stage_id=stage.id,  # type: ignore[attr-defined]
        default_assignee_id=user.id,  # type: ignore[attr-defined]
    )
    db.add(connection)
    await db.flush()
    conversation = Conversation(
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        channel_connection_id=connection.id,
        contact_id=contact.id,  # type: ignore[attr-defined]
        deal_id=deal.id,  # type: ignore[attr-defined]
        external_thread_id="telegram-chat-42",
    )
    db.add(conversation)
    await db.flush()

    message, routed_connection = await queue_outbound_message(
        db,
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        deal_id=deal.id,  # type: ignore[attr-defined]
        actor_id=user.id,  # type: ignore[attr-defined]
        body="  Добрый день!  ",
        required_deal_assignee_id=user.id,  # type: ignore[attr-defined]
    )
    assert message.body == "Добрый день!"
    assert message.status.value == "queued"
    assert routed_connection.id == connection.id
    rows = await list_deal_messages(
        db,
        workspace_id=workspace.id,  # type: ignore[attr-defined]
        deal_id=deal.id,  # type: ignore[attr-defined]
        limit=50,
    )
    assert [(row[0].id, row[1].kind.value) for row in rows] == [(message.id, "telegram")]

    deal.assignee_id = None  # type: ignore[attr-defined]
    await db.flush()
    with pytest.raises(MessagingNotFoundError, match="deal not found"):
        await queue_outbound_message(
            db,
            workspace_id=workspace.id,  # type: ignore[attr-defined]
            deal_id=deal.id,  # type: ignore[attr-defined]
            actor_id=user.id,  # type: ignore[attr-defined]
            body="Уже не моя сделка",
            required_deal_assignee_id=user.id,  # type: ignore[attr-defined]
        )


@pytest.mark.asyncio
async def test_outbound_queue_rejects_conversation_moved_after_authorization(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = uuid.uuid4()
    requested_deal_id = uuid.uuid4()
    connection = ChannelConnection(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        kind=ChannelKind.telegram,
        name="Moved route",
        status=ConnectionStatus.active,
    )
    conversation = Conversation(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        channel_connection_id=connection.id,
        deal_id=uuid.uuid4(),
        external_thread_id="moved-route-thread",
    )

    async def moved_route(*_args: object, **_kwargs: object) -> ConversationRoute:
        return ConversationRoute(conversation=conversation, connection=connection)

    monkeypatch.setattr(messaging, "resolve_origin_conversation", moved_route)

    with pytest.raises(OriginConversationError, match="moved to another deal"):
        await queue_outbound_message(
            db,
            workspace_id=workspace_id,
            deal_id=requested_deal_id,
            actor_id=uuid.uuid4(),
            body="Не должно попасть в чужую сделку",
        )

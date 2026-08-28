from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from app.db import SessionLocal
from app.integrations.models import (
    ChannelConnection,
    ChannelKind,
    ConnectionStatus,
    Conversation,
    Message,
    MessageDirection,
    MessageStatus,
)
from app.integrations.s3 import AttachmentStorage
from app.main import app
from app.models import Deal, Membership, Pipeline, Stage


class _RecordingS3:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> None:
        self.puts.append(kwargs)

    def delete_object(self, **kwargs: Any) -> None:
        del kwargs

    def generate_presigned_url(
        self,
        client_method: str,
        *,
        Params: dict[str, Any],
        ExpiresIn: int,
    ) -> str:
        return f"https://s3.test/{client_method}/{Params['Key']}?expires={ExpiresIn}"


def _workspace_id(auth: dict[str, object]) -> uuid.UUID:
    workspace = auth["workspace"]
    assert isinstance(workspace, dict)
    return uuid.UUID(str(workspace["id"]))


@pytest.mark.asyncio
async def test_attachment_upload_metadata_and_private_download_link(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _workspace_id(owner_auth)
    fake_s3 = _RecordingS3()
    storage = AttachmentStorage(fake_s3, bucket="pulse-test-private")
    monkeypatch.setattr(app.state, "attachment_storage", storage, raising=False)

    async with SessionLocal() as db:
        owner_id = await db.scalar(
            sa.select(Membership.user_id).where(Membership.workspace_id == workspace_id)
        )
        assert owner_id is not None
        connection = ChannelConnection(
            workspace_id=workspace_id,
            kind=ChannelKind.telegram,
            name="Attachment test bot",
            status=ConnectionStatus.active,
            default_assignee_id=owner_id,
        )
        db.add(connection)
        await db.flush()
        conversation = Conversation(
            workspace_id=workspace_id,
            channel_connection_id=connection.id,
            external_thread_id="attachment-thread",
            participant={"recipient_id": "42"},
        )
        db.add(conversation)
        await db.flush()
        message = Message(
            workspace_id=workspace_id,
            conversation_id=conversation.id,
            direction=MessageDirection.outbound,
            status=MessageStatus.queued,
            body="See the proposal",
        )
        db.add(message)
        await db.commit()

    uploaded = await client.post(
        f"/api/v1/messages/{message.id}/attachments",
        headers={"X-CSRF-Token": str(owner_auth["csrf_token"])},
        files={"file": ("proposal.pdf", b"%PDF-1.7\nproposal", "application/pdf")},
    )

    assert uploaded.status_code == 201, uploaded.text
    attachment = uploaded.json()
    assert attachment["message_id"] == str(message.id)
    assert attachment["original_filename"] == "proposal.pdf"
    assert attachment["content_type"] == "application/pdf"
    assert attachment["size_bytes"] == len(b"%PDF-1.7\nproposal")
    assert len(attachment["sha256"]) == 64
    assert len(fake_s3.puts) == 1
    assert fake_s3.puts[0]["Key"].startswith(f"attachments/{workspace_id}/")
    assert "ACL" not in fake_s3.puts[0]

    metadata = await client.get(f"/api/v1/attachments/{attachment['id']}")
    assert metadata.status_code == 200, metadata.text
    assert metadata.json() == attachment

    download = await client.get(f"/api/v1/attachments/{attachment['id']}/download")
    assert download.status_code == 200, download.text
    assert download.json()["expires_in"] == 300
    assert download.json()["url"].startswith("https://s3.test/get_object/attachments/")
    assert download.json()["url"].endswith("?expires=300")


@pytest.mark.asyncio
async def test_message_and_attachment_are_created_together(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _workspace_id(owner_auth)
    fake_s3 = _RecordingS3()
    monkeypatch.setattr(
        app.state,
        "attachment_storage",
        AttachmentStorage(fake_s3, bucket="pulse-test-private"),
        raising=False,
    )

    async with SessionLocal() as db:
        owner_id = await db.scalar(
            sa.select(Membership.user_id).where(Membership.workspace_id == workspace_id)
        )
        pipeline = await db.scalar(
            sa.select(Pipeline).where(Pipeline.workspace_id == workspace_id)
        )
        assert owner_id is not None and pipeline is not None
        stage = await db.scalar(
            sa.select(Stage)
            .where(Stage.pipeline_id == pipeline.id)
            .order_by(Stage.position)
        )
        assert stage is not None
        deal = Deal(
            workspace_id=workspace_id,
            pipeline_id=pipeline.id,
            stage_id=stage.id,
            title="Attachment deal",
        )
        connection = ChannelConnection(
            workspace_id=workspace_id,
            kind=ChannelKind.telegram,
            name="Atomic attachment bot",
            status=ConnectionStatus.active,
            default_assignee_id=owner_id,
        )
        db.add_all([deal, connection])
        await db.flush()
        conversation = Conversation(
            workspace_id=workspace_id,
            channel_connection_id=connection.id,
            deal_id=deal.id,
            external_thread_id="atomic-attachment-thread",
            participant={"recipient_id": "43"},
        )
        db.add(conversation)
        await db.commit()

    response = await client.post(
        f"/api/v1/deals/{deal.id}/messages/with-attachment",
        headers={"X-CSRF-Token": str(owner_auth["csrf_token"])},
        data={"body": "Proposal attached"},
        files={"file": ("proposal.pdf", b"%PDF-1.7\nproposal", "application/pdf")},
    )

    assert response.status_code == 201, response.text
    result = response.json()
    assert result["message"]["status"] == "queued"
    assert result["message"]["body"] == "Proposal attached"
    assert result["attachment"]["message_id"] == result["message"]["id"]
    assert len(fake_s3.puts) == 1

    async with SessionLocal() as db:
        persisted = await db.get(Message, uuid.UUID(result["message"]["id"]))
        assert persisted is not None

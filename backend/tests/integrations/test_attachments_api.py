from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from app.db import SessionLocal, engine
from app.integrations.models import (
    Attachment,
    ChannelConnection,
    ChannelKind,
    ConnectionStatus,
    Conversation,
    Message,
    MessageDirection,
    MessageStatus,
)
from app.integrations.s3 import AttachmentStorage, StoredObject, ValidatedAttachment
from app.main import app
from app.models import (
    ActivityEvent,
    Deal,
    Membership,
    NoteAttachment,
    Pipeline,
    Stage,
    StageType,
    Workspace,
)


class _RecordingS3:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []
        self.deletes: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> None:
        self.puts.append(kwargs)

    def delete_object(self, **kwargs: Any) -> None:
        self.deletes.append(kwargs)

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


def _csrf(auth: dict[str, object]) -> dict[str, str]:
    return {"X-CSRF-Token": str(auth["csrf_token"])}


async def _create_deal(
    client: httpx.AsyncClient,
    auth: dict[str, object],
    *,
    title: str,
    assignee_id: str | None = None,
) -> dict[str, Any]:
    pipeline = (await client.get("/api/v1/pipelines")).json()[0]
    payload: dict[str, Any] = {
        "title": title,
        "pipeline_id": pipeline["id"],
        "stage_id": pipeline["stages"][0]["id"],
    }
    if assignee_id is not None:
        payload["assignee_id"] = assignee_id
    response = await client.post(
        "/api/v1/deals",
        headers=_csrf(auth),
        json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _invite_employee(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
    *,
    email: str,
) -> tuple[httpx.AsyncClient, dict[str, Any]]:
    invitation = await client.post(
        "/api/v1/invitations",
        headers=_csrf(owner_auth),
        json={"email": email, "role": "employee"},
    )
    assert invitation.status_code == 201, invitation.text
    employee = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    accepted = await employee.post(
        "/api/v1/auth/accept-invitation",
        json={
            "token": invitation.json()["token"],
            "full_name": "Note Attachment Employee",
            "password": "employee test password",
        },
    )
    assert accepted.status_code == 201, accepted.text
    return employee, accepted.json()


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

    async with SessionLocal() as db:
        audit = await db.scalar(
            sa.select(ActivityEvent).where(
                ActivityEvent.workspace_id == workspace_id,
                ActivityEvent.entity_id == uuid.UUID(str(attachment["id"])),
                ActivityEvent.event_type == "attachment.download_link_issued",
            )
        )
        assert audit is not None
        assert audit.actor_id == owner_id
        assert audit.payload == {
            "message_id": str(message.id),
            "size_bytes": len(b"%PDF-1.7\nproposal"),
            "expires_in": 300,
        }


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
        pipeline = await db.scalar(sa.select(Pipeline).where(Pipeline.workspace_id == workspace_id))
        assert owner_id is not None and pipeline is not None
        stage = await db.scalar(
            sa.select(Stage).where(Stage.pipeline_id == pipeline.id).order_by(Stage.position)
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


@pytest.mark.asyncio
async def test_deal_note_files_are_separate_private_attachments_in_activity(
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
    deal = await _create_deal(client, owner_auth, title="Note files")

    response = await client.post(
        f"/api/v1/deals/{deal['id']}/notes/with-attachments",
        headers=_csrf(owner_auth),
        data={"body": "  Договор и реквизиты  "},
        files=[
            ("files", ("agreement.pdf", b"%PDF-1.7\nagreement", "application/pdf")),
            ("files", ("details.txt", b"Test company details", "text/plain")),
        ],
    )

    assert response.status_code == 201, response.text
    note = response.json()
    assert note["event_type"] == "deal.note.created"
    assert note["payload"] == {"body": "Договор и реквизиты"}
    assert [item["original_filename"] for item in note["attachments"]] == [
        "agreement.pdf",
        "details.txt",
    ]
    assert all("object_key" not in item for item in note["attachments"])
    assert "object_key" not in response.text
    assert len(fake_s3.puts) == 2
    assert all(item["Key"].startswith(f"attachments/{workspace_id}/") for item in fake_s3.puts)
    assert all("ACL" not in item for item in fake_s3.puts)

    attachment_selects: list[str] = []

    def count_attachment_selects(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().casefold().startswith("select") and "note_attachments" in statement:
            attachment_selects.append(statement)

    sa.event.listen(engine.sync_engine, "before_cursor_execute", count_attachment_selects)
    try:
        activity = await client.get(
            "/api/v1/activity",
            params={"entity_type": "deal", "entity_id": deal["id"]},
        )
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", count_attachment_selects)
    assert activity.status_code == 200, activity.text
    assert len(attachment_selects) == 1
    activity_note = next(item for item in activity.json()["items"] if item["id"] == note["id"])
    assert activity_note["attachments"] == note["attachments"]
    assert "object_key" not in activity.text

    first_attachment = note["attachments"][0]
    metadata = await client.get(f"/api/v1/note-attachments/{first_attachment['id']}")
    assert metadata.status_code == 200, metadata.text
    assert metadata.json() == first_attachment
    download = await client.get(f"/api/v1/note-attachments/{first_attachment['id']}/download")
    assert download.status_code == 200, download.text
    assert download.json()["expires_in"] == 300
    assert download.json()["url"].startswith("https://s3.test/get_object/attachments/")

    plain_note = await client.post(
        f"/api/v1/deals/{deal['id']}/notes",
        headers=_csrf(owner_auth),
        json={"body": "JSON note remains supported"},
    )
    assert plain_note.status_code == 201, plain_note.text
    assert plain_note.json()["attachments"] == []

    async with SessionLocal() as db:
        note_records = list(
            (
                await db.scalars(
                    sa.select(NoteAttachment).where(
                        NoteAttachment.workspace_id == workspace_id,
                        NoteAttachment.activity_event_id == uuid.UUID(note["id"]),
                    )
                )
            ).all()
        )
        message_records = list(
            (
                await db.scalars(
                    sa.select(Attachment).where(Attachment.workspace_id == workspace_id)
                )
            ).all()
        )
        audit = await db.scalar(
            sa.select(ActivityEvent).where(
                ActivityEvent.workspace_id == workspace_id,
                ActivityEvent.event_type == "note_attachment.download_link_issued",
                ActivityEvent.entity_id == uuid.UUID(first_attachment["id"]),
            )
        )
    assert len(note_records) == 2
    assert message_records == []
    assert audit is not None
    assert audit.payload == {
        "activity_event_id": note["id"],
        "size_bytes": len(b"%PDF-1.7\nagreement"),
        "expires_in": 300,
    }


@pytest.mark.asyncio
async def test_note_attachment_employee_acl_and_cross_workspace_are_fail_closed(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_s3 = _RecordingS3()
    monkeypatch.setattr(
        app.state,
        "attachment_storage",
        AttachmentStorage(fake_s3, bucket="pulse-test-private"),
        raising=False,
    )
    employee, employee_auth = await _invite_employee(
        client,
        owner_auth,
        email="note-files-employee@example.com",
    )
    try:
        own_deal = await _create_deal(
            client,
            owner_auth,
            title="Employee note files",
            assignee_id=str(employee_auth["user"]["id"]),
        )
        foreign_deal = await _create_deal(
            client,
            owner_auth,
            title="Owner note files",
            assignee_id=str(owner_auth["user"]["id"]),
        )
        own_note = await employee.post(
            f"/api/v1/deals/{own_deal['id']}/notes/with-attachments",
            headers=_csrf(employee_auth),
            data={"body": "Own file"},
            files={"files": ("own.txt", b"Own", "text/plain")},
        )
        assert own_note.status_code == 201, own_note.text
        attachment_id = own_note.json()["attachments"][0]["id"]
        assert (await employee.get(f"/api/v1/note-attachments/{attachment_id}")).status_code == 200
        employee_activity = await employee.get(
            "/api/v1/activity",
            params={"entity_type": "deal", "entity_id": own_deal["id"]},
        )
        assert employee_activity.status_code == 200, employee_activity.text
        visible_note = next(
            item
            for item in employee_activity.json()["items"]
            if item["id"] == own_note.json()["id"]
        )
        assert visible_note["payload"] == {"body": "Own file"}
        assert visible_note["attachments"][0]["id"] == attachment_id

        owner_note = await client.post(
            f"/api/v1/deals/{foreign_deal['id']}/notes/with-attachments",
            headers=_csrf(owner_auth),
            data={"body": "Hidden file"},
            files={"files": ("hidden.txt", b"Hidden", "text/plain")},
        )
        assert owner_note.status_code == 201, owner_note.text
        hidden_attachment_id = owner_note.json()["attachments"][0]["id"]
        puts_before_idor = len(fake_s3.puts)

        rejected_upload = await employee.post(
            f"/api/v1/deals/{foreign_deal['id']}/notes/with-attachments",
            headers=_csrf(employee_auth),
            data={"body": "IDOR"},
            files={"files": ("idor.txt", b"IDOR", "text/plain")},
        )
        assert rejected_upload.status_code == 404, rejected_upload.text
        assert len(fake_s3.puts) == puts_before_idor
        assert (
            await employee.get(f"/api/v1/note-attachments/{hidden_attachment_id}")
        ).status_code == 404
        assert (
            await employee.get(f"/api/v1/note-attachments/{hidden_attachment_id}/download")
        ).status_code == 404

        async with SessionLocal() as db:
            other_workspace = Workspace(name="Other Workspace", slug="other-workspace")
            db.add(other_workspace)
            await db.flush()
            pipeline = Pipeline(workspace_id=other_workspace.id, name="Other pipeline")
            db.add(pipeline)
            await db.flush()
            stage = Stage(
                workspace_id=other_workspace.id,
                pipeline_id=pipeline.id,
                name="Open",
                position=0,
                stage_type=StageType.open,
            )
            db.add(stage)
            await db.flush()
            other_deal = Deal(
                workspace_id=other_workspace.id,
                pipeline_id=pipeline.id,
                stage_id=stage.id,
                title="Cross-workspace deal",
            )
            db.add(other_deal)
            await db.flush()
            other_activity = ActivityEvent(
                workspace_id=other_workspace.id,
                event_type="deal.note.created",
                entity_type="deal",
                entity_id=other_deal.id,
                payload={"body": "Foreign workspace"},
            )
            db.add(other_activity)
            await db.flush()
            other_attachment = NoteAttachment(
                workspace_id=other_workspace.id,
                activity_event_id=other_activity.id,
                position=0,
                object_key=f"attachments/{other_workspace.id}/foreign/file.txt",
                original_filename="file.txt",
                content_type="text/plain",
                size_bytes=4,
                sha256="0" * 64,
            )
            db.add(other_attachment)
            await db.commit()
            other_attachment_id = other_attachment.id

        assert (
            await client.get(f"/api/v1/note-attachments/{other_attachment_id}")
        ).status_code == 404
        assert (
            await client.get(f"/api/v1/note-attachments/{other_attachment_id}/download")
        ).status_code == 404
        assert (
            await client.get(f"/api/v1/note-attachments/{uuid.uuid4()}/download")
        ).status_code == 404
    finally:
        await employee.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content", "content_type", "expected_status"),
    [
        ("script.exe", b"MZ executable", "application/octet-stream", 422),
        ("empty.txt", b"", "text/plain", 422),
        ("large.txt", b"x" * (20 * 1024 * 1024 + 1), "text/plain", 413),
    ],
    ids=("executable", "empty", "oversized"),
)
async def test_note_attachment_validation_rejects_unsafe_files_before_storage(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    content: bytes,
    content_type: str,
    expected_status: int,
) -> None:
    fake_s3 = _RecordingS3()
    monkeypatch.setattr(
        app.state,
        "attachment_storage",
        AttachmentStorage(fake_s3, bucket="pulse-test-private"),
        raising=False,
    )
    deal = await _create_deal(client, owner_auth, title=f"Invalid {filename}")

    response = await client.post(
        f"/api/v1/deals/{deal['id']}/notes/with-attachments",
        headers=_csrf(owner_auth),
        data={"body": "Unsafe"},
        files={"files": (filename, content, content_type)},
    )

    assert response.status_code == expected_status, response.text
    assert fake_s3.puts == []
    async with SessionLocal() as db:
        assert (
            await db.scalar(
                sa.select(sa.func.count())
                .select_from(ActivityEvent)
                .where(
                    ActivityEvent.entity_id == uuid.UUID(deal["id"]),
                    ActivityEvent.event_type == "deal.note.created",
                )
            )
        ) == 0


@pytest.mark.asyncio
async def test_note_attachment_count_and_blank_body_are_rejected(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_s3 = _RecordingS3()
    monkeypatch.setattr(
        app.state,
        "attachment_storage",
        AttachmentStorage(fake_s3, bucket="pulse-test-private"),
        raising=False,
    )
    deal = await _create_deal(client, owner_auth, title="Attachment count")
    six_files = [("files", (f"file-{index}.txt", b"safe", "text/plain")) for index in range(6)]

    too_many = await client.post(
        f"/api/v1/deals/{deal['id']}/notes/with-attachments",
        headers=_csrf(owner_auth),
        data={"body": "Too many"},
        files=six_files,
    )
    blank = await client.post(
        f"/api/v1/deals/{deal['id']}/notes/with-attachments",
        headers=_csrf(owner_auth),
        data={"body": "   "},
        files={"files": ("safe.txt", b"safe", "text/plain")},
    )

    assert too_many.status_code == 422, too_many.text
    assert too_many.json()["detail"] == "a note can have at most 5 attachments"
    assert blank.status_code == 422, blank.text
    assert blank.json()["detail"] == "note must not be blank"
    assert fake_s3.puts == []


class _FailingSecondPutS3(_RecordingS3):
    def put_object(self, **kwargs: Any) -> None:
        if len(self.puts) == 1:
            raise RuntimeError("simulated S3 outage")
        super().put_object(**kwargs)


@pytest.mark.asyncio
async def test_note_attachment_storage_failure_rolls_back_and_cleans_uploaded_objects(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = _workspace_id(owner_auth)
    fake_s3 = _FailingSecondPutS3()
    monkeypatch.setattr(
        app.state,
        "attachment_storage",
        AttachmentStorage(fake_s3, bucket="pulse-test-private"),
        raising=False,
    )
    deal = await _create_deal(client, owner_auth, title="Atomic note files")

    response = await client.post(
        f"/api/v1/deals/{deal['id']}/notes/with-attachments",
        headers=_csrf(owner_auth),
        data={"body": "Must roll back"},
        files=[
            ("files", ("first.txt", b"first", "text/plain")),
            ("files", ("second.txt", b"second", "text/plain")),
        ],
    )

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "note attachments could not be stored"
    assert len(fake_s3.puts) == 1
    assert len(fake_s3.deletes) == 1
    assert fake_s3.deletes[0]["Key"] == fake_s3.puts[0]["Key"]
    assert fake_s3.deletes[0]["Key"].startswith(f"attachments/{workspace_id}/")
    async with SessionLocal() as db:
        notes = await db.scalar(
            sa.select(sa.func.count())
            .select_from(ActivityEvent)
            .where(
                ActivityEvent.entity_id == uuid.UUID(deal["id"]),
                ActivityEvent.event_type == "deal.note.created",
            )
        )
        attachments = await db.scalar(
            sa.select(sa.func.count())
            .select_from(NoteAttachment)
            .where(NoteAttachment.workspace_id == workspace_id)
        )
    assert notes == 0
    assert attachments == 0


class _DuplicateObjectStorage(AttachmentStorage):
    def __init__(self) -> None:
        super().__init__(_RecordingS3(), bucket="pulse-test-private")
        self.deleted: list[str] = []

    async def store(
        self,
        *,
        workspace_id: uuid.UUID,
        filename: str,
        content_type: str,
        content: bytes,
        now: Any = None,
    ) -> StoredObject:
        del workspace_id, now
        return StoredObject(
            object_key="attachments/shared/duplicate.txt",
            attachment=ValidatedAttachment(
                filename=filename,
                content_type=content_type,
                size_bytes=len(content),
                sha256="1" * 64,
            ),
        )

    async def delete(self, *, workspace_id: uuid.UUID, object_key: str) -> None:
        del workspace_id
        self.deleted.append(object_key)


@pytest.mark.asyncio
async def test_note_attachment_database_failure_cleans_all_stored_objects(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _DuplicateObjectStorage()
    monkeypatch.setattr(app.state, "attachment_storage", storage, raising=False)
    deal = await _create_deal(client, owner_auth, title="Database rollback")

    response = await client.post(
        f"/api/v1/deals/{deal['id']}/notes/with-attachments",
        headers=_csrf(owner_auth),
        data={"body": "Duplicate keys"},
        files=[
            ("files", ("first.txt", b"first", "text/plain")),
            ("files", ("second.txt", b"second", "text/plain")),
        ],
    )

    assert response.status_code == 503, response.text
    assert storage.deleted == [
        "attachments/shared/duplicate.txt",
        "attachments/shared/duplicate.txt",
    ]
    async with SessionLocal() as db:
        assert (
            await db.scalar(
                sa.select(sa.func.count())
                .select_from(NoteAttachment)
                .where(NoteAttachment.workspace_id == _workspace_id(owner_auth))
            )
        ) == 0

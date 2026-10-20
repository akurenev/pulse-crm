from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from app.db import SessionLocal
from app.integrations.s3 import AttachmentStorage
from app.integrations.secrets import SecretCipher
from app.mail.models import MailAccount, MailAttachment, MailFolder, MailLink, MailMessage
from app.mail.service import decrypt_credentials
from app.main import app
from app.models import (
    ActivityEvent,
    BackgroundJob,
    Contact,
    Deal,
    Membership,
    NoteAttachment,
    RealtimeEvent,
    Role,
    Session,
    User,
    Workspace,
)
from app.security import digest_token


class MailS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, **kwargs: Any) -> None:
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        content = self.objects[kwargs["Key"]]
        return {"Body": io.BytesIO(content), "ContentLength": len(content)}

    def delete_object(self, **kwargs: Any) -> None:
        self.objects.pop(kwargs["Key"], None)

    def generate_presigned_url(
        self, client_method: str, *, Params: dict[str, Any], ExpiresIn: int
    ) -> str:
        return f"https://storage.example.com/{client_method}/{Params['Key']}?ttl={ExpiresIn}"


class CheckTransport:
    async def check(self, account: MailAccount, credentials: dict[str, str]) -> None:
        del account, credentials


@pytest.fixture
def mail_storage(monkeypatch: pytest.MonkeyPatch) -> MailS3:
    s3 = MailS3()
    monkeypatch.setattr(
        app.state, "integration_secret_cipher", SecretCipher(b"t" * 32, "mail-test"), raising=False
    )
    monkeypatch.setattr(
        app.state, "attachment_storage", AttachmentStorage(s3, bucket="test-mail"), raising=False
    )
    monkeypatch.setattr(app.state, "mail_transport", CheckTransport(), raising=False)
    return s3


def headers(auth: dict[str, object]) -> dict[str, str]:
    return {"X-CSRF-Token": str(auth["csrf_token"])}


def account_input(**changes: Any) -> dict[str, Any]:
    return {
        "email": "mailbox@example.com",
        "display_name": "Test Employee",
        "password": "test-mail-password",
        "imap_host": "imap.example.com",
        "smtp_host": "smtp.example.com",
        **changes,
    }


async def account(
    client: httpx.AsyncClient, auth: dict[str, object], **changes: Any
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/mail/accounts", headers=headers(auth), json=account_input(**changes)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def draft(
    client: httpx.AsyncClient, auth: dict[str, object], account_id: str, **changes: Any
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/mail/accounts/{account_id}/drafts",
        headers=headers(auth),
        json={
            "subject": "Private offer",
            "to_addresses": ["customer@example.com"],
            "body_text": "Private mailbox content",
            **changes,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def peer_session(
    auth: dict[str, object], role: Role, *, other_workspace: bool = False
) -> dict[str, str]:
    async with SessionLocal() as db:
        workspace_id = uuid.UUID(str(auth["workspace"]["id"]))  # type: ignore[index]
        if other_workspace:
            workspace = Workspace(name="Other workspace", slug="other-mail-workspace")
            db.add(workspace)
            await db.flush()
            workspace_id = workspace.id
            # Same human in a different workspace must not cross tenant boundaries.
            user_id = uuid.UUID(str(auth["user"]["id"]))  # type: ignore[index]
        else:
            user = User(email=f"peer-{role.value}@example.com", full_name="Peer", password_hash="x")
            db.add(user)
            await db.flush()
            user_id = user.id
        db.add(Membership(workspace_id=workspace_id, user_id=user_id, role=role))
        token = f"test-mail-{role.value}-{'other' if other_workspace else 'same'}"
        db.add(
            Session(
                user_id=user_id,
                workspace_id=workspace_id,
                token_hash=digest_token(token),
                csrf_token_hash=digest_token("unused"),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        await db.commit()
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_mail_credentials_csrf_validation_and_no_automatic_crm_objects(
    client: httpx.AsyncClient, owner_auth: dict[str, object], mail_storage: MailS3
) -> None:
    del mail_storage
    denied = await client.post("/api/v1/mail/accounts", json=account_input())
    assert denied.status_code == 403
    invalid = account_input()
    invalid.pop("smtp_host")
    response = await client.post("/api/v1/mail/accounts", headers=headers(owner_auth), json=invalid)
    assert response.status_code == 422
    assert "test-mail-password" not in response.text
    invalid_password = await client.post(
        "/api/v1/mail/accounts",
        headers=headers(owner_auth),
        json=account_input(password="x" * 4097),
    )
    assert invalid_password.status_code == 422
    assert "x" * 20 not in invalid_password.text
    injected_password = await client.post(
        "/api/v1/mail/accounts",
        headers=headers(owner_auth),
        json=account_input(password="private-password\r\nINJECT"),
    )
    assert injected_password.status_code == 422
    assert "private-password" not in injected_password.text
    created = await account(client, owner_auth)
    assert created["has_credentials"] is True
    assert "password" not in str(created)
    assert "encrypted_credentials" not in created
    assert (
        await client.post(
            f"/api/v1/mail/accounts/{created['id']}/check", headers=headers(owner_auth)
        )
    ).json()["healthy"] is True
    async with SessionLocal() as db:
        saved = await db.get(MailAccount, uuid.UUID(created["id"]))
        assert saved is not None and saved.encrypted_credentials is not None
        assert b"test-mail-password" not in saved.encrypted_credentials
        assert (
            decrypt_credentials(saved, app.state.integration_secret_cipher)["password"]
            == "test-mail-password"
        )
        assert await db.scalar(sa.select(sa.func.count()).select_from(Contact)) == 0
        assert await db.scalar(sa.select(sa.func.count()).select_from(Deal)) == 0
        assert (
            await db.scalar(
                sa.select(sa.func.count())
                .select_from(RealtimeEvent)
                .where(
                    RealtimeEvent.event_type.like("mail.%"),
                )
            )
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("other_workspace", [False, True])
async def test_all_roles_and_workspaces_cannot_read_or_mutate_another_private_mailbox(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
    mail_storage: MailS3,
    role: Role,
    other_workspace: bool,
) -> None:
    del mail_storage
    created = await account(client, owner_auth)
    message = await draft(client, owner_auth, created["id"])
    uploaded = await client.post(
        f"/api/v1/mail/messages/{message['id']}/attachments",
        headers=headers(owner_auth),
        files={"file": ("private.txt", b"private attachment", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    attachment_id = uploaded.json()["id"]
    peer = await peer_session(owner_auth, role, other_workspace=other_workspace)
    assert (await client.get("/api/v1/mail/accounts", headers=peer)).json() == {"items": []}
    operations: list[tuple[str, str, dict[str, Any]]] = [
        ("GET", f"/accounts/{created['id']}/folders", {}),
        ("GET", f"/accounts/{created['id']}/messages", {}),
        ("GET", f"/messages/{message['id']}", {}),
        (
            "PATCH",
            f"/accounts/{created['id']}",
            {"json": {"expected_version": 1, "display_name": "stolen"}},
        ),
        ("DELETE", f"/accounts/{created['id']}?expected_version=1", {}),
        ("POST", f"/accounts/{created['id']}/check", {}),
        ("POST", f"/accounts/{created['id']}/sync", {}),
        ("POST", f"/accounts/{created['id']}/drafts", {"json": {}}),
        ("PATCH", f"/messages/{message['id']}/draft", {"json": {"expected_version": 1}}),
        ("PATCH", f"/messages/{message['id']}", {"json": {"expected_version": 1, "is_read": True}}),
        ("POST", f"/messages/{message['id']}/send", {"json": {"expected_version": 1}}),
        ("DELETE", f"/messages/{message['id']}?expected_version=1", {}),
        ("POST", f"/messages/{message['id']}/link", {"json": {"deal_id": str(uuid.uuid4())}}),
        ("POST", f"/attachments/{attachment_id}/download", {}),
        ("DELETE", f"/attachments/{attachment_id}?expected_version=1", {}),
        (
            "POST",
            f"/messages/{message['id']}/attachments",
            {"files": {"file": ("x.txt", b"x", "text/plain")}},
        ),
    ]
    for method, path, kwargs in operations:
        response = await client.request(method, "/api/v1/mail" + path, headers=peer, **kwargs)
        assert response.status_code == 404, (role, path, response.text)


@pytest.mark.asyncio
async def test_drafts_queue_once_with_stable_id_bounded_search_and_versions(
    client: httpx.AsyncClient, owner_auth: dict[str, object], mail_storage: MailS3
) -> None:
    del mail_storage
    created = await account(client, owner_auth)
    first = await draft(client, owner_auth, created["id"], subject="Offer 100%_confirmed")
    await draft(client, owner_auth, created["id"], subject="Offer 100XXconfirmed")
    filtered = await client.get(
        f"/api/v1/mail/accounts/{created['id']}/messages", params={"search": "%_"}
    )
    assert [item["id"] for item in filtered.json()["items"]] == [first["id"]]
    page = (
        await client.get(f"/api/v1/mail/accounts/{created['id']}/messages", params={"limit": 1})
    ).json()
    assert len(page["items"]) == 1 and page["next_cursor"]
    next_page = (
        await client.get(
            f"/api/v1/mail/accounts/{created['id']}/messages",
            params={"limit": 1, "cursor": page["next_cursor"]},
        )
    ).json()
    assert len(next_page["items"]) == 1 and next_page["items"][0]["id"] != page["items"][0]["id"]
    changed = await client.patch(
        f"/api/v1/mail/messages/{first['id']}/draft",
        headers=headers(owner_auth),
        json={
            "expected_version": 1,
            "subject": "Changed",
            "to_addresses": ["customer@example.com"],
        },
    )
    assert changed.status_code == 200, changed.text
    stale = await client.post(
        f"/api/v1/mail/messages/{first['id']}/send",
        headers=headers(owner_auth),
        json={"expected_version": 1},
    )
    assert stale.status_code == 409
    sent = await client.post(
        f"/api/v1/mail/messages/{first['id']}/send",
        headers=headers(owner_auth),
        json={"expected_version": changed.json()["version"]},
    )
    assert sent.status_code == 202, sent.text
    assert sent.json()["status"] == "queued" and sent.json()["internet_message_id"]
    repeated = await client.post(
        f"/api/v1/mail/messages/{first['id']}/send",
        headers=headers(owner_auth),
        json={"expected_version": sent.json()["version"]},
    )
    assert repeated.status_code == 409
    async with SessionLocal() as db:
        jobs = list(
            (
                await db.scalars(
                    sa.select(BackgroundJob).where(
                        BackgroundJob.job_type == "mail.send",
                    )
                )
            ).all()
        )
        assert len(jobs) == 1
        assert jobs[0].payload == {"message_id": first["id"]}
        assert "Private" not in str(jobs[0].payload)


@pytest.mark.asyncio
async def test_forward_copies_attachments_and_removal_does_not_change_original(
    client: httpx.AsyncClient, owner_auth: dict[str, object], mail_storage: MailS3
) -> None:
    created = await account(client, owner_auth)
    original = await draft(client, owner_auth, created["id"])
    upload = await client.post(
        f"/api/v1/mail/messages/{original['id']}/attachments",
        headers=headers(owner_auth),
        files={"file": ("offer.txt", b"original offer", "text/plain")},
    )
    assert upload.status_code == 201, upload.text
    changed_original = (await client.get(f"/api/v1/mail/messages/{original['id']}")).json()
    assert changed_original["version"] == original["version"] + 1
    stale_send = await client.post(
        f"/api/v1/mail/messages/{original['id']}/send",
        headers=headers(owner_auth),
        json={"expected_version": original["version"]},
    )
    assert stale_send.status_code == 409
    forwarded = await draft(client, owner_auth, created["id"], forward_message_id=original["id"])
    assert len(forwarded["attachments"]) == 1
    original_file = upload.json()
    copied_file = forwarded["attachments"][0]
    assert copied_file["id"] != original_file["id"]
    assert copied_file["sha256"] == original_file["sha256"]
    assert len(mail_storage.objects) == 2
    denied_csrf = await client.post(f"/api/v1/mail/attachments/{copied_file['id']}/download")
    assert denied_csrf.status_code == 403
    download = await client.post(
        f"/api/v1/mail/attachments/{copied_file['id']}/download", headers=headers(owner_auth)
    )
    assert download.status_code == 200
    removed = await client.delete(
        f"/api/v1/mail/attachments/{copied_file['id']}?expected_version={forwarded['version']}",
        headers=headers(owner_auth),
    )
    assert removed.status_code == 204, removed.text
    loaded = (await client.get(f"/api/v1/mail/messages/{forwarded['id']}")).json()
    assert loaded["version"] == forwarded["version"] + 1 and loaded["attachments"] == []
    assert len(mail_storage.objects) == 1
    assert (
        len((await client.get(f"/api/v1/mail/messages/{original['id']}")).json()["attachments"])
        == 1
    )


@pytest.mark.asyncio
async def test_deal_snapshot_is_independent_and_disconnect_closes_mail_access(
    client: httpx.AsyncClient, owner_auth: dict[str, object], mail_storage: MailS3
) -> None:
    created = await account(client, owner_auth)
    message = await draft(client, owner_auth, created["id"])
    upload = await client.post(
        f"/api/v1/mail/messages/{message['id']}/attachments",
        headers=headers(owner_auth),
        files={"file": ("offer.txt", b"snapshot offer", "text/plain")},
    )
    assert upload.status_code == 201
    pipeline = (await client.get("/api/v1/pipelines")).json()[0]
    deal = await client.post(
        "/api/v1/deals",
        headers=headers(owner_auth),
        json={
            "title": "Explicitly linked deal",
            "pipeline_id": pipeline["id"],
            "stage_id": pipeline["stages"][0]["id"],
        },
    )
    assert deal.status_code == 201, deal.text
    deal_id = deal.json()["id"]
    async with SessionLocal() as db:
        saved = await db.get(MailMessage, uuid.UUID(message["id"]))
        assert saved is not None
        saved.status = "sent"
        saved.sent_at = datetime.now(UTC)
        saved.metadata_json = {
            "reply_to_addresses": ["reply@example.com", "invalid", "bad\r\n@example.com", None]
        }
        await db.commit()
    loaded_mail = (await client.get(f"/api/v1/mail/messages/{message['id']}")).json()
    assert loaded_mail["reply_to_addresses"] == ["reply@example.com"]
    linked = await client.post(
        f"/api/v1/mail/messages/{message['id']}/link",
        headers=headers(owner_auth),
        json={"deal_id": deal_id},
    )
    assert linked.status_code == 201, linked.text
    repeated = await client.post(
        f"/api/v1/mail/messages/{message['id']}/link",
        headers=headers(owner_auth),
        json={"deal_id": deal_id},
    )
    assert repeated.json() == linked.json()
    peer = await peer_session(owner_auth, Role.manager)
    shared = await client.get(
        "/api/v1/activity",
        headers=peer,
        params={
            "entity_type": "deal",
            "entity_id": deal_id,
        },
    )
    note = next(item for item in shared.json()["items"] if item["id"] == linked.json()["note_id"])
    assert "Private mailbox content" in note["payload"]["body"]
    assert "mail" not in note["payload"] and len(note["attachments"]) == 1
    assert (
        await client.get(f"/api/v1/mail/messages/{message['id']}", headers=peer)
    ).status_code == 404
    note_file_id = note["attachments"][0]["id"]
    assert (
        await client.get(f"/api/v1/note-attachments/{note_file_id}/download", headers=peer)
    ).status_code == 200
    original_file_id = upload.json()["id"]
    async with SessionLocal() as db:
        mail_file = await db.get(MailAttachment, uuid.UUID(original_file_id))
        snapshot = await db.get(NoteAttachment, uuid.UUID(note_file_id))
        assert mail_file is not None and snapshot is not None
        assert snapshot.object_key != mail_file.object_key
        assert await db.scalar(sa.select(sa.func.count()).select_from(MailLink)) == 1
        assert (
            await db.scalar(
                sa.select(sa.func.count())
                .select_from(ActivityEvent)
                .where(
                    ActivityEvent.id == uuid.UUID(linked.json()["note_id"]),
                )
            )
            == 1
        )
    disconnected = await client.delete(
        f"/api/v1/mail/accounts/{created['id']}?expected_version={created['version']}",
        headers=headers(owner_auth),
    )
    assert disconnected.status_code == 204
    assert (await client.get("/api/v1/mail/accounts")).json() == {"items": []}
    assert (await client.get(f"/api/v1/mail/messages/{message['id']}")).status_code == 404
    assert (
        await client.post(
            f"/api/v1/mail/attachments/{original_file_id}/download", headers=headers(owner_auth)
        )
    ).status_code == 404
    assert (
        await client.get(f"/api/v1/note-attachments/{note_file_id}/download", headers=peer)
    ).status_code == 200
    assert len(mail_storage.objects) == 2
    reconnected = await account(client, owner_auth, password="replacement-test-password")
    assert reconnected["id"] == created["id"] and reconnected["has_credentials"]


@pytest.mark.asyncio
async def test_reply_and_move_cannot_use_another_owned_account_folder_or_message(
    client: httpx.AsyncClient, owner_auth: dict[str, object], mail_storage: MailS3
) -> None:
    del mail_storage
    first = await account(client, owner_auth)
    second = await account(client, owner_auth, email="second@example.com")
    original = await draft(client, owner_auth, first["id"])
    denied = await client.post(
        f"/api/v1/mail/accounts/{second['id']}/drafts",
        headers=headers(owner_auth),
        json={"reply_to_message_id": original["id"]},
    )
    assert denied.status_code == 404
    denied_forward = await client.post(
        f"/api/v1/mail/accounts/{second['id']}/drafts",
        headers=headers(owner_auth),
        json={"forward_message_id": original["id"]},
    )
    assert denied_forward.status_code == 404
    async with SessionLocal() as db:
        folder = await db.scalar(
            sa.select(MailFolder).where(MailFolder.account_id == uuid.UUID(second["id"]))
        )
        assert folder is not None
        foreign_folder_id = folder.id
    denied_move = await client.patch(
        f"/api/v1/mail/messages/{original['id']}",
        headers=headers(owner_auth),
        json={"expected_version": original["version"], "folder_id": str(foreign_folder_id)},
    )
    assert denied_move.status_code == 404


@pytest.mark.asyncio
async def test_pending_move_does_not_collide_with_target_uid_and_preserves_later_edits(
    client: httpx.AsyncClient, owner_auth: dict[str, object], mail_storage: MailS3
) -> None:
    del mail_storage
    created = await account(client, owner_auth)
    message = await draft(client, owner_auth, created["id"])
    async with SessionLocal() as db:
        workspace_id = uuid.UUID(str(owner_auth["workspace"]["id"]))  # type: ignore[index]
        source = MailFolder(
            workspace_id=workspace_id,
            account_id=uuid.UUID(created["id"]),
            name="INBOX",
            display_name="Inbox",
            kind="inbox",
            uidvalidity=12,
        )
        target = MailFolder(
            workspace_id=workspace_id,
            account_id=uuid.UUID(created["id"]),
            name="Archive",
            display_name="Archive",
            kind="archive",
            uidvalidity=12,
        )
        db.add_all([source, target])
        await db.flush()
        original = await db.get(MailMessage, uuid.UUID(message["id"]))
        assert original is not None
        original.folder_id = source.id
        original.direction, original.status = "inbound", "received"
        original.imap_uid, original.imap_uidvalidity = 7, 12
        unrelated = MailMessage(
            workspace_id=workspace_id,
            account_id=uuid.UUID(created["id"]),
            folder_id=target.id,
            direction="inbound",
            status="received",
            from_address="unrelated@example.com",
            subject="Unrelated archived letter",
            imap_uid=7,
            imap_uidvalidity=12,
        )
        db.add(unrelated)
        await db.commit()
        target_id, source_id, unrelated_id = str(target.id), str(source.id), unrelated.id
    moved = await client.patch(
        f"/api/v1/mail/messages/{message['id']}",
        headers=headers(owner_auth),
        json={"expected_version": 1, "folder_id": target_id},
    )
    assert moved.status_code == 200, moved.text
    starred = await client.patch(
        f"/api/v1/mail/messages/{message['id']}",
        headers=headers(owner_auth),
        json={"expected_version": moved.json()["version"], "is_starred": True},
    )
    assert starred.status_code == 200, starred.text
    async with SessionLocal() as db:
        saved = await db.get(MailMessage, uuid.UUID(message["id"]))
        archived = await db.get(MailMessage, unrelated_id)
        assert saved is not None and archived is not None
        assert saved.imap_uid is None and saved.imap_uidvalidity is None
        assert archived.imap_uid == 7 and archived.subject == "Unrelated archived letter"
        pending = saved.metadata_json["pending_action"]
        assert pending["source_folder_id"] == source_id and pending["uid"] == 7
        assert pending["uidvalidity"] == 12 and pending["source_name"] == "INBOX"
        assert pending["target_folder_id"] == target_id and pending["is_starred"] is True
        assert pending["version"] == starred.json()["version"]
        jobs = list(
            (
                await db.scalars(
                    sa.select(BackgroundJob).where(
                        BackgroundJob.job_type == "mail.action",
                    )
                )
            ).all()
        )
        assert len(jobs) == 2
    cancelled = await client.patch(
        f"/api/v1/mail/messages/{message['id']}",
        headers=headers(owner_auth),
        json={"expected_version": starred.json()["version"], "folder_id": source_id},
    )
    assert cancelled.status_code == 200, cancelled.text
    async with SessionLocal() as db:
        saved = await db.get(MailMessage, uuid.UUID(message["id"]))
        assert saved is not None
        pending = saved.metadata_json["pending_action"]
        assert pending["source_folder_id"] == pending["target_folder_id"] == source_id
        assert pending["uid"] == 7 and pending["is_starred"] is True


@pytest.mark.asyncio
async def test_owned_thread_filter_keeps_original_and_replies_in_the_same_chain(
    client: httpx.AsyncClient, owner_auth: dict[str, object], mail_storage: MailS3
) -> None:
    del mail_storage
    created = await account(client, owner_auth)
    original = await draft(client, owner_auth, created["id"])
    queued_response = await client.post(
        f"/api/v1/mail/messages/{original['id']}/send",
        headers=headers(owner_auth),
        json={"expected_version": original["version"]},
    )
    assert queued_response.status_code == 202, queued_response.text
    queued = queued_response.json()
    root_key = queued["internet_message_id"]
    assert queued["thread_key"] == root_key
    await draft(client, owner_auth, created["id"], subject="Separate conversation")
    other_account = await account(client, owner_auth, email="another@example.com")
    other_message = await draft(client, owner_auth, other_account["id"])
    async with SessionLocal() as db:
        original_row = await db.get(MailMessage, uuid.UUID(original["id"]))
        other_row = await db.get(MailMessage, uuid.UUID(other_message["id"]))
        assert original_row is not None and other_row is not None
        original_row.status = "sent"
        other_row.thread_key = root_key
        incoming = MailMessage(
            workspace_id=original_row.workspace_id,
            account_id=original_row.account_id,
            folder_id=original_row.folder_id,
            direction="inbound",
            status="received",
            from_address="customer@example.com",
            subject="Re: Private offer",
            internet_message_id="<reply@example.com>",
            in_reply_to=root_key,
            references=[root_key],
            thread_key=root_key,
            body_text="Customer reply",
        )
        db.add(incoming)
        await db.commit()
        incoming_id = str(incoming.id)
    reply = await draft(
        client,
        owner_auth,
        created["id"],
        reply_to_message_id=incoming_id,
        subject="Re: Private offer",
        body_text="Our response",
    )
    assert reply["thread_key"] == root_key
    queued_reply = await client.post(
        f"/api/v1/mail/messages/{reply['id']}/send",
        headers=headers(owner_auth),
        json={"expected_version": reply["version"]},
    )
    assert queued_reply.status_code == 202, queued_reply.text
    assert queued_reply.json()["thread_key"] == root_key
    chain = await client.get(
        f"/api/v1/mail/accounts/{created['id']}/messages",
        params={"thread_key": root_key},
    )
    assert chain.status_code == 200, chain.text
    assert {item["id"] for item in chain.json()["items"]} == {
        original["id"],
        incoming_id,
        reply["id"],
    }
    too_long = await client.get(
        f"/api/v1/mail/accounts/{created['id']}/messages",
        params={"thread_key": "x" * 999},
    )
    assert too_long.status_code == 422

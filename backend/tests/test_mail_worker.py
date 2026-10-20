from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from app.db import SessionLocal
from app.integrations.secrets import SecretCipher
from app.mail.models import MailAccount, MailFolder, MailMessage
from app.mail.transport import (
    FolderSnapshot,
    MailDeliveryUncertain,
    MailTransport,
    MailTransportError,
    RemoteFolder,
    RemoteMessage,
)
from app.mail.worker import (
    JOB_MAIL_ACTION,
    JOB_MAIL_SEND,
    JOB_MAIL_SYNC,
    MailRuntime,
    schedule_mail_jobs,
)
from app.models import BackgroundJob, Membership, Role, User, Workspace
from app.services.jobs import ClaimedJob

CIPHER = SecretCipher(b"m" * 32, key_id="test")


class FakeMailTransport(MailTransport):
    def __init__(self) -> None:
        super().__init__(allowed_hosts=("imap.example.com", "smtp.example.com"))
        self.sent: list[bytes] = []
        self.appended: list[bytes] = []
        self.actions: list[dict[str, Any]] = []
        self.send_error: Exception | None = None
        self.append_error: Exception | None = None
        self.snapshot = FolderSnapshot(1, 0, False, (), frozenset(), {})

    async def send(self, *args: Any, **kwargs: Any) -> None:
        self.sent.append(kwargs["raw"])
        if self.send_error:
            raise self.send_error

    async def append_sent(self, *args: Any, **kwargs: Any) -> None:
        self.appended.append(kwargs["raw"])
        if self.append_error:
            raise self.append_error

    async def folders(self, *args: Any, **kwargs: Any) -> tuple[RemoteFolder, ...]:
        return (RemoteFolder("INBOX", "INBOX", "inbox"), RemoteFolder("Sent", "Sent", "sent"))

    async def sync_folder(self, *args: Any, **kwargs: Any) -> FolderSnapshot:
        return self.snapshot

    async def action(self, *args: Any, **kwargs: Any) -> None:
        self.actions.append(kwargs)


async def seed(*, status: str = "queued") -> tuple[MailAccount, MailFolder, MailMessage]:
    async with SessionLocal() as db, db.begin():
        workspace = Workspace(name="Mail test", slug="mail-test")
        user = User(email="employee@example.com", full_name="Employee", password_hash="test-only")
        db.add_all([workspace, user])
        await db.flush()
        db.add(Membership(workspace_id=workspace.id, user_id=user.id, role=Role.employee))
        account = MailAccount(
            workspace_id=workspace.id,
            owner_id=user.id,
            email=user.email,
            display_name="Employee",
            username=user.email,
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
            credentials_key_id="test",
        )
        db.add(account)
        await db.flush()
        account.encrypted_credentials = CIPHER.encrypt(
            json.dumps({"username": user.email, "password": "test-only"}),
            associated_data=f"mail:{account.id}".encode(),
        )
        inbox = MailFolder(
            workspace_id=workspace.id,
            account_id=account.id,
            name="INBOX",
            display_name="INBOX",
            kind="inbox",
            uidvalidity=1,
            last_uid=1,
        )
        sent = MailFolder(
            workspace_id=workspace.id,
            account_id=account.id,
            name="Sent",
            display_name="Sent",
            kind="sent",
        )
        db.add_all([inbox, sent])
        await db.flush()
        message = MailMessage(
            workspace_id=workspace.id,
            account_id=account.id,
            folder_id=inbox.id,
            subject="Test message",
            from_address=user.email,
            to_addresses=["customer@example.com"],
            cc_addresses=[],
            bcc_addresses=["copy@example.com"],
            body_text="Message body",
            direction="outbound",
            status=status,
        )
        db.add(message)
        await db.flush()
        return account, inbox, message


def job(kind: str, account: MailAccount, **payload: Any) -> ClaimedJob:
    return ClaimedJob(
        uuid.uuid4(),
        kind,
        payload,
        1,
        5,
        "mail-test-worker",
        workspace_id=account.workspace_id,
    )


def runtime(transport: FakeMailTransport) -> MailRuntime:
    return MailRuntime(cipher=CIPHER, transport=transport, session_factory=SessionLocal)


async def test_recovered_sending_is_uncertain_without_second_smtp_attempt() -> None:
    account, _, message = await seed(status="sending")
    transport = FakeMailTransport()
    worker = runtime(transport)
    await worker.send(job(JOB_MAIL_SEND, account, message_id=str(message.id)))
    async with SessionLocal() as db:
        current = await db.get(MailMessage, message.id)
        assert current and current.status == "uncertain"
    assert transport.sent == []


async def test_uncertain_smtp_is_never_automatically_resent() -> None:
    account, _, message = await seed()
    transport = FakeMailTransport()
    transport.send_error = MailDeliveryUncertain("Uncertain delivery")
    worker = runtime(transport)
    work = job(JOB_MAIL_SEND, account, message_id=str(message.id))
    await worker.send(work)
    await worker.send(work)
    async with SessionLocal() as db:
        current = await db.get(MailMessage, message.id)
        assert current and current.status == "uncertain" and current.internet_message_id
    assert len(transport.sent) == 1


async def test_append_retry_keeps_sent_state_and_original_message_id() -> None:
    account, _, message = await seed()
    transport = FakeMailTransport()
    transport.append_error = MailTransportError("IMAP unavailable")
    worker = runtime(transport)
    work = job(JOB_MAIL_SEND, account, message_id=str(message.id))
    with pytest.raises(MailTransportError):
        await worker.send(work)
    async with SessionLocal() as db:
        current = await db.get(MailMessage, message.id)
        assert current and current.status == "sent"
        assert current.metadata_json["sent_append_pending"]
    transport.append_error = None
    await worker.send(work)
    assert len(transport.sent) == 1
    assert transport.appended == [transport.sent[0], transport.sent[0]]
    assert b"Bcc:" not in transport.sent[0]
    async with SessionLocal() as db:
        current = await db.get(MailMessage, message.id)
        assert current and not current.metadata_json["sent_append_pending"]


async def test_queued_send_rechecks_membership_and_workspace() -> None:
    account, _, message = await seed()
    transport = FakeMailTransport()
    worker = runtime(transport)
    async with SessionLocal() as db, db.begin():
        await db.execute(sa.delete(Membership).where(Membership.user_id == account.owner_id))
    await worker.send(job(JOB_MAIL_SEND, account, message_id=str(message.id)))
    async with SessionLocal() as db:
        current = await db.get(MailMessage, message.id)
        assert current and current.status == "failed"
    assert transport.sent == []


async def test_folder_sync_reconciles_external_flags_and_deletions() -> None:
    account, folder, first = await seed(status="received")
    async with SessionLocal() as db, db.begin():
        current = await db.get(MailMessage, first.id)
        assert current
        current.imap_uid = 1
        current.imap_uidvalidity = 1
        second = MailMessage(
            workspace_id=account.workspace_id,
            account_id=account.id,
            folder_id=folder.id,
            from_address="other@example.com",
            direction="inbound",
            status="received",
            imap_uid=2,
            imap_uidvalidity=1,
        )
        db.add(second)
        await db.flush()
        second_id = second.id
    transport = FakeMailTransport()
    transport.snapshot = FolderSnapshot(
        1,
        1,
        False,
        (),
        frozenset({1}),
        {1: frozenset({r"\Seen", r"\Flagged"})},
    )
    await runtime(transport).sync(
        job(
            JOB_MAIL_SYNC,
            account,
            account_id=str(account.id),
            folder_id=str(folder.id),
        )
    )
    async with SessionLocal() as db:
        current = await db.get(MailMessage, first.id)
        assert current and current.is_read and current.is_starred
        assert await db.get(MailMessage, second_id) is None
        updated_folder = await db.get(MailFolder, folder.id)
        assert (
            updated_folder and updated_folder.total_count == 1 and updated_folder.unread_count == 0
        )


async def test_uidvalidity_change_and_page_continuation_are_durable() -> None:
    account, folder, first = await seed(status="received")
    async with SessionLocal() as db, db.begin():
        current = await db.get(MailMessage, first.id)
        assert current
        current.imap_uid, current.imap_uidvalidity = 1, 1
    transport = FakeMailTransport()
    transport.snapshot = FolderSnapshot(
        2,
        1,
        True,
        (
            RemoteMessage(
                1, b"From: other@example.com\r\nSubject: New validity\r\n\r\nNew", frozenset()
            ),
        ),
        frozenset({1}),
        {},
    )
    await runtime(transport).sync(
        job(
            JOB_MAIL_SYNC,
            account,
            account_id=str(account.id),
            folder_id=str(folder.id),
        )
    )
    async with SessionLocal() as db:
        assert await db.get(MailMessage, first.id) is None
        cached = list((await db.scalars(sa.select(MailMessage))).all())
        assert len(cached) == 1 and cached[0].subject == "New validity"
        assert cached[0].imap_uidvalidity == 2
        continuation = await db.scalar(
            sa.select(BackgroundJob).where(BackgroundJob.job_type == JOB_MAIL_SYNC)
        )
        assert continuation and continuation.payload["folder_id"] == str(folder.id)


async def test_stale_action_job_cannot_reverse_newer_user_edit() -> None:
    account, folder, message = await seed(status="received")
    pending = dict(
        version=3,
        source_folder_id=str(folder.id),
        source_name=folder.name,
        uid=1,
        uidvalidity=1,
        target_folder_id=str(folder.id),
        target_name=folder.name,
        is_read=True,
        is_starred=True,
    )
    async with SessionLocal() as db, db.begin():
        current = await db.get(MailMessage, message.id)
        assert current
        current.version = 3
        current.metadata_json = {"pending_action": pending}
    transport = FakeMailTransport()
    worker = runtime(transport)
    await worker.action(job(JOB_MAIL_ACTION, account, message_id=str(message.id), version=2))
    assert transport.actions == []
    await worker.action(job(JOB_MAIL_ACTION, account, message_id=str(message.id), version=3))
    assert len(transport.actions) == 1
    async with SessionLocal() as db:
        current = await db.get(MailMessage, message.id)
        assert current and "pending_action" not in current.metadata_json


async def test_scheduler_recovers_queued_mail_and_never_uncertain_mail() -> None:
    account, _, message = await seed()
    now = datetime(2026, 10, 20, tzinfo=UTC)
    async with SessionLocal() as db, db.begin():
        counts = await schedule_mail_jobs(db, now)
        assert counts[JOB_MAIL_SEND] == 1 and counts[JOB_MAIL_SYNC] == 1
    async with SessionLocal() as db, db.begin():
        current = await db.get(MailMessage, message.id)
        assert current
        current.status = "uncertain"
        await db.execute(sa.delete(BackgroundJob))
    async with SessionLocal() as db, db.begin():
        counts = await schedule_mail_jobs(db, now)
        assert JOB_MAIL_SEND not in counts


@pytest.mark.parametrize("mode", ["discover", "folder", "failure"])
async def test_disconnect_during_sync_never_revives_account(mode: str) -> None:
    account, folder, _ = await seed(status="received")

    class DisconnectingTransport(FakeMailTransport):
        async def disconnect(self) -> None:
            async with SessionLocal() as db, db.begin():
                current = await db.get(MailAccount, account.id)
                assert current
                current.status = "disconnected"
                current.encrypted_credentials = None
                current.version += 1

        async def folders(self, *args: Any, **kwargs: Any) -> tuple[RemoteFolder, ...]:
            await self.disconnect()
            if mode == "failure":
                raise MailTransportError("Connection failed")
            return await super().folders(*args, **kwargs)

        async def sync_folder(self, *args: Any, **kwargs: Any) -> FolderSnapshot:
            await self.disconnect()
            return await super().sync_folder(*args, **kwargs)

    worker = runtime(DisconnectingTransport())
    work = job(JOB_MAIL_SYNC, account, account_id=str(account.id))
    if mode == "folder":
        work = job(JOB_MAIL_SYNC, account, account_id=str(account.id), folder_id=str(folder.id))
    if mode == "failure":
        with pytest.raises(MailTransportError):
            await worker.sync(work)
    else:
        await worker.sync(work)
    async with SessionLocal() as db:
        current = await db.get(MailAccount, account.id)
        assert (
            current and current.status == "disconnected" and current.encrypted_credentials is None
        )


async def test_owner_revoked_during_sync_cannot_persist_network_result() -> None:
    account, folder, _ = await seed(status="received")

    class RevokingTransport(FakeMailTransport):
        async def sync_folder(self, *args: Any, **kwargs: Any) -> FolderSnapshot:
            async with SessionLocal() as db, db.begin():
                await db.execute(
                    sa.delete(Membership).where(Membership.user_id == account.owner_id)
                )
            return FolderSnapshot(
                2,
                99,
                False,
                (RemoteMessage(99, b"Subject: Should not persist\r\n\r\nBody", frozenset()),),
                frozenset({99}),
                {},
            )

    await runtime(RevokingTransport()).sync(
        job(
            JOB_MAIL_SYNC,
            account,
            account_id=str(account.id),
            folder_id=str(folder.id),
        )
    )
    async with SessionLocal() as db:
        assert not await db.scalar(
            sa.select(MailMessage.id).where(MailMessage.subject == "Should not persist")
        )
        current_folder = await db.get(MailFolder, folder.id)
        assert current_folder and current_folder.uidvalidity == 1 and current_folder.last_uid == 1


async def test_stale_sync_cleans_unused_private_objects() -> None:
    from app.integrations.s3 import AttachmentStorage

    account, folder, _ = await seed(status="received")

    class MemoryS3:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}
            self.deleted: list[str] = []

        def put_object(self, **kwargs: Any) -> Any:
            self.objects[kwargs["Key"]] = kwargs["Body"]

        def delete_object(self, **kwargs: Any) -> Any:
            self.objects.pop(kwargs["Key"])
            self.deleted.append(kwargs["Key"])

        def get_object(self, **kwargs: Any) -> Any:
            return {"Body": self.objects[kwargs["Key"]]}

        def generate_presigned_url(self, *args: Any, **kwargs: Any) -> str:
            return "https://files.example.com/download"

    class StaleTransport(FakeMailTransport):
        async def sync_folder(self, *args: Any, **kwargs: Any) -> FolderSnapshot:
            async with SessionLocal() as db, db.begin():
                current = await db.get(MailAccount, account.id)
                assert current
                current.status = "disconnected"
                current.encrypted_credentials = None
                current.version += 1
            raw = (
                b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="test"\r\n\r\n'
                b"--test\r\nContent-Type: text/plain\r\n\r\nText\r\n"
                b"--test\r\nContent-Type: text/plain\r\n"
                b'Content-Disposition: attachment; filename="file.txt"\r\n\r\nFile content\r\n'
                b"--test--\r\n"
            )
            return FolderSnapshot(
                1, 2, False, (RemoteMessage(2, raw, frozenset()),), frozenset({2}), {}
            )

    objects = MemoryS3()
    worker = MailRuntime(
        cipher=CIPHER,
        transport=StaleTransport(),
        storage=AttachmentStorage(objects, bucket="mail-test"),
    )
    await worker.sync(
        job(JOB_MAIL_SYNC, account, account_id=str(account.id), folder_id=str(folder.id))
    )
    assert not objects.objects and len(objects.deleted) == 1


async def test_failed_remote_action_remains_durable_and_is_rediscovered() -> None:
    account, folder, message = await seed(status="received")

    class FailingActionTransport(FakeMailTransport):
        async def action(self, *args: Any, **kwargs: Any) -> None:
            raise MailTransportError("IMAP unavailable")

    async with SessionLocal() as db, db.begin():
        current = await db.get(MailMessage, message.id)
        assert current
        current.metadata_json = {
            "pending_action": dict(
                version=2,
                source_folder_id=str(folder.id),
                source_name=folder.name,
                uid=1,
                uidvalidity=1,
                target_folder_id=str(folder.id),
                target_name=folder.name,
                is_read=True,
                is_starred=False,
            )
        }
    work = job(JOB_MAIL_ACTION, account, message_id=str(message.id), version=2)
    with pytest.raises(MailTransportError):
        await runtime(FailingActionTransport()).action(work)
    async with SessionLocal() as db, db.begin():
        current = await db.get(MailMessage, message.id)
        assert current and current.last_error and current.metadata_json.get("pending_action")
        counts = await schedule_mail_jobs(db, datetime(2026, 10, 20, tzinfo=UTC))
        assert counts[JOB_MAIL_ACTION] == 1
        recovered = await db.scalar(
            sa.select(BackgroundJob).where(BackgroundJob.job_type == JOB_MAIL_ACTION)
        )
        assert recovered and recovered.payload["version"] == 2


async def test_reconnection_during_attachment_load_cannot_revive_old_send() -> None:
    account, _, message = await seed()

    class ReconnectedRuntime(MailRuntime):
        async def _outbound_bytes(self, message: MailMessage, account: MailAccount) -> bytes:
            raw = await super()._outbound_bytes(message, account)
            async with SessionLocal() as db, db.begin():
                current_account = await db.get(MailAccount, account.id)
                current_message = await db.get(MailMessage, message.id)
                assert current_account and current_message
                # Disconnect and reconnect preserve the account identity but
                # advance its generation and cancel queued/sending work.
                current_account.version += 2
                current_message.status = "failed"
            return raw

    transport = FakeMailTransport()
    await ReconnectedRuntime(cipher=CIPHER, transport=transport).send(
        job(JOB_MAIL_SEND, account, message_id=str(message.id)),
    )
    assert transport.sent == []
    async with SessionLocal() as db:
        current = await db.get(MailMessage, message.id)
        assert current and current.status == "failed"


async def test_cancelled_optimistic_move_restores_original_uid_after_flags_apply() -> None:
    account, folder, message = await seed(status="received")
    async with SessionLocal() as db, db.begin():
        current = await db.get(MailMessage, message.id)
        assert current
        current.imap_uid = None
        current.imap_uidvalidity = None
        current.metadata_json = {
            "pending_action": dict(
                version=3,
                source_folder_id=str(folder.id),
                source_name=folder.name,
                uid=7,
                uidvalidity=1,
                target_folder_id=str(folder.id),
                target_name=folder.name,
                is_read=True,
                is_starred=True,
            )
        }
    await runtime(FakeMailTransport()).action(
        job(JOB_MAIL_ACTION, account, message_id=str(message.id), version=3),
    )
    async with SessionLocal() as db:
        current = await db.get(MailMessage, message.id)
        assert current and current.imap_uid == 7 and current.imap_uidvalidity == 1
        assert not current.metadata_json.get("pending_action")


async def test_discovery_removes_remote_deleted_folders_and_keeps_local_drafts() -> None:
    account, inbox, message = await seed(status="received")
    async with SessionLocal() as db, db.begin():
        folder = MailFolder(
            workspace_id=account.workspace_id,
            account_id=account.id,
            name="Deleted on server",
            display_name="Deleted on server",
            kind="custom",
        )
        drafts = MailFolder(
            workspace_id=account.workspace_id,
            account_id=account.id,
            name="__drafts__",
            display_name="Drafts",
            kind="drafts",
        )
        db.add_all([folder, drafts])
        await db.flush()
        moved = await db.get(MailMessage, message.id)
        assert moved
        moved.folder_id = folder.id
        folder_id, draft_id = folder.id, drafts.id
    await runtime(FakeMailTransport()).sync(job(JOB_MAIL_SYNC, account, account_id=str(account.id)))
    async with SessionLocal() as db:
        assert await db.get(MailFolder, folder_id) is None
        assert await db.get(MailMessage, message.id) is None
        assert await db.get(MailFolder, draft_id) is not None
        assert await db.get(MailFolder, inbox.id) is not None


@pytest.mark.parametrize("has_message_id", [False, True])
async def test_uidvalidity_reset_cancels_pending_move_and_rebases_source(
    has_message_id: bool,
) -> None:
    account, source, message = await seed(status="received")
    stable_id = "<stable@example.com>" if has_message_id else None
    async with SessionLocal() as db, db.begin():
        destination = await db.scalar(
            sa.select(MailFolder).where(
                MailFolder.account_id == account.id,
                MailFolder.kind == "sent",
            )
        )
        current = await db.get(MailMessage, message.id)
        assert current and destination
        current.folder_id = destination.id
        current.imap_uid = None
        current.imap_uidvalidity = None
        current.internet_message_id = stable_id
        current.metadata_json = {
            "pending_action": dict(
                version=2,
                source_folder_id=str(source.id),
                source_name=source.name,
                uid=1,
                uidvalidity=1,
                target_folder_id=str(destination.id),
                target_name=destination.name,
                is_read=True,
                is_starred=False,
            )
        }
    transport = FakeMailTransport()
    header = b"Message-ID: <stable@example.com>\r\n" if has_message_id else b""
    transport.snapshot = FolderSnapshot(
        2,
        5,
        False,
        (
            RemoteMessage(
                5, header + b"From: other@example.com\r\nSubject: Current\r\n\r\nBody", frozenset()
            ),
        ),
        frozenset({5}),
        {},
    )
    await runtime(transport).sync(
        job(
            JOB_MAIL_SYNC,
            account,
            account_id=str(account.id),
            folder_id=str(source.id),
        )
    )
    async with SessionLocal() as db, db.begin():
        messages = list((await db.scalars(sa.select(MailMessage))).all())
        assert len(messages) == 1
        current = messages[0]
        assert (
            current.folder_id == source.id
            and current.imap_uid == 5
            and current.imap_uidvalidity == 2
        )
        assert not current.metadata_json.get("pending_action")
        if has_message_id:
            assert current.id == message.id and current.last_error
        else:
            assert current.id != message.id
        counts = await schedule_mail_jobs(db, datetime(2026, 10, 20, tzinfo=UTC))
        assert JOB_MAIL_ACTION not in counts


async def test_stale_remote_identity_cancels_action_and_queues_sync() -> None:
    from app.mail.transport import MailIdentityChanged

    account, source, message = await seed(status="received")
    async with SessionLocal() as db, db.begin():
        current = await db.get(MailMessage, message.id)
        assert current
        current.internet_message_id = "<stable@example.com>"
        current.metadata_json = {
            "pending_action": dict(
                version=2,
                source_folder_id=str(source.id),
                source_name=source.name,
                uid=1,
                uidvalidity=1,
                target_folder_id=str(source.id),
                target_name=source.name,
                is_read=True,
                is_starred=False,
            )
        }

    class ChangedIdentityTransport(FakeMailTransport):
        async def action(self, *args: Any, **kwargs: Any) -> None:
            self.actions.append(kwargs)
            raise MailIdentityChanged("Folder identity changed")

    transport = ChangedIdentityTransport()
    worker = runtime(transport)
    work = job(JOB_MAIL_ACTION, account, message_id=str(message.id), version=2)
    await worker.action(work)
    await worker.action(work)
    assert len(transport.actions) == 1
    async with SessionLocal() as db:
        current = await db.get(MailMessage, message.id)
        assert current and current.last_error and not current.metadata_json.get("pending_action")
        assert current.imap_uid is None and current.metadata_json.get("moved_from")
        sync_job = await db.scalar(
            sa.select(BackgroundJob).where(BackgroundJob.job_type == JOB_MAIL_SYNC)
        )
        assert sync_job and sync_job.payload["folder_id"] == str(source.id)

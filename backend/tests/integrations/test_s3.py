from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.integrations.s3 import (
    AttachmentStorage,
    AttachmentValidationError,
    validate_attachment,
)


class FakeS3:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []
        self.deletes: list[dict[str, Any]] = []
        self.objects: dict[str, bytes] = {}

    def put_object(self, **kwargs: Any) -> None:
        self.puts.append(kwargs)
        self.objects[str(kwargs["Key"])] = bytes(kwargs["Body"])

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        content = self.objects[str(kwargs["Key"])]
        return {"Body": content, "ContentLength": len(content)}

    def delete_object(self, **kwargs: Any) -> None:
        self.deletes.append(kwargs)

    def generate_presigned_url(
        self, client_method: str, *, Params: dict[str, Any], ExpiresIn: int
    ) -> str:
        return f"https://s3.test/{client_method}/{Params['Key']}?expires={ExpiresIn}"


def test_attachment_validation_rejects_executables_archives_and_fake_pdf() -> None:
    with pytest.raises(AttachmentValidationError):
        validate_attachment("payload.exe", "application/octet-stream", b"MZbad")
    with pytest.raises(AttachmentValidationError):
        validate_attachment("files.zip", "application/zip", b"PK\x03\x04")
    with pytest.raises(AttachmentValidationError, match="PDF"):
        validate_attachment("report.pdf", "application/pdf", b"not a pdf")


@pytest.mark.asyncio
async def test_private_storage_scopes_signed_url_to_workspace() -> None:
    fake = FakeS3()
    workspace_id = uuid.uuid4()
    storage = AttachmentStorage(fake, bucket="pulse-private")
    stored = await storage.store(
        workspace_id=workspace_id,
        filename="report.pdf",
        content_type="application/pdf",
        content=b"%PDF-1.7\ncontent",
    )
    assert stored.object_key.startswith(f"attachments/{workspace_id}/")
    assert "ACL" not in fake.puts[0]
    content = await storage.read_attachment(
        workspace_id=workspace_id,
        object_key=stored.object_key,
        filename=stored.attachment.filename,
        content_type=stored.attachment.content_type,
        expected_size_bytes=stored.attachment.size_bytes,
        expected_sha256=stored.attachment.sha256,
    )
    assert content == b"%PDF-1.7\ncontent"
    url = await storage.signed_download_url(
        workspace_id=workspace_id,
        object_key=stored.object_key,
        filename="report.pdf",
    )
    assert "expires=300" in url
    with pytest.raises(PermissionError):
        await storage.signed_download_url(
            workspace_id=uuid.uuid4(),
            object_key=stored.object_key,
            filename="report.pdf",
        )
    with pytest.raises(PermissionError):
        await storage.read_attachment(
            workspace_id=uuid.uuid4(),
            object_key=stored.object_key,
            filename=stored.attachment.filename,
            content_type=stored.attachment.content_type,
            expected_size_bytes=stored.attachment.size_bytes,
            expected_sha256=stored.attachment.sha256,
        )
    with pytest.raises(AttachmentValidationError, match="metadata"):
        await storage.read_attachment(
            workspace_id=workspace_id,
            object_key=stored.object_key,
            filename=stored.attachment.filename,
            content_type=stored.attachment.content_type,
            expected_size_bytes=stored.attachment.size_bytes,
            expected_sha256="0" * 64,
        )


@pytest.mark.asyncio
async def test_import_report_uses_deterministic_private_key_and_scoped_url() -> None:
    fake = FakeS3()
    workspace_id = uuid.uuid4()
    import_job_id = uuid.uuid4()
    storage = AttachmentStorage(fake, bucket="pulse-private")
    content = b'{"schema_version":1}'

    first_key = await storage.store_import_report(
        workspace_id=workspace_id,
        import_job_id=import_job_id,
        content=content,
    )
    second_key = await storage.store_import_report(
        workspace_id=workspace_id,
        import_job_id=import_job_id,
        content=content,
    )

    expected = f"imports/{workspace_id}/{import_job_id}/report.json"
    assert first_key == second_key == expected
    assert [item["Key"] for item in fake.puts] == [expected, expected]
    assert fake.puts[0]["Body"] == content
    assert fake.puts[0]["ContentType"] == "application/json; charset=utf-8"
    assert "ACL" not in fake.puts[0]

    url = await storage.signed_import_report_url(
        workspace_id=workspace_id,
        object_key=expected,
    )
    assert expected in url
    assert "expires=300" in url
    with pytest.raises(PermissionError):
        await storage.signed_import_report_url(
            workspace_id=uuid.uuid4(),
            object_key=expected,
        )

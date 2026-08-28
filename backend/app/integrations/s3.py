"""Private S3 attachment storage with strict validation and scoped links."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any, Protocol
from urllib.parse import quote

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_IMPORT_REPORT_BYTES = 1024 * 1024

ALLOWED_TYPES_BY_EXTENSION: dict[str, frozenset[str]] = {
    ".jpg": frozenset({"image/jpeg"}),
    ".jpeg": frozenset({"image/jpeg"}),
    ".png": frozenset({"image/png"}),
    ".gif": frozenset({"image/gif"}),
    ".webp": frozenset({"image/webp"}),
    ".pdf": frozenset({"application/pdf"}),
    ".doc": frozenset({"application/msword", "application/octet-stream"}),
    ".docx": frozenset({"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}),
    ".xls": frozenset({"application/vnd.ms-excel", "application/octet-stream"}),
    ".xlsx": frozenset({"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}),
    ".ppt": frozenset({"application/vnd.ms-powerpoint", "application/octet-stream"}),
    ".pptx": frozenset(
        {"application/vnd.openxmlformats-officedocument.presentationml.presentation"}
    ),
    ".odt": frozenset({"application/vnd.oasis.opendocument.text"}),
    ".ods": frozenset({"application/vnd.oasis.opendocument.spreadsheet"}),
    ".odp": frozenset({"application/vnd.oasis.opendocument.presentation"}),
    ".rtf": frozenset({"application/rtf", "text/rtf"}),
    ".txt": frozenset({"text/plain"}),
    ".csv": frozenset({"text/csv", "text/plain"}),
}

BLOCKED_EXTENSIONS = frozenset(
    {
        ".7z",
        ".apk",
        ".app",
        ".bat",
        ".bin",
        ".bz2",
        ".cmd",
        ".com",
        ".deb",
        ".dmg",
        ".exe",
        ".gz",
        ".iso",
        ".jar",
        ".js",
        ".msi",
        ".php",
        ".ps1",
        ".rar",
        ".rpm",
        ".sh",
        ".tar",
        ".vbs",
        ".zip",
    }
)


class AttachmentValidationError(ValueError):
    pass


class S3Client(Protocol):
    def put_object(self, **kwargs: Any) -> Any: ...

    def get_object(self, **kwargs: Any) -> Any: ...

    def delete_object(self, **kwargs: Any) -> Any: ...

    def generate_presigned_url(
        self, client_method: str, *, Params: dict[str, Any], ExpiresIn: int
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class ValidatedAttachment:
    filename: str
    content_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class StoredObject:
    object_key: str
    attachment: ValidatedAttachment


def sanitize_filename(filename: str) -> str:
    basename = PurePath(filename.replace("\\", "/")).name.strip().replace("\x00", "")
    basename = re.sub(r"[\r\n\t]", " ", basename)
    basename = re.sub(r"\s+", " ", basename)
    basename = re.sub(r"[^\w.()\- ]", "_", basename, flags=re.UNICODE)
    basename = basename.strip(" .")
    if not basename:
        raise AttachmentValidationError("filename must not be empty")
    return basename[:240]


def validate_attachment(filename: str, content_type: str, content: bytes) -> ValidatedAttachment:
    safe_name = sanitize_filename(filename)
    extension = PurePath(safe_name).suffix.casefold()
    normalized_type = content_type.partition(";")[0].strip().casefold()

    if not content:
        raise AttachmentValidationError("attachment must not be empty")
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise AttachmentValidationError("attachment exceeds the 20 MB limit")
    if extension in BLOCKED_EXTENSIONS:
        raise AttachmentValidationError("archives and executable files are not allowed")
    allowed_types = ALLOWED_TYPES_BY_EXTENSION.get(extension)
    if allowed_types is None or normalized_type not in allowed_types:
        raise AttachmentValidationError("file extension and content type are not allowed")
    if content.startswith(b"MZ"):
        raise AttachmentValidationError("executable file content is not allowed")
    if extension == ".pdf" and not content.startswith(b"%PDF-"):
        raise AttachmentValidationError("invalid PDF signature")
    if extension in {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"} and not content.startswith(
        b"PK\x03\x04"
    ):
        raise AttachmentValidationError("invalid office document signature")

    return ValidatedAttachment(
        filename=safe_name,
        content_type=normalized_type,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


class AttachmentStorage:
    def __init__(self, client: S3Client, *, bucket: str) -> None:
        if not bucket.strip():
            raise ValueError("S3 bucket must not be empty")
        self._client = client
        self._bucket = bucket

    async def store(
        self,
        *,
        workspace_id: uuid.UUID,
        filename: str,
        content_type: str,
        content: bytes,
        now: datetime | None = None,
    ) -> StoredObject:
        validated = validate_attachment(filename, content_type, content)
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        object_key = (
            f"attachments/{workspace_id}/{timestamp:%Y/%m}/{uuid.uuid4()}/{validated.filename}"
        )
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=object_key,
            Body=content,
            ContentType=validated.content_type,
            ContentLength=validated.size_bytes,
            Metadata={"sha256": validated.sha256},
            # The bucket is private; no ACL is provided intentionally.
        )
        return StoredObject(object_key=object_key, attachment=validated)

    async def delete(self, *, workspace_id: uuid.UUID, object_key: str) -> None:
        self._ensure_workspace_key(workspace_id, object_key)
        await asyncio.to_thread(
            self._client.delete_object,
            Bucket=self._bucket,
            Key=object_key,
        )

    async def read_attachment(
        self,
        *,
        workspace_id: uuid.UUID,
        object_key: str,
        filename: str,
        content_type: str,
        expected_size_bytes: int,
        expected_sha256: str,
    ) -> bytes:
        """Read and revalidate one workspace-owned private attachment."""

        self._ensure_workspace_key(workspace_id, object_key)
        try:
            content = await asyncio.to_thread(self._read_object, object_key)
        except Exception:
            raise AttachmentValidationError("stored attachment could not be read") from None
        validated = validate_attachment(filename, content_type, content)
        if validated.size_bytes != expected_size_bytes or not hmac.compare_digest(
            validated.sha256, expected_sha256
        ):
            raise AttachmentValidationError("stored attachment metadata does not match")
        return content

    def _read_object(self, object_key: str) -> bytes:
        response = self._client.get_object(Bucket=self._bucket, Key=object_key)
        if not isinstance(response, dict):
            raise TypeError("invalid S3 response")
        content_length = response.get("ContentLength")
        if isinstance(content_length, int) and content_length > MAX_ATTACHMENT_BYTES:
            raise AttachmentValidationError("attachment exceeds the 20 MB limit")
        body = response.get("Body")
        if isinstance(body, bytes):
            content = body
        elif isinstance(body, bytearray):
            content = bytes(body)
        elif callable(reader := getattr(body, "read", None)):
            try:
                content = reader(MAX_ATTACHMENT_BYTES + 1)
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
        else:
            raise TypeError("invalid S3 response body")
        if not isinstance(content, bytes):
            raise TypeError("invalid S3 response body")
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise AttachmentValidationError("attachment exceeds the 20 MB limit")
        return content

    async def signed_download_url(
        self,
        *,
        workspace_id: uuid.UUID,
        object_key: str,
        filename: str,
        expires_seconds: int = 300,
    ) -> str:
        if not 30 <= expires_seconds <= 900:
            raise ValueError("signed URL lifetime must be between 30 and 900 seconds")
        self._ensure_workspace_key(workspace_id, object_key)
        safe_name = sanitize_filename(filename)
        disposition = f"attachment; filename*=UTF-8''{quote(safe_name)}"
        return await asyncio.to_thread(
            self._client.generate_presigned_url,
            "get_object",
            Params={
                "Bucket": self._bucket,
                "Key": object_key,
                "ResponseContentDisposition": disposition,
            },
            ExpiresIn=expires_seconds,
        )

    async def store_import_report(
        self,
        *,
        workspace_id: uuid.UUID,
        import_job_id: uuid.UUID,
        content: bytes,
    ) -> str:
        """Put a deterministic private JSON report; retries safely overwrite it."""

        if not content:
            raise ValueError("import report must not be empty")
        if len(content) > MAX_IMPORT_REPORT_BYTES:
            raise ValueError("import report exceeds the 1 MB limit")
        object_key = self.import_report_key(workspace_id, import_job_id)
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=object_key,
            Body=content,
            ContentType="application/json; charset=utf-8",
            ContentLength=len(content),
            Metadata={"sha256": hashlib.sha256(content).hexdigest()},
        )
        return object_key

    async def signed_import_report_url(
        self,
        *,
        workspace_id: uuid.UUID,
        object_key: str,
        expires_seconds: int = 300,
    ) -> str:
        if not 30 <= expires_seconds <= 900:
            raise ValueError("signed URL lifetime must be between 30 and 900 seconds")
        self._ensure_import_key(workspace_id, object_key)
        return await asyncio.to_thread(
            self._client.generate_presigned_url,
            "get_object",
            Params={
                "Bucket": self._bucket,
                "Key": object_key,
                "ResponseContentDisposition": "attachment; filename=report.json",
                "ResponseContentType": "application/json",
            },
            ExpiresIn=expires_seconds,
        )

    @staticmethod
    def import_report_key(workspace_id: uuid.UUID, import_job_id: uuid.UUID) -> str:
        return f"imports/{workspace_id}/{import_job_id}/report.json"

    @staticmethod
    def _ensure_workspace_key(workspace_id: uuid.UUID, object_key: str) -> None:
        expected_prefix = f"attachments/{workspace_id}/"
        if not object_key.startswith(expected_prefix):
            raise PermissionError("attachment does not belong to this workspace")

    @staticmethod
    def _ensure_import_key(workspace_id: uuid.UUID, object_key: str) -> None:
        expected_prefix = f"imports/{workspace_id}/"
        if not object_key.startswith(expected_prefix):
            raise PermissionError("import report does not belong to this workspace")

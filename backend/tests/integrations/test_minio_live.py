from __future__ import annotations

import asyncio
import os
import uuid

import boto3
import pytest

from app.integrations.s3 import AttachmentStorage


@pytest.mark.asyncio
async def test_attachment_storage_against_ci_minio() -> None:
    endpoint = os.getenv("PULSE_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("live MinIO is only enabled in CI integration tests")
    access_key = os.environ["PULSE_TEST_S3_ACCESS_KEY_ID"]
    secret_key = os.environ["PULSE_TEST_S3_SECRET_ACCESS_KEY"]
    bucket = os.getenv("PULSE_TEST_S3_BUCKET", "pulse-ci")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )
    await asyncio.to_thread(client.create_bucket, Bucket=bucket)
    storage = AttachmentStorage(client, bucket=bucket)
    workspace_id = uuid.uuid4()

    stored = await storage.store(
        workspace_id=workspace_id,
        filename="ci-check.pdf",
        content_type="application/pdf",
        content=b"%PDF-1.7\nPulse CRM CI",
    )
    metadata = await asyncio.to_thread(client.head_object, Bucket=bucket, Key=stored.object_key)
    assert metadata["Metadata"]["sha256"] == stored.attachment.sha256
    signed_url = await storage.signed_download_url(
        workspace_id=workspace_id,
        object_key=stored.object_key,
        filename=stored.attachment.filename,
    )
    assert stored.object_key in signed_url
    await storage.delete(workspace_id=workspace_id, object_key=stored.object_key)


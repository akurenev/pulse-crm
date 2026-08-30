from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import sqlalchemy as sa
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.health import router as health_router
from app.api.router import api_router
from app.config import get_settings
from app.db import SessionLocal, close_database
from app.integrations.amocrm_live import (
    AMO_IMPORT_JOB_TYPE,
    AMO_IMPORT_REPORT_JOB_TYPE,
    make_amo_import_handler,
    make_amo_import_report_handler,
)
from app.integrations.channels.base import ChannelAdapter
from app.integrations.models import ChannelConnection, ChannelKind, ConnectionStatus
from app.integrations.public_api import router as public_integrations_router
from app.integrations.runtime import IntegrationRuntime
from app.integrations.s3 import AttachmentStorage
from app.integrations.secrets import SecretCipher
from app.integrations.transports import ChannelAdapterFactory
from app.integrations.web_push import WebPushSender


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "request_id": getattr(record, "request_id", None),
                "method": getattr(record, "method", None),
                "path": getattr(record, "path", None),
                "status_code": getattr(record, "status_code", None),
                "duration_ms": getattr(record, "duration_ms", None),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("pulse.requests")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    encoded_key = settings.integration_encryption_key
    cipher = (
        SecretCipher.from_base64(
            encoded_key,
            key_id=settings.integration_encryption_key_id,
        )
        if encoded_key
        else SecretCipher(
            hashlib.sha256(settings.secret_key.encode("utf-8")).digest(),
            key_id="development-derived",
        )
    )
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(15.0),
        follow_redirects=False,
    )
    channel_factory = ChannelAdapterFactory(cipher, http_client=http_client)

    async def notification_adapter_factory(workspace_id: uuid.UUID, channel: str) -> ChannelAdapter:
        try:
            kind = ChannelKind(channel)
        except ValueError as exc:
            raise RuntimeError(f"unsupported notification channel {channel!r}") from exc
        async with SessionLocal() as db:
            connection = await db.scalar(
                sa.select(ChannelConnection)
                .where(
                    ChannelConnection.workspace_id == workspace_id,
                    ChannelConnection.kind == kind,
                    ChannelConnection.status == ConnectionStatus.active,
                )
                .order_by(ChannelConnection.created_at)
                .limit(1)
            )
        if connection is None:
            raise RuntimeError(f"no active {channel} channel connection")
        return channel_factory.build(connection)

    runtime: IntegrationRuntime | None = None
    attachment_storage: AttachmentStorage | None = None
    if settings.s3_bucket and settings.s3_access_key_id and settings.s3_secret_access_key:
        import boto3  # type: ignore[import-untyped]

        s3_client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
        )
        attachment_storage = AttachmentStorage(s3_client, bucket=settings.s3_bucket)

    app.state.integration_secret_cipher = cipher
    app.state.channel_adapter_factory = channel_factory
    app.state.amocrm_http_client = http_client
    app.state.attachment_storage = attachment_storage
    if settings.job_runner_enabled:
        web_push_sender = (
            WebPushSender(
                session_factory=SessionLocal,
                cipher=cipher,
                vapid_private_key=settings.web_push_vapid_private_key or "",
                vapid_subject=settings.web_push_vapid_subject or "",
            )
            if settings.web_push_enabled
            else None
        )
        runtime = IntegrationRuntime(
            adapter_factory=channel_factory.build,
            notification_adapter_factory=notification_adapter_factory,
            imap_poller_factory=channel_factory.build_imap_poller,
            attachment_storage=attachment_storage,
            web_push_sender=web_push_sender,
            extra_handlers={
                AMO_IMPORT_JOB_TYPE: make_amo_import_handler(
                    cipher=cipher,
                    http_client=http_client,
                ),
                AMO_IMPORT_REPORT_JOB_TYPE: make_amo_import_report_handler(
                    storage=attachment_storage,
                ),
            },
            concurrency=4,
            scheduler_interval_seconds=settings.job_runner_poll_seconds,
        )
        await runtime.start()
    app.state.integration_runtime = runtime
    app.state.job_supervisor = runtime.supervisor if runtime else None
    try:
        yield
    finally:
        if runtime is not None:
            await runtime.stop()
        await channel_factory.aclose()
        await http_client.aclose()
        await close_database()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging()
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("x-request-id", "")[:128] or str(uuid.uuid4())
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logging.getLogger("pulse.requests").exception(
                "unhandled request error",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                },
            )
            raise
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        if request.url.path.startswith("/api/"):
            # Authenticated CRM responses can contain personal data.  They
            # must not be retained by browsers or intermediary caches.
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        logging.getLogger("pulse.requests").info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response

    app.include_router(health_router)
    app.include_router(api_router)
    app.include_router(public_integrations_router)

    static_dir = settings.static_dir
    assets_dir = static_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> Response:
        if full_path.startswith(("api/", "hooks/", "forms/", "health/")):
            raise HTTPException(status_code=404, detail="not found")
        root = static_dir.resolve()
        requested = (static_dir / full_path).resolve()
        if requested.is_file() and (requested == root or root in requested.parents):
            return FileResponse(requested)
        index = static_dir / "index.html"
        if index.is_file():
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="frontend is not built")

    return app


app = create_app()

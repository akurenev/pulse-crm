from __future__ import annotations

import asyncio

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Request, status

from app.config import get_settings
from app.db import SessionLocal
from app.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health/live", response_model=HealthResponse, include_in_schema=False)
async def liveness() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/health/ready", response_model=HealthResponse, include_in_schema=False)
async def readiness(request: Request) -> HealthResponse:
    settings = get_settings()
    database_status = "ok"
    try:
        async with asyncio.timeout(3):
            async with SessionLocal() as db:
                await db.execute(sa.text("SELECT 1"))
    except Exception as exc:
        database_status = "unavailable"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"database": database_status, "job_runner": "unknown"},
        ) from exc

    runner_status = "disabled"
    if settings.job_runner_enabled:
        runtime = getattr(request.app.state, "integration_runtime", None)
        runner = getattr(request.app.state, "job_supervisor", None)
        healthy = bool(
            (
                runtime
                and runtime.is_healthy(
                    max_age_seconds=settings.job_runner_heartbeat_timeout_seconds
                )
            )
            or (
                runtime is None
                and runner
                and runner.is_healthy(max_age_seconds=settings.job_runner_heartbeat_timeout_seconds)
            )
        )
        runner_status = "ok" if healthy else "stale"
        if not healthy:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"database": database_status, "job_runner": runner_status},
            )
    return HealthResponse(status="ok", database=database_status, job_runner=runner_status)

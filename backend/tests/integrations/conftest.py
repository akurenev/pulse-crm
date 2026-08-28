from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import Contact, Deal, Membership, Pipeline, Role, Source, Stage, User, Workspace


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def integration_domain(db: AsyncSession) -> dict[str, Any]:
    workspace = Workspace(name="Integrations", slug="integrations")
    user = User(
        email="integrations@example.com",
        full_name="Integration Owner",
        password_hash="not-used-in-service-tests",
    )
    db.add_all([workspace, user])
    await db.flush()
    membership = Membership(workspace_id=workspace.id, user_id=user.id, role=Role.owner)
    pipeline = Pipeline(workspace_id=workspace.id, name="Продажи")
    source = Source(workspace_id=workspace.id, key="telegram", name="Telegram")
    db.add_all([membership, pipeline, source])
    await db.flush()
    stage = Stage(
        workspace_id=workspace.id,
        pipeline_id=pipeline.id,
        name="Новый лид",
        position=0,
    )
    contact = Contact(workspace_id=workspace.id, first_name="Анна")
    db.add_all([stage, contact])
    await db.flush()
    deal = Deal(
        workspace_id=workspace.id,
        pipeline_id=pipeline.id,
        stage_id=stage.id,
        assignee_id=user.id,
        source_id=source.id,
        title="Тестовая сделка",
    )
    db.add(deal)
    await db.flush()
    return {
        "workspace": workspace,
        "user": user,
        "pipeline": pipeline,
        "stage": stage,
        "source": source,
        "contact": contact,
        "deal": deal,
    }

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa

import app.api.health as health_api
from app.db import SessionLocal
from app.integrations.models import (
    NotificationAudience,
    NotificationDelivery,
    PurchaseSchedule,
)
from app.main import app
from app.models import (
    ActivityEvent,
    BackgroundJob,
    Deal,
    DeliveryStatus,
    JobStatus,
    Membership,
    Pipeline,
    Role,
    Stage,
    StageType,
    Task,
    TaskStatus,
    User,
    Workspace,
)


def _workspace_id(auth: dict[str, object]) -> uuid.UUID:
    workspace = auth["workspace"]
    assert isinstance(workspace, dict)
    return uuid.UUID(str(workspace["id"]))


@pytest.mark.asyncio
async def test_dashboard_metrics_are_scoped_to_authenticated_workspace(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
) -> None:
    now = datetime.now(UTC)
    workspace_id = _workspace_id(owner_auth)

    async with SessionLocal() as db:
        owner_id = await db.scalar(
            sa.select(Membership.user_id).where(Membership.workspace_id == workspace_id)
        )
        assert owner_id is not None

        other_workspace = Workspace(name="Other Workspace", slug="other-workspace")
        other_user = User(
            email="other-owner@example.com",
            full_name="Other Owner",
            password_hash="not-used",
        )
        db.add_all([other_workspace, other_user])
        await db.flush()
        own_pipeline = Pipeline(workspace_id=workspace_id, name="Primary", position=0)
        other_pipeline = Pipeline(
            workspace_id=other_workspace.id,
            name="Other pipeline",
            position=0,
        )
        db.add_all([own_pipeline, other_pipeline])
        await db.flush()
        db.add(
            Membership(
                workspace_id=other_workspace.id,
                user_id=other_user.id,
                role=Role.owner,
            )
        )

        own_open = Stage(
            workspace_id=workspace_id,
            pipeline_id=own_pipeline.id,
            name="Open",
            position=0,
            stage_type=StageType.open,
        )
        own_won = Stage(
            workspace_id=workspace_id,
            pipeline_id=own_pipeline.id,
            name="Won",
            position=1,
            stage_type=StageType.won,
        )
        other_open = Stage(
            workspace_id=other_workspace.id,
            pipeline_id=other_pipeline.id,
            name="Open",
            position=0,
            stage_type=StageType.open,
        )
        db.add_all([own_open, own_won, other_open])
        await db.flush()

        own_inactive_deal = Deal(
            workspace_id=workspace_id,
            pipeline_id=own_pipeline.id,
            stage_id=own_open.id,
            assignee_id=owner_id,
            title="Own inactive",
            last_activity_at=now - timedelta(days=8),
        )
        own_won_deal = Deal(
            workspace_id=workspace_id,
            pipeline_id=own_pipeline.id,
            stage_id=own_won.id,
            assignee_id=owner_id,
            title="Own won",
            last_activity_at=now,
        )
        other_deal = Deal(
            workspace_id=other_workspace.id,
            pipeline_id=other_pipeline.id,
            stage_id=other_open.id,
            assignee_id=other_user.id,
            title="Other inactive",
            last_activity_at=now - timedelta(days=30),
        )
        db.add_all([own_inactive_deal, own_won_deal, other_deal])
        await db.flush()

        db.add_all(
            [
                ActivityEvent(
                    workspace_id=workspace_id,
                    event_type="lead.created",
                    entity_type="deal",
                    entity_id=own_inactive_deal.id,
                    payload={},
                    occurred_at=now,
                ),
                ActivityEvent(
                    workspace_id=other_workspace.id,
                    event_type="lead.created",
                    entity_type="deal",
                    entity_id=other_deal.id,
                    payload={},
                    occurred_at=now,
                ),
                Task(
                    workspace_id=workspace_id,
                    title="Own overdue",
                    status=TaskStatus.open,
                    due_at=now - timedelta(hours=1),
                    assignee_id=owner_id,
                    deal_id=own_inactive_deal.id,
                ),
                Task(
                    workspace_id=other_workspace.id,
                    title="Other overdue",
                    status=TaskStatus.open,
                    due_at=now - timedelta(days=2),
                    assignee_id=other_user.id,
                    deal_id=other_deal.id,
                ),
                PurchaseSchedule(
                    workspace_id=workspace_id,
                    deal_id=own_inactive_deal.id,
                    assignee_id=owner_id,
                    scheduled_for=now + timedelta(days=10),
                    remind_at=now + timedelta(days=7),
                ),
                PurchaseSchedule(
                    workspace_id=other_workspace.id,
                    deal_id=other_deal.id,
                    assignee_id=other_user.id,
                    scheduled_for=now + timedelta(days=5),
                    remind_at=now + timedelta(days=2),
                ),
            ]
        )
        await db.commit()

    response = await client.get("/api/v1/dashboard")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["new_leads_24h"] == 1
    assert body["overdue_tasks"] == 1
    assert body["inactive_deals"] == 1
    assert body["upcoming_purchases_30d"] == 1
    primary = next(
        pipeline
        for pipeline in body["pipelines"]
        if pipeline["pipeline_id"] == str(own_pipeline.id)
    )
    assert primary == {
        "pipeline_id": str(own_pipeline.id),
        "pipeline_name": "Primary",
        "total_deals": 2,
        "won_deals": 1,
        "conversion_percent": 50.0,
    }
    assert all(pipeline["pipeline_id"] != str(other_pipeline.id) for pipeline in body["pipelines"])


@pytest.mark.asyncio
async def test_manager_cannot_view_admin_jobs(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
) -> None:
    invitation = await client.post(
        "/api/v1/invitations",
        headers={"X-CSRF-Token": str(owner_auth["csrf_token"])},
        json={"email": "operations-manager@example.com", "role": "manager"},
    )
    assert invitation.status_code == 201, invitation.text
    accepted = await client.post(
        "/api/v1/auth/accept-invitation",
        json={
            "token": invitation.json()["token"],
            "full_name": "Operations Manager",
            "password": "another secure manager password",
        },
    )
    assert accepted.status_code == 201, accepted.text

    response = await client.get("/api/v1/admin/jobs")

    assert response.status_code == 403
    assert response.json()["detail"] == "insufficient role"


@pytest.mark.asyncio
async def test_user_sees_only_personal_in_app_notifications(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
) -> None:
    now = datetime.now(UTC)
    workspace_id = _workspace_id(owner_auth)
    async with SessionLocal() as db:
        owner_id = await db.scalar(
            sa.select(Membership.user_id).where(Membership.workspace_id == workspace_id)
        )
        assert owner_id is not None
        db.add_all(
            [
                NotificationDelivery(
                    workspace_id=workspace_id,
                    audience=NotificationAudience.employee,
                    channel="in_app",
                    recipient_id=owner_id,
                    recipient_address=str(owner_id),
                    subject="Own notification",
                    body="Visible to the owner",
                    status=DeliveryStatus.delivered,
                    dedupe_key="own-in-app",
                    scheduled_at=now,
                    delivered_at=now,
                ),
                NotificationDelivery(
                    workspace_id=workspace_id,
                    audience=NotificationAudience.employee,
                    channel="in_app",
                    recipient_id=uuid.uuid4(),
                    recipient_address="someone-else",
                    subject="Hidden notification",
                    body="Must stay hidden",
                    status=DeliveryStatus.delivered,
                    dedupe_key="hidden-in-app",
                    scheduled_at=now,
                    delivered_at=now,
                ),
            ]
        )
        await db.commit()

    response = await client.get("/api/v1/notifications")

    assert response.status_code == 200, response.text
    assert [item["subject"] for item in response.json()] == ["Own notification"]


@pytest.mark.asyncio
async def test_admin_can_retry_failed_job_and_scoped_notification_delivery(
    client: httpx.AsyncClient,
    owner_auth: dict[str, object],
) -> None:
    now = datetime.now(UTC)
    workspace_id = _workspace_id(owner_auth)
    own_delivery_id = uuid.uuid4()
    failed_job = BackgroundJob(
        workspace_id=workspace_id,
        job_type="test.failed",
        payload={},
        status=JobStatus.failed,
        run_at=now - timedelta(minutes=5),
        attempts=5,
        max_attempts=5,
        lease_owner="dead-worker",
        lease_until=now - timedelta(minutes=1),
        last_error="network unavailable",
    )
    own_delivery = NotificationDelivery(
        id=own_delivery_id,
        workspace_id=workspace_id,
        audience=NotificationAudience.employee,
        channel="email",
        recipient_address="owner@example.com",
        body="Retry me",
        status=DeliveryStatus.failed,
        dedupe_key="own-failed-delivery",
        scheduled_at=now - timedelta(minutes=10),
        attempts=3,
        last_error="SMTP timeout",
    )
    other_workspace = Workspace(name="Hidden Workspace", slug="hidden-workspace")
    delivery_job = BackgroundJob(
        workspace_id=workspace_id,
        job_type="notification.deliver",
        payload={"notification_delivery_id": str(own_delivery_id)},
        status=JobStatus.failed,
        run_at=now,
        attempts=4,
        dedupe_key=f"notification-delivery:{own_delivery_id}:send",
        lease_owner="old-worker",
        lease_until=now - timedelta(seconds=1),
        last_error="SMTP timeout",
    )
    async with SessionLocal() as db:
        db.add(other_workspace)
        await db.flush()
        other_job = BackgroundJob(
            workspace_id=other_workspace.id,
            job_type="test.hidden",
            payload={},
            status=JobStatus.failed,
            run_at=now,
            last_error="hidden workspace error",
        )
        other_delivery = NotificationDelivery(
            workspace_id=other_workspace.id,
            audience=NotificationAudience.employee,
            channel="email",
            recipient_address="hidden@example.com",
            body="Must stay hidden",
            status=DeliveryStatus.failed,
            dedupe_key="other-failed-delivery",
            scheduled_at=now,
            last_error="hidden failure",
        )
        db.add_all(
            [
                failed_job,
                own_delivery,
                other_delivery,
                other_job,
                delivery_job,
            ]
        )
        await db.commit()

    csrf = {"X-CSRF-Token": str(owner_auth["csrf_token"])}
    jobs = await client.get("/api/v1/admin/jobs?status=failed")
    assert jobs.status_code == 200, jobs.text
    visible_job_ids = {item["id"] for item in jobs.json()}
    assert str(failed_job.id) in visible_job_ids
    assert str(other_job.id) not in visible_job_ids

    hidden_job_retry = await client.post(
        f"/api/v1/admin/jobs/{other_job.id}/retry",
        headers=csrf,
    )
    assert hidden_job_retry.status_code == 404

    retried_job = await client.post(
        f"/api/v1/admin/jobs/{failed_job.id}/retry",
        headers=csrf,
    )
    assert retried_job.status_code == 200, retried_job.text
    assert retried_job.json()["status"] == "queued"
    assert retried_job.json()["attempts"] == 0
    assert retried_job.json()["last_error"] is None
    assert retried_job.json()["lease_until"] is None

    deliveries = await client.get("/api/v1/admin/notification-deliveries?status=failed")
    assert deliveries.status_code == 200, deliveries.text
    assert [item["id"] for item in deliveries.json()] == [str(own_delivery.id)]

    retried_delivery = await client.post(
        f"/api/v1/admin/notification-deliveries/{own_delivery.id}/retry",
        headers=csrf,
    )
    assert retried_delivery.status_code == 200, retried_delivery.text
    assert retried_delivery.json()["status"] == "pending"
    assert retried_delivery.json()["attempts"] == 0
    assert retried_delivery.json()["last_error"] is None

    hidden_retry = await client.post(
        f"/api/v1/admin/notification-deliveries/{other_delivery.id}/retry",
        headers=csrf,
    )
    assert hidden_retry.status_code == 404

    async with SessionLocal() as db:
        refreshed_delivery_job = await db.get(BackgroundJob, delivery_job.id)
        assert refreshed_delivery_job is not None
        assert refreshed_delivery_job.status is JobStatus.queued
        assert refreshed_delivery_job.attempts == 0
        assert refreshed_delivery_job.lease_owner is None
        assert refreshed_delivery_job.last_error is None


class _UnhealthyRuntime:
    def is_healthy(self, *, max_age_seconds: float) -> bool:
        del max_age_seconds
        return False


@pytest.mark.asyncio
async def test_readiness_fails_when_enabled_runtime_is_unhealthy(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        health_api,
        "get_settings",
        lambda: SimpleNamespace(
            job_runner_enabled=True,
            job_runner_heartbeat_timeout_seconds=30.0,
        ),
    )
    monkeypatch.setattr(app.state, "integration_runtime", _UnhealthyRuntime(), raising=False)

    response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["detail"] == {"database": "ok", "job_runner": "stale"}

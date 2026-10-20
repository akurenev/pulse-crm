from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError, ProgrammingError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from app.models import BackgroundJob, JobStatus
from app.services import jobs as job_service

POSTGRES_DIALECT = PGDialect()  # type: ignore[no-untyped-call]


class FakeScalarRows:
    def __init__(self, rows: list[BackgroundJob]) -> None:
        self.rows = rows

    def all(self) -> list[BackgroundJob]:
        return self.rows


class FakeResult:
    def __init__(
        self,
        *,
        rows: list[BackgroundJob] | None = None,
        scalar: BackgroundJob | None = None,
        rowcount: int = 0,
    ) -> None:
        self._rows = rows or []
        self._scalar = scalar
        self.rowcount = rowcount

    def scalars(self) -> FakeScalarRows:
        return FakeScalarRows(self._rows)

    def scalar_one_or_none(self) -> BackgroundJob | None:
        return self._scalar


class FakeSession:
    def __init__(
        self,
        *,
        execute_results: list[FakeResult] | None = None,
        scalar_results: list[BackgroundJob | None] | None = None,
    ) -> None:
        self.execute_results = list(execute_results or [])
        self.scalar_results = list(scalar_results or [])
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.flush_count = 0

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flush_count += 1

    async def execute(self, statement: Any) -> FakeResult:
        self.statements.append(statement)
        if self.execute_results:
            return self.execute_results.pop(0)
        return FakeResult()

    async def scalar(self, statement: Any) -> BackgroundJob | None:
        self.statements.append(statement)
        if self.scalar_results:
            return self.scalar_results.pop(0)
        return None


class TransactionalFakeSession(FakeSession):
    async def __aenter__(self) -> TransactionalFakeSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        return None

    def begin(self) -> TransactionalFakeSession:
        return self


class FakeSessionFactory:
    def __init__(self) -> None:
        self.sessions: list[TransactionalFakeSession] = []

    def __call__(self) -> TransactionalFakeSession:
        session = TransactionalFakeSession()
        self.sessions.append(session)
        return session


def make_job(
    *,
    status: JobStatus = JobStatus.queued,
    attempts: int = 0,
    max_attempts: int = 5,
    lease_owner: str | None = None,
) -> BackgroundJob:
    now = datetime.now(UTC)
    return BackgroundJob(
        id=uuid.uuid4(),
        job_type="notification.send",
        payload={"delivery_id": "delivery-1"},
        status=status,
        run_at=now,
        attempts=attempts,
        max_attempts=max_attempts,
        dedupe_key=None,
        lease_owner=lease_owner,
        lease_until=None,
        last_error=None,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_enqueue_job_adds_job_to_callers_transaction() -> None:
    session = FakeSession()
    run_at = datetime(2026, 8, 27, 10, tzinfo=UTC)

    job = await job_service.enqueue_job(
        session,  # type: ignore[arg-type]
        "message.send",
        {"message_id": "message-1"},
        run_at=run_at,
        max_attempts=3,
    )

    assert session.added == [job]
    assert session.flush_count == 1
    assert job.job_type == "message.send"
    assert job.payload == {"message_id": "message-1"}
    assert job.status is JobStatus.queued
    assert job.run_at == run_at
    assert job.attempts == 0
    assert job.max_attempts == 3


@pytest.mark.asyncio
async def test_enqueue_job_uses_postgres_conflict_guard_for_dedupe_key() -> None:
    inserted = make_job()
    inserted.dedupe_key = "message:1"
    session = FakeSession(execute_results=[FakeResult(scalar=inserted)])

    result = await job_service.enqueue_job(
        session,  # type: ignore[arg-type]
        "message.send",
        dedupe_key="message:1",
    )

    assert result is inserted
    sql = str(session.statements[0].compile(dialect=POSTGRES_DIALECT))
    assert "ON CONFLICT (dedupe_key) DO NOTHING" in sql
    assert "RETURNING" in sql


@pytest.mark.asyncio
async def test_claim_jobs_uses_skip_locked_and_sets_lease() -> None:
    first = make_job()
    second = make_job(attempts=2)
    session = FakeSession(execute_results=[FakeResult(rows=[first, second])])
    now = datetime(2026, 8, 27, 11, tzinfo=UTC)

    claimed = await job_service.claim_jobs(
        session,  # type: ignore[arg-type]
        "worker-1",
        limit=2,
        lease_seconds=45,
        now=now,
    )

    assert claimed == [first, second]
    assert first.status is JobStatus.running
    assert second.status is JobStatus.running
    assert first.attempts == 1
    assert second.attempts == 3
    assert first.lease_owner == second.lease_owner == "worker-1"
    assert first.lease_until == second.lease_until == now + timedelta(seconds=45)
    assert session.flush_count == 1

    sql = str(session.statements[0].compile(dialect=POSTGRES_DIALECT))
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "background_jobs.status" in sql
    assert "background_jobs.run_at" in sql


@pytest.mark.asyncio
async def test_complete_job_requires_running_job_owned_by_worker() -> None:
    session = FakeSession(execute_results=[FakeResult(rowcount=1)])
    job_id = uuid.uuid4()

    changed = await job_service.complete_job(
        session,  # type: ignore[arg-type]
        job_id,
        lease_owner="worker-1",
    )

    assert changed is True
    statement = session.statements[0]
    sql = str(statement.compile(dialect=POSTGRES_DIALECT))
    assert sql.startswith("UPDATE background_jobs")
    assert "background_jobs.lease_owner" in sql
    assert "background_jobs.status" in sql


@pytest.mark.asyncio
async def test_fail_job_requeues_with_backoff_before_attempt_limit() -> None:
    job = make_job(status=JobStatus.running, attempts=2, max_attempts=3, lease_owner="worker-1")
    session = FakeSession(scalar_results=[job])
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)

    status = await job_service.fail_job(
        session,  # type: ignore[arg-type]
        job.id,
        RuntimeError("provider unavailable"),
        lease_owner="worker-1",
        retry_base_seconds=2,
        now=now,
    )

    assert status == "queued"
    assert job.status is JobStatus.queued
    assert job.run_at == now + timedelta(seconds=4)
    assert job.lease_owner is None
    assert job.lease_until is None
    assert job.last_error == "provider unavailable"
    assert session.flush_count == 1


@pytest.mark.asyncio
async def test_fail_job_is_terminal_at_attempt_limit() -> None:
    job = make_job(status=JobStatus.running, attempts=3, max_attempts=3, lease_owner="worker-1")
    session = FakeSession(scalar_results=[job])

    status = await job_service.fail_job(
        session,  # type: ignore[arg-type]
        job.id,
        "still unavailable",
        lease_owner="worker-1",
    )

    assert status == "failed"
    assert job.status is JobStatus.failed
    assert job.lease_owner is None


@pytest.mark.asyncio
async def test_recover_expired_leases_reports_requeued_and_failed_counts() -> None:
    session = FakeSession(execute_results=[FakeResult(rowcount=2), FakeResult(rowcount=3)])
    now = datetime(2026, 8, 27, 13, tzinfo=UTC)

    recovered = await job_service.recover_expired_leases(
        session,  # type: ignore[arg-type]
        now=now,
    )

    assert recovered.failed == 2
    assert recovered.requeued == 3
    assert recovered.total == 5
    assert len(session.statements) == 2
    for statement in session.statements:
        sql = str(statement.compile(dialect=POSTGRES_DIALECT))
        assert sql.startswith("UPDATE background_jobs")
        assert "background_jobs.lease_until" in sql


@pytest.mark.asyncio
async def test_supervisor_heartbeat_controls_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    async def claim_none(*args: Any, **kwargs: Any) -> list[BackgroundJob]:
        return []

    async def recover_none(*args: Any, **kwargs: Any) -> job_service.RecoveryResult:
        return job_service.RecoveryResult(requeued=0, failed=0)

    monkeypatch.setattr(job_service, "claim_jobs", claim_none)
    monkeypatch.setattr(job_service, "recover_expired_leases", recover_none)

    supervisor = job_service.JobSupervisor(
        {},
        session_factory=FakeSessionFactory(),  # type: ignore[arg-type]
        concurrency=1,
        poll_interval_seconds=0.005,
        heartbeat_interval_seconds=0.005,
        recovery_interval_seconds=0.01,
        lease_seconds=0.1,
        job_timeout_seconds=0.05,
    )

    assert supervisor.is_healthy() is False
    await supervisor.start()
    await asyncio.sleep(0.02)

    assert supervisor.running is True
    assert supervisor.last_heartbeat_at is not None
    assert supervisor.is_healthy(max_age_seconds=0.05) is True

    await supervisor.stop()
    assert supervisor.running is False
    assert supervisor.is_healthy() is False


@pytest.mark.asyncio
async def test_supervisor_executes_and_acknowledges_claimed_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = make_job(status=JobStatus.running, attempts=1, lease_owner="worker-test")
    claimed_once = False
    handler_called = asyncio.Event()
    completed = asyncio.Event()

    async def fake_claim(*args: Any, **kwargs: Any) -> list[BackgroundJob]:
        nonlocal claimed_once
        if claimed_once:
            return []
        claimed_once = True
        return [model]

    async def recover_none(*args: Any, **kwargs: Any) -> job_service.RecoveryResult:
        return job_service.RecoveryResult(requeued=0, failed=0)

    async def fake_complete(*args: Any, **kwargs: Any) -> bool:
        completed.set()
        return True

    async def handler(job: job_service.ClaimedJob) -> None:
        assert job.id == model.id
        assert job.payload == model.payload
        handler_called.set()

    monkeypatch.setattr(job_service, "claim_jobs", fake_claim)
    monkeypatch.setattr(job_service, "recover_expired_leases", recover_none)
    monkeypatch.setattr(job_service, "complete_job", fake_complete)

    supervisor = job_service.JobSupervisor(
        {"notification.send": handler},
        session_factory=FakeSessionFactory(),  # type: ignore[arg-type]
        worker_id="worker-test",
        concurrency=1,
        poll_interval_seconds=0.005,
        heartbeat_interval_seconds=0.005,
        recovery_interval_seconds=0.01,
        lease_seconds=0.1,
        job_timeout_seconds=0.05,
    )

    await supervisor.start()
    await asyncio.wait_for(handler_called.wait(), timeout=0.25)
    await asyncio.wait_for(completed.wait(), timeout=0.25)
    await supervisor.stop()


@pytest.mark.asyncio
async def test_supervisor_terminally_fails_unknown_job_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = make_job(status=JobStatus.running, attempts=1, lease_owner="worker-test")
    claimed_once = False
    failure: dict[str, Any] = {}
    failed = asyncio.Event()

    async def fake_claim(*args: Any, **kwargs: Any) -> list[BackgroundJob]:
        nonlocal claimed_once
        if claimed_once:
            return []
        claimed_once = True
        return [model]

    async def recover_none(*args: Any, **kwargs: Any) -> job_service.RecoveryResult:
        return job_service.RecoveryResult(requeued=0, failed=0)

    async def fake_fail(*args: Any, **kwargs: Any) -> str:
        failure.update(kwargs)
        failed.set()
        return "failed"

    monkeypatch.setattr(job_service, "claim_jobs", fake_claim)
    monkeypatch.setattr(job_service, "recover_expired_leases", recover_none)
    monkeypatch.setattr(job_service, "fail_job", fake_fail)

    supervisor = job_service.JobSupervisor(
        {},
        session_factory=FakeSessionFactory(),  # type: ignore[arg-type]
        worker_id="worker-test",
        concurrency=1,
        poll_interval_seconds=0.005,
        heartbeat_interval_seconds=0.005,
        recovery_interval_seconds=0.01,
        lease_seconds=0.1,
        job_timeout_seconds=0.05,
    )

    await supervisor.start()
    await asyncio.wait_for(failed.wait(), timeout=0.25)
    await supervisor.stop()

    assert failure["lease_owner"] == "worker-test"
    assert failure["retryable"] is False


async def wait_until(predicate: Any) -> None:
    async with asyncio.timeout(1):
        while not predicate():  # noqa: ASYNC110 - readiness exposes a predicate, not an event.
            await asyncio.sleep(0.001)


def recovery_supervisor(
    handlers: dict[str, Any] | None = None,
    *,
    session_factory: Any = None,
    concurrency: int = 1,
) -> job_service.JobSupervisor:
    return job_service.JobSupervisor(
        handlers or {},
        session_factory=session_factory or FakeSessionFactory(),
        concurrency=concurrency,
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=0.01,
        recovery_interval_seconds=0.01,
        lease_seconds=2,
        job_timeout_seconds=1,
    )


@pytest.mark.asyncio
async def test_supervisor_requires_first_successful_database_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_database = asyncio.Event()

    class Session(TransactionalFakeSession):
        async def execute(self, statement: Any) -> FakeResult:
            await allow_database.wait()
            return FakeResult()

    async def claim(*args: Any, **kwargs: Any) -> list[BackgroundJob]:
        await allow_database.wait()
        return []

    async def recover(*args: Any) -> job_service.RecoveryResult:
        await allow_database.wait()
        return job_service.RecoveryResult(requeued=0, failed=0)

    monkeypatch.setattr(job_service, "claim_jobs", claim)
    monkeypatch.setattr(job_service, "recover_expired_leases", recover)
    supervisor = recovery_supervisor(session_factory=Session)
    await supervisor.start()
    try:
        assert supervisor.running
        assert supervisor.last_heartbeat_at is None
        assert not supervisor.is_healthy()
        allow_database.set()
        await wait_until(supervisor.is_healthy)
    finally:
        allow_database.set()
        await supervisor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["dispatch", "recovery", "heartbeat"])
@pytest.mark.parametrize("failure_stage", ["query", "commit"])
async def test_supervisor_retries_database_outage_and_restores_readiness(
    monkeypatch: pytest.MonkeyPatch, operation: str, failure_stage: str
) -> None:
    unavailable = False
    failed = asyncio.Event()
    calls = {"dispatch": 0, "recovery": 0, "heartbeat": 0}
    committed = {"dispatch": 0, "recovery": 0, "heartbeat": 0}

    def maybe_fail(current_operation: str, current_stage: str) -> None:
        if unavailable and current_operation == operation and current_stage == failure_stage:
            failed.set()
            raise OperationalError("test operation", {}, RuntimeError("test database unavailable"))

    class Transaction:
        def __init__(self, session: Session) -> None:
            self.session = session

        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
            if exc_type is None:
                maybe_fail(self.session.operation, "commit")
                committed[self.session.operation] += 1

    class Session(TransactionalFakeSession):
        operation = "heartbeat"

        def begin(self) -> Transaction:  # type: ignore[override]
            return Transaction(self)

        async def execute(self, statement: Any) -> FakeResult:
            self.operation = "heartbeat"
            calls[self.operation] += 1
            maybe_fail(self.operation, "query")
            return FakeResult()

    async def claim_none(session: Session, *args: Any, **kwargs: Any) -> list[BackgroundJob]:
        session.operation = "dispatch"
        calls[session.operation] += 1
        maybe_fail(session.operation, "query")
        return []

    async def recover_none(session: Session) -> job_service.RecoveryResult:
        session.operation = "recovery"
        calls[session.operation] += 1
        maybe_fail(session.operation, "query")
        return job_service.RecoveryResult(requeued=0, failed=0)

    monkeypatch.setattr(job_service, "claim_jobs", claim_none)
    monkeypatch.setattr(job_service, "recover_expired_leases", recover_none)
    supervisor = recovery_supervisor(session_factory=Session)
    await supervisor.start()
    try:
        await wait_until(lambda: all(committed.values()) and supervisor.is_healthy())
        unavailable = True
        await asyncio.wait_for(failed.wait(), timeout=0.5)
        await wait_until(lambda: not supervisor.is_healthy())
        heartbeat = supervisor.last_heartbeat_at
        attempts = calls[operation]
        await asyncio.sleep(0.035)
        assert supervisor.running is True
        assert supervisor.is_healthy() is False
        assert supervisor.last_heartbeat_at == heartbeat
        # Retries wait for the configured interval; other successful paths must
        # not conceal an unavailable dispatch, recovery or heartbeat operation.
        assert 1 <= calls[operation] - attempts <= 5
        unavailable = False
        await wait_until(supervisor.is_healthy)
        assert supervisor.last_heartbeat_at != heartbeat
    finally:
        await asyncio.wait_for(supervisor.stop(), timeout=0.25)
    assert supervisor.running is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        OperationalError("test", {}, RuntimeError("unavailable")),
        InterfaceError("test", {}, RuntimeError("connection closed")),
        SQLAlchemyTimeoutError("test pool unavailable"),
        DBAPIError("test", {}, RuntimeError("disconnected"), connection_invalidated=True),
    ],
)
async def test_supervisor_claims_jobs_after_transient_database_errors(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    model = make_job(status=JobStatus.running, attempts=1, lease_owner="worker-test")
    attempts = 0
    handled = asyncio.Event()
    acknowledged = asyncio.Event()

    async def claim(*args: Any, **kwargs: Any) -> list[BackgroundJob]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error
        return [model] if attempts == 2 else []

    async def recover(*args: Any) -> job_service.RecoveryResult:
        return job_service.RecoveryResult(requeued=0, failed=0)

    async def complete(*args: Any, **kwargs: Any) -> bool:
        acknowledged.set()
        return True

    async def handler(job: job_service.ClaimedJob) -> None:
        handled.set()

    monkeypatch.setattr(job_service, "claim_jobs", claim)
    monkeypatch.setattr(job_service, "recover_expired_leases", recover)
    monkeypatch.setattr(job_service, "complete_job", complete)
    supervisor = recovery_supervisor({"notification.send": handler})
    await supervisor.start()
    try:
        await asyncio.wait_for(acknowledged.wait(), timeout=0.5)
        assert handled.is_set()
        assert supervisor.running is True
        await wait_until(supervisor.is_healthy)
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledgement", ["complete", "fail", "unknown"])
async def test_supervisor_acknowledgement_outage_keeps_other_jobs_and_durable_lease(
    monkeypatch: pytest.MonkeyPatch, acknowledgement: str
) -> None:
    first = make_job(status=JobStatus.running, attempts=1, lease_owner="worker-test")
    second = make_job(status=JobStatus.running, attempts=1, lease_owner="worker-test")
    first.job_type = "unknown" if acknowledgement == "unknown" else "notification.send"
    claimed = False
    handler_calls: list[Any] = []
    acknowledgement_failed = asyncio.Event()
    other_job_started = asyncio.Event()
    release_other_job = asyncio.Event()
    allow_recovery = asyncio.Event()
    other_job_completed = asyncio.Event()

    async def claim(*args: Any, **kwargs: Any) -> list[BackgroundJob]:
        nonlocal claimed
        if claimed:
            return []
        claimed = True
        assert kwargs["limit"] == 2
        return [first, second]

    async def recover(*args: Any) -> job_service.RecoveryResult:
        if acknowledgement_failed.is_set():
            await allow_recovery.wait()
        return job_service.RecoveryResult(requeued=0, failed=0)

    async def complete(session: Any, job_id: Any, **kwargs: Any) -> bool:
        if job_id == first.id:
            acknowledgement_failed.set()
            raise OperationalError("test", {}, RuntimeError("database unavailable"))
        other_job_completed.set()
        return True

    async def fail(session: Any, job_id: Any, *args: Any, **kwargs: Any) -> str:
        assert job_id == first.id
        acknowledgement_failed.set()
        raise OperationalError("test", {}, RuntimeError("database unavailable"))

    async def handler(job: job_service.ClaimedJob) -> None:
        handler_calls.append(job.id)
        if job.id == first.id:
            if acknowledgement == "fail":
                raise RuntimeError("test provider unavailable")
            return
        other_job_started.set()
        await release_other_job.wait()

    monkeypatch.setattr(job_service, "claim_jobs", claim)
    monkeypatch.setattr(job_service, "recover_expired_leases", recover)
    monkeypatch.setattr(job_service, "complete_job", complete)
    monkeypatch.setattr(job_service, "fail_job", fail)
    supervisor = recovery_supervisor({"notification.send": handler}, concurrency=2)
    await supervisor.start()
    try:
        await asyncio.wait_for(acknowledgement_failed.wait(), timeout=0.5)
        await asyncio.wait_for(other_job_started.wait(), timeout=0.5)
        await wait_until(lambda: not supervisor.is_healthy())
        await wait_until(lambda: supervisor.active_job_count == 1)
        assert supervisor.running is True
        assert supervisor.active_job_count == 1
        assert not other_job_completed.is_set()
        assert first.status is JobStatus.running
        assert first.lease_owner == "worker-test"
        assert first.attempts == 1
        # A later successful recovery pass restores health even if no new job
        # acknowledgement occurs; the original side effect is not replayed.
        allow_recovery.set()
        await wait_until(supervisor.is_healthy)
        release_other_job.set()
        await asyncio.wait_for(other_job_completed.wait(), timeout=0.5)
    finally:
        allow_recovery.set()
        release_other_job.set()
        await supervisor.stop()
    assert handler_calls.count(first.id) == (0 if acknowledgement == "unknown" else 1)
    assert handler_calls.count(second.id) == 1


@pytest.mark.asyncio
async def test_supervisor_saturated_workers_require_successful_database_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = False
    started = asyncio.Event()
    release = asyncio.Event()

    async def claim(*args: Any, **kwargs: Any) -> list[BackgroundJob]:
        nonlocal claimed
        if claimed:
            return []
        claimed = True
        return [make_job(status=JobStatus.running, attempts=1, lease_owner="worker-test")]

    async def recover(*args: Any) -> job_service.RecoveryResult:
        return job_service.RecoveryResult(requeued=0, failed=0)

    async def complete(*args: Any, **kwargs: Any) -> bool:
        return True

    async def handler(job: job_service.ClaimedJob) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(job_service, "claim_jobs", claim)
    monkeypatch.setattr(job_service, "recover_expired_leases", recover)
    monkeypatch.setattr(job_service, "complete_job", complete)
    supervisor = recovery_supervisor({"notification.send": handler})
    supervisor.recovery_interval_seconds = 1
    await supervisor.start()
    try:
        await asyncio.wait_for(started.wait(), timeout=0.5)
        await asyncio.sleep(0.06)
        assert supervisor.active_job_count == 1
        assert supervisor.is_healthy(max_age_seconds=0.04)
    finally:
        release.set()
        await supervisor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_supervisor_stops_or_cancels_during_database_retry(
    monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    failed = asyncio.Event()

    async def claim(*args: Any, **kwargs: Any) -> list[BackgroundJob]:
        failed.set()
        raise OperationalError("test", {}, RuntimeError("database unavailable"))

    async def recover(*args: Any) -> job_service.RecoveryResult:
        return job_service.RecoveryResult(requeued=0, failed=0)

    monkeypatch.setattr(job_service, "claim_jobs", claim)
    monkeypatch.setattr(job_service, "recover_expired_leases", recover)
    supervisor = recovery_supervisor()
    supervisor.poll_interval_seconds = 10
    await supervisor.start()
    await asyncio.wait_for(failed.wait(), timeout=0.5)
    if cancel:
        assert supervisor._runner_task is not None
        supervisor._runner_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(supervisor._runner_task, timeout=0.25)
    else:
        await asyncio.wait_for(supervisor.stop(), timeout=0.25)
    assert not supervisor.running
    assert not supervisor.is_healthy()
    assert supervisor.active_job_count == 0


@pytest.mark.asyncio
async def test_supervisor_does_not_hide_programming_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def claim(*args: Any, **kwargs: Any) -> list[BackgroundJob]:
        raise ProgrammingError("test invalid statement", {}, RuntimeError("test programming error"))

    async def recover(*args: Any) -> job_service.RecoveryResult:
        return job_service.RecoveryResult(requeued=0, failed=0)

    monkeypatch.setattr(job_service, "claim_jobs", claim)
    monkeypatch.setattr(job_service, "recover_expired_leases", recover)
    supervisor = recovery_supervisor()
    with pytest.raises(ExceptionGroup) as captured:
        await supervisor.run()
    assert isinstance(captured.value.exceptions[0], ProgrammingError)
    assert not supervisor.running
    assert not supervisor.is_healthy()

"""PostgreSQL-backed background job queue.

The queue deliberately lives in PostgreSQL so the MVP can run as a single
web application without Redis or a separate worker.  The public helpers do
not commit transactions: callers can compose a job state change with their
own domain changes.  :class:`JobSupervisor` owns its transaction boundaries
when it claims or acknowledges jobs.

The implementation assumes ``BackgroundJob`` exposes the fields documented
in the project architecture: ``id``, ``workspace_id``, ``job_type``, ``status``, ``payload``,
``run_at``, ``attempts``, ``max_attempts``, ``dedupe_key``, ``lease_owner``,
``lease_until`` and ``last_error``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import BackgroundJob

logger = logging.getLogger(__name__)

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"

DEFAULT_LEASE_SECONDS = 45.0
DEFAULT_JOB_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_ATTEMPTS = 5
MAX_ERROR_LENGTH = 4_000


class AsyncSessionContext(Protocol):
    async def __aenter__(self) -> AsyncSession: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> bool | None: ...


class SessionFactory(Protocol):
    def __call__(self) -> AsyncSessionContext: ...


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp.

    Kept as a function (instead of a module constant or database ``now()``)
    so lease decisions use one timestamp per operation and are easy to test.
    """

    return datetime.now(UTC)


def _job_status(name: str) -> Any:
    """Return the model's Python enum member, or ``name`` for string columns."""

    try:
        enum_class = getattr(BackgroundJob.__table__.c.status.type, "enum_class", None)
    except KeyError:
        enum_class = None

    if enum_class is None:
        return name

    try:
        return enum_class(name)
    except (TypeError, ValueError):
        try:
            return enum_class[name]
        except (KeyError, TypeError):
            return name


def _status_name(status: Any) -> str:
    value = getattr(status, "value", status)
    return str(value)


def _retry_delay(attempts: int, *, base_seconds: float, cap_seconds: float) -> float:
    """Calculate capped exponential backoff after a failed claimed attempt."""

    exponent = max(0, attempts - 1)
    return float(min(cap_seconds, base_seconds * (2**exponent)))


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """Detached, immutable data passed to a job handler."""

    id: Any
    job_type: str
    payload: Mapping[str, Any]
    attempts: int
    max_attempts: int
    lease_owner: str
    workspace_id: uuid.UUID | None = None

    @classmethod
    def from_model(cls, job: BackgroundJob) -> ClaimedJob:
        if job.lease_owner is None:
            raise ValueError("claimed job is missing lease_owner")
        return cls(
            id=job.id,
            job_type=job.job_type,
            payload=dict(job.payload or {}),
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            lease_owner=job.lease_owner,
            workspace_id=job.workspace_id,
        )


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    requeued: int
    failed: int

    @property
    def total(self) -> int:
        return self.requeued + self.failed


type JobHandler = Callable[[ClaimedJob], Awaitable[None]]


async def enqueue_job(
    session: AsyncSession,
    job_type: str,
    payload: Mapping[str, Any] | None = None,
    *,
    workspace_id: uuid.UUID | None = None,
    run_at: datetime | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    dedupe_key: str | None = None,
) -> BackgroundJob:
    """Queue a job and return it without committing the surrounding transaction.

    A non-null ``dedupe_key`` is an idempotency key for the lifetime of its row.
    ``ON CONFLICT DO NOTHING`` makes concurrent enqueue requests safe; both
    callers receive the same job row.  The database schema must enforce a
    unique constraint/index on ``BackgroundJob.dedupe_key``.
    """

    if not job_type.strip():
        raise ValueError("job_type must not be empty")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    values: dict[str, Any] = {
        "workspace_id": workspace_id,
        "job_type": job_type,
        "payload": dict(payload or {}),
        "status": _job_status(QUEUED),
        "run_at": run_at or utcnow(),
        "attempts": 0,
        "max_attempts": max_attempts,
        "dedupe_key": dedupe_key,
        "lease_owner": None,
        "lease_until": None,
        "last_error": None,
    }

    if dedupe_key is None:
        job = BackgroundJob(**values)
        session.add(job)
        await session.flush()
        return job

    insert_statement = (
        postgresql_insert(BackgroundJob)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[BackgroundJob.dedupe_key])
        .returning(BackgroundJob)
    )
    result = await session.execute(insert_statement)
    inserted_job = result.scalar_one_or_none()
    if inserted_job is not None:
        return inserted_job

    # A conflicting transaction has committed the idempotent job. PostgreSQL
    # waits for that transaction while resolving the unique conflict, so the
    # row is visible under the default READ COMMITTED isolation level here.
    existing_job = await session.scalar(
        select(BackgroundJob).where(BackgroundJob.dedupe_key == dedupe_key)
    )
    if existing_job is None:  # Defensive guard for a misconfigured/missing unique index.
        raise RuntimeError("deduplicated job could not be loaded")
    return existing_job


async def claim_jobs(
    session: AsyncSession,
    lease_owner: str,
    *,
    limit: int = 1,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> list[BackgroundJob]:
    """Atomically lease runnable jobs using ``FOR UPDATE SKIP LOCKED``.

    The caller must commit the transaction before executing the jobs.  Each
    successful claim increments ``attempts`` exactly once.
    """

    if not lease_owner:
        raise ValueError("lease_owner must not be empty")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")

    claimed_at = now or utcnow()
    statement = (
        select(BackgroundJob)
        .where(
            BackgroundJob.status == _job_status(QUEUED),
            BackgroundJob.run_at <= claimed_at,
            BackgroundJob.attempts < BackgroundJob.max_attempts,
        )
        .order_by(
            BackgroundJob.run_at.asc(),
            BackgroundJob.created_at.asc(),
            BackgroundJob.id.asc(),
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(statement)
    jobs = list(result.scalars().all())
    lease_until = claimed_at + timedelta(seconds=lease_seconds)

    for job in jobs:
        job.status = _job_status(RUNNING)
        job.attempts += 1
        job.lease_owner = lease_owner
        job.lease_until = lease_until

    if jobs:
        await session.flush()
    return jobs


async def complete_job(
    session: AsyncSession,
    job_id: Any,
    *,
    lease_owner: str | None = None,
) -> bool:
    """Mark a running job succeeded if its lease still belongs to the caller."""

    predicates = [
        BackgroundJob.id == job_id,
        BackgroundJob.status == _job_status(RUNNING),
    ]
    if lease_owner is not None:
        predicates.append(BackgroundJob.lease_owner == lease_owner)

    result = await session.execute(
        update(BackgroundJob)
        .where(*predicates)
        .values(
            status=_job_status(SUCCEEDED),
            lease_owner=None,
            lease_until=None,
            last_error=None,
        )
        .execution_options(synchronize_session=False)
    )
    return bool(getattr(result, "rowcount", 0))


async def fail_job(
    session: AsyncSession,
    job_id: Any,
    error: BaseException | str,
    *,
    lease_owner: str | None = None,
    retryable: bool = True,
    retry_delay_seconds: float | None = None,
    retry_base_seconds: float = 2.0,
    retry_cap_seconds: float = 300.0,
    now: datetime | None = None,
) -> str | None:
    """Record a failure and either requeue or terminally fail a leased job.

    Returns the new status, or ``None`` when the job is no longer running or
    the supplied worker does not own its lease.
    """

    failed_at = now or utcnow()
    predicates = [
        BackgroundJob.id == job_id,
        BackgroundJob.status == _job_status(RUNNING),
    ]
    if lease_owner is not None:
        predicates.append(BackgroundJob.lease_owner == lease_owner)

    job = await session.scalar(select(BackgroundJob).where(*predicates).with_for_update())
    if job is None:
        return None

    job.last_error = str(error)[:MAX_ERROR_LENGTH]
    job.lease_owner = None
    job.lease_until = None

    should_retry = retryable and job.attempts < job.max_attempts
    if should_retry:
        delay = retry_delay_seconds
        if delay is None:
            delay = _retry_delay(
                job.attempts,
                base_seconds=retry_base_seconds,
                cap_seconds=retry_cap_seconds,
            )
        if delay < 0:
            raise ValueError("retry_delay_seconds must not be negative")
        job.status = _job_status(QUEUED)
        job.run_at = failed_at + timedelta(seconds=delay)
    else:
        job.status = _job_status(FAILED)

    await session.flush()
    return _status_name(job.status)


async def recover_expired_leases(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> RecoveryResult:
    """Requeue abandoned leases and terminally fail exhausted jobs.

    Bulk updates make recovery idempotent and safe when two supervisors briefly
    overlap during a deployment.  PostgreSQL row locks serialize conflicting
    updates, and each predicate is re-evaluated after the wait.
    """

    recovered_at = now or utcnow()
    expired = (
        BackgroundJob.status == _job_status(RUNNING),
        BackgroundJob.lease_until.is_not(None),
        BackgroundJob.lease_until <= recovered_at,
    )

    failed_result = await session.execute(
        update(BackgroundJob)
        .where(*expired, BackgroundJob.attempts >= BackgroundJob.max_attempts)
        .values(
            status=_job_status(FAILED),
            lease_owner=None,
            lease_until=None,
            last_error=func.coalesce(
                BackgroundJob.last_error,
                "job lease expired after maximum attempts",
            ),
        )
        .execution_options(synchronize_session=False)
    )
    requeued_result = await session.execute(
        update(BackgroundJob)
        .where(*expired, BackgroundJob.attempts < BackgroundJob.max_attempts)
        .values(
            status=_job_status(QUEUED),
            run_at=recovered_at,
            lease_owner=None,
            lease_until=None,
            last_error=func.coalesce(
                BackgroundJob.last_error,
                "job lease expired before acknowledgement",
            ),
        )
        .execution_options(synchronize_session=False)
    )

    return RecoveryResult(
        requeued=int(getattr(requeued_result, "rowcount", 0) or 0),
        failed=int(getattr(failed_result, "rowcount", 0) or 0),
    )


class JobSupervisor:
    """Run PostgreSQL jobs inside the single FastAPI application process.

    Handlers receive a detached :class:`ClaimedJob`.  A handler must be
    idempotent because a process can stop after performing its side effect but
    before acknowledging the job.  The lease is intentionally longer than the
    default handler timeout.
    """

    def __init__(
        self,
        handlers: Mapping[str, JobHandler],
        *,
        session_factory: SessionFactory = SessionLocal,
        concurrency: int = 4,
        batch_size: int | None = None,
        poll_interval_seconds: float = 1.0,
        heartbeat_interval_seconds: float = 2.0,
        recovery_interval_seconds: float = 15.0,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        job_timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS,
        worker_id: str | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if batch_size is not None and batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if (
            min(
                poll_interval_seconds,
                heartbeat_interval_seconds,
                recovery_interval_seconds,
                lease_seconds,
                job_timeout_seconds,
            )
            <= 0
        ):
            raise ValueError("supervisor intervals and timeouts must be positive")
        if lease_seconds <= job_timeout_seconds:
            raise ValueError("lease_seconds must be greater than job_timeout_seconds")

        self.handlers = dict(handlers)
        self.session_factory = session_factory
        self.concurrency = concurrency
        self.batch_size = min(batch_size or concurrency, concurrency)
        self.poll_interval_seconds = poll_interval_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.recovery_interval_seconds = recovery_interval_seconds
        self.lease_seconds = lease_seconds
        self.job_timeout_seconds = job_timeout_seconds
        self.worker_id = worker_id or f"pulse-{uuid.uuid4()}"

        self._stop_event = asyncio.Event()
        self._started_event = asyncio.Event()
        self._runner_task: asyncio.Task[None] | None = None
        self._active_tasks: set[asyncio.Task[None]] = set()
        self._running = False
        self._last_heartbeat_monotonic: float | None = None
        self._last_heartbeat_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_heartbeat_at(self) -> datetime | None:
        return self._last_heartbeat_at

    @property
    def active_job_count(self) -> int:
        return len(self._active_tasks)

    def is_healthy(self, *, max_age_seconds: float | None = None) -> bool:
        """Return whether readiness can trust the in-process supervisor."""

        if not self._running or self._last_heartbeat_monotonic is None:
            return False
        max_age = max_age_seconds or max(5.0, self.heartbeat_interval_seconds * 3)
        return time.monotonic() - self._last_heartbeat_monotonic <= max_age

    async def start(self) -> None:
        """Start the supervisor in a task and wait until its loops are live."""

        if self._runner_task is not None and not self._runner_task.done():
            return
        self._stop_event.clear()
        self._started_event.clear()
        self._runner_task = asyncio.create_task(self.run(), name="job-supervisor")
        await self._started_event.wait()
        if self._runner_task.done():
            await self._runner_task

    async def stop(self) -> None:
        """Request a graceful stop and wait for already claimed jobs."""

        self._stop_event.set()
        task = self._runner_task
        if task is not None and task is not asyncio.current_task():
            await task

    async def run(self) -> None:
        """Run until :meth:`stop` is called or an infrastructure loop fails."""

        if self._running:
            raise RuntimeError("job supervisor is already running")

        self._running = True
        self._touch_heartbeat()
        self._started_event.set()
        try:
            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(self._heartbeat_loop(), name="job-supervisor-heartbeat")
                task_group.create_task(self._recovery_loop(), name="job-supervisor-recovery")
                await self._dispatch_loop(task_group)
        finally:
            self._running = False
            self._active_tasks.clear()

    def _touch_heartbeat(self) -> None:
        self._last_heartbeat_monotonic = time.monotonic()
        self._last_heartbeat_at = utcnow()

    async def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            self._touch_heartbeat()
            await self._wait_or_stop(self.heartbeat_interval_seconds)

    async def _recovery_loop(self) -> None:
        while not self._stop_event.is_set():
            async with self.session_factory() as session:
                async with session.begin():
                    recovered = await recover_expired_leases(session)
            if recovered.total:
                logger.warning(
                    "recovered expired background job leases",
                    extra={
                        "requeued_jobs": recovered.requeued,
                        "failed_jobs": recovered.failed,
                    },
                )
            await self._wait_or_stop(self.recovery_interval_seconds)

    async def _dispatch_loop(self, task_group: asyncio.TaskGroup) -> None:
        while not self._stop_event.is_set():
            capacity = self.concurrency - len(self._active_tasks)
            if capacity <= 0:
                await self._wait_for_capacity()
                continue

            async with self.session_factory() as session:
                async with session.begin():
                    models = await claim_jobs(
                        session,
                        self.worker_id,
                        limit=min(capacity, self.batch_size),
                        lease_seconds=self.lease_seconds,
                    )
                    claimed = [ClaimedJob.from_model(job) for job in models]

            if not claimed:
                await self._wait_or_stop(self.poll_interval_seconds)
                continue

            for job in claimed:
                task = task_group.create_task(
                    self._execute(job),
                    name=f"job-{job.job_type}-{job.id}",
                )
                self._active_tasks.add(task)
                task.add_done_callback(self._active_tasks.discard)

    async def _wait_for_capacity(self) -> None:
        if not self._active_tasks:
            return
        stop_task = asyncio.create_task(self._stop_event.wait())
        try:
            await asyncio.wait(
                {*self._active_tasks, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stop_task.cancel()

    async def _execute(self, job: ClaimedJob) -> None:
        handler = self.handlers.get(job.job_type)
        if handler is None:
            await self._acknowledge_failure(
                job,
                f"no handler registered for job type {job.job_type!r}",
                retryable=False,
            )
            return

        try:
            result = handler(job)
            if not inspect.isawaitable(result):
                raise TypeError(f"handler for {job.job_type!r} must be async")
            await asyncio.wait_for(result, timeout=self.job_timeout_seconds)
        except asyncio.CancelledError:
            # Do not acknowledge cancellation. A replacement process will
            # recover the lease, preserving at-least-once execution.
            raise
        except Exception as exc:  # Handlers are the isolation boundary.
            logger.exception(
                "background job failed",
                extra={"job_id": str(job.id), "job_type": job.job_type},
            )
            await self._acknowledge_failure(job, exc, retryable=True)
            return

        async with self.session_factory() as session:
            async with session.begin():
                completed = await complete_job(
                    session,
                    job.id,
                    lease_owner=job.lease_owner,
                )
        if not completed:
            logger.warning(
                "background job completion ignored because its lease was lost",
                extra={"job_id": str(job.id), "job_type": job.job_type},
            )

    async def _acknowledge_failure(
        self,
        job: ClaimedJob,
        error: BaseException | str,
        *,
        retryable: bool,
    ) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                status = await fail_job(
                    session,
                    job.id,
                    error,
                    lease_owner=job.lease_owner,
                    retryable=retryable,
                )
        if status is None:
            logger.warning(
                "background job failure ignored because its lease was lost",
                extra={"job_id": str(job.id), "job_type": job.job_type},
            )

    async def _wait_or_stop(self, delay_seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay_seconds)
        except TimeoutError:
            pass


async def run_supervisor(
    handlers: Mapping[str, JobHandler],
    **kwargs: Any,
) -> None:
    """Convenience entry point for lifespan integrations and small scripts."""

    await JobSupervisor(handlers, **kwargs).run()


__all__ = [
    "ClaimedJob",
    "JobHandler",
    "JobSupervisor",
    "RecoveryResult",
    "claim_jobs",
    "complete_job",
    "enqueue_job",
    "fail_job",
    "recover_expired_leases",
    "run_supervisor",
]

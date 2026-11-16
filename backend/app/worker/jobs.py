"""Processing job tracking: enqueue_job(), the job_task decorator, cancellation
and recovery of jobs left running by a lost worker.

Every background job has a ProcessingJob row, written before the Celery
message is published. Status changes are conditional UPDATE statements
(`... WHERE id = :id AND status = :expected`), so a duplicate delivery, a
cancellation or a concurrent worker can never move a job backwards or
report it twice:

    queued -> running -> succeeded
                      -> failed
                      -> queued      (retryable failure, attempts left)
    queued | running  -> cancelled
    queued            -> failed      (the message could not be published)
    running           -> failed      (stale: the worker was lost)

Tasks receive a JobContext and read their validated JSON payload from it:

    @job_task(JobType.METADATA)
    def extract_metadata(ctx: JobContext) -> None:
        video_id = ctx.payload["video_id"]
        ...

    enqueue_job(JobType.METADATA, extract_metadata, db=db, user_id=user.id,
                video_id=video.id, payload={"video_id": str(video.id)},
                idempotency_key=str(video.id))
"""

import json
import logging
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from celery import Task
from celery.exceptions import Retry, SoftTimeLimitExceeded
from celery.signals import worker_ready
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import database
from app.core.request_id import get_request_id, is_valid_request_id
from app.models.processing_job import (
    ACTIVE_JOB_STATUSES,
    IDEMPOTENCY_KEY_MAX_LENGTH,
    JobStatus,
    JobType,
    ProcessingJob,
)
from app.worker.celery_app import FAIL_STALE_JOBS_TASK_NAME, celery_app

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 3
MAX_ATTEMPTS_LIMIT = 10
# Soft limit: SoftTimeLimitExceeded is raised inside the task, which marks
# the job failed. Hard limit: the worker process is killed; the job is then
# failed by fail_stale_jobs().
DEFAULT_SOFT_TIME_LIMIT_SECONDS = 600
DEFAULT_TIME_LIMIT_SECONDS = 660
MAX_TIME_LIMIT_SECONDS = 60 * 60
# A running job whose attempt started longer ago than the largest allowed
# hard time limit plus this grace period cannot still be running.
STALE_GRACE_SECONDS = 5 * 60
STALE_RUNNING_AFTER = timedelta(seconds=MAX_TIME_LIMIT_SECONDS + STALE_GRACE_SECONDS)
# Retry delay: RETRY_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), capped.
RETRY_BACKOFF_BASE_SECONDS = 10
RETRY_BACKOFF_MAX_SECONDS = 10 * 60

MAX_PAYLOAD_BYTES = 16 * 1024
ERROR_MESSAGE_MAX_LENGTH = 500

DEFAULT_FAILURE_MESSAGE = "The job failed."
TIMEOUT_MESSAGE = "The job timed out."
DISPATCH_FAILURE_MESSAGE = "The job could not be queued. Try again later."
RETRY_DISPATCH_FAILURE_MESSAGE = "The job failed and its retry could not be queued."
STALE_JOB_MESSAGE = (
    "The job was interrupted before it finished (for example by a worker "
    "restart)."
)

# user:password@ in URLs, so credentials never reach error_message.
_URL_CREDENTIALS = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^\s/@]+@")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class JobError(Exception):
    """A job failure whose message is safe to show to the job's owner.

    Raise it from a task for expected failures (for example "The video has no
    audio track."). Any other exception is recorded only by its type name.
    """

    retryable = False

    def __init__(self, public_message: str) -> None:
        super().__init__(public_message)
        self.public_message = public_message


class RetryableJobError(JobError):
    """A temporary failure; the job is retried with backoff while attempts remain."""

    retryable = True


class InvalidJobPayloadError(ValueError):
    """The job payload is not a small JSON object."""


class JobDispatchError(Exception):
    """The job row was written but its message could not be published.

    The job is marked failed (with a safe message) and its idempotency key is
    released, so the operation can be queued again.
    """

    def __init__(self, job_id: uuid.UUID) -> None:
        super().__init__(f"Job {job_id} could not be queued.")
        self.job_id = job_id


class BrokerNotConfiguredError(RuntimeError):
    """REDIS_URL is not set, so jobs cannot be published."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    # SQLite returns naive datetimes; they are stored in UTC.
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _milliseconds(start: datetime | None, end: datetime | None) -> int | None:
    start, end = _as_utc(start), _as_utc(end)
    if start is None or end is None:
        return None
    return max(0, round((end - start).total_seconds() * 1000))


def clean_error_text(message: str) -> str:
    """Return `message` without URL credentials or control characters, truncated."""
    message = _URL_CREDENTIALS.sub(r"\1<redacted>@", message)
    message = _CONTROL_CHARACTERS.sub(" ", message)
    message = " ".join(message.split())
    if not message:
        return DEFAULT_FAILURE_MESSAGE
    if len(message) > ERROR_MESSAGE_MAX_LENGTH:
        message = message[: ERROR_MESSAGE_MAX_LENGTH - 3].rstrip() + "..."
    return message


def sanitize_error_message(exc: BaseException) -> str:
    """Return a safe, short description of `exc` for `error_message`.

    Only JobError messages are kept (cleaned and truncated). Other exceptions
    can contain paths, SQL, URLs or secrets, so only their type is recorded.
    Stack traces are never included.
    """
    if isinstance(exc, SoftTimeLimitExceeded):
        return TIMEOUT_MESSAGE
    if isinstance(exc, JobError):
        return clean_error_text(str(exc.public_message))
    return f"The job failed unexpectedly ({type(exc).__name__})."


def validate_job_payload(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return the payload as a plain JSON object or raise InvalidJobPayloadError.

    The payload must be a mapping with string keys, serializable as strict
    JSON (no NaN or infinity) and at most MAX_PAYLOAD_BYTES once encoded.
    """
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise InvalidJobPayloadError("The job payload must be a JSON object.")
    if not all(isinstance(key, str) for key in payload):
        raise InvalidJobPayloadError("Job payload keys must be strings.")
    try:
        encoded = json.dumps(dict(payload), allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        raise InvalidJobPayloadError("The job payload must be JSON serializable.") from None
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise InvalidJobPayloadError(
            f"The job payload must be at most {MAX_PAYLOAD_BYTES} bytes."
        )
    # A JSON round trip so the task sees exactly what is stored.
    return json.loads(encoded)


def retry_backoff_seconds(attempt: int) -> int:
    """Delay before retrying after failed attempt number `attempt` (1-based)."""
    exponent = max(0, attempt - 1)
    if exponent >= 32:
        return RETRY_BACKOFF_MAX_SECONDS
    return min(RETRY_BACKOFF_BASE_SECONDS * 2**exponent, RETRY_BACKOFF_MAX_SECONDS)


def _log_extra(job: "ProcessingJob | JobContext", **extra: Any) -> dict[str, Any]:
    job_type = job.job_type.value if isinstance(job.job_type, JobType) else job.job_type
    return {
        "job_id": str(job.id if isinstance(job, ProcessingJob) else job.job_id),
        "job_type": job_type,
        "request_id": job.request_id,
        **extra,
    }


def _update_job(
    db: Session,
    job_id: uuid.UUID,
    from_statuses: frozenset[JobStatus] | set[JobStatus],
    **values: Any,
) -> bool:
    """Apply `values` to the job only if it is in one of `from_statuses`.

    The check and the change are a single UPDATE, so they are atomic. Commits
    and returns whether the job was changed.
    """
    result = db.execute(
        update(ProcessingJob)
        .where(
            ProcessingJob.id == job_id,
            ProcessingJob.status.in_(sorted(from_statuses, key=lambda s: s.value)),
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def _job_session() -> Session:
    return database.get_session_factory()()


# Enqueueing


def _active_job(db: Session, active_key: str) -> ProcessingJob | None:
    return db.scalars(
        select(ProcessingJob).where(ProcessingJob.active_key == active_key)
    ).one_or_none()


def _ensure_broker_configured() -> None:
    if not celery_app.conf.task_always_eager and not celery_app.conf.broker_url:
        raise BrokerNotConfiguredError(
            "REDIS_URL is not set; background jobs cannot be queued."
        )


def enqueue_job(
    job_type: JobType | str,
    task: Task,
    *,
    db: Session,
    user_id: uuid.UUID | None = None,
    video_id: uuid.UUID | None = None,
    payload: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
    request_id: str | None = None,
) -> ProcessingJob:
    """Create a queued job and dispatch `task` for it.

    `task` must be a job_task for `job_type`. The payload is validated (see
    validate_job_payload) and stored on the job. `idempotency_key` identifies
    the operation (for example the video id): while a job of the same type
    with the same key is queued or running, that job is returned and nothing
    new is queued. `request_id` defaults to the current API request's ID.

    The row is committed before the message is published, and the Celery
    task id is stored with it, so a worker always finds its job. If
    publishing fails the job is marked failed, its key is released and
    JobDispatchError is raised; nothing is left queued forever.

    Raises ValueError for an unknown job type, a task of another type, or an
    invalid payload or key.
    """
    job_type = JobType(job_type)
    if getattr(task, "job_type", None) != job_type:
        raise ValueError(f"The task does not handle {job_type.value!r} jobs.")
    clean_payload = validate_job_payload(payload)
    if idempotency_key is not None:
        idempotency_key = idempotency_key.strip()
        if not idempotency_key or len(idempotency_key) > IDEMPOTENCY_KEY_MAX_LENGTH:
            raise ValueError(
                "The idempotency key must be 1 to "
                f"{IDEMPOTENCY_KEY_MAX_LENGTH} characters."
            )
    active_key = f"{job_type.value}:{idempotency_key}" if idempotency_key else None
    if request_id is None:
        request_id = get_request_id()
    if not is_valid_request_id(request_id):
        request_id = None

    if active_key is not None:
        existing = _active_job(db, active_key)
        if existing is not None:
            logger.info("job_deduplicated", extra=_log_extra(existing))
            return existing

    job_id = uuid.uuid4()
    celery_task_id = str(uuid.uuid4())
    log_extra = {
        "job_id": str(job_id),
        "job_type": job_type.value,
        "request_id": request_id,
    }
    job = ProcessingJob(
        id=job_id,
        user_id=user_id,
        video_id=video_id,
        job_type=job_type,
        status=JobStatus.QUEUED,
        idempotency_key=idempotency_key,
        active_key=active_key,
        celery_task_id=celery_task_id,
        attempts=0,
        max_attempts=task.job_max_attempts,
        payload=clean_payload,
        request_id=request_id,
        created_at=_utcnow(),
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        # Another request queued the same operation concurrently.
        db.rollback()
        existing = _active_job(db, active_key) if active_key is not None else None
        if existing is None:
            raise
        logger.info("job_deduplicated", extra=_log_extra(existing))
        return existing

    logger.info("job_queued", extra=log_extra)

    try:
        _ensure_broker_configured()
        task.apply_async(args=[str(job_id)], task_id=celery_task_id)
    except Exception as exc:
        db.rollback()
        _update_job(
            db,
            job_id,
            {JobStatus.QUEUED},
            status=JobStatus.FAILED,
            error_message=DISPATCH_FAILURE_MESSAGE,
            finished_at=_utcnow(),
            active_key=None,
        )
        logger.error(
            "job_dispatch_failed",
            extra={**log_extra, "error_type": type(exc).__name__},
        )
        db.refresh(job)
        raise JobDispatchError(job_id) from exc

    # An eager task has already run and changed the row.
    db.refresh(job)
    return job


# Running


@dataclass(frozen=True)
class JobContext:
    """What a job_task function receives about the job it runs."""

    job_id: uuid.UUID
    job_type: JobType
    user_id: uuid.UUID | None
    video_id: uuid.UUID | None
    attempt: int
    max_attempts: int
    request_id: str | None
    payload: dict[str, Any] = field(default_factory=dict)

    def is_cancelled(self) -> bool:
        """Return True if the job was cancelled; long tasks should check this."""
        with _job_session() as db:
            status = db.scalar(
                select(ProcessingJob.status).where(ProcessingJob.id == self.job_id)
            )
        return status == JobStatus.CANCELLED


class JobTask(Task):
    """Base class of tasks created by job_task."""

    job_type: JobType
    job_max_attempts: int = DEFAULT_MAX_ATTEMPTS


def _claim(job_id: uuid.UUID) -> tuple[JobContext, datetime | None, datetime] | None:
    """Move a queued job to running and return its context.

    Returns None when the job is unknown or not queued (already running,
    finished or cancelled), so a duplicate or late message does nothing.
    """
    started_at = _utcnow()
    with _job_session() as db:
        claimed = _update_job(
            db,
            job_id,
            {JobStatus.QUEUED},
            status=JobStatus.RUNNING,
            started_at=started_at,
            finished_at=None,
            attempts=ProcessingJob.attempts + 1,
        )
        if not claimed:
            return None
        job = db.get(ProcessingJob, job_id)
        context = JobContext(
            job_id=job.id,
            job_type=JobType(job.job_type),
            user_id=job.user_id,
            video_id=job.video_id,
            attempt=job.attempts,
            max_attempts=job.max_attempts,
            request_id=job.request_id,
            payload=dict(job.payload or {}),
        )
        return context, job.created_at, started_at


def _run_job(task: JobTask, raw_job_id: Any, function: Callable[[JobContext], Any]) -> Any:
    try:
        job_id = uuid.UUID(str(raw_job_id))
    except ValueError:
        logger.error("job_invalid_id", extra={"task": task.name})
        return None

    claimed = _claim(job_id)
    if claimed is None:
        logger.info("job_skipped", extra={"job_id": str(job_id), "task": task.name})
        return None
    context, queued_at, started_at = claimed
    logger.info(
        "job_started",
        extra=_log_extra(
            context,
            attempt=context.attempt,
            queue_wait_ms=_milliseconds(queued_at, started_at),
        ),
    )

    try:
        result = function(context)
    except Exception as exc:
        return _handle_failure(task, context, started_at, exc)

    with _job_session() as db:
        finished = _update_job(
            db,
            job_id,
            {JobStatus.RUNNING},
            status=JobStatus.SUCCEEDED,
            finished_at=_utcnow(),
            error_message=None,
            active_key=None,
        )
    if finished:
        logger.info(
            "job_succeeded",
            extra=_log_extra(
                context,
                attempt=context.attempt,
                duration_ms=_milliseconds(started_at, _utcnow()),
            ),
        )
    else:
        # Cancelled (or failed as stale) while running: keep that status.
        logger.info("job_result_discarded", extra=_log_extra(context))
    return result


def _handle_failure(
    task: JobTask, context: JobContext, started_at: datetime, exc: Exception
) -> Any:
    message = sanitize_error_message(exc)
    duration_ms = _milliseconds(started_at, _utcnow())
    error_type = type(exc).__name__

    if isinstance(exc, JobError) and exc.retryable and context.attempt < context.max_attempts:
        with _job_session() as db:
            requeued = _update_job(
                db,
                context.job_id,
                {JobStatus.RUNNING},
                status=JobStatus.QUEUED,
                error_message=message,
            )
        if not requeued:
            logger.info("job_result_discarded", extra=_log_extra(context))
            raise exc
        countdown = retry_backoff_seconds(context.attempt)
        logger.warning(
            "job_retry_scheduled",
            extra=_log_extra(
                context,
                attempt=context.attempt,
                error_type=error_type,
                retry_in_seconds=countdown,
                duration_ms=duration_ms,
            ),
        )
        try:
            # Celery's own retry counter is bounded too; the job row's
            # attempts are the source of truth.
            raise task.retry(
                exc=exc, countdown=countdown, max_retries=context.max_attempts
            )
        except Retry:
            raise
        except Exception:
            message = RETRY_DISPATCH_FAILURE_MESSAGE
            with _job_session() as db:
                _update_job(
                    db,
                    context.job_id,
                    {JobStatus.QUEUED},
                    status=JobStatus.FAILED,
                    error_message=message,
                    finished_at=_utcnow(),
                    active_key=None,
                )
            logger.error(
                "job_failed",
                extra=_log_extra(context, attempt=context.attempt, error_type=error_type),
            )
            raise exc from None

    with _job_session() as db:
        failed = _update_job(
            db,
            context.job_id,
            {JobStatus.RUNNING},
            status=JobStatus.FAILED,
            error_message=message,
            finished_at=_utcnow(),
            active_key=None,
        )
    if failed:
        logger.warning(
            "job_failed",
            extra=_log_extra(
                context,
                attempt=context.attempt,
                error_type=error_type,
                duration_ms=duration_ms,
            ),
        )
    else:
        logger.info("job_result_discarded", extra=_log_extra(context))
    # Re-raised so Celery records the task as failed as well; the traceback
    # stays in the worker, never in error_message.
    raise exc


def job_task(
    job_type: JobType | str,
    *,
    name: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    soft_time_limit: int = DEFAULT_SOFT_TIME_LIMIT_SECONDS,
    time_limit: int = DEFAULT_TIME_LIMIT_SECONDS,
) -> Callable[[Callable[[JobContext], Any]], JobTask]:
    """Register `function(context)` as a Celery task that tracks its job.

    The task marks the job running (incrementing attempts) when it starts,
    succeeded when the function returns, and failed with a sanitized message
    when it raises. A RetryableJobError is retried with exponential backoff
    (retry_backoff_seconds) until `max_attempts` attempts have been made.
    `soft_time_limit` raises inside the task (the job fails as timed out);
    `time_limit` kills the worker process.

    Retries run the same function again with the same job and payload, so
    the function must be idempotent: write results keyed by the job (or the
    operation) and overwrite them rather than appending.
    """
    job_type = JobType(job_type)
    if not 1 <= max_attempts <= MAX_ATTEMPTS_LIMIT:
        raise ValueError(f"max_attempts must be between 1 and {MAX_ATTEMPTS_LIMIT}.")
    if not 0 < soft_time_limit < time_limit <= MAX_TIME_LIMIT_SECONDS:
        raise ValueError(
            "Time limits must satisfy 0 < soft_time_limit < time_limit <= "
            f"{MAX_TIME_LIMIT_SECONDS}."
        )

    def decorator(function: Callable[[JobContext], Any]) -> JobTask:
        task_name = name or f"{function.__module__}.{function.__qualname__}"

        def run(self: JobTask, job_id: str) -> Any:
            return _run_job(self, job_id, function)

        run.__name__ = function.__name__
        run.__doc__ = function.__doc__
        return celery_app.task(
            bind=True,
            base=JobTask,
            name=task_name,
            soft_time_limit=soft_time_limit,
            time_limit=time_limit,
            acks_late=True,
            job_type=job_type,
            job_max_attempts=max_attempts,
        )(run)

    return decorator


# Cancellation and recovery


def cancel_job(
    db: Session, job_id: uuid.UUID, *, user_id: uuid.UUID | None = None
) -> bool:
    """Cancel a queued or running job; return whether it was cancelled.

    With `user_id`, only that user's job is cancelled. A queued job's message
    is skipped when it is delivered; a running task is not interrupted, but
    its result is discarded and it can stop early via JobContext.is_cancelled().
    """
    statement = (
        update(ProcessingJob)
        .where(
            ProcessingJob.id == job_id,
            ProcessingJob.status.in_(sorted(ACTIVE_JOB_STATUSES, key=lambda s: s.value)),
        )
        .values(
            status=JobStatus.CANCELLED,
            finished_at=_utcnow(),
            active_key=None,
        )
        .execution_options(synchronize_session=False)
    )
    if user_id is not None:
        statement = statement.where(ProcessingJob.user_id == user_id)
    cancelled = db.execute(statement).rowcount == 1
    db.commit()
    if cancelled:
        logger.info("job_cancelled", extra={"job_id": str(job_id)})
    return cancelled


def fail_stale_jobs(db: Session, *, now: datetime | None = None) -> int:
    """Mark running jobs that cannot still be running as failed.

    A job is stale when its attempt started more than STALE_RUNNING_AFTER ago
    (the largest hard time limit plus a grace period), which only happens
    when the worker running it was lost (killed, restarted, out of memory).
    Returns the number of jobs failed.
    """
    if now is None:
        now = _utcnow()
    cutoff = now - STALE_RUNNING_AFTER
    stale_ids = list(
        db.scalars(
            select(ProcessingJob.id).where(
                ProcessingJob.status == JobStatus.RUNNING,
                ProcessingJob.started_at < cutoff,
            )
        )
    )
    failed = 0
    for job_id in stale_ids:
        # Re-checked atomically in case the job finished meanwhile.
        result = db.execute(
            update(ProcessingJob)
            .where(
                ProcessingJob.id == job_id,
                ProcessingJob.status == JobStatus.RUNNING,
                ProcessingJob.started_at < cutoff,
            )
            .values(
                status=JobStatus.FAILED,
                error_message=STALE_JOB_MESSAGE,
                finished_at=now,
                active_key=None,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            failed += 1
            logger.warning("job_marked_stale", extra={"job_id": str(job_id)})
    db.commit()
    return failed


@celery_app.task(name=FAIL_STALE_JOBS_TASK_NAME, soft_time_limit=60, time_limit=90)
def fail_stale_jobs_task() -> int:
    """Periodic (Celery beat) wrapper around fail_stale_jobs()."""
    with _job_session() as db:
        return fail_stale_jobs(db)


@worker_ready.connect
def _fail_stale_jobs_on_worker_start(**_: Any) -> None:
    # Jobs of a worker that was killed are failed when a worker starts again.
    try:
        with _job_session() as db:
            count = fail_stale_jobs(db)
    except Exception as exc:
        logger.error("stale_job_check_failed", extra={"error_type": type(exc).__name__})
        return
    if count:
        logger.warning("stale_jobs_failed", extra={"count": count})


__all__ = [
    "ACTIVE_JOB_STATUSES",
    "BrokerNotConfiguredError",
    "InvalidJobPayloadError",
    "JobContext",
    "JobDispatchError",
    "JobError",
    "JobTask",
    "RetryableJobError",
    "cancel_job",
    "enqueue_job",
    "fail_stale_jobs",
    "job_task",
    "retry_backoff_seconds",
    "sanitize_error_message",
    "validate_job_payload",
]

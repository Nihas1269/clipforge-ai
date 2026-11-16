"""Tests for app.worker.jobs.

Celery runs eagerly (APP_ENV=test) for the task lifecycle tests; dispatch and
broker behavior is tested with a mocked apply_async, so no Redis is needed.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from celery.exceptions import Reject, SoftTimeLimitExceeded
from kombu.exceptions import OperationalError
from sqlalchemy import func, select
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app import database
from app.core.config import Settings
from app.core.request_id import request_id_var
from app.database import Base, create_db_engine, create_session_factory
from app.models.processing_job import JobStatus, JobType, ProcessingJob
from app.models.user import User
from app.worker import jobs
from app.worker.celery_app import celery_app, configure_celery_app, create_celery_app
from app.worker.jobs import (
    DISPATCH_FAILURE_MESSAGE,
    ERROR_MESSAGE_MAX_LENGTH,
    RETRY_DISPATCH_FAILURE_MESSAGE,
    STALE_JOB_MESSAGE,
    TIMEOUT_MESSAGE,
    BrokerNotConfiguredError,
    InvalidJobPayloadError,
    JobContext,
    JobDispatchError,
    JobError,
    JobTask,
    RetryableJobError,
    cancel_job,
    enqueue_job,
    fail_stale_jobs,
    job_task,
    retry_backoff_seconds,
    sanitize_error_message,
)

SECRET_IN_ERROR = "hunter2-db-password"
PRIVATE_CONTENT = "private-transcript-text"

# What the dummy tasks observed and produced, reset for every test.
OBSERVED: list[dict] = []
OUTPUTS: dict[uuid.UUID, dict] = {}


def _read_job(job_id: uuid.UUID) -> ProcessingJob:
    with database.get_session_factory()() as session:
        return session.get(ProcessingJob, job_id)


@job_task(JobType.METADATA, name="tests.jobs.succeed")
def succeed(ctx: JobContext) -> str:
    job = _read_job(ctx.job_id)
    OBSERVED.append(
        {
            "status": job.status,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "attempt": ctx.attempt,
            "request_id": ctx.request_id,
            "payload": ctx.payload,
        }
    )
    # Keyed by job: running again overwrites instead of duplicating.
    OUTPUTS[ctx.job_id] = {"value": ctx.payload.get("value")}
    return "done"


@job_task(JobType.TRANSCRIPTION, name="tests.jobs.crash")
def crash(ctx: JobContext) -> None:
    raise RuntimeError(
        f"could not connect to postgresql://clipforge:{SECRET_IN_ERROR}@db/x\n"
        'Traceback (most recent call last):\n  File "/srv/app.py", line 1'
    )


@job_task(JobType.TRANSCRIPTION, name="tests.jobs.expected_failure")
def expected_failure(ctx: JobContext) -> None:
    raise JobError("The video has no audio track.")


@job_task(JobType.CLIP_DETECTION, name="tests.jobs.flaky", max_attempts=3)
def flaky(ctx: JobContext) -> None:
    OBSERVED.append({"attempt": ctx.attempt})
    OUTPUTS[ctx.job_id] = {"clips": [1, 2, 3], "attempt": ctx.attempt}
    if ctx.attempt == 1:
        raise RetryableJobError("The provider timed out.")


@job_task(JobType.CLIP_DETECTION, name="tests.jobs.always_temporary", max_attempts=2)
def always_temporary(ctx: JobContext) -> None:
    OBSERVED.append({"attempt": ctx.attempt})
    raise RetryableJobError("The provider timed out.")


@job_task(JobType.RENDER, name="tests.jobs.too_slow")
def too_slow(ctx: JobContext) -> None:
    raise SoftTimeLimitExceeded()


@job_task(JobType.RENDER, name="tests.jobs.cancelled_while_running")
def cancelled_while_running(ctx: JobContext) -> str:
    with database.get_session_factory()() as session:
        cancel_job(session, ctx.job_id)
    OBSERVED.append({"is_cancelled": ctx.is_cancelled()})
    return "finished anyway"


@pytest.fixture
def session_factory():
    engine = create_db_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def job_environment(session_factory, monkeypatch):
    """Point the worker at the test database and run Celery eagerly."""
    monkeypatch.setattr(database, "_session_factory", session_factory)
    configure_celery_app(celery_app, Settings(APP_ENV="test"))
    OBSERVED.clear()
    OUTPUTS.clear()
    yield
    configure_celery_app(celery_app, Settings(APP_ENV="test"))


@pytest.fixture
def db(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def user(db) -> User:
    user = User(email="alice@example.com", hashed_password="x")
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def published(monkeypatch):
    """Use a real (non-eager) dispatch with a mocked broker publish."""
    calls: list[dict] = []

    def fake_apply_async(self, args=None, kwargs=None, task_id=None, **options):
        job = _read_job(uuid.UUID(args[0]))
        calls.append(
            {
                "task": self.name,
                "args": args,
                "task_id": task_id,
                "status_at_publish": job.status if job else None,
                "celery_task_id_at_publish": job.celery_task_id if job else None,
            }
        )

    monkeypatch.setitem(celery_app.conf, "task_always_eager", False)
    monkeypatch.setattr(JobTask, "apply_async", fake_apply_async)
    return calls


def _job_count(db) -> int:
    return db.scalar(select(func.count()).select_from(ProcessingJob))


# Lifecycle (eager)


def test_successful_job_moves_queued_running_succeeded_with_timestamps(db, user):
    job = enqueue_job(
        JobType.METADATA, succeed, db=db, user_id=user.id, payload={"value": 42}
    )

    assert len(OBSERVED) == 1
    during = OBSERVED[0]
    assert during["status"] == JobStatus.RUNNING
    assert during["started_at"] is not None
    assert during["finished_at"] is None
    assert during["attempt"] == 1
    assert during["payload"] == {"value": 42}

    assert job.status == JobStatus.SUCCEEDED
    assert job.user_id == user.id
    assert job.attempts == 1
    assert job.error_message is None
    assert job.celery_task_id
    assert job.active_key is None
    assert job.created_at <= job.started_at <= job.finished_at
    assert OUTPUTS == {job.id: {"value": 42}}


def test_failing_job_is_failed_with_sanitized_error(db, user):
    job = enqueue_job(JobType.TRANSCRIPTION, crash, db=db, user_id=user.id)

    assert job.status == JobStatus.FAILED
    assert job.error_message == "The job failed unexpectedly (RuntimeError)."
    assert SECRET_IN_ERROR not in job.error_message
    assert "Traceback" not in job.error_message
    # Unexpected errors are not retried.
    assert job.attempts == 1
    assert job.started_at is not None
    assert job.finished_at is not None
    assert job.active_key is None


def test_job_error_message_is_recorded(db, user):
    job = enqueue_job(JobType.TRANSCRIPTION, expected_failure, db=db, user_id=user.id)

    assert job.status == JobStatus.FAILED
    assert job.error_message == "The video has no audio track."


def test_failed_job_is_never_reported_as_succeeded_by_celery(db, user, published):
    job = enqueue_job(JobType.TRANSCRIPTION, crash, db=db, user_id=user.id)

    result = crash.apply(args=[str(job.id)])

    assert result.state == "FAILURE"
    assert isinstance(result.result, RuntimeError)
    assert _read_job(job.id).status == JobStatus.FAILED


def test_retryable_failure_is_retried_without_duplicate_output(db, user):
    job = enqueue_job(JobType.CLIP_DETECTION, flaky, db=db, user_id=user.id)

    assert [entry["attempt"] for entry in OBSERVED] == [1, 2]
    assert job.status == JobStatus.SUCCEEDED
    assert job.attempts == 2
    assert job.max_attempts == 3
    assert job.error_message is None
    # One result for the job, written by the successful attempt.
    assert OUTPUTS == {job.id: {"clips": [1, 2, 3], "attempt": 2}}


def test_retries_are_bounded_by_max_attempts(db, user):
    job = enqueue_job(JobType.CLIP_DETECTION, always_temporary, db=db, user_id=user.id)

    assert [entry["attempt"] for entry in OBSERVED] == [1, 2]
    assert job.status == JobStatus.FAILED
    assert job.attempts == 2
    assert job.error_message == "The provider timed out."
    assert job.finished_at is not None


def test_retry_that_cannot_be_queued_fails_the_job(db, user, monkeypatch):
    def broken_retry(self, *args, **kwargs):
        raise Reject(OperationalError("broker down"), requeue=False)

    monkeypatch.setattr(JobTask, "retry", broken_retry)

    job = enqueue_job(JobType.CLIP_DETECTION, always_temporary, db=db, user_id=user.id)

    assert len(OBSERVED) == 1
    assert job.status == JobStatus.FAILED
    assert job.error_message == RETRY_DISPATCH_FAILURE_MESSAGE
    assert job.active_key is None


def test_soft_time_limit_marks_job_failed_as_timed_out(db, user):
    job = enqueue_job(JobType.RENDER, too_slow, db=db, user_id=user.id)

    assert job.status == JobStatus.FAILED
    assert job.error_message == TIMEOUT_MESSAGE


def test_duplicate_delivery_does_not_run_a_finished_job_again(db, user):
    job = enqueue_job(
        JobType.METADATA, succeed, db=db, user_id=user.id, payload={"value": 1}
    )

    succeed.apply(args=[str(job.id)])

    assert len(OBSERVED) == 1
    assert len(OUTPUTS) == 1
    stored = _read_job(job.id)
    assert stored.status == JobStatus.SUCCEEDED
    assert stored.attempts == 1


def test_unknown_or_invalid_job_id_is_ignored(db):
    assert succeed.apply(args=[str(uuid.uuid4())]).result is None
    assert succeed.apply(args=["not-a-uuid"]).result is None
    assert OBSERVED == []


def test_tasks_have_timeouts_and_late_acknowledgement():
    assert succeed.soft_time_limit == jobs.DEFAULT_SOFT_TIME_LIMIT_SECONDS
    assert succeed.time_limit == jobs.DEFAULT_TIME_LIMIT_SECONDS
    assert succeed.acks_late is True


@pytest.mark.parametrize(
    "options",
    [
        {"max_attempts": 0},
        {"max_attempts": jobs.MAX_ATTEMPTS_LIMIT + 1},
        {"soft_time_limit": 0},
        {"soft_time_limit": 60, "time_limit": 60},
        {"soft_time_limit": 60, "time_limit": jobs.MAX_TIME_LIMIT_SECONDS + 1},
    ],
)
def test_job_task_rejects_unbounded_settings(options):
    with pytest.raises(ValueError):
        job_task(JobType.METADATA, name="tests.jobs.invalid", **options)


def test_retry_backoff_is_exponential_and_capped():
    assert [retry_backoff_seconds(attempt) for attempt in (1, 2, 3)] == [10, 20, 40]
    assert retry_backoff_seconds(10) == jobs.RETRY_BACKOFF_MAX_SECONDS
    assert retry_backoff_seconds(1000) == jobs.RETRY_BACKOFF_MAX_SECONDS


# Dispatch and broker (mocked)


def test_job_is_persisted_before_it_is_published(db, user, published):
    job = enqueue_job(JobType.METADATA, succeed, db=db, user_id=user.id)

    assert len(published) == 1
    call = published[0]
    assert call["task"] == "tests.jobs.succeed"
    assert call["args"] == [str(job.id)]
    assert call["task_id"] == job.celery_task_id
    assert call["status_at_publish"] == JobStatus.QUEUED
    assert call["celery_task_id_at_publish"] == job.celery_task_id
    assert job.status == JobStatus.QUEUED
    assert job.attempts == 0
    assert job.started_at is None


def test_dispatch_failure_fails_the_job_and_allows_requeue(db, user, monkeypatch):
    def broker_down(self, *args, **kwargs):
        raise OperationalError("Error 111 connecting to localhost:6379.")

    monkeypatch.setitem(celery_app.conf, "task_always_eager", False)
    monkeypatch.setattr(JobTask, "apply_async", broker_down)

    with pytest.raises(JobDispatchError) as excinfo:
        enqueue_job(
            JobType.METADATA, succeed, db=db, user_id=user.id, idempotency_key="video-1"
        )

    failed = _read_job(excinfo.value.job_id)
    assert failed.status == JobStatus.FAILED
    assert failed.error_message == DISPATCH_FAILURE_MESSAGE
    assert failed.finished_at is not None
    assert failed.active_key is None

    # The operation can be queued again once the broker is back.
    monkeypatch.setattr(JobTask, "apply_async", lambda self, *a, **k: None)
    retried = enqueue_job(
        JobType.METADATA, succeed, db=db, user_id=user.id, idempotency_key="video-1"
    )
    assert retried.id != failed.id
    assert retried.status == JobStatus.QUEUED


def test_missing_broker_configuration_fails_the_job(db, user, monkeypatch):
    monkeypatch.setitem(celery_app.conf, "task_always_eager", False)
    monkeypatch.setitem(celery_app.conf, "broker_url", None)

    with pytest.raises(JobDispatchError) as excinfo:
        enqueue_job(JobType.METADATA, succeed, db=db, user_id=user.id)

    assert isinstance(excinfo.value.__cause__, BrokerNotConfiguredError)
    assert _read_job(excinfo.value.job_id).status == JobStatus.FAILED


def test_active_job_for_the_same_operation_is_not_duplicated(db, user, published):
    first = enqueue_job(
        JobType.METADATA, succeed, db=db, user_id=user.id, idempotency_key="video-1"
    )
    second = enqueue_job(
        JobType.METADATA, succeed, db=db, user_id=user.id, idempotency_key="video-1"
    )
    other_type = enqueue_job(
        JobType.TRANSCRIPTION, crash, db=db, user_id=user.id, idempotency_key="video-1"
    )
    other_key = enqueue_job(
        JobType.METADATA, succeed, db=db, user_id=user.id, idempotency_key="video-2"
    )

    assert second.id == first.id
    assert other_type.id != first.id
    assert other_key.id != first.id
    assert len(published) == 3
    assert _job_count(db) == 3


def test_finished_job_does_not_block_the_same_operation(db, user):
    first = enqueue_job(
        JobType.METADATA, succeed, db=db, user_id=user.id, idempotency_key="video-1"
    )
    second = enqueue_job(
        JobType.METADATA, succeed, db=db, user_id=user.id, idempotency_key="video-1"
    )

    assert first.status == second.status == JobStatus.SUCCEEDED
    assert second.id != first.id


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "an", "object"],
        {1: "non-string key"},
        {"value": float("nan")},
        {"value": object()},
        {"value": "x" * (jobs.MAX_PAYLOAD_BYTES + 1)},
    ],
)
def test_invalid_payload_is_rejected_before_anything_is_stored(db, user, payload):
    with pytest.raises(InvalidJobPayloadError):
        enqueue_job(JobType.METADATA, succeed, db=db, user_id=user.id, payload=payload)

    assert _job_count(db) == 0
    assert OBSERVED == []


def test_enqueue_rejects_unknown_type_mismatched_task_and_bad_key(db, user):
    with pytest.raises(ValueError):
        enqueue_job("thumbnail", succeed, db=db, user_id=user.id)
    with pytest.raises(ValueError):
        enqueue_job(JobType.RENDER, succeed, db=db, user_id=user.id)
    with pytest.raises(ValueError):
        enqueue_job(JobType.METADATA, succeed, db=db, idempotency_key="  ")
    with pytest.raises(ValueError):
        enqueue_job(JobType.METADATA, succeed, db=db, idempotency_key="k" * 256)

    assert _job_count(db) == 0


# Cancellation


def test_cancelled_queued_job_is_not_run(db, user, published):
    job = enqueue_job(JobType.METADATA, succeed, db=db, user_id=user.id)

    assert cancel_job(db, job.id, user_id=user.id) is True
    # The message is delivered after the cancellation.
    succeed.apply(args=[str(job.id)])

    stored = _read_job(job.id)
    assert stored.status == JobStatus.CANCELLED
    assert stored.finished_at is not None
    assert stored.attempts == 0
    assert OBSERVED == []


def test_cancellation_is_scoped_to_the_owner_and_active_jobs(db, user, published):
    job = enqueue_job(JobType.METADATA, succeed, db=db, user_id=user.id)

    assert cancel_job(db, job.id, user_id=uuid.uuid4()) is False
    assert _read_job(job.id).status == JobStatus.QUEUED
    assert cancel_job(db, job.id, user_id=user.id) is True
    # Already finished.
    assert cancel_job(db, job.id, user_id=user.id) is False


def test_job_cancelled_while_running_stays_cancelled(db, user):
    job = enqueue_job(JobType.RENDER, cancelled_while_running, db=db, user_id=user.id)

    assert OBSERVED == [{"is_cancelled": True}]
    assert job.status == JobStatus.CANCELLED
    assert job.error_message is None


# Recovery after a lost worker


def _running_job(db, started_at: datetime) -> ProcessingJob:
    job = ProcessingJob(
        job_type=JobType.RENDER,
        status=JobStatus.RUNNING,
        attempts=1,
        max_attempts=3,
        active_key=f"render:{uuid.uuid4()}",
        started_at=started_at,
    )
    db.add(job)
    db.commit()
    return job


def test_job_interrupted_by_a_worker_restart_is_marked_failed(db):
    now = datetime.now(timezone.utc)
    stale = _running_job(db, now - jobs.STALE_RUNNING_AFTER - timedelta(minutes=1))
    recent = _running_job(db, now - timedelta(minutes=5))

    assert fail_stale_jobs(db, now=now) == 1

    stale_row = _read_job(stale.id)
    assert stale_row.status == JobStatus.FAILED
    assert stale_row.error_message == STALE_JOB_MESSAGE
    assert stale_row.finished_at is not None
    assert stale_row.active_key is None
    assert _read_job(recent.id).status == JobStatus.RUNNING

    # A redelivered message for the interrupted job does nothing.
    too_slow.apply(args=[str(stale.id)])
    assert _read_job(stale.id).status == JobStatus.FAILED


def test_running_job_is_not_claimed_twice(db):
    job = _running_job(db, datetime.now(timezone.utc))

    too_slow.apply(args=[str(job.id)])

    stored = _read_job(job.id)
    assert stored.status == JobStatus.RUNNING
    assert stored.attempts == 1


# Error sanitizing


def test_sanitize_keeps_job_error_messages_clean_and_short():
    message = sanitize_error_message(
        JobError(
            "Upload to https://user:s3cr3t@storage.example/x failed\n\tretry later"
            + "!" * 1000
        )
    )

    assert "s3cr3t" not in message
    assert "https://<redacted>@storage.example/x" in message
    assert "\n" not in message and "\t" not in message
    assert len(message) <= ERROR_MESSAGE_MAX_LENGTH
    assert message.endswith("...")


def test_sanitize_never_includes_unexpected_exception_text():
    message = sanitize_error_message(KeyError(SECRET_IN_ERROR))

    assert message == "The job failed unexpectedly (KeyError)."


def test_sanitize_blank_job_error_uses_generic_message():
    assert sanitize_error_message(JobError("   ")) == jobs.DEFAULT_FAILURE_MESSAGE


# Request ID and logging


def test_request_id_is_stored_on_the_job_and_logged(db, user, caplog):
    caplog.set_level(logging.INFO, logger="app.worker.jobs")
    token = request_id_var.set("req-1234")
    try:
        job = enqueue_job(JobType.METADATA, succeed, db=db, user_id=user.id)
    finally:
        request_id_var.reset(token)

    assert job.request_id == "req-1234"
    assert OBSERVED[0]["request_id"] == "req-1234"
    records = {
        record.getMessage(): record
        for record in caplog.records
        if record.name == "app.worker.jobs"
    }
    assert {"job_queued", "job_started", "job_succeeded"} <= set(records)
    for name in ("job_queued", "job_started", "job_succeeded"):
        assert records[name].request_id == "req-1234"
        assert records[name].job_id == str(job.id)
        assert records[name].job_type == "metadata"
    assert records["job_started"].queue_wait_ms >= 0
    assert records["job_succeeded"].duration_ms >= 0


def test_explicit_invalid_request_id_is_not_stored(db, user):
    job = enqueue_job(
        JobType.METADATA, succeed, db=db, user_id=user.id, request_id="bad id\n"
    )

    assert job.request_id is None


def test_job_logs_contain_no_payload_or_error_text(db, user, caplog):
    caplog.set_level(logging.DEBUG, logger="app.worker.jobs")

    enqueue_job(
        JobType.METADATA,
        succeed,
        db=db,
        user_id=user.id,
        payload={"text": PRIVATE_CONTENT},
    )
    enqueue_job(JobType.TRANSCRIPTION, crash, db=db, user_id=user.id)

    job_records = [
        record for record in caplog.records if record.name == "app.worker.jobs"
    ]
    assert any(record.getMessage() == "job_failed" for record in job_records)
    for record in job_records:
        text = repr(record.__dict__)
        assert PRIVATE_CONTENT not in text
        assert SECRET_IN_ERROR not in text


# Celery configuration


def test_test_environment_runs_tasks_eagerly_without_redis():
    app = create_celery_app(
        Settings(APP_ENV="test", REDIS_URL="redis://localhost:6379/0")
    )

    assert app.conf.task_always_eager is True
    assert app.conf.broker_url == "memory://"
    assert not app.conf.result_backend


def test_redis_url_is_broker_and_result_backend():
    app = create_celery_app(
        Settings(APP_ENV="development", REDIS_URL="redis://localhost:6379/0")
    )

    assert app.conf.task_always_eager is False
    assert app.conf.broker_url == "redis://localhost:6379/0"
    assert app.conf.result_backend == "redis://localhost:6379/0"
    assert app.conf.task_acks_late is True
    assert app.conf.accept_content == ["json"]
    assert app.conf.task_publish_retry_policy["max_retries"] == 3

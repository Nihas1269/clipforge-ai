"""The Celery application for background processing jobs.

The broker and result backend are `Settings.REDIS_URL`. With `APP_ENV=test`
tasks run eagerly in the calling process (`task_always_eager`), so tests need
no Redis. Start a worker from the backend/ directory:

    celery -A app.worker.celery_app worker --loglevel=INFO

See backend/README.md ("Background jobs").
"""

from celery import Celery

from app.core.config import Settings, get_settings

# Modules imported by the worker so their tasks are registered.
TASK_MODULES = (
    "app.worker.jobs",
    "app.worker.clip_detection",
    "app.worker.metadata",
    "app.worker.storage_cleanup",
    "app.worker.transcription",
)

# Name of the periodic task that fails jobs left running by a lost worker
# (see app.worker.jobs.fail_stale_jobs).
FAIL_STALE_JOBS_TASK_NAME = "app.worker.jobs.fail_stale_jobs"
FAIL_STALE_JOBS_INTERVAL_SECONDS = 300.0

# Name of the periodic task that retries stored-object deletions that failed
# (see app.services.storage_cleanup).
RETRY_STORAGE_DELETIONS_TASK_NAME = "app.worker.storage_cleanup.retry_storage_deletions"
RETRY_STORAGE_DELETIONS_INTERVAL_SECONDS = 600.0

# Connection and socket timeouts for Redis, so publishing a job never hangs
# a request when the broker is down.
BROKER_TIMEOUT_SECONDS = 5.0

# Publishing retries before enqueue_job() reports a dispatch failure.
PUBLISH_RETRY_POLICY = {
    "max_retries": 3,
    "interval_start": 0,
    "interval_step": 0.5,
    "interval_max": 2,
}

# Task results are only kept for monitoring; the job row is the source of
# truth.
RESULT_EXPIRES_SECONDS = 24 * 60 * 60


def configure_celery_app(app: Celery, settings: Settings) -> Celery:
    """Apply the ClipForge configuration for `settings` to `app`."""
    eager = settings.APP_ENV == "test"
    if eager:
        # Eager tasks never touch a broker or result backend.
        broker_url: str | None = "memory://"
        result_backend: str | None = None
    else:
        # When REDIS_URL is unset the broker stays unset and enqueue_job()
        # fails the job with a clear error instead of publishing.
        broker_url = settings.REDIS_URL
        result_backend = settings.REDIS_URL

    app.conf.update(
        broker_url=broker_url,
        result_backend=result_backend,
        task_always_eager=eager,
        # A failing eager task is recorded on its job and result instead of
        # raising out of enqueue_job().
        task_eager_propagates=False,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        # Acknowledge after the task finishes, so a message is redelivered if
        # the whole worker dies; one message at a time per worker process.
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        result_expires=RESULT_EXPIRES_SECONDS,
        broker_connection_timeout=BROKER_TIMEOUT_SECONDS,
        broker_connection_retry_on_startup=True,
        broker_transport_options={
            "socket_timeout": BROKER_TIMEOUT_SECONDS,
            "socket_connect_timeout": BROKER_TIMEOUT_SECONDS,
        },
        redis_socket_timeout=BROKER_TIMEOUT_SECONDS,
        redis_socket_connect_timeout=BROKER_TIMEOUT_SECONDS,
        task_publish_retry=True,
        task_publish_retry_policy=PUBLISH_RETRY_POLICY,
        imports=TASK_MODULES,
        beat_schedule={
            "fail-stale-jobs": {
                "task": FAIL_STALE_JOBS_TASK_NAME,
                "schedule": FAIL_STALE_JOBS_INTERVAL_SECONDS,
            },
            "retry-storage-deletions": {
                "task": RETRY_STORAGE_DELETIONS_TASK_NAME,
                "schedule": RETRY_STORAGE_DELETIONS_INTERVAL_SECONDS,
            },
        },
    )
    return app


def create_celery_app(settings: Settings | None = None) -> Celery:
    """Create the Celery application configured from `settings`."""
    if settings is None:
        settings = get_settings()
    return configure_celery_app(Celery("clipforge"), settings)


celery_app = create_celery_app()

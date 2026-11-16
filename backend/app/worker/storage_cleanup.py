"""Periodic retry of stored-object deletions that failed (Celery beat).

See app.services.storage_cleanup for the cleanup outbox itself.
"""

import logging

from app import database
from app.core.config import get_settings
from app.services.storage_cleanup import retry_pending_storage_deletions
from app.storage import StorageConfigurationError, get_storage
from app.worker.celery_app import RETRY_STORAGE_DELETIONS_TASK_NAME, celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name=RETRY_STORAGE_DELETIONS_TASK_NAME, soft_time_limit=240, time_limit=300
)
def retry_storage_deletions_task() -> int:
    """Retry a batch of pending storage deletions; return how many succeeded."""
    try:
        storage = get_storage(get_settings())
    except StorageConfigurationError:
        # Pending deletions stay recorded until storage is configured.
        logger.error("storage_deletion_retry_storage_unavailable")
        return 0
    with database.get_session_factory()() as db:
        return retry_pending_storage_deletions(db, storage)

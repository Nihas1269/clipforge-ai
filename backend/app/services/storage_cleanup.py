"""Durable deletion of stored objects (a transactional cleanup outbox).

Callers add a PendingStorageDeletion in the same transaction that removes the
database record pointing at an object, commit, and then call
delete_pending_object(). If the object cannot be deleted (storage outage,
timeout, crash in between), the row stays and retry_pending_storage_deletions()
tries again later; it runs periodically in the Celery worker (see
app.worker.storage_cleanup). Deleting a missing object succeeds, so retrying
an object that is already gone is harmless.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.storage_deletion import ERROR_TYPE_MAX_LENGTH, PendingStorageDeletion
from app.storage import StorageBackend

logger = logging.getLogger(__name__)

# Pending deletions handled per retry run.
DEFAULT_RETRY_BATCH_SIZE = 50
# From this many failed attempts on, each further failure is logged as an
# error so that a deletion that keeps failing is noticed.
ALERT_AFTER_ATTEMPTS = 10


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def delete_pending_object(
    db: Session, storage: StorageBackend, deletion: PendingStorageDeletion
) -> bool:
    """Try to delete the object of a committed pending deletion.

    On success the pending row is removed and True is returned. On failure
    the attempt is recorded on the row (which stays for a later retry) and
    False is returned. Never raises for a storage failure.
    """
    # Read before any commit or rollback can expire the row. The storage key
    # is never logged; the deletion id identifies it.
    deletion_id = str(deletion.id)
    attempts = deletion.attempts or 0
    key = deletion.storage_key

    try:
        storage.delete(key)
    except Exception as exc:
        attempts += 1
        error_type = type(exc).__name__[:ERROR_TYPE_MAX_LENGTH]
        try:
            deletion.attempts = attempts
            deletion.last_error_type = error_type
            deletion.last_attempt_at = _utcnow()
            db.commit()
        except Exception as record_exc:
            db.rollback()
            logger.error(
                "storage_deletion_attempt_not_recorded",
                extra={"deletion_id": deletion_id, "error_type": type(record_exc).__name__},
            )
        level = logging.ERROR if attempts >= ALERT_AFTER_ATTEMPTS else logging.WARNING
        logger.log(
            level,
            "storage_deletion_failed",
            extra={"deletion_id": deletion_id, "attempts": attempts, "error_type": error_type},
        )
        return False

    try:
        db.delete(deletion)
        db.commit()
    except Exception as exc:
        # The object is gone; the row is retried later, which is a no-op.
        db.rollback()
        logger.warning(
            "storage_deletion_not_cleared",
            extra={"deletion_id": deletion_id, "error_type": type(exc).__name__},
        )
    else:
        logger.info(
            "storage_deletion_completed",
            extra={"deletion_id": deletion_id, "attempts": attempts},
        )
    return True


def retry_pending_storage_deletions(
    db: Session, storage: StorageBackend, *, limit: int = DEFAULT_RETRY_BATCH_SIZE
) -> int:
    """Retry up to `limit` pending deletions; return how many succeeded.

    Deletions with the fewest failed attempts go first, so one object that
    keeps failing cannot starve the others.
    """
    if limit <= 0:
        raise ValueError("limit must be positive.")
    pending = db.scalars(
        select(PendingStorageDeletion)
        .order_by(
            PendingStorageDeletion.attempts,
            PendingStorageDeletion.created_at,
            PendingStorageDeletion.id,
        )
        .limit(limit)
    ).all()
    deleted = sum(1 for deletion in pending if delete_pending_object(db, storage, deletion))
    if pending:
        logger.info(
            "storage_deletion_retry_finished",
            extra={"attempted": len(pending), "deleted": deleted},
        )
    return deleted

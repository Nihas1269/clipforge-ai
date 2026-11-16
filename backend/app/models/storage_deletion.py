import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.video import STORAGE_KEY_MAX_LENGTH

ERROR_TYPE_MAX_LENGTH = 100


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PendingStorageDeletion(Base):
    """A stored object that must still be deleted (a cleanup outbox entry).

    The row is written in the same transaction that deletes the object's
    database record, so a failed or interrupted object deletion is never
    forgotten. It is removed once the object has been deleted; until then
    app.services.storage_cleanup retries it.
    """

    __tablename__ = "pending_storage_deletions"
    __table_args__ = (
        CheckConstraint(
            "attempts >= 0", name="ck_pending_storage_deletions_attempts_non_negative"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    storage_key: Mapped[str] = mapped_column(String(STORAGE_KEY_MAX_LENGTH))
    # Failed deletion attempts so far.
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # Exception type of the latest failure; never the exception text.
    last_error_type: Mapped[str | None] = mapped_column(String(ERROR_TYPE_MAX_LENGTH))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), index=True
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        # No storage key: it contains user and video ids.
        return f"PendingStorageDeletion(id={self.id!r}, attempts={self.attempts!r})"

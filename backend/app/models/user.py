import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, String, Uuid, false, func, true
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.database import Base

# Common upper bound: local part (64) + "@" + domain (255).
EMAIL_MAX_LENGTH = 320


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(email: str) -> str:
    """Return the email address stripped of surrounding whitespace and lowercased.

    Raises ValueError for values that are not a plausible email address.
    """
    if not isinstance(email, str):
        raise ValueError("Email must be a string.")
    normalized = email.strip().lower()
    local, separator, domain = normalized.rpartition("@")
    if not separator or not local or not domain:
        raise ValueError("Email must contain a local part and a domain.")
    if len(normalized) > EMAIL_MAX_LENGTH:
        raise ValueError(f"Email must be at most {EMAIL_MAX_LENGTH} characters.")
    if any(character.isspace() for character in normalized):
        raise ValueError("Email must not contain whitespace.")
    return normalized


class User(Base):
    """An account that can sign in to ClipForge."""

    __tablename__ = "users"
    __table_args__ = (
        # Emails are stored lowercase, so the unique index on `email` makes
        # uniqueness case-insensitive at the database level.
        CheckConstraint("email = lower(email)", name="ck_users_email_lowercase"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(
        String(EMAIL_MAX_LENGTH), unique=True, index=True
    )
    hashed_password: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    is_admin: Mapped[bool] = mapped_column(default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )

    @validates("email")
    def _normalize_email(self, key: str, value: str) -> str:
        return normalize_email(value)

    def __repr__(self) -> str:
        # Only the id: never include the password hash or personal data.
        return f"User(id={self.id!r})"

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    String,
    Uuid,
    false,
    func,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.database import Base

if TYPE_CHECKING:
    from app.models.video import Video

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
    # Set when the user confirms ownership of the email address.
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Copied into every access token as the `ver` claim. Incrementing it (for
    # example on a password reset) invalidates all previously issued tokens.
    token_version: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )

    # The database deletes a user's videos (ON DELETE CASCADE).
    videos: Mapped[list["Video"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    @validates("email")
    def _normalize_email(self, key: str, value: str) -> str:
        return normalize_email(value)

    def __repr__(self) -> str:
        # Only the id: never include the password hash or personal data.
        return f"User(id={self.id!r})"

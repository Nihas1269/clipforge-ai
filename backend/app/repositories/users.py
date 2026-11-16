import logging
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models.user import User, normalize_email

logger = logging.getLogger(__name__)


class DuplicateEmailError(Exception):
    """Raised when creating a user whose email is already registered."""

    def __init__(self) -> None:
        super().__init__("A user with this email already exists.")


def get_by_email(db: Session, email: str) -> User | None:
    """Return the user with the given email (case-insensitive), or None."""
    try:
        normalized = normalize_email(email)
    except ValueError:
        return None
    return db.scalars(select(User).where(User.email == normalized)).one_or_none()


def get_by_id(db: Session, user_id: uuid.UUID) -> User | None:
    """Return the user with the given id, or None."""
    return db.get(User, user_id)


def create_user(
    db: Session,
    *,
    email: str,
    password: str,
    full_name: str | None = None,
    is_admin: bool = False,
) -> User:
    """Create and commit a new user with a hashed password.

    The email is normalized to lowercase. Raises ValueError for an invalid
    email or empty password, and DuplicateEmailError if the email is taken.
    """
    normalized = normalize_email(email)
    if get_by_email(db, normalized) is not None:
        raise DuplicateEmailError()

    user = User(
        email=normalized,
        hashed_password=hash_password(password),
        full_name=full_name,
        is_admin=is_admin,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # Another transaction may have registered the same email between the
        # check above and the commit; the unique index rejects it.
        if get_by_email(db, normalized) is not None:
            raise DuplicateEmailError() from exc
        raise

    logger.info("user_created", extra={"user_id": str(user.id)})
    return user


def update_full_name(db: Session, user: User, full_name: str | None) -> User:
    """Set and commit the user's full name."""
    user.full_name = full_name
    db.commit()
    logger.info("user_profile_updated", extra={"user_id": str(user.id)})
    return user


def set_password(db: Session, user: User, new_password: str) -> User:
    """Hash and commit a new password.

    Increments the token version, so every access token issued before the
    change stops working. Raises ValueError for an empty password.
    """
    user.hashed_password = hash_password(new_password)
    user.token_version += 1
    db.commit()
    logger.info("user_password_changed", extra={"user_id": str(user.id)})
    return user

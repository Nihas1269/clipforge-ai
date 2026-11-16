"""Storage of single-use email verification and password reset tokens.

Only the SHA-256 hash of each token is stored. A token is valid while it is
unused and unexpired; consuming it marks it used in a conditional UPDATE, so
two concurrent requests with the same token cannot both succeed.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.security import (
    ONE_TIME_TOKEN_MAX_LENGTH,
    generate_one_time_token,
    hash_one_time_token,
)
from app.models.auth_token import AuthToken, AuthTokenPurpose
from app.models.user import User

logger = logging.getLogger(__name__)


class InvalidOneTimeTokenError(Exception):
    """Raised for a malformed, unknown, expired, used, or wrong-purpose token.

    The reason is deliberately not exposed so every failure looks the same.
    """

    def __init__(self) -> None:
        super().__init__("The token is invalid or has expired.")


def _utc(now: datetime | None) -> datetime:
    # Timestamps are always stored and compared in UTC (SQLite keeps only the
    # wall-clock time).
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return now.astimezone(timezone.utc)


def invalidate_tokens(
    db: Session,
    user_id: uuid.UUID,
    purpose: AuthTokenPurpose,
    *,
    now: datetime | None = None,
) -> None:
    """Mark every outstanding token of the purpose for the user as used.

    Does not commit; the caller commits as part of its transaction.
    """
    db.execute(
        update(AuthToken)
        .where(
            AuthToken.user_id == user_id,
            AuthToken.purpose == purpose.value,
            AuthToken.used_at.is_(None),
        )
        .values(used_at=_utc(now))
    )


def create_token(
    db: Session,
    *,
    user: User,
    purpose: AuthTokenPurpose,
    ttl: timedelta,
    now: datetime | None = None,
) -> str:
    """Create, commit, and return a new raw token for the user.

    Earlier outstanding tokens of the same purpose are invalidated, so only
    the most recently issued token works. The raw token is returned once and
    never stored.
    """
    if ttl <= timedelta(0):
        raise ValueError("Token lifetime must be positive.")
    now = _utc(now)
    raw_token = generate_one_time_token()

    invalidate_tokens(db, user.id, purpose, now=now)
    db.add(
        AuthToken(
            user_id=user.id,
            purpose=purpose.value,
            token_hash=hash_one_time_token(raw_token),
            created_at=now,
            expires_at=now + ttl,
        )
    )
    db.commit()

    logger.info(
        "auth_token_created",
        extra={"user_id": str(user.id), "purpose": purpose.value},
    )
    return raw_token


def consume_token(
    db: Session,
    raw_token: str,
    purpose: AuthTokenPurpose,
    *,
    now: datetime | None = None,
) -> User:
    """Mark a valid token as used and return its (active) user.

    Raises InvalidOneTimeTokenError if the token is malformed, unknown, of
    another purpose, expired, already used, or belongs to an inactive user.
    Does not commit: the caller applies the token's effect and commits both
    together, or rolls back and the token stays unused. On failure the
    session is rolled back, which also discards any other uncommitted work
    the caller has pending in it.
    """
    if (
        not isinstance(raw_token, str)
        or not raw_token
        or len(raw_token) > ONE_TIME_TOKEN_MAX_LENGTH
    ):
        raise InvalidOneTimeTokenError()
    now = _utc(now)

    token = db.scalars(
        select(AuthToken).where(
            AuthToken.token_hash == hash_one_time_token(raw_token),
            AuthToken.purpose == purpose.value,
        )
    ).one_or_none()
    if token is None:
        raise InvalidOneTimeTokenError()

    # Conditional update: only one request can move used_at from NULL.
    result = db.execute(
        update(AuthToken)
        .where(
            AuthToken.id == token.id,
            AuthToken.used_at.is_(None),
            AuthToken.expires_at > now,
        )
        .values(used_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        logger.info(
            "auth_token_rejected",
            extra={"token_id": str(token.id), "purpose": purpose.value},
        )
        raise InvalidOneTimeTokenError()

    user = db.get(User, token.user_id)
    if user is None or not user.is_active:
        db.rollback()
        raise InvalidOneTimeTokenError()
    return user

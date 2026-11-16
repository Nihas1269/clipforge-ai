"""Email verification and password reset primitives.

These functions issue, send, and redeem single-use tokens. The HTTP endpoints
and email templates that use them are part of CF-045. Every function takes
its email sender and rate limiters as arguments so callers (and tests) choose
the implementation.

- Raw tokens are only returned inside the email message; the database holds
  their SHA-256 hashes (see app/repositories/auth_tokens.py).
- `request_password_reset` behaves the same whether or not the email has an
  account, so callers can always return the same response.
- `reset_password` increments the user's token version, which invalidates
  every access token issued before the reset.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.rate_limit import InMemoryRateLimiter
from app.core.security import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH, hash_password
from app.models.auth_token import AuthTokenPurpose
from app.models.user import User, normalize_email
from app.repositories import auth_tokens
from app.repositories.users import get_by_email
from app.services.email import EmailMessage, EmailSender

logger = logging.getLogger(__name__)

EMAIL_VERIFICATION_TEMPLATE = "email_verification"
PASSWORD_RESET_TEMPLATE = "password_reset"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def send_email_verification(
    db: Session,
    user: User,
    *,
    email_sender: EmailSender,
    rate_limiter: InMemoryRateLimiter,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> bool:
    """Email a new verification token to the user.

    Returns False without sending if the email is already verified. Raises
    RateLimitExceededError when the user requested too many tokens.
    """
    rate_limiter.hit(f"email_verification_request:{user.id}")
    if user.email_verified_at is not None:
        return False
    settings = settings or get_settings()
    raw_token = auth_tokens.create_token(
        db,
        user=user,
        purpose=AuthTokenPurpose.EMAIL_VERIFICATION,
        ttl=timedelta(minutes=settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_MINUTES),
        now=now,
    )
    email_sender.send(
        EmailMessage(
            to=user.email,
            subject="Verify your ClipForge email address",
            template=EMAIL_VERIFICATION_TEMPLATE,
            context={"token": raw_token},
        )
    )
    return True


def verify_email(
    db: Session,
    raw_token: str,
    *,
    rate_limiter: InMemoryRateLimiter,
    rate_limit_key: str,
    now: datetime | None = None,
) -> User:
    """Redeem a verification token and mark the user's email as verified.

    `rate_limit_key` identifies the caller (for example the client address).
    Raises RateLimitExceededError or InvalidOneTimeTokenError.
    """
    rate_limiter.hit(f"email_verification_attempt:{rate_limit_key}")
    now = now or _utcnow()
    user = auth_tokens.consume_token(
        db, raw_token, AuthTokenPurpose.EMAIL_VERIFICATION, now=now
    )
    if user.email_verified_at is None:
        user.email_verified_at = now
    db.commit()
    logger.info("email_verified", extra={"user_id": str(user.id)})
    return user


def request_password_reset(
    db: Session,
    email: str,
    *,
    email_sender: EmailSender,
    rate_limiter: InMemoryRateLimiter,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> None:
    """Email a password reset token if the address belongs to an active user.

    Returns nothing and raises nothing for unknown, invalid, or inactive
    addresses, so the caller's response never reveals whether an account
    exists. The rate limit applies per address whether or not it exists.
    """
    try:
        key = normalize_email(email)
    except ValueError:
        return
    rate_limiter.hit(f"password_reset_request:{key}")

    user = get_by_email(db, key)
    if user is None or not user.is_active:
        logger.info("password_reset_requested_without_account")
        return

    settings = settings or get_settings()
    raw_token = auth_tokens.create_token(
        db,
        user=user,
        purpose=AuthTokenPurpose.PASSWORD_RESET,
        ttl=timedelta(minutes=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES),
        now=now,
    )
    email_sender.send(
        EmailMessage(
            to=user.email,
            subject="Reset your ClipForge password",
            template=PASSWORD_RESET_TEMPLATE,
            context={"token": raw_token},
        )
    )


def reset_password(
    db: Session,
    raw_token: str,
    new_password: str,
    *,
    rate_limiter: InMemoryRateLimiter,
    rate_limit_key: str,
    now: datetime | None = None,
) -> User:
    """Redeem a password reset token and set a new password.

    Invalidates every outstanding reset token and every access token issued
    to the user before the reset. Raises ValueError for an unacceptable new
    password (without using up the token), RateLimitExceededError, or
    InvalidOneTimeTokenError.
    """
    if (
        not isinstance(new_password, str)
        or not PASSWORD_MIN_LENGTH <= len(new_password) <= PASSWORD_MAX_LENGTH
    ):
        raise ValueError(
            f"Password must be {PASSWORD_MIN_LENGTH} to {PASSWORD_MAX_LENGTH} "
            "characters long."
        )
    rate_limiter.hit(f"password_reset_attempt:{rate_limit_key}")
    now = now or _utcnow()

    user = auth_tokens.consume_token(
        db, raw_token, AuthTokenPurpose.PASSWORD_RESET, now=now
    )
    user.hashed_password = hash_password(new_password)
    user.token_version += 1
    auth_tokens.invalidate_tokens(db, user.id, AuthTokenPurpose.PASSWORD_RESET, now=now)
    db.commit()
    logger.info("password_reset_completed", extra={"user_id": str(user.id)})
    return user

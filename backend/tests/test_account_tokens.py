import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.core.config import Settings
from app.core.rate_limit import InMemoryRateLimiter, RateLimitExceededError
from app.core.security import hash_one_time_token, verify_password
from app.database import Base, create_db_engine, create_session_factory
from app.models.auth_token import AuthToken, AuthTokenPurpose
from app.repositories import auth_tokens
from app.repositories.auth_tokens import InvalidOneTimeTokenError
from app.repositories.users import create_user
from app.schemas.user import UserPublic
from app.services import account_tokens
from app.services.email import FakeEmailSender

SQLITE_IN_MEMORY_URL = "sqlite:///:memory:"
PASSWORD = "s3cure-Passw0rd"
NEW_PASSWORD = "an0ther-Passw0rd"
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
SETTINGS = Settings(
    EMAIL_VERIFICATION_TOKEN_EXPIRE_MINUTES=60,
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES=15,
)


@pytest.fixture
def db():
    engine = create_db_engine(
        SQLITE_IN_MEMORY_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = create_session_factory(engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def user(db):
    return create_user(db, email="alice@example.com", password=PASSWORD)


@pytest.fixture
def sender():
    return FakeEmailSender()


@pytest.fixture
def limiter():
    return InMemoryRateLimiter(100, 60)


def _send_verification(db, user, sender, limiter, now=NOW):
    sent = account_tokens.send_email_verification(
        db, user, email_sender=sender, rate_limiter=limiter, settings=SETTINGS, now=now
    )
    assert sent is True
    return sender.outbox[-1].context["token"]


def _request_reset(db, sender, limiter, email="alice@example.com", now=NOW):
    account_tokens.request_password_reset(
        db, email, email_sender=sender, rate_limiter=limiter, settings=SETTINGS, now=now
    )


def _verify(db, token, limiter, now=NOW):
    return account_tokens.verify_email(
        db, token, rate_limiter=limiter, rate_limit_key="client", now=now
    )


def _reset(db, token, limiter, password=NEW_PASSWORD, now=NOW):
    return account_tokens.reset_password(
        db, token, password, rate_limiter=limiter, rate_limit_key="client", now=now
    )


def _token_count(db) -> int:
    return db.scalar(select(func.count()).select_from(AuthToken))


# Email verification


def test_new_user_email_is_not_verified(user):
    assert user.email_verified_at is None
    assert UserPublic.from_user(user).email_verified is False


def test_verification_email_contains_token_and_only_its_hash_is_stored(
    db, user, sender, limiter
):
    token = _send_verification(db, user, sender, limiter)

    message = sender.outbox[0]
    assert message.to == "alice@example.com"
    assert message.template == "email_verification"
    stored = db.scalars(select(AuthToken)).one()
    assert stored.purpose == AuthTokenPurpose.EMAIL_VERIFICATION.value
    assert stored.token_hash == hash_one_time_token(token)
    assert token not in stored.token_hash
    assert token not in repr(stored)
    assert stored.used_at is None


def test_verify_email_marks_user_verified(db, user, sender, limiter):
    token = _send_verification(db, user, sender, limiter)

    verified = _verify(db, token, limiter, now=NOW + timedelta(minutes=5))

    assert verified.id == user.id
    assert verified.email_verified_at is not None
    assert UserPublic.from_user(verified).email_verified is True


def test_verification_token_cannot_be_reused(db, user, sender, limiter):
    token = _send_verification(db, user, sender, limiter)
    _verify(db, token, limiter)

    with pytest.raises(InvalidOneTimeTokenError):
        _verify(db, token, limiter)


def test_expired_verification_token_is_rejected(db, user, sender, limiter):
    token = _send_verification(db, user, sender, limiter)

    with pytest.raises(InvalidOneTimeTokenError):
        _verify(db, token, limiter, now=NOW + timedelta(minutes=60))

    db.refresh(user)
    assert user.email_verified_at is None


def test_verification_token_is_valid_until_expiry(db, user, sender, limiter):
    token = _send_verification(db, user, sender, limiter)

    _verify(db, token, limiter, now=NOW + timedelta(minutes=59, seconds=59))


@pytest.mark.parametrize("token", ["", "not-a-token", "x" * 500, None, 42])
def test_malformed_verification_token_is_rejected(db, user, sender, limiter, token):
    _send_verification(db, user, sender, limiter)

    with pytest.raises(InvalidOneTimeTokenError):
        _verify(db, token, limiter)


def test_unknown_verification_token_is_rejected(db, user, limiter):
    with pytest.raises(InvalidOneTimeTokenError):
        _verify(db, "A" * 43, limiter)


def test_new_verification_token_invalidates_previous_one(db, user, sender, limiter):
    first = _send_verification(db, user, sender, limiter)
    second = _send_verification(db, user, sender, limiter)

    with pytest.raises(InvalidOneTimeTokenError):
        _verify(db, first, limiter)
    _verify(db, second, limiter)


def test_no_verification_email_for_already_verified_user(db, user, sender, limiter):
    _verify(db, _send_verification(db, user, sender, limiter), limiter)

    sent = account_tokens.send_email_verification(
        db, user, email_sender=sender, rate_limiter=limiter, settings=SETTINGS
    )

    assert sent is False
    assert len(sender.outbox) == 1


def test_verification_token_does_not_work_for_password_reset(db, user, sender, limiter):
    token = _send_verification(db, user, sender, limiter)

    with pytest.raises(InvalidOneTimeTokenError):
        _reset(db, token, limiter)
    # The failed attempt did not use up the token.
    _verify(db, token, limiter)


def test_verification_requests_are_rate_limited(db, user, sender):
    limiter = InMemoryRateLimiter(2, 60)
    _send_verification(db, user, sender, limiter)
    _send_verification(db, user, sender, limiter)

    with pytest.raises(RateLimitExceededError):
        _send_verification(db, user, sender, limiter)
    assert len(sender.outbox) == 2


def test_verification_attempts_are_rate_limited(db, user, sender):
    token = _send_verification(db, user, sender, InMemoryRateLimiter(10, 60))
    limiter = InMemoryRateLimiter(2, 60)
    for _ in range(2):
        with pytest.raises(InvalidOneTimeTokenError):
            _verify(db, "A" * 43, limiter)

    with pytest.raises(RateLimitExceededError):
        _verify(db, token, limiter)


# Password reset


def test_password_reset_request_sends_token_for_known_email(db, user, sender, limiter):
    _request_reset(db, sender, limiter, email="ALICE@example.com")

    assert len(sender.outbox) == 1
    message = sender.outbox[0]
    assert message.to == "alice@example.com"
    assert message.template == "password_reset"
    stored = db.scalars(select(AuthToken)).one()
    assert stored.token_hash == hash_one_time_token(message.context["token"])


@pytest.mark.parametrize("email", ["nobody@example.com", "not-an-email", ""])
def test_password_reset_request_for_unknown_email_reveals_nothing(
    db, user, sender, limiter, email
):
    result = account_tokens.request_password_reset(
        db, email, email_sender=sender, rate_limiter=limiter, settings=SETTINGS
    )

    assert result is None
    assert sender.outbox == []
    assert _token_count(db) == 0


def test_password_reset_request_for_inactive_user_sends_nothing(
    db, user, sender, limiter
):
    user.is_active = False
    db.commit()

    _request_reset(db, sender, limiter)

    assert sender.outbox == []
    assert _token_count(db) == 0


def test_password_reset_changes_password_and_token_version(db, user, sender, limiter):
    _request_reset(db, sender, limiter)
    old_version = user.token_version

    updated = _reset(db, sender.outbox[0].context["token"], limiter)

    assert verify_password(NEW_PASSWORD, updated.hashed_password)
    assert not verify_password(PASSWORD, updated.hashed_password)
    assert updated.token_version == old_version + 1


def test_password_reset_token_cannot_be_reused(db, user, sender, limiter):
    _request_reset(db, sender, limiter)
    token = sender.outbox[0].context["token"]
    _reset(db, token, limiter)

    with pytest.raises(InvalidOneTimeTokenError):
        _reset(db, token, limiter, password="yet-another-Passw0rd")
    db.refresh(user)
    assert verify_password(NEW_PASSWORD, user.hashed_password)


def test_expired_password_reset_token_is_rejected(db, user, sender, limiter):
    _request_reset(db, sender, limiter)

    with pytest.raises(InvalidOneTimeTokenError):
        _reset(
            db,
            sender.outbox[0].context["token"],
            limiter,
            now=NOW + timedelta(minutes=15),
        )
    db.refresh(user)
    assert verify_password(PASSWORD, user.hashed_password)


@pytest.mark.parametrize("token", ["", "garbage", "y" * 129, None])
def test_malformed_password_reset_token_is_rejected(db, user, limiter, token):
    with pytest.raises(InvalidOneTimeTokenError):
        _reset(db, token, limiter)


def test_new_password_reset_request_invalidates_previous_token(
    db, user, sender, limiter
):
    _request_reset(db, sender, limiter)
    _request_reset(db, sender, limiter)
    first, second = (message.context["token"] for message in sender.outbox)

    with pytest.raises(InvalidOneTimeTokenError):
        _reset(db, first, limiter)
    _reset(db, second, limiter)


def test_short_new_password_does_not_use_up_token(db, user, sender, limiter):
    _request_reset(db, sender, limiter)
    token = sender.outbox[0].context["token"]

    with pytest.raises(ValueError):
        _reset(db, token, limiter, password="short")

    _reset(db, token, limiter)


def test_password_reset_token_of_deactivated_user_is_rejected(
    db, user, sender, limiter
):
    _request_reset(db, sender, limiter)
    user.is_active = False
    db.commit()

    with pytest.raises(InvalidOneTimeTokenError):
        _reset(db, sender.outbox[0].context["token"], limiter)


def test_password_reset_requests_are_rate_limited_for_any_email(db, user, sender):
    limiter = InMemoryRateLimiter(1, 60)
    _request_reset(db, sender, limiter)
    account_tokens.request_password_reset(
        db, "nobody@example.com", email_sender=sender, rate_limiter=limiter
    )

    # Known and unknown addresses hit the limit the same way.
    for email in ("alice@example.com", "nobody@example.com"):
        with pytest.raises(RateLimitExceededError):
            _request_reset(db, sender, limiter, email=email)
    assert len(sender.outbox) == 1


def test_password_reset_attempts_are_rate_limited(db, user, sender):
    _request_reset(db, sender, InMemoryRateLimiter(10, 60))
    limiter = InMemoryRateLimiter(1, 60)
    with pytest.raises(InvalidOneTimeTokenError):
        _reset(db, "A" * 43, limiter)

    with pytest.raises(RateLimitExceededError):
        _reset(db, sender.outbox[0].context["token"], limiter)


def test_failed_email_delivery_is_not_reported_as_success(db, user, limiter):
    class FailingSender:
        def send(self, message):
            raise ConnectionError("SMTP unavailable")

    with pytest.raises(ConnectionError):
        _request_reset(db, FailingSender(), limiter)


def test_raw_tokens_are_never_logged(db, user, sender, limiter, caplog):
    with caplog.at_level(logging.DEBUG):
        verification = _send_verification(db, user, sender, limiter)
        _verify(db, verification, limiter)
        _request_reset(db, sender, limiter)
        reset = sender.outbox[-1].context["token"]
        _reset(db, reset, limiter)

    assert verification not in caplog.text
    assert reset not in caplog.text
    assert NEW_PASSWORD not in caplog.text
    assert "alice@example.com" not in caplog.text


def test_create_token_rejects_non_positive_lifetime(db, user):
    with pytest.raises(ValueError):
        auth_tokens.create_token(
            db, user=user, purpose=AuthTokenPurpose.PASSWORD_RESET, ttl=timedelta(0)
        )


def test_token_timestamps_must_be_timezone_aware(db, user):
    with pytest.raises(ValueError):
        auth_tokens.create_token(
            db,
            user=user,
            purpose=AuthTokenPurpose.PASSWORD_RESET,
            ttl=timedelta(minutes=5),
            now=datetime(2026, 11, 16, 12, 0),
        )

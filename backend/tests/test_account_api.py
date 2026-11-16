"""Email verification and password reset endpoints (/api/v1/auth)."""

import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.api.deps import AccountRateLimiters
from app.core.rate_limit import InMemoryRateLimiter
from app.core.security import hash_one_time_token, verify_password
from app.database import Base, create_db_engine, create_session_factory, get_db
from app.main import create_app
from app.models.auth_token import AuthToken
from app.models.user import User
from app.services import account_tokens
from app.services.email import EmailDeliveryError, FakeEmailSender

SQLITE_IN_MEMORY_URL = "sqlite:///:memory:"
SECRET = "test-only-jwt-signing-secret-0123456789abcdef"
PASSWORD = "s3cure-Passw0rd"
NEW_PASSWORD = "a-brand-new-Passw0rd"
EMAIL = "alice@example.com"
SELF_ORIGIN = "http://testserver"
EVIL_ORIGIN = "http://evil.example"

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
ME_URL = "/api/v1/users/me"
VERIFY_REQUEST_URL = "/api/v1/auth/verify-email/request"
VERIFY_CONFIRM_URL = "/api/v1/auth/verify-email/confirm"
RESET_REQUEST_URL = "/api/v1/auth/password-reset/request"
RESET_CONFIRM_URL = "/api/v1/auth/password-reset/confirm"

EMAIL_ENV_VARS = (
    "EMAIL_PROVIDER",
    "EMAIL_FROM",
    "FRONTEND_URL",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_SECURITY",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_TIMEOUT_SECONDS",
    "PASSWORD_RESET_TOKEN_EXPIRE_MINUTES",
    "EMAIL_VERIFICATION_TOKEN_EXPIRE_MINUTES",
)

# Sentinel: let the app build its email sender from the settings.
FROM_SETTINGS = object()


class FailingEmailSender:
    def __init__(self) -> None:
        self.attempts = 0

    def send(self, message) -> None:
        self.attempts += 1
        raise EmailDeliveryError()


@pytest.fixture
def session_factory():
    engine = create_db_engine(
        SQLITE_IN_MEMORY_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


@pytest.fixture
def db(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def make_client(session_factory, monkeypatch):
    """Build a TestClient using the test database.

    By default the app gets a FakeEmailSender (reachable as
    `client.app.state.email_sender`); pass `email_sender=FROM_SETTINGS` to
    build it from the environment instead.
    """
    monkeypatch.setenv("SECRET_KEY", SECRET)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    for name in EMAIL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    def factory(email_sender=None, rate_limiters=None, **env: str) -> TestClient:
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        application = create_app()

        def override_get_db():
            session = session_factory()
            try:
                yield session
            finally:
                session.close()

        application.dependency_overrides[get_db] = override_get_db
        if email_sender is None:
            email_sender = FakeEmailSender()
        if email_sender is not FROM_SETTINGS:
            application.state.email_sender = email_sender
        if rate_limiters is not None:
            application.state.account_rate_limiters = rate_limiters
        return TestClient(application)

    return factory


@pytest.fixture
def client(make_client):
    return make_client()


def _outbox(client) -> list:
    return client.app.state.email_sender.outbox


def _register(client, email=EMAIL, password=PASSWORD):
    response = client.post(REGISTER_URL, json={"email": email, "password": password})
    assert response.status_code == 201
    return response


def _login(client, email=EMAIL, password=PASSWORD):
    return client.post(
        LOGIN_URL,
        json={"email": email, "password": password},
        headers={"Origin": SELF_ORIGIN},
    )


def _bearer(client, email=EMAIL, password=PASSWORD) -> dict[str, str]:
    response = _login(client, email, password)
    assert response.status_code == 200
    client.cookies.clear()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _user(db, email=EMAIL) -> User:
    db.expire_all()
    return db.scalars(select(User).where(User.email == email)).one()


def _confirm_verification(client, token):
    return client.post(VERIFY_CONFIRM_URL, json={"token": token})


def _request_reset(client, email=EMAIL, **kwargs):
    return client.post(RESET_REQUEST_URL, json={"email": email}, **kwargs)


def _confirm_reset(client, token, new_password=NEW_PASSWORD, **kwargs):
    return client.post(
        RESET_CONFIRM_URL, json={"token": token, "new_password": new_password}, **kwargs
    )


def _last_token(client, template: str) -> str:
    messages = [m for m in _outbox(client) if m.template == template]
    assert messages, f"no {template} email was sent"
    return messages[-1].context["token"]


# Email verification


def test_register_sends_verification_email_without_returning_token(client, db):
    response = _register(client)

    assert len(_outbox(client)) == 1
    message = _outbox(client)[0]
    assert message.to == EMAIL
    assert message.template == "email_verification"
    token = message.context["token"]
    assert token not in response.text
    assert response.json()["email_verified"] is False
    assert _user(db).email_verified_at is None
    # Only the hash is stored.
    stored = db.scalars(select(AuthToken)).one()
    assert stored.token_hash == hash_one_time_token(token)
    assert stored.token_hash != token


def test_valid_verification_token_marks_email_verified(client, db):
    _register(client)
    token = _last_token(client, "email_verification")

    response = _confirm_verification(client, token)

    assert response.status_code == 200
    assert response.json() == {"message": "Your email address has been verified."}
    assert token not in response.text
    assert _user(db).email_verified_at is not None
    me = client.get(ME_URL, headers=_bearer(client))
    assert me.json()["email_verified"] is True


def test_reused_verification_token_is_rejected(client):
    _register(client)
    token = _last_token(client, "email_verification")
    assert _confirm_verification(client, token).status_code == 200

    response = _confirm_verification(client, token)

    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": "invalid_token",
        "message": "The link is invalid or has expired.",
        "details": None,
    }


def test_expired_verification_token_is_rejected_and_user_stays_unverified(client, db):
    _register(client)
    sender = FakeEmailSender()
    account_tokens.send_email_verification(
        db,
        _user(db),
        email_sender=sender,
        rate_limiter=InMemoryRateLimiter(10, 60),
        now=datetime.now(timezone.utc) - timedelta(days=2),
    )

    response = _confirm_verification(client, sender.outbox[0].context["token"])

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_token"
    assert _user(db).email_verified_at is None


@pytest.mark.parametrize("token", ["not-a-real-token", "a" * 128, "!!!", " "])
def test_malformed_or_unknown_verification_token_returns_400(client, db, token):
    _register(client)

    response = _confirm_verification(client, token)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_token"
    assert _user(db).email_verified_at is None


@pytest.mark.parametrize(
    "body", [{}, {"token": ""}, {"token": "a" * 129}, {"token": 123}, {"token": "x", "extra": 1}]
)
def test_invalid_verification_body_returns_422(client, body):
    response = client.post(VERIFY_CONFIRM_URL, json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_password_reset_token_cannot_verify_email(client, db):
    _register(client)
    _request_reset(client)

    response = _confirm_verification(client, _last_token(client, "password_reset"))

    assert response.status_code == 400
    assert _user(db).email_verified_at is None


def test_verification_request_requires_authentication(client):
    _register(client)

    response = client.post(VERIFY_REQUEST_URL)

    assert response.status_code == 401
    assert len(_outbox(client)) == 1


def test_verification_request_sends_new_token_and_invalidates_old(client, db):
    _register(client)
    old_token = _last_token(client, "email_verification")
    headers = _bearer(client)

    response = client.post(VERIFY_REQUEST_URL, headers=headers)

    assert response.status_code == 202
    assert response.json() == {"message": "A verification email has been sent."}
    new_token = _last_token(client, "email_verification")
    assert new_token != old_token
    assert new_token not in response.text
    assert _confirm_verification(client, old_token).status_code == 400
    assert _confirm_verification(client, new_token).status_code == 200
    assert _user(db).email_verified_at is not None


def test_verification_request_only_acts_on_the_current_user(client, db):
    _register(client, email="alice@example.com")
    _register(client, email="bob@example.com")
    bob_headers = _bearer(client, email="bob@example.com")

    client.post(VERIFY_REQUEST_URL, headers=bob_headers)
    _confirm_verification(client, _last_token(client, "email_verification"))

    assert _outbox(client)[-1].to == "bob@example.com"
    assert _user(db, "bob@example.com").email_verified_at is not None
    assert _user(db, "alice@example.com").email_verified_at is None


def test_verification_request_for_verified_user_sends_nothing(client):
    _register(client)
    _confirm_verification(client, _last_token(client, "email_verification"))
    headers = _bearer(client)
    sent_before = len(_outbox(client))

    response = client.post(VERIFY_REQUEST_URL, headers=headers)

    assert response.status_code == 200
    assert response.json() == {"message": "Your email address is already verified."}
    assert len(_outbox(client)) == sent_before


def test_verification_request_reports_delivery_failure(make_client):
    sender = FailingEmailSender()
    client = make_client(email_sender=sender)
    _register(client)

    response = client.post(VERIFY_REQUEST_URL, headers=_bearer(client))

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "email_delivery_failed"
    # Registration tried once in the background, the request once more.
    assert sender.attempts == 2


def test_unconfigured_email_returns_503_but_registration_still_works(make_client):
    client = make_client(email_sender=FROM_SETTINGS, EMAIL_PROVIDER="smtp")

    register = client.post(REGISTER_URL, json={"email": EMAIL, "password": PASSWORD})
    request = client.post(VERIFY_REQUEST_URL, headers=_bearer(client))

    assert register.status_code == 201
    assert request.status_code == 503
    assert request.json()["error"]["code"] == "email_unavailable"


def test_fake_provider_is_refused_in_production(make_client):
    client = make_client(email_sender=FROM_SETTINGS, APP_ENV="production")
    _register(client)

    known = _request_reset(client)
    unknown = _request_reset(client, email="nobody@example.com")

    assert known.status_code == unknown.status_code == 503
    assert known.json() == unknown.json()


def test_default_fake_provider_records_messages_in_development(make_client):
    client = make_client(email_sender=FROM_SETTINGS)

    _register(client)

    assert isinstance(client.app.state.email_sender, FakeEmailSender)
    assert len(_outbox(client)) == 1


# Password reset


def test_reset_request_response_does_not_reveal_whether_account_exists(client, db):
    _register(client)
    _register(client, email="inactive@example.com")
    inactive = _user(db, "inactive@example.com")
    inactive.is_active = False
    db.commit()
    sent_before = len(_outbox(client))

    responses = [
        _request_reset(client, email=email)
        for email in (
            EMAIL,
            "ALICE@example.com",
            "nobody@example.com",
            "not-an-email",
            "inactive@example.com",
        )
    ]

    assert {r.status_code for r in responses} == {202}
    assert all(r.json() == responses[0].json() for r in responses)
    assert all(r.headers.get("set-cookie") is None for r in responses)
    new_messages = _outbox(client)[sent_before:]
    assert [m.to for m in new_messages] == [EMAIL, EMAIL]
    assert all(m.template == "password_reset" for m in new_messages)
    for message in new_messages:
        assert all(message.context["token"] not in r.text for r in responses)


def test_reset_request_delivery_failure_is_not_revealed(make_client, caplog):
    sender = FailingEmailSender()
    client = make_client(email_sender=sender)
    _register(client)

    with caplog.at_level(logging.INFO):
        known = _request_reset(client)
    unknown = _request_reset(client, email="nobody@example.com")

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert "email_delivery_failed" in caplog.text


def test_valid_reset_token_changes_password_and_invalidates_sessions(client, db):
    _register(client)
    login = _login(client)
    old_access_token = login.json()["access_token"]
    assert "access_token" in client.cookies
    _request_reset(client, headers={"Origin": SELF_ORIGIN})
    token = _last_token(client, "password_reset")

    response = _confirm_reset(client, token, headers={"Origin": SELF_ORIGIN})

    assert response.status_code == 204
    assert response.content == b""
    assert "access_token" not in client.cookies
    assert verify_password(NEW_PASSWORD, _user(db).hashed_password)
    old_session = client.get(
        ME_URL, headers={"Authorization": f"Bearer {old_access_token}"}
    )
    assert old_session.status_code == 401
    assert _login(client).status_code == 401
    assert _login(client, password=NEW_PASSWORD).status_code == 200


def test_reused_reset_token_is_rejected(client, db):
    _register(client)
    _request_reset(client)
    token = _last_token(client, "password_reset")
    assert _confirm_reset(client, token).status_code == 204

    response = _confirm_reset(client, token, new_password="yet-another-Passw0rd")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_token"
    assert verify_password(NEW_PASSWORD, _user(db).hashed_password)


def test_new_reset_request_invalidates_earlier_token(client):
    _register(client)
    _request_reset(client)
    first = _last_token(client, "password_reset")
    _request_reset(client)
    second = _last_token(client, "password_reset")

    assert _confirm_reset(client, first).status_code == 400
    assert _confirm_reset(client, second).status_code == 204


def test_expired_reset_token_is_rejected(client, db):
    _register(client)
    sender = FakeEmailSender()
    account_tokens.request_password_reset(
        db,
        EMAIL,
        email_sender=sender,
        rate_limiter=InMemoryRateLimiter(10, 60),
        now=datetime.now(timezone.utc) - timedelta(minutes=31),
    )

    response = _confirm_reset(client, sender.outbox[0].context["token"])

    assert response.status_code == 400
    assert verify_password(PASSWORD, _user(db).hashed_password)
    assert _login(client).status_code == 200


@pytest.mark.parametrize("token", ["not-a-real-token", "b" * 128, "%00"])
def test_malformed_or_unknown_reset_token_returns_400(client, db, token):
    _register(client)

    response = _confirm_reset(client, token)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_token"
    assert verify_password(PASSWORD, _user(db).hashed_password)


def test_verification_token_cannot_reset_password(client, db):
    _register(client)

    response = _confirm_reset(client, _last_token(client, "email_verification"))

    assert response.status_code == 400
    assert verify_password(PASSWORD, _user(db).hashed_password)


@pytest.mark.parametrize(
    "body",
    [
        {"token": "x"},
        {"new_password": NEW_PASSWORD},
        {"token": "", "new_password": NEW_PASSWORD},
        {"token": "c" * 129, "new_password": NEW_PASSWORD},
        {"token": "x", "new_password": "x" * 257},
    ],
)
def test_invalid_reset_body_returns_422(client, body):
    assert client.post(RESET_CONFIRM_URL, json=body).status_code == 422


def test_short_new_password_is_rejected_without_using_the_token(client):
    _register(client)
    _request_reset(client)
    token = _last_token(client, "password_reset")

    rejected = _confirm_reset(client, token, new_password="short")

    assert rejected.status_code == 422
    assert token not in rejected.text
    assert _confirm_reset(client, token).status_code == 204


@pytest.mark.parametrize("email", ["", "a" * 321])
def test_reset_request_rejects_empty_or_overlong_email(client, email):
    assert _request_reset(client, email=email).status_code == 422


def test_reset_request_from_untrusted_origin_is_rejected(client):
    _register(client)
    sent_before = len(_outbox(client))

    response = _request_reset(client, headers={"Origin": EVIL_ORIGIN})

    assert response.status_code == 403
    assert len(_outbox(client)) == sent_before


# Rate limiting


def test_reset_requests_are_rate_limited_per_address_whether_or_not_it_exists(
    make_client,
):
    limiters = AccountRateLimiters(
        requests=InMemoryRateLimiter(2, 3600), attempts=InMemoryRateLimiter(100, 900)
    )
    client = make_client(rate_limiters=limiters)
    _register(client)

    known = [_request_reset(client).status_code for _ in range(3)]
    unknown = [_request_reset(client, email="nobody@x.example").status_code for _ in range(3)]

    assert known == unknown == [202, 202, 429]
    limited = _request_reset(client)
    assert limited.json()["error"]["code"] == "too_many_requests"
    assert int(limited.headers["retry-after"]) > 0
    # Only the two allowed requests for the real account sent email.
    assert len([m for m in _outbox(client) if m.template == "password_reset"]) == 2


def test_reset_requests_are_rate_limited_per_client(make_client):
    limiters = AccountRateLimiters(
        requests=InMemoryRateLimiter(100, 3600), attempts=InMemoryRateLimiter(3, 900)
    )
    client = make_client(rate_limiters=limiters)

    statuses = [
        _request_reset(client, email=f"user{i}@example.com").status_code for i in range(4)
    ]

    assert statuses == [202, 202, 202, 429]


def test_token_attempts_are_rate_limited(make_client, db):
    limiters = AccountRateLimiters(
        requests=InMemoryRateLimiter(100, 3600), attempts=InMemoryRateLimiter(3, 900)
    )
    client = make_client(rate_limiters=limiters)
    _register(client)
    valid = _last_token(client, "email_verification")

    statuses = [_confirm_verification(client, f"guess-{i}").status_code for i in range(3)]
    limited = _confirm_verification(client, valid)

    assert statuses == [400, 400, 400]
    assert limited.status_code == 429
    assert "retry-after" in limited.headers
    # The rate-limited attempt did not use the token or verify the user.
    assert _user(db).email_verified_at is None


def test_reset_confirm_attempts_are_rate_limited(make_client, db):
    limiters = AccountRateLimiters(
        requests=InMemoryRateLimiter(100, 3600), attempts=InMemoryRateLimiter(2, 900)
    )
    client = make_client(rate_limiters=limiters)
    _register(client)

    statuses = [_confirm_reset(client, f"guess-{i}").status_code for i in range(3)]

    assert statuses == [400, 400, 429]
    assert verify_password(PASSWORD, _user(db).hashed_password)


def test_verification_requests_are_rate_limited_per_user(make_client):
    limiters = AccountRateLimiters(
        requests=InMemoryRateLimiter(3, 3600), attempts=InMemoryRateLimiter(100, 900)
    )
    client = make_client(rate_limiters=limiters)
    _register(client)  # Uses the first of the three requests.
    headers = _bearer(client)

    statuses = [client.post(VERIFY_REQUEST_URL, headers=headers).status_code for _ in range(3)]

    assert statuses == [202, 202, 429]


# Secrecy


def test_tokens_and_addresses_are_never_logged(client, caplog):
    with caplog.at_level(logging.DEBUG):
        _register(client)
        verification_token = _last_token(client, "email_verification")
        _confirm_verification(client, verification_token)
        _confirm_verification(client, verification_token)
        _request_reset(client)
        _request_reset(client, email="nobody@example.com")
        reset_token = _last_token(client, "password_reset")
        _confirm_reset(client, reset_token)
        _confirm_reset(client, reset_token)

    assert verification_token not in caplog.text
    assert reset_token not in caplog.text
    assert hash_one_time_token(reset_token) not in caplog.text
    assert EMAIL not in caplog.text
    assert "nobody@example.com" not in caplog.text
    assert NEW_PASSWORD not in caplog.text

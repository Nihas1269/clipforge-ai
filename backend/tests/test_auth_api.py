import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.api.deps import get_current_user
from app.core.config import Settings, get_settings
from app.core.rate_limit import InMemoryRateLimiter
from app.core.security import create_access_token, decode_access_token
from app.database import Base, create_db_engine, create_session_factory, get_db
from app.main import create_app
from app.models.user import User
from app.repositories.users import create_user
from app.services import account_tokens
from app.services.email import FakeEmailSender

SQLITE_IN_MEMORY_URL = "sqlite:///:memory:"
SECRET = "test-only-jwt-signing-secret-0123456789abcdef"
PASSWORD = "s3cure-Passw0rd"
SELF_ORIGIN = "http://testserver"
FRONTEND_ORIGIN = "http://localhost:3000"
EVIL_ORIGIN = "http://evil.example"

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
LOGOUT_URL = "/api/v1/auth/logout"


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
    """Build a TestClient for an app using the test database and settings.

    The app gets two extra routes protected by get_current_user so the
    access token can be exercised end to end.
    """
    monkeypatch.setenv("SECRET_KEY", SECRET)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("CORS_ORIGINS", FRONTEND_ORIGIN)
    monkeypatch.delenv("ACCESS_TOKEN_EXPIRE_MINUTES", raising=False)

    def factory(**env: str) -> TestClient:
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        get_settings.cache_clear()
        application = create_app()

        def override_get_db():
            session = session_factory()
            try:
                yield session
            finally:
                session.close()

        application.dependency_overrides[get_db] = override_get_db

        @application.get("/test/me")
        def me(user: User = Depends(get_current_user)) -> dict[str, str]:
            return {"id": str(user.id)}

        @application.post("/test/action")
        def action(user: User = Depends(get_current_user)) -> dict[str, str]:
            return {"id": str(user.id)}

        return TestClient(application)

    return factory


@pytest.fixture
def client(make_client):
    return make_client()


def _register(client, email="alice@example.com", password=PASSWORD, **extra):
    return client.post(REGISTER_URL, json={"email": email, "password": password, **extra})


def _login(client, email="alice@example.com", password=PASSWORD, **kwargs):
    return client.post(LOGIN_URL, json={"email": email, "password": password}, **kwargs)


def _set_cookie_headers(response) -> list[str]:
    return response.headers.get_list("set-cookie")


def _access_token_cookie_header(response) -> str:
    headers = [h for h in _set_cookie_headers(response) if h.startswith("access_token=")]
    assert len(headers) == 1
    return headers[0]


def _cookie_attributes(header: str) -> set[str]:
    return {part.strip().lower() for part in header.split(";")[1:]}


def _user_count(db) -> int:
    return db.scalar(select(func.count()).select_from(User))


# Registration


def test_register_creates_user_and_returns_public_fields(client, db):
    response = _register(client, email="  Alice@Example.COM ", full_name="Alice")

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "id",
        "email",
        "full_name",
        "is_active",
        "email_verified",
        "created_at",
    }
    assert body["email"] == "alice@example.com"
    assert body["full_name"] == "Alice"
    assert body["is_active"] is True
    assert body["email_verified"] is False
    assert PASSWORD not in response.text
    assert "hash" not in response.text
    stored = db.scalars(select(User)).one()
    assert str(stored.id) == body["id"]
    assert stored.email == "alice@example.com"


def test_register_full_name_is_optional(client):
    response = _register(client)

    assert response.status_code == 201
    assert response.json()["full_name"] is None


@pytest.mark.parametrize("email", ["alice@example.com", "ALICE@example.com"])
def test_register_duplicate_email_returns_409(client, db, email):
    assert _register(client).status_code == 201

    response = _register(client, email=email)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_already_registered"
    assert _user_count(db) == 1


@pytest.mark.parametrize(
    "email",
    [
        "",
        "not-an-email",
        "alice@",
        "@example.com",
        "a b@example.com",
        "alice@example",
        "älice@example.com",
        "a" * 320 + "@example.com",
        123,
    ],
)
def test_register_invalid_email_returns_422(client, db, email):
    response = _register(client, email=email)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"][0]["loc"] == ["body", "email"]
    assert _user_count(db) == 0


@pytest.mark.parametrize("password", ["", "short", "7-chars"])
def test_register_short_password_returns_422(client, db, password):
    response = _register(client, password=password)

    assert response.status_code == 422
    details = response.json()["error"]["details"]
    assert details[0]["loc"] == ["body", "password"]
    assert details[0]["type"] == "string_too_short"
    assert _user_count(db) == 0


def test_register_accepts_password_of_exactly_eight_characters(client):
    assert _register(client, password="8-chars!").status_code == 201


def test_register_rejects_overlong_password_and_unknown_fields(client, db):
    assert _register(client, password="x" * 257).status_code == 422
    assert _register(client, is_admin=True).status_code == 422
    assert _user_count(db) == 0


def test_register_missing_fields_returns_422(client):
    response = client.post(REGISTER_URL, json={})

    assert response.status_code == 422
    locs = [detail["loc"] for detail in response.json()["error"]["details"]]
    assert ["body", "email"] in locs
    assert ["body", "password"] in locs


# Login


def test_login_returns_token_and_sets_cookie(client, db):
    _register(client)

    response = _login(client, email="ALICE@example.com")

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert response.headers["cache-control"] == "no-store"

    header = _access_token_cookie_header(response)
    assert header.startswith(f"access_token={body['access_token']};")
    attributes = _cookie_attributes(header)
    assert "httponly" in attributes
    assert "samesite=lax" in attributes
    assert "path=/" in attributes
    assert "max-age=1800" in attributes
    assert "secure" not in attributes
    assert client.cookies.get("access_token") == body["access_token"]


def test_login_token_decodes_to_user_and_configured_expiry(make_client, db):
    client = make_client(ACCESS_TOKEN_EXPIRE_MINUTES="5")
    user_id = _register(client).json()["id"]

    response = _login(client)

    payload = decode_access_token(response.json()["access_token"])
    assert str(payload.user_id) == user_id
    assert payload.expires_at - payload.issued_at == timedelta(minutes=5)
    assert "max-age=300" in _cookie_attributes(_access_token_cookie_header(response))


def test_login_cookie_is_secure_in_production(make_client):
    client = make_client(APP_ENV="production")
    _register(client)

    response = _login(client)

    assert response.status_code == 200
    assert "secure" in _cookie_attributes(_access_token_cookie_header(response))


def test_login_wrong_password_returns_401(client):
    _register(client)

    response = _login(client, password="wrong-password")

    assert response.status_code == 401
    assert response.json()["error"] == {
        "code": "invalid_credentials",
        "message": "Incorrect email or password.",
        "details": None,
    }
    assert _set_cookie_headers(response) == []


def test_login_errors_do_not_reveal_whether_email_exists(client):
    _register(client)

    wrong_password = _login(client, password="wrong-password")
    unknown_email = _login(client, email="nobody@example.com")
    malformed_email = _login(client, email="not-an-email")
    blank_email = _login(client, email="   ")

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert malformed_email.status_code == blank_email.status_code == 401
    assert wrong_password.json() == unknown_email.json() == malformed_email.json()
    assert blank_email.json() == wrong_password.json()


def test_inactive_user_cannot_log_in(client, db):
    _register(client)
    user = db.scalars(select(User)).one()
    user.is_active = False
    db.commit()

    response = _login(client)

    assert response.status_code == 401
    assert response.json() == _login(client, password="wrong-password").json()
    assert _set_cookie_headers(response) == []


def test_login_missing_fields_returns_422(client):
    assert client.post(LOGIN_URL, json={"email": "alice@example.com"}).status_code == 422


def test_login_logs_do_not_contain_password_or_token(client, caplog):
    _register(client)

    with caplog.at_level(logging.DEBUG):
        token = _login(client).json()["access_token"]
        _login(client, password="wrong-password")

    assert PASSWORD not in caplog.text
    assert token not in caplog.text
    assert "alice@example.com" not in caplog.text


# Logout


def test_logout_clears_cookie_with_matching_attributes(client):
    _register(client)
    _login(client)
    assert "access_token" in client.cookies

    response = client.post(LOGOUT_URL, headers={"Origin": SELF_ORIGIN})

    assert response.status_code == 204
    assert response.content == b""
    header = _access_token_cookie_header(response)
    attributes = _cookie_attributes(header)
    assert header.startswith('access_token="";') or header.startswith("access_token=;")
    assert "max-age=0" in attributes
    assert "path=/" in attributes
    assert "httponly" in attributes
    assert "samesite=lax" in attributes
    assert "access_token" not in client.cookies
    assert client.get("/test/me").status_code == 401


def test_logout_clears_secure_cookie_in_production(make_client):
    client = make_client(APP_ENV="production")

    response = client.post(LOGOUT_URL)

    assert response.status_code == 204
    assert "secure" in _cookie_attributes(_access_token_cookie_header(response))


def test_logout_does_not_revoke_already_issued_token(client):
    # Documented behavior: JWTs are stateless, so clearing the cookie does not
    # invalidate a token that was copied before logout.
    _register(client)
    token = _login(client).json()["access_token"]

    assert client.post(LOGOUT_URL, headers={"Origin": SELF_ORIGIN}).status_code == 204

    response = client.get("/test/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


# Access token validation (get_current_user)


def test_access_token_authenticates_via_header_and_cookie(client):
    user_id = _register(client).json()["id"]
    token = _login(client).json()["access_token"]

    via_cookie = client.get("/test/me")
    client.cookies.clear()
    via_header = client.get("/test/me", headers={"Authorization": f"Bearer {token}"})

    assert via_cookie.status_code == 200
    assert via_cookie.json() == {"id": user_id}
    assert via_header.status_code == 200
    assert via_header.json() == {"id": user_id}


@pytest.mark.parametrize(
    "authorization", [None, "Bearer", "Bearer not-a-jwt", "Basic YWxpY2U6cHc="]
)
def test_missing_or_invalid_token_returns_401(client, authorization):
    headers = {"Authorization": authorization} if authorization else {}

    response = client.get("/test/me", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_expired_token_returns_401(client, db):
    _register(client)
    user = db.scalars(select(User)).one()
    issued = datetime.now(timezone.utc) - timedelta(minutes=31)
    token = create_access_token(user.id, settings=get_settings(), now=issued)

    response = client.get("/test/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_token_of_deactivated_user_returns_401(client, db):
    _register(client)
    token = _login(client).json()["access_token"]
    user = db.scalars(select(User)).one()
    user.is_active = False
    db.commit()

    response = client.get("/test/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_password_reset_invalidates_existing_access_tokens(client, db):
    _register(client)
    token = _login(client).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/test/me", headers=headers).status_code == 200

    sender = FakeEmailSender()
    limiter = InMemoryRateLimiter(10, 60)
    account_tokens.request_password_reset(
        db, "alice@example.com", email_sender=sender, rate_limiter=limiter
    )
    account_tokens.reset_password(
        db,
        sender.outbox[0].context["token"],
        "a-brand-new-Passw0rd",
        rate_limiter=limiter,
        rate_limit_key="test",
    )

    assert client.get("/test/me", headers=headers).status_code == 401
    # The jar still holds the cookie from the first login, so these login
    # requests need a trusted Origin to get past CSRF protection.
    origin = {"Origin": SELF_ORIGIN}
    assert _login(client, headers=origin).status_code == 401
    new_token = _login(client, password="a-brand-new-Passw0rd", headers=origin).json()[
        "access_token"
    ]
    assert (
        client.get("/test/me", headers={"Authorization": f"Bearer {new_token}"}).status_code
        == 200
    )


def test_tokens_signed_with_another_secret_are_rejected(client, db):
    _register(client)
    user = db.scalars(select(User)).one()
    other = Settings(SECRET_KEY="another-signing-secret-0123456789abcdef")
    token = create_access_token(user.id, settings=other)

    response = client.get("/test/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


# CSRF protection


@pytest.mark.parametrize("origin", [SELF_ORIGIN, FRONTEND_ORIGIN, "HTTP://LOCALHOST:3000"])
def test_login_from_trusted_origin_is_allowed(client, origin):
    _register(client)

    assert _login(client, headers={"Origin": origin}).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": EVIL_ORIGIN},
        {"Origin": "null"},
        {"Origin": "http://localhost:3001"},
        {"Origin": "https://testserver.evil.example"},
        {"Referer": f"{EVIL_ORIGIN}/page"},
    ],
)
def test_auth_requests_from_untrusted_origin_are_rejected(client, db, headers):
    register = client.post(
        REGISTER_URL,
        json={"email": "alice@example.com", "password": PASSWORD},
        headers=headers,
    )
    assert register.status_code == 403
    assert register.json()["error"]["code"] == "csrf_failed"
    assert _user_count(db) == 0

    create_user(db, email="alice@example.com", password=PASSWORD)
    login = _login(client, headers=headers)
    assert login.status_code == 403
    assert _set_cookie_headers(login) == []

    assert client.post(LOGOUT_URL, headers=headers).status_code == 403


def test_trusted_referer_is_accepted_when_origin_is_absent(client):
    _register(client)

    response = _login(client, headers={"Referer": f"{SELF_ORIGIN}/login?next=/"})

    assert response.status_code == 200


def test_requests_without_origin_or_cookie_are_allowed(client):
    # Non-browser clients send neither header and carry no ambient cookie.
    assert _register(client).status_code == 201
    assert _login(client).status_code == 200


def test_cookie_authenticated_post_requires_trusted_origin(client):
    _register(client)
    _login(client)

    no_origin = client.post("/test/action")
    evil = client.post("/test/action", headers={"Origin": EVIL_ORIGIN})
    trusted = client.post("/test/action", headers={"Origin": FRONTEND_ORIGIN})
    safe_method = client.get("/test/me")

    assert no_origin.status_code == 403
    assert no_origin.json()["error"]["code"] == "csrf_failed"
    assert evil.status_code == 403
    assert trusted.status_code == 200
    assert safe_method.status_code == 200


def test_logout_with_cookie_and_no_origin_is_rejected(client):
    _register(client)
    _login(client)

    response = client.post(LOGOUT_URL)

    assert response.status_code == 403
    assert "access_token" in client.cookies


def test_bearer_authenticated_post_does_not_need_origin(client):
    _register(client)
    token = _login(client).json()["access_token"]
    client.cookies.clear()

    response = client.post("/test/action", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200

import logging
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.api.deps import get_current_admin
from app.core.config import Settings, get_settings
from app.core.security import create_access_token, verify_password
from app.database import Base, create_db_engine, create_session_factory, get_db
from app.main import create_app
from app.models.user import User
from app.repositories.users import create_user

SQLITE_IN_MEMORY_URL = "sqlite:///:memory:"
SECRET = "test-only-jwt-signing-secret-0123456789abcdef"
PASSWORD = "s3cure-Passw0rd"
NEW_PASSWORD = "a-brand-new-Passw0rd"
SELF_ORIGIN = "http://testserver"
FRONTEND_ORIGIN = "http://localhost:3000"
EVIL_ORIGIN = "http://evil.example"

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
ME_URL = "/api/v1/users/me"
PASSWORD_URL = "/api/v1/users/me/password"
ADMIN_URL = "/test/admin"

PUBLIC_FIELDS = {
    "id",
    "email",
    "full_name",
    "is_active",
    "email_verified",
    "created_at",
}


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
def client(session_factory, monkeypatch):
    """A TestClient using the test database, plus a test-only admin route."""
    monkeypatch.setenv("SECRET_KEY", SECRET)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("CORS_ORIGINS", FRONTEND_ORIGIN)
    monkeypatch.delenv("ACCESS_TOKEN_EXPIRE_MINUTES", raising=False)
    get_settings.cache_clear()
    application = create_app()

    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    application.dependency_overrides[get_db] = override_get_db

    @application.get(ADMIN_URL)
    def admin_only(user: User = Depends(get_current_admin)) -> dict[str, str]:
        return {"id": str(user.id)}

    return TestClient(application)


def _register(client, email="alice@example.com", password=PASSWORD, **extra):
    return client.post(
        REGISTER_URL, json={"email": email, "password": password, **extra}
    )


def _login(client, email="alice@example.com", password=PASSWORD, **kwargs):
    return client.post(LOGIN_URL, json={"email": email, "password": password}, **kwargs)


def _token(client, email="alice@example.com", password=PASSWORD) -> str:
    """Log in without keeping the cookie and return the access token."""
    response = _login(client, email=email, password=password)
    assert response.status_code == 200
    client.cookies.clear()
    return response.json()["access_token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _stored_user(db, email="alice@example.com") -> User:
    db.expire_all()
    return db.scalars(select(User).where(User.email == email)).one()


def _forge(claims: dict, secret: str = SECRET) -> str:
    return jwt.encode(claims, secret, algorithm="HS256")


def _valid_claims(user_id) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "ver": 0,
    }


# GET /users/me: authentication


def test_me_without_token_returns_401(client):
    response = client.get(ME_URL)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(
    "authorization",
    ["Bearer", "Bearer not-a-jwt", "Bearer a.b.c", "Basic YWxpY2U6cHc=", "Token abc"],
)
def test_me_with_malformed_authorization_returns_401(client, authorization):
    _register(client)

    response = client.get(ME_URL, headers={"Authorization": authorization})

    assert response.status_code == 401


def test_me_with_malformed_cookie_returns_401(client):
    client.cookies.set("access_token", "not-a-jwt")

    assert client.get(ME_URL).status_code == 401


def test_me_with_expired_token_returns_401(client, db):
    _register(client)
    user = _stored_user(db)
    issued = datetime.now(timezone.utc) - timedelta(minutes=31)
    token = create_access_token(user.id, settings=get_settings(), now=issued)

    assert client.get(ME_URL, headers=_bearer(token)).status_code == 401


def test_me_with_invalid_signature_returns_401(client, db):
    _register(client)
    user = _stored_user(db)
    other = Settings(SECRET_KEY="another-signing-secret-0123456789abcdef")
    token = create_access_token(user.id, settings=other)

    assert client.get(ME_URL, headers=_bearer(token)).status_code == 401


def test_me_with_tampered_token_returns_401(client):
    _register(client)
    token = _token(client)
    header, payload, signature = token.split(".")
    tampered = (
        f"{header}.{payload}.{signature[:-2]}{'AA' if signature[-2:] != 'AA' else 'BB'}"
    )

    assert client.get(ME_URL, headers=_bearer(tampered)).status_code == 401


def test_me_with_unsigned_token_returns_401(client, db):
    _register(client)
    user = _stored_user(db)
    token = jwt.encode(_valid_claims(user.id), key=None, algorithm="none")

    assert client.get(ME_URL, headers=_bearer(token)).status_code == 401


@pytest.mark.parametrize(
    "change",
    [
        {"sub": None},
        {"sub": "not-a-uuid"},
        {"sub": ""},
        {"sub": 123},
        {"sub": str(uuid.uuid4())},
        {"ver": None},
        {"ver": "0"},
        {"iat": None},
        {"exp": None},
    ],
    ids=[
        "missing-sub",
        "non-uuid-sub",
        "empty-sub",
        "integer-sub",
        "unknown-user",
        "missing-ver",
        "string-ver",
        "missing-iat",
        "missing-exp",
    ],
)
def test_me_with_bad_claims_returns_401(client, db, change):
    _register(client)
    user = _stored_user(db)
    claims = _valid_claims(user.id)
    for name, value in change.items():
        if value is None:
            claims.pop(name)
        else:
            claims[name] = value

    response = client.get(ME_URL, headers=_bearer(_forge(claims)))

    assert response.status_code == 401


def test_forged_claims_with_correct_secret_are_accepted(client, db):
    # Control for the test above: the forged token is valid when unchanged.
    user_id = _register(client).json()["id"]
    user = _stored_user(db)

    response = client.get(ME_URL, headers=_bearer(_forge(_valid_claims(user.id))))

    assert response.status_code == 200
    assert response.json()["id"] == user_id


def test_me_for_inactive_user_with_existing_token_returns_401(client, db):
    _register(client)
    token = _token(client)
    assert client.get(ME_URL, headers=_bearer(token)).status_code == 200
    user = _stored_user(db)
    user.is_active = False
    db.commit()

    via_header = client.get(ME_URL, headers=_bearer(token))
    client.cookies.set("access_token", token)
    via_cookie = client.get(ME_URL)

    assert via_header.status_code == 401
    assert via_cookie.status_code == 401


# GET /users/me: success


def test_me_via_bearer_header_returns_public_profile(client):
    registered = _register(client, full_name="Alice").json()
    token = _token(client)

    response = client.get(ME_URL, headers=_bearer(token))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == PUBLIC_FIELDS
    assert body["id"] == registered["id"]
    assert body["email"] == "alice@example.com"
    assert body["full_name"] == "Alice"
    assert body["is_active"] is True
    assert body["email_verified"] is False
    assert "hash" not in response.text
    assert "token_version" not in response.text
    assert "is_admin" not in response.text
    assert PASSWORD not in response.text


def test_me_via_cookie_returns_public_profile(client):
    user_id = _register(client).json()["id"]
    assert _login(client).status_code == 200

    response = client.get(ME_URL)

    assert response.status_code == 200
    assert response.json()["id"] == user_id
    assert set(response.json()) == PUBLIC_FIELDS


def test_bearer_header_takes_precedence_over_cookie(client):
    _register(client, email="alice@example.com")
    bob_id = _register(client, email="bob@example.com").json()["id"]
    bob_token = _token(client, email="bob@example.com")
    assert _login(client, email="alice@example.com").status_code == 200

    response = client.get(ME_URL, headers=_bearer(bob_token))

    assert response.json()["id"] == bob_id


def test_me_returns_email_normalized_like_registration(client):
    _register(client, email="  Alice@Example.COM ")
    token = _token(client, email="ALICE@example.com")

    response = client.get(ME_URL, headers=_bearer(token))

    assert response.json()["email"] == "alice@example.com"


# PATCH /users/me


def test_patch_me_updates_full_name_and_persists(client, db):
    _register(client, full_name="Alice")
    token = _token(client)

    response = client.patch(
        ME_URL, json={"full_name": "  Alice Liddell "}, headers=_bearer(token)
    )

    assert response.status_code == 200
    assert response.json()["full_name"] == "Alice Liddell"
    assert set(response.json()) == PUBLIC_FIELDS
    assert _stored_user(db).full_name == "Alice Liddell"
    assert (
        client.get(ME_URL, headers=_bearer(token)).json()["full_name"]
        == "Alice Liddell"
    )


@pytest.mark.parametrize("value", [None, "", "   "])
def test_patch_me_null_or_blank_full_name_clears_it(client, db, value):
    _register(client, full_name="Alice")
    token = _token(client)

    response = client.patch(ME_URL, json={"full_name": value}, headers=_bearer(token))

    assert response.status_code == 200
    assert response.json()["full_name"] is None
    assert _stored_user(db).full_name is None


def test_patch_me_with_empty_body_changes_nothing(client, db):
    _register(client, full_name="Alice")
    token = _token(client)

    response = client.patch(ME_URL, json={}, headers=_bearer(token))

    assert response.status_code == 200
    assert response.json()["full_name"] == "Alice"
    assert _stored_user(db).full_name == "Alice"


def test_patch_me_accepts_max_length_and_rejects_longer_full_name(client, db):
    _register(client)
    token = _token(client)

    longest = client.patch(
        ME_URL, json={"full_name": "x" * 255}, headers=_bearer(token)
    )
    too_long = client.patch(
        ME_URL, json={"full_name": "y" * 256}, headers=_bearer(token)
    )
    wrong_type = client.patch(ME_URL, json={"full_name": 5}, headers=_bearer(token))

    assert longest.status_code == 200
    assert too_long.status_code == 422
    assert wrong_type.status_code == 422
    assert _stored_user(db).full_name == "x" * 255


@pytest.mark.parametrize(
    "body",
    [
        {"email": "mallory@example.com"},
        {"is_admin": True},
        {"is_active": False},
        {"full_name": "Alice", "is_admin": True},
        {"hashed_password": "x"},
        {"token_version": 5},
    ],
)
def test_patch_me_rejects_protected_fields(client, db, body):
    _register(client, full_name="Alice")
    token = _token(client)

    response = client.patch(ME_URL, json=body, headers=_bearer(token))

    assert response.status_code == 422
    user = _stored_user(db)
    assert user.email == "alice@example.com"
    assert user.full_name == "Alice"
    assert user.is_admin is False
    assert user.is_active is True
    assert user.token_version == 0


def test_patch_me_without_token_returns_401(client, db):
    _register(client, full_name="Alice")

    response = client.patch(ME_URL, json={"full_name": "Mallory"})

    assert response.status_code == 401
    assert _stored_user(db).full_name == "Alice"


def test_patch_me_only_changes_the_authenticated_user(client, db):
    _register(client, email="alice@example.com", full_name="Alice")
    _register(client, email="bob@example.com", full_name="Bob")
    bob_token = _token(client, email="bob@example.com")

    response = client.patch(
        ME_URL, json={"full_name": "Robert"}, headers=_bearer(bob_token)
    )

    assert response.status_code == 200
    assert _stored_user(db, "bob@example.com").full_name == "Robert"
    assert _stored_user(db, "alice@example.com").full_name == "Alice"


def test_patch_me_with_cookie_requires_trusted_origin(client, db):
    _register(client, full_name="Alice")
    assert _login(client).status_code == 200

    no_origin = client.patch(ME_URL, json={"full_name": "Mallory"})
    evil = client.patch(
        ME_URL, json={"full_name": "Mallory"}, headers={"Origin": EVIL_ORIGIN}
    )
    assert no_origin.status_code == 403
    assert evil.status_code == 403
    assert _stored_user(db).full_name == "Alice"

    trusted = client.patch(
        ME_URL, json={"full_name": "Alice L"}, headers={"Origin": FRONTEND_ORIGIN}
    )
    assert trusted.status_code == 200
    assert _stored_user(db).full_name == "Alice L"


# POST /users/me/password


def _change_password(client, token, current=PASSWORD, new=NEW_PASSWORD, **kwargs):
    return client.post(
        PASSWORD_URL,
        json={"current_password": current, "new_password": new},
        headers=_bearer(token),
        **kwargs,
    )


def test_change_password_success_new_password_works_for_login(client, db):
    _register(client)
    token = _token(client)

    response = _change_password(client, token)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["token_type"] == "bearer"
    assert verify_password(NEW_PASSWORD, _stored_user(db).hashed_password)
    client.cookies.clear()
    assert _login(client, password=PASSWORD).status_code == 401
    assert _login(client, password=NEW_PASSWORD).status_code == 200


def test_change_password_invalidates_old_tokens_and_returns_new_one(client):
    _register(client)
    token = _token(client)
    other_session = _token(client)

    response = _change_password(client, token)
    new_token = response.json()["access_token"]

    assert client.cookies.get("access_token") == new_token
    client.cookies.clear()
    assert client.get(ME_URL, headers=_bearer(token)).status_code == 401
    assert client.get(ME_URL, headers=_bearer(other_session)).status_code == 401
    assert client.get(ME_URL, headers=_bearer(new_token)).status_code == 200


def test_change_password_with_wrong_current_password_is_rejected(client, db):
    _register(client)
    token = _token(client)

    response = _change_password(client, token, current="wrong-password")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_current_password"
    assert "access_token" not in client.cookies
    user = _stored_user(db)
    assert verify_password(PASSWORD, user.hashed_password)
    assert user.token_version == 0
    assert client.get(ME_URL, headers=_bearer(token)).status_code == 200
    assert _login(client, password=NEW_PASSWORD).status_code == 401


@pytest.mark.parametrize("new", ["", "short", "7-chars", "x" * 257])
def test_change_password_enforces_registration_rules(client, db, new):
    _register(client)
    token = _token(client)

    response = _change_password(client, token, new=new)

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["body", "new_password"]
    assert verify_password(PASSWORD, _stored_user(db).hashed_password)


def test_change_password_accepts_boundary_lengths(client):
    _register(client)
    token = _token(client)

    shortest = _change_password(client, token, new="8-chars!")
    assert shortest.status_code == 200
    token = shortest.json()["access_token"]
    longest = _change_password(client, token, current="8-chars!", new="z" * 256)
    assert longest.status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"current_password": PASSWORD},
        {"new_password": NEW_PASSWORD},
        {"current_password": "", "new_password": NEW_PASSWORD},
        {
            "current_password": PASSWORD,
            "new_password": NEW_PASSWORD,
            "email": "m@example.com",
        },
    ],
)
def test_change_password_invalid_body_returns_422(client, db, body):
    _register(client)
    token = _token(client)

    response = client.post(PASSWORD_URL, json=body, headers=_bearer(token))

    assert response.status_code == 422
    user = _stored_user(db)
    assert user.email == "alice@example.com"
    assert verify_password(PASSWORD, user.hashed_password)


def test_change_password_without_token_returns_401(client, db):
    _register(client)

    response = client.post(
        PASSWORD_URL, json={"current_password": PASSWORD, "new_password": NEW_PASSWORD}
    )

    assert response.status_code == 401
    assert verify_password(PASSWORD, _stored_user(db).hashed_password)


def test_change_password_only_changes_the_authenticated_user(client, db):
    _register(client, email="alice@example.com")
    _register(client, email="bob@example.com")
    bob_token = _token(client, email="bob@example.com")

    assert _change_password(client, bob_token).status_code == 200

    assert verify_password(
        NEW_PASSWORD, _stored_user(db, "bob@example.com").hashed_password
    )
    assert verify_password(
        PASSWORD, _stored_user(db, "alice@example.com").hashed_password
    )


def test_change_password_with_cookie_requires_trusted_origin(client, db):
    _register(client)
    assert _login(client).status_code == 200
    body = {"current_password": PASSWORD, "new_password": NEW_PASSWORD}

    evil = client.post(PASSWORD_URL, json=body, headers={"Origin": EVIL_ORIGIN})
    assert evil.status_code == 403
    assert verify_password(PASSWORD, _stored_user(db).hashed_password)

    trusted = client.post(PASSWORD_URL, json=body, headers={"Origin": SELF_ORIGIN})
    assert trusted.status_code == 200


def test_change_password_logs_contain_no_secrets(client, caplog):
    _register(client)
    token = _token(client)

    with caplog.at_level(logging.DEBUG):
        _change_password(client, token, current="wrong-password")
        new_token = _change_password(client, token).json()["access_token"]

    assert PASSWORD not in caplog.text
    assert NEW_PASSWORD not in caplog.text
    assert "wrong-password" not in caplog.text
    assert token not in caplog.text
    assert new_token not in caplog.text
    assert "alice@example.com" not in caplog.text


# get_current_admin


def test_admin_dependency_returns_403_for_normal_user(client):
    _register(client)
    token = _token(client)

    response = client.get(ADMIN_URL, headers=_bearer(token))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_admin_dependency_allows_admin(client, db):
    admin = create_user(db, email="admin@example.com", password=PASSWORD, is_admin=True)
    token = _token(client, email="admin@example.com")

    response = client.get(ADMIN_URL, headers=_bearer(token))

    assert response.status_code == 200
    assert response.json() == {"id": str(admin.id)}


def test_admin_dependency_returns_401_without_token(client):
    assert client.get(ADMIN_URL).status_code == 401


def test_admin_dependency_returns_401_for_inactive_admin(client, db):
    create_user(db, email="admin@example.com", password=PASSWORD, is_admin=True)
    token = _token(client, email="admin@example.com")
    _stored_user(db, "admin@example.com").is_active = False
    db.commit()

    assert client.get(ADMIN_URL, headers=_bearer(token)).status_code == 401

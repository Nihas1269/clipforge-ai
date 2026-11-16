import logging
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.core.security import verify_password
from app.database import Base, create_db_engine, create_session_factory
from app.models.user import User
from app.repositories import users as users_repository
from app.repositories.users import (
    DuplicateEmailError,
    create_user,
    get_by_email,
    get_by_id,
)

SQLITE_IN_MEMORY_URL = "sqlite:///:memory:"
PASSWORD = "s3cure-Passw0rd"


@pytest.fixture
def db():
    """Session on an isolated in-memory database with the full schema."""
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


def _user_count(db) -> int:
    return db.scalar(select(func.count()).select_from(User))


def test_create_user_normalizes_email_to_lowercase(db):
    user = create_user(db, email="  Alice@Example.COM ", password=PASSWORD)

    assert user.email == "alice@example.com"
    stored = db.scalar(text("SELECT email FROM users"))
    assert stored == "alice@example.com"


def test_create_user_stores_password_hash_not_plaintext(db):
    user = create_user(db, email="alice@example.com", password=PASSWORD)

    assert user.hashed_password != PASSWORD
    assert verify_password(PASSWORD, user.hashed_password) is True
    assert verify_password("wrong", user.hashed_password) is False


def test_create_user_sets_defaults(db):
    user = create_user(db, email="alice@example.com", password=PASSWORD)

    assert isinstance(user.id, uuid.UUID)
    assert user.full_name is None
    assert user.is_active is True
    assert user.is_admin is False
    assert user.created_at is not None
    assert user.updated_at is not None


def test_create_user_accepts_optional_fields(db):
    user = create_user(
        db,
        email="admin@example.com",
        password=PASSWORD,
        full_name="Ada Admin",
        is_admin=True,
    )

    assert user.full_name == "Ada Admin"
    assert user.is_admin is True


def test_updating_user_refreshes_updated_at(db):
    user = create_user(db, email="alice@example.com", password=PASSWORD)
    original = user.updated_at

    user.full_name = "Alice"
    db.commit()

    assert user.updated_at >= original


def test_create_user_rejects_duplicate_email(db):
    create_user(db, email="alice@example.com", password=PASSWORD)

    with pytest.raises(DuplicateEmailError):
        create_user(db, email="alice@example.com", password=PASSWORD)

    assert _user_count(db) == 1


def test_create_user_rejects_duplicate_email_with_different_case(db):
    create_user(db, email="alice@example.com", password=PASSWORD)

    with pytest.raises(DuplicateEmailError):
        create_user(db, email="ALICE@Example.com", password=PASSWORD)

    assert _user_count(db) == 1


def test_create_user_maps_unique_violation_to_domain_error(db, monkeypatch):
    # Simulates a concurrent registration: the pre-check misses the existing
    # user, so the database unique index must reject the insert.
    create_user(db, email="alice@example.com", password=PASSWORD)
    real_get_by_email = users_repository.get_by_email
    calls = []

    def racing_get_by_email(session, email):
        calls.append(email)
        if len(calls) == 1:
            return None
        return real_get_by_email(session, email)

    monkeypatch.setattr(users_repository, "get_by_email", racing_get_by_email)

    with pytest.raises(DuplicateEmailError) as exc_info:
        create_user(db, email="Alice@example.com", password=PASSWORD)

    assert isinstance(exc_info.value.__cause__, IntegrityError)
    assert _user_count(db) == 1


def test_database_rejects_duplicate_email(db):
    db.add(User(email="alice@example.com", hashed_password="x"))
    db.commit()
    db.add(User(email="ALICE@example.com", hashed_password="x"))

    with pytest.raises(IntegrityError):
        db.commit()


def test_database_rejects_uppercase_email(db):
    # Bypasses the model's normalization to prove the database itself
    # enforces lowercase storage (and so case-insensitive uniqueness).
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO users (id, email, hashed_password) "
                "VALUES (:id, :email, :hashed_password)"
            ),
            {
                "id": uuid.uuid4().hex,
                "email": "Alice@Example.com",
                "hashed_password": "x",
            },
        )


@pytest.mark.parametrize(
    "email", ["", "   ", "no-at-sign", "@example.com", "alice@", "a b@example.com"]
)
def test_create_user_rejects_invalid_email(db, email):
    with pytest.raises(ValueError):
        create_user(db, email=email, password=PASSWORD)

    assert _user_count(db) == 0


def test_create_user_rejects_too_long_email(db):
    email = "a" * 310 + "@example.com"

    with pytest.raises(ValueError):
        create_user(db, email=email, password=PASSWORD)


def test_create_user_rejects_empty_password(db):
    with pytest.raises(ValueError):
        create_user(db, email="alice@example.com", password="")

    assert _user_count(db) == 0


def test_get_by_email_is_case_insensitive(db):
    user = create_user(db, email="alice@example.com", password=PASSWORD)

    assert get_by_email(db, "ALICE@example.COM") == user
    assert get_by_email(db, " alice@example.com ") == user


def test_get_by_email_returns_none_for_unknown_or_invalid_email(db):
    create_user(db, email="alice@example.com", password=PASSWORD)

    assert get_by_email(db, "bob@example.com") is None
    assert get_by_email(db, "not-an-email") is None


def test_get_by_id(db):
    user = create_user(db, email="alice@example.com", password=PASSWORD)

    assert get_by_id(db, user.id) == user
    assert get_by_id(db, uuid.uuid4()) is None


def test_user_repr_and_logs_do_not_expose_password_or_hash(db, caplog):
    with caplog.at_level(logging.DEBUG):
        user = create_user(db, email="alice@example.com", password=PASSWORD)

    assert user.hashed_password not in repr(user)
    assert "alice@example.com" not in repr(user)
    assert PASSWORD not in caplog.text
    assert user.hashed_password not in caplog.text

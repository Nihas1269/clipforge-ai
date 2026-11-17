"""Clip editor read endpoints: `GET /clips/{id}` and `GET /clips/limits`."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.core.config import get_settings
from app.core.security import create_access_token
from app.database import Base, create_db_engine, create_session_factory, get_db
from app.main import create_app
from app.models.clip import Clip, ClipSource, ClipStatus
from app.models.user import User
from app.models.video import Video, VideoStatus

SECRET = "test-only-jwt-signing-secret-0123456789abcdef"


@pytest.fixture
def session_factory():
    engine = create_db_engine(
        "sqlite:///:memory:",
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
    monkeypatch.setenv("SECRET_KEY", SECRET)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("CLIP_MIN_SECONDS", "5")
    monkeypatch.setenv("CLIP_MAX_SECONDS", "60")
    get_settings.cache_clear()
    application = create_app()

    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    application.dependency_overrides[get_db] = override_get_db
    with TestClient(application) as test_client:
        yield test_client
    get_settings.cache_clear()


def make_user(db, email: str) -> User:
    user = User(email=email, hashed_password="not-a-real-hash")
    db.add(user)
    db.commit()
    return user


def auth(user: User) -> dict[str, str]:
    token = create_access_token(
        user.id, token_version=user.token_version, settings=get_settings()
    )
    return {"Authorization": f"Bearer {token}"}


def make_clip(db, user: User) -> Clip:
    video = Video(
        user_id=user.id,
        original_filename="talk.mp4",
        content_type="video/mp4",
        size_bytes=1024,
        storage_key=f"users/{user.id}/videos/x/source.mp4",
        status=VideoStatus.READY,
        duration_seconds=120.0,
    )
    db.add(video)
    db.flush()
    clip = Clip(
        video_id=video.id,
        start_seconds=10.0,
        end_seconds=40.0,
        title="A clip",
        reason="Test.",
        score=0.5,
        status=ClipStatus.ACCEPTED,
        source=ClipSource.MANUAL,
    )
    db.add(clip)
    db.commit()
    return clip


def test_read_clip_returns_the_owned_clip(client, db):
    owner = make_user(db, "owner@example.com")
    clip = make_clip(db, owner)

    response = client.get(f"/api/v1/clips/{clip.id}", headers=auth(owner))

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(clip.id)
    assert body["video_id"] == str(clip.video_id)
    assert body["start_seconds"] == 10.0
    assert body["end_seconds"] == 40.0
    assert body["status"] == "accepted"


def test_read_clip_requires_authentication(client, db):
    owner = make_user(db, "owner@example.com")
    clip = make_clip(db, owner)

    assert client.get(f"/api/v1/clips/{clip.id}").status_code == 401


def test_read_clip_of_another_user_is_not_found(client, db):
    owner = make_user(db, "owner@example.com")
    other = make_user(db, "other@example.com")
    clip = make_clip(db, owner)

    response = client.get(f"/api/v1/clips/{clip.id}", headers=auth(other))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_read_unknown_clip_is_not_found(client, db):
    owner = make_user(db, "owner@example.com")

    response = client.get(f"/api/v1/clips/{uuid.uuid4()}", headers=auth(owner))

    assert response.status_code == 404


def test_clip_limits_come_from_the_settings(client, db):
    user = make_user(db, "owner@example.com")

    response = client.get("/api/v1/clips/limits", headers=auth(user))

    assert response.status_code == 200
    assert response.json() == {"min_seconds": 5.0, "max_seconds": 60.0}


def test_clip_limits_require_authentication(client):
    assert client.get("/api/v1/clips/limits").status_code == 401

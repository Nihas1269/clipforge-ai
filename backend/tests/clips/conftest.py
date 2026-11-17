"""Fixtures for the clip detection and review tests.

Celery runs eagerly against an in-memory SQLite database and transcripts are
written directly to it, so no broker, FFmpeg, network or credentials are
needed. The heuristic detector runs for real (it is deterministic and
offline).
"""

import logging

import pytest
from celery import Task
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app import database
from app.core.config import Settings, get_settings
from app.database import Base, create_db_engine, create_session_factory, get_db
from app.main import create_app
from app.worker.celery_app import celery_app, configure_celery_app
from app.worker.jobs import JobTask
from tests.clips.helpers import (
    FRONTEND_ORIGIN,
    SECRET,
    add_transcript,
    auth_headers,
    make_video,
    register,
)


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
def storage_dir(tmp_path):
    return tmp_path / "storage"


@pytest.fixture(autouse=True)
def info_logging(caplog):
    """Build log records at INFO, as in production.

    Records are only created when the level is enabled, so a bad `extra`
    key (such as a reserved LogRecord attribute) would otherwise go unnoticed.
    """
    caplog.set_level(logging.INFO, logger="app")


@pytest.fixture(autouse=True)
def published_jobs(monkeypatch):
    """Record published Celery messages instead of sending them to a broker."""
    calls: list[dict] = []

    def fake_apply_async(self, args=None, kwargs=None, task_id=None, **options):
        calls.append({"task": self.name, "args": args, "task_id": task_id})

    monkeypatch.setitem(celery_app.conf, "task_always_eager", False)
    monkeypatch.setitem(celery_app.conf, "broker_url", "memory://")
    monkeypatch.setattr(JobTask, "apply_async", fake_apply_async)
    return calls


@pytest.fixture
def eager_jobs(session_factory, monkeypatch):
    """Run queued jobs immediately against the test database."""
    monkeypatch.setattr(database, "_session_factory", session_factory)
    monkeypatch.setattr(JobTask, "apply_async", Task.apply_async)
    configure_celery_app(celery_app, Settings(APP_ENV="test"))
    yield
    configure_celery_app(celery_app, Settings(APP_ENV="test"))


@pytest.fixture
def client(session_factory, storage_dir, monkeypatch) -> TestClient:
    monkeypatch.setenv("SECRET_KEY", SECRET)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("CORS_ORIGINS", FRONTEND_ORIGIN)
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(storage_dir))
    for name in (
        "CLIP_DETECTOR",
        "CLIP_MIN_SECONDS",
        "CLIP_MAX_SECONDS",
        "CLIP_MAX_CLIPS",
        "CLIP_KEYWORDS",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    application = create_app()

    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    application.dependency_overrides[get_db] = override_get_db
    return TestClient(application)


@pytest.fixture
def alice(client):
    user_id = register(client, "alice@example.com")
    return {"id": user_id, "headers": auth_headers(client, "alice@example.com")}


@pytest.fixture
def bob(client):
    user_id = register(client, "bob@example.com")
    return {"id": user_id, "headers": auth_headers(client, "bob@example.com")}


@pytest.fixture
def video_id(session_factory, storage_dir, alice):
    return make_video(session_factory, storage_dir, alice["id"])


@pytest.fixture
def transcribed_video_id(session_factory, video_id):
    add_transcript(session_factory, video_id)
    return video_id

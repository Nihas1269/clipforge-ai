import uuid
from datetime import datetime, timezone

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app import database
from app.core.config import Settings, get_settings
from app.core.request_id import REQUEST_ID_HEADER
from app.core.security import create_access_token
from app.database import Base, create_db_engine, create_session_factory, get_db
from app.main import create_app
from app.models.processing_job import JobStatus, JobType, ProcessingJob
from app.repositories.users import create_user
from app.worker.celery_app import celery_app, configure_celery_app
from app.worker.jobs import JobContext, enqueue_job, job_task

SECRET = "test-only-jwt-signing-secret-0123456789abcdef"
PASSWORD = "s3cure-Passw0rd"
ENQUEUE_URL = "/test/enqueue"

PUBLIC_JOB_FIELDS = {
    "id",
    "job_type",
    "status",
    "video_id",
    "attempts",
    "max_attempts",
    "error_message",
    "created_at",
    "started_at",
    "finished_at",
}


@job_task(JobType.METADATA, name="tests.jobs_api.noop")
def noop(ctx: JobContext) -> None:
    return None


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
    get_settings.cache_clear()
    monkeypatch.setattr(database, "_session_factory", session_factory)
    configure_celery_app(celery_app, Settings(APP_ENV="test"))
    application = create_app()

    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    application.dependency_overrides[get_db] = override_get_db

    @application.post(ENQUEUE_URL)
    def enqueue(db: Session = Depends(get_db)) -> dict[str, str]:
        job = enqueue_job(JobType.METADATA, noop, db=db)
        return {"id": str(job.id)}

    return TestClient(application)


def _user_and_token(db, email: str):
    user = create_user(db, email=email, password=PASSWORD)
    token = create_access_token(
        user.id, token_version=user.token_version, settings=get_settings()
    )
    return user, {"Authorization": f"Bearer {token}"}


def _job(db, user_id, **values) -> ProcessingJob:
    job = ProcessingJob(
        user_id=user_id,
        job_type=JobType.TRANSCRIPTION,
        status=JobStatus.QUEUED,
        max_attempts=3,
        celery_task_id=str(uuid.uuid4()),
        idempotency_key="internal-key",
        payload={"text": "private"},
        request_id="req-internal",
        **values,
    )
    db.add(job)
    db.commit()
    return job


def test_owner_reads_job_status(client, db):
    alice, headers = _user_and_token(db, "alice@example.com")
    job = _job(db, alice.id)

    response = client.get(f"/api/v1/jobs/{job.id}", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == PUBLIC_JOB_FIELDS
    assert body["id"] == str(job.id)
    assert body["job_type"] == "transcription"
    assert body["status"] == "queued"
    assert body["attempts"] == 0
    assert body["max_attempts"] == 3
    assert body["error_message"] is None
    assert body["started_at"] is None
    for internal in ("private", "internal-key", "req-internal", job.celery_task_id):
        assert internal not in response.text


def test_owner_reads_failed_job_error(client, db):
    alice, headers = _user_and_token(db, "alice@example.com")
    now = datetime.now(timezone.utc)
    job = _job(
        db,
        alice.id,
        attempts=1,
        error_message="The video has no audio track.",
        started_at=now,
        finished_at=now,
    )
    job.status = JobStatus.FAILED
    db.commit()

    body = client.get(f"/api/v1/jobs/{job.id}", headers=headers).json()

    assert body["status"] == "failed"
    assert body["error_message"] == "The video has no audio track."
    assert body["finished_at"] is not None


def test_job_requires_authentication(client, db):
    alice, _ = _user_and_token(db, "alice@example.com")
    job = _job(db, alice.id)

    response = client.get(f"/api/v1/jobs/{job.id}")

    assert response.status_code == 401
    assert "transcription" not in response.text


def test_other_users_job_is_not_found(client, db):
    alice, _ = _user_and_token(db, "alice@example.com")
    _, bob_headers = _user_and_token(db, "bob@example.com")
    job = _job(db, alice.id)

    other = client.get(f"/api/v1/jobs/{job.id}", headers=bob_headers)
    missing = client.get(f"/api/v1/jobs/{uuid.uuid4()}", headers=bob_headers)

    assert other.status_code == 404
    assert other.json() == missing.json()
    assert other.json()["error"]["code"] == "not_found"


def test_job_without_owner_is_not_found(client, db):
    _, headers = _user_and_token(db, "alice@example.com")
    job = _job(db, None)

    assert client.get(f"/api/v1/jobs/{job.id}", headers=headers).status_code == 404


def test_invalid_job_id_returns_422(client, db):
    _, headers = _user_and_token(db, "alice@example.com")

    assert client.get("/api/v1/jobs/not-a-uuid", headers=headers).status_code == 422


# Request ID propagation


def test_request_id_header_is_propagated_into_the_job(client, db):
    response = client.post(ENQUEUE_URL, headers={REQUEST_ID_HEADER: "req-abc_123"})

    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] == "req-abc_123"
    job = db.get(ProcessingJob, uuid.UUID(response.json()["id"]))
    assert job.request_id == "req-abc_123"
    assert job.status == JobStatus.SUCCEEDED


@pytest.mark.parametrize(
    "incoming", [None, "", "has space", "x" * 65, "../etc", "a;b=c"]
)
def test_missing_or_unsafe_request_id_is_replaced(client, db, incoming):
    headers = {} if incoming is None else {REQUEST_ID_HEADER: incoming}

    response = client.post(ENQUEUE_URL, headers=headers)

    request_id = response.headers[REQUEST_ID_HEADER]
    assert request_id != incoming
    assert len(request_id) == 32
    job = db.get(ProcessingJob, uuid.UUID(response.json()["id"]))
    assert job.request_id == request_id


def test_every_response_has_a_request_id(client):
    first = client.get("/health")
    second = client.get("/health")

    assert first.headers[REQUEST_ID_HEADER]
    assert first.headers[REQUEST_ID_HEADER] != second.headers[REQUEST_ID_HEADER]

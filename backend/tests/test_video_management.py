"""Tests for listing, reading, deleting and streaming videos.

Storage is LocalStorage in tmp_path; the S3-style redirect path uses a fake
backend that issues URLs, so no network access or credentials are needed.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app import database
from app.api.v1.videos import MEDIA_URL_EXPIRE_SECONDS, get_video_storage
from app.core.config import Settings, get_settings
from app.core.security import create_access_token
from app.database import Base, create_db_engine, create_session_factory, get_db
from app.main import create_app
from app.models.processing_job import JobStatus, JobType, ProcessingJob
from app.models.storage_deletion import PendingStorageDeletion
from app.models.video import Video, VideoStatus
from app.repositories.users import create_user
from app.services.http_range import (
    MAX_RANGE_HEADER_LENGTH,
    RangeNotSatisfiableError,
    parse_range_header,
)
from app.services.storage_cleanup import retry_pending_storage_deletions
from app.storage import LocalStorage, StorageError, validate_key, video_source_key
from app.worker.celery_app import (
    RETRY_STORAGE_DELETIONS_TASK_NAME,
    celery_app,
    configure_celery_app,
)
from app.worker.storage_cleanup import retry_storage_deletions_task

SECRET = "test-only-jwt-signing-secret-0123456789abcdef"
PASSWORD = "s3cure-Passw0rd"
FRONTEND_ORIGIN = "http://localhost:3000"
EVIL_ORIGIN = "http://evil.example"
VIDEOS_URL = "/api/v1/videos"

CONTENT = bytes(range(256)) * 4  # 1024 bytes, every offset distinguishable
FAKE_URL_HOST = "https://storage.example.test"

VIDEO_FIELDS = {
    "id",
    "user_id",
    "original_filename",
    "content_type",
    "size_bytes",
    "status",
    "error_message",
    "duration_seconds",
    "width",
    "height",
    "fps",
    "created_at",
    "updated_at",
    "project",
}


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


@pytest.fixture
def storage(storage_dir):
    return LocalStorage(storage_dir)


@pytest.fixture
def client(session_factory, storage_dir, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", SECRET)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("CORS_ORIGINS", FRONTEND_ORIGIN)
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(storage_dir))
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


def _user(db, email):
    user = create_user(db, email=email, password=PASSWORD)
    token = create_access_token(
        user.id, token_version=user.token_version, settings=get_settings()
    )
    return user, token


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def alice(client, db):
    return _user(db, "alice@example.com")


@pytest.fixture
def bob(client, db):
    return _user(db, "bob@example.com")


def _video(
    db,
    storage,
    user,
    *,
    content=CONTENT,
    store=True,
    created_at=None,
    status=VideoStatus.UPLOADED,
    content_type="video/mp4",
):
    video_id = uuid.uuid4()
    key = video_source_key(str(user.id), str(video_id), ".mp4")
    if store:
        storage.save(key, BytesIO(content))
    video = Video(
        id=video_id,
        user_id=user.id,
        original_filename="talk.mp4",
        content_type=content_type,
        size_bytes=len(content),
        storage_key=key,
        status=status,
    )
    if created_at is not None:
        video.created_at = created_at
    db.add(video)
    db.commit()
    # Detached, so its attributes stay readable after the row is deleted.
    db.expunge(video)
    return video


def _count(db, model) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(model))


def _video_exists(db, video_id) -> bool:
    db.expire_all()
    return db.get(Video, video_id) is not None


# Authentication and ownership on every endpoint


@pytest.mark.parametrize(
    ("method", "suffix"),
    [("get", ""), ("get", "/{id}"), ("delete", "/{id}"), ("get", "/{id}/media")],
)
def test_every_endpoint_requires_authentication(
    client, db, storage, alice, method, suffix
):
    user, _ = alice
    video = _video(db, storage, user)

    response = client.request(method.upper(), VIDEOS_URL + suffix.format(id=video.id))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert _video_exists(db, video.id)
    assert storage.exists(video.storage_key)


@pytest.mark.parametrize(
    ("method", "suffix"),
    [("get", "/{id}"), ("delete", "/{id}"), ("get", "/{id}/media")],
)
def test_other_users_video_is_not_found(
    client, db, storage, alice, bob, method, suffix
):
    user, _ = alice
    _, bob_token = bob
    video = _video(db, storage, user)

    other = client.request(
        method.upper(),
        VIDEOS_URL + suffix.format(id=video.id),
        headers=_bearer(bob_token),
    )
    missing = client.request(
        method.upper(),
        VIDEOS_URL + suffix.format(id=uuid.uuid4()),
        headers=_bearer(bob_token),
    )

    assert other.status_code == 404
    # Indistinguishable from a video that does not exist.
    assert other.json() == missing.json()
    assert other.json()["error"]["code"] == "not_found"
    assert _video_exists(db, video.id)
    assert storage.exists(video.storage_key)


def test_list_never_includes_other_users_videos(client, db, storage, alice, bob):
    alice_user, _ = alice
    _, bob_token = bob
    _video(db, storage, alice_user)

    response = client.get(VIDEOS_URL, headers=_bearer(bob_token))

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


@pytest.mark.parametrize("suffix", ["/not-a-uuid", "/not-a-uuid/media"])
def test_invalid_video_id_returns_422(client, alice, suffix):
    _, token = alice

    assert client.get(VIDEOS_URL + suffix, headers=_bearer(token)).status_code == 422


# GET /videos


def _videos_at(db, storage, user, count, **values):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        _video(
            db,
            storage,
            user,
            store=False,
            created_at=base + timedelta(minutes=i),
            **values,
        )
        for i in range(count)
    ]


def test_list_is_paginated_newest_first(client, db, storage, alice, bob):
    user, token = alice
    videos = _videos_at(db, storage, user, 5)
    _videos_at(db, storage, bob[0], 2)
    newest_first = [str(v.id) for v in reversed(videos)]

    pages = [
        client.get(
            VIDEOS_URL, params={"limit": 2, "offset": offset}, headers=_bearer(token)
        )
        for offset in (0, 2, 4, 10)
    ]

    for page in pages:
        assert page.status_code == 200
        assert page.json()["total"] == 5
    ids = [[item["id"] for item in page.json()["items"]] for page in pages]
    assert ids == [newest_first[0:2], newest_first[2:4], newest_first[4:5], []]
    item = pages[0].json()["items"][0]
    assert set(item) == VIDEO_FIELDS
    assert "storage_key" not in pages[0].text
    assert videos[-1].storage_key not in pages[0].text


def test_list_defaults_to_20_items(client, db, storage, alice):
    user, token = alice
    _videos_at(db, storage, user, 21)

    body = client.get(VIDEOS_URL, headers=_bearer(token)).json()

    assert len(body["items"]) == 20
    assert body["total"] == 21


def test_list_accepts_the_maximum_page_size(client, db, storage, alice):
    user, token = alice
    _videos_at(db, storage, user, 3)

    response = client.get(VIDEOS_URL, params={"limit": 100}, headers=_bearer(token))

    assert response.status_code == 200
    assert len(response.json()["items"]) == 3


def test_list_filters_by_status(client, db, storage, alice):
    user, token = alice
    ready = _videos_at(db, storage, user, 2, status=VideoStatus.READY)
    _videos_at(db, storage, user, 3, status=VideoStatus.UPLOADED)

    body = client.get(
        VIDEOS_URL, params={"status": "ready"}, headers=_bearer(token)
    ).json()

    assert body["total"] == 2
    assert {item["id"] for item in body["items"]} == {str(v.id) for v in ready}
    assert all(item["status"] == "ready" for item in body["items"])
    failed = client.get(VIDEOS_URL, params={"status": "failed"}, headers=_bearer(token))
    assert failed.json() == {"items": [], "total": 0}


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": "x"},
        {"offset": -1},
        {"status": "archived"},
    ],
)
def test_list_rejects_invalid_parameters(client, alice, params):
    _, token = alice

    response = client.get(VIDEOS_URL, params=params, headers=_bearer(token))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# GET /videos/{id}


def test_owner_reads_video(client, db, storage, alice):
    user, token = alice
    video = _video(db, storage, user, status=VideoStatus.READY)

    response = client.get(f"{VIDEOS_URL}/{video.id}", headers=_bearer(token))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == VIDEO_FIELDS
    assert body["id"] == str(video.id)
    assert body["user_id"] == str(user.id)
    assert body["status"] == "ready"
    assert body["size_bytes"] == len(CONTENT)
    assert video.storage_key not in response.text


# DELETE /videos/{id}


def test_delete_removes_row_jobs_and_file(client, db, storage, alice):
    user, token = alice
    video = _video(db, storage, user)
    other = _video(db, storage, user)
    db.add(
        ProcessingJob(
            user_id=user.id,
            video_id=video.id,
            job_type=JobType.METADATA,
            status=JobStatus.QUEUED,
        )
    )
    db.commit()

    response = client.delete(f"{VIDEOS_URL}/{video.id}", headers=_bearer(token))

    assert response.status_code == 204
    assert response.content == b""
    assert not _video_exists(db, video.id)
    assert not storage.exists(video.storage_key)
    assert _count(db, ProcessingJob) == 0
    assert _count(db, PendingStorageDeletion) == 0
    # Other videos are untouched.
    assert _video_exists(db, other.id)
    assert storage.exists(other.storage_key)
    assert (
        client.get(f"{VIDEOS_URL}/{video.id}", headers=_bearer(token)).status_code
        == 404
    )
    again = client.delete(f"{VIDEOS_URL}/{video.id}", headers=_bearer(token))
    assert again.status_code == 404


def test_delete_succeeds_when_the_file_is_already_missing(client, db, storage, alice):
    user, token = alice
    video = _video(db, storage, user, store=False)

    response = client.delete(f"{VIDEOS_URL}/{video.id}", headers=_bearer(token))

    assert response.status_code == 204
    assert not _video_exists(db, video.id)
    assert _count(db, PendingStorageDeletion) == 0


class FailingDeleteStorage(LocalStorage):
    """Local storage whose deletes fail, as during a storage outage."""

    def delete(self, key):
        raise StorageError("simulated storage outage")


def test_failed_file_deletion_is_recorded_and_retried(
    client, db, storage, storage_dir, alice, caplog
):
    user, token = alice
    video = _video(db, storage, user)
    client.app.dependency_overrides[get_video_storage] = lambda: FailingDeleteStorage(
        storage_dir
    )

    with caplog.at_level(logging.INFO):
        response = client.delete(f"{VIDEOS_URL}/{video.id}", headers=_bearer(token))

    # The video is gone even though its file could not be deleted yet.
    assert response.status_code == 204
    assert not _video_exists(db, video.id)
    assert storage.exists(video.storage_key)
    pending = db.scalars(select(PendingStorageDeletion)).one()
    assert pending.storage_key == video.storage_key
    assert pending.attempts == 1
    assert pending.last_error_type == "StorageError"
    assert pending.last_attempt_at is not None
    failures = [
        r for r in caplog.records if r.getMessage() == "storage_deletion_failed"
    ]
    assert len(failures) == 1
    assert failures[0].error_type == "StorageError"
    assert video.storage_key not in caplog.text
    assert "simulated storage outage" not in caplog.text

    # Still failing: the attempt is counted and the record kept.
    assert retry_pending_storage_deletions(db, FailingDeleteStorage(storage_dir)) == 0
    db.expire_all()
    assert db.scalars(select(PendingStorageDeletion)).one().attempts == 2

    # Storage is back: the retry deletes the file and clears the record.
    assert retry_pending_storage_deletions(db, storage) == 1
    assert not storage.exists(video.storage_key)
    assert _count(db, PendingStorageDeletion) == 0


def test_database_failure_deletes_nothing(client, session_factory, db, storage, alice):
    user, token = alice
    video = _video(db, storage, user)

    def failing_db():
        session = session_factory()

        def fail_commit():
            raise SQLAlchemyError("simulated database failure")

        session.commit = fail_commit
        try:
            yield session
        finally:
            session.close()

    client.app.dependency_overrides[get_db] = failing_db

    response = client.delete(f"{VIDEOS_URL}/{video.id}", headers=_bearer(token))

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "delete_failed"
    assert "simulated" not in response.text
    assert _video_exists(db, video.id)
    assert storage.exists(video.storage_key)
    assert _count(db, PendingStorageDeletion) == 0


def test_cookie_authenticated_delete_requires_trusted_origin(
    client, db, storage, alice
):
    user, token = alice
    video = _video(db, storage, user)
    client.cookies.set("access_token", token)

    evil = client.delete(f"{VIDEOS_URL}/{video.id}", headers={"Origin": EVIL_ORIGIN})
    assert evil.status_code == 403
    assert _video_exists(db, video.id)

    trusted = client.delete(
        f"{VIDEOS_URL}/{video.id}", headers={"Origin": FRONTEND_ORIGIN}
    )
    assert trusted.status_code == 204
    assert not _video_exists(db, video.id)


# GET /videos/{id}/media: streaming


def _media(client, video, token, **headers):
    return client.get(
        f"{VIDEOS_URL}/{video.id}/media", headers={**_bearer(token), **headers}
    )


def test_media_full_request_returns_200(client, db, storage, alice):
    user, token = alice
    video = _video(db, storage, user)

    response = _media(client, video, token)

    assert response.status_code == 200
    assert response.content == CONTENT
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["content-length"] == str(len(CONTENT))
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-range" not in response.headers


def test_media_range_returns_206_with_exact_bytes(client, db, storage, alice):
    user, token = alice
    video = _video(db, storage, user)

    response = _media(client, video, token, Range="bytes=0-9")

    assert response.status_code == 206
    assert response.content == CONTENT[:10]
    assert len(response.content) == 10
    assert response.headers["content-range"] == f"bytes 0-9/{len(CONTENT)}"
    assert response.headers["content-length"] == "10"
    assert response.headers["content-type"] == "video/mp4"


@pytest.mark.parametrize(
    ("header", "start", "end"),
    [
        ("bytes=100-199", 100, 199),
        ("bytes=1000-", 1000, 1023),
        ("bytes=-24", 1000, 1023),
        ("bytes=1020-5000", 1020, 1023),
        ("bytes=1023-1023", 1023, 1023),
    ],
)
def test_media_range_forms(client, db, storage, alice, header, start, end):
    user, token = alice
    video = _video(db, storage, user)

    response = _media(client, video, token, Range=header)

    assert response.status_code == 206
    assert response.content == CONTENT[start : end + 1]
    assert response.headers["content-range"] == f"bytes {start}-{end}/{len(CONTENT)}"
    assert response.headers["content-length"] == str(end - start + 1)


@pytest.mark.parametrize(
    "header",
    [
        "bytes=1024-",
        "bytes=5000-6000",
        "bytes=-0",
        "bytes=9-5",
        "bytes=abc",
        "items=0-9",
        "bytes=",
    ],
)
def test_invalid_or_unsatisfiable_range_returns_416(client, db, storage, alice, header):
    user, token = alice
    video = _video(db, storage, user)

    response = _media(client, video, token, Range=header)

    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(CONTENT)}"
    assert response.json()["error"]["code"] == "requested_range_not_satisfiable"
    assert CONTENT[:16] not in response.content


def test_multiple_ranges_return_the_whole_file(client, db, storage, alice):
    user, token = alice
    video = _video(db, storage, user)

    response = _media(client, video, token, Range="bytes=0-9,20-29")

    assert response.status_code == 200
    assert response.content == CONTENT


def test_conditional_range_returns_the_whole_file(client, db, storage, alice):
    user, token = alice
    video = _video(db, storage, user)

    response = _media(client, video, token, Range="bytes=0-9", **{"If-Range": '"etag"'})

    assert response.status_code == 200
    assert response.content == CONTENT


def test_empty_file_streams_with_zero_length(client, db, storage, alice):
    user, token = alice
    video = _video(db, storage, user, content=b"")

    full = _media(client, video, token)
    ranged = _media(client, video, token, Range="bytes=0-9")

    assert full.status_code == 200
    assert full.content == b""
    assert full.headers["content-length"] == "0"
    assert ranged.status_code == 416
    assert ranged.headers["content-range"] == "bytes */0"


def test_unknown_content_type_is_served_as_octet_stream(client, db, storage, alice):
    user, token = alice
    video = _video(db, storage, user, content_type="text/html")

    response = _media(client, video, token)

    assert response.headers["content-type"] == "application/octet-stream"


def test_missing_file_returns_404(client, db, storage, alice):
    user, token = alice
    video = _video(db, storage, user, store=False)

    response = _media(client, video, token)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "media_not_found"
    assert video.storage_key not in response.text


class CountingReader:
    """A file wrapper that counts the bytes read and records closing."""

    def __init__(self, fileobj, seekable=True):
        self._fileobj = fileobj
        self._seekable = seekable
        self.bytes_read = 0
        self.closed = False

    def read(self, size=-1):
        data = self._fileobj.read(size)
        self.bytes_read += len(data)
        return data

    def seekable(self):
        return self._seekable

    def seek(self, offset, whence=0):
        if not self._seekable:
            raise OSError("not seekable")
        return self._fileobj.seek(offset, whence)

    def close(self):
        self.closed = True
        self._fileobj.close()


class CountingStorage(LocalStorage):
    def __init__(self, root, seekable=True):
        super().__init__(root)
        self.seekable = seekable
        self.readers = []

    def open(self, key):
        reader = CountingReader(super().open(key), seekable=self.seekable)
        self.readers.append(reader)
        return reader


@pytest.mark.parametrize(("seekable", "expected_read"), [(True, 10), (False, 110)])
def test_range_streaming_is_bounded(
    client, db, storage, storage_dir, alice, seekable, expected_read
):
    user, token = alice
    big = bytes(range(256)) * 4096  # 1 MiB
    video = _video(db, storage, user, content=big)
    counting = CountingStorage(storage_dir, seekable=seekable)
    client.app.dependency_overrides[get_video_storage] = lambda: counting

    response = _media(client, video, token, Range="bytes=100-109")

    assert response.status_code == 206
    assert response.content == big[100:110]
    (reader,) = counting.readers
    # Only the requested bytes (plus the skipped prefix when the stream
    # cannot seek) are read, never the whole file.
    assert reader.bytes_read == expected_read
    assert reader.closed


# GET /videos/{id}/media: redirect (S3-style backend)


class FakeUrlStorage(LocalStorage):
    """A backend that issues download URLs, like S3 presigned URLs."""

    def __init__(self, root):
        super().__init__(root)
        self.url_requests = []

    def get_download_url(self, key, expires_seconds):
        validate_key(key)
        self.url_requests.append((key, expires_seconds))
        return f"{FAKE_URL_HOST}/object?X-Amz-Expires={expires_seconds}&X-Amz-Signature=fake"


def test_media_redirects_to_short_lived_url(
    client, db, storage, storage_dir, alice, bob
):
    user, token = alice
    _, bob_token = bob
    video = _video(db, storage, user)
    fake = FakeUrlStorage(storage_dir)
    client.app.dependency_overrides[get_video_storage] = lambda: fake

    response = client.get(
        f"{VIDEOS_URL}/{video.id}/media", headers=_bearer(token), follow_redirects=False
    )

    assert response.status_code == 307
    assert response.headers["location"].startswith(f"{FAKE_URL_HOST}/object?")
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert fake.url_requests == [(video.storage_key, MEDIA_URL_EXPIRE_SECONDS)]
    assert MEDIA_URL_EXPIRE_SECONDS <= 15 * 60

    # Another user never gets a URL.
    other = client.get(
        f"{VIDEOS_URL}/{video.id}/media",
        headers=_bearer(bob_token),
        follow_redirects=False,
    )
    assert other.status_code == 404
    assert len(fake.url_requests) == 1


class BrokenUrlStorage(LocalStorage):
    def get_download_url(self, key, expires_seconds):
        raise StorageError("simulated signing failure")


def test_media_url_failure_returns_503(client, db, storage, storage_dir, alice):
    user, token = alice
    video = _video(db, storage, user)
    client.app.dependency_overrides[get_video_storage] = lambda: BrokenUrlStorage(
        storage_dir
    )

    response = _media(client, video, token)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert "simulated" not in response.text


# Storage cleanup worker


def test_retry_task_deletes_pending_objects(
    session_factory, db, storage, storage_dir, monkeypatch
):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(storage_dir))
    get_settings.cache_clear()
    monkeypatch.setattr(database, "_session_factory", session_factory)
    configure_celery_app(celery_app, Settings(APP_ENV="test"))
    key = video_source_key(str(uuid.uuid4()), str(uuid.uuid4()), ".mp4")
    storage.save(key, BytesIO(CONTENT))
    db.add(PendingStorageDeletion(storage_key=key, attempts=3))
    db.commit()

    result = retry_storage_deletions_task.apply()

    assert result.get() == 1
    assert not storage.exists(key)
    assert _count(db, PendingStorageDeletion) == 0


def test_retry_task_is_scheduled():
    schedule = celery_app.conf.beat_schedule
    assert any(
        entry["task"] == RETRY_STORAGE_DELETIONS_TASK_NAME
        for entry in schedule.values()
    )


def test_retry_rejects_non_positive_limit(db, storage):
    with pytest.raises(ValueError):
        retry_pending_storage_deletions(db, storage, limit=0)


# Range header parsing


@pytest.mark.parametrize(
    ("header", "size", "expected"),
    [
        ("bytes=0-9", 100, (0, 9)),
        ("bytes=5-", 100, (5, 99)),
        ("bytes=-10", 100, (90, 99)),
        ("bytes=-500", 100, (0, 99)),
        ("bytes=90-500", 100, (90, 99)),
        ("bytes=0-0", 1, (0, 0)),
        (" Bytes = 3-4 ", 10, (3, 4)),
    ],
)
def test_parse_range_header_single_ranges(header, size, expected):
    byte_range = parse_range_header(header, size)

    assert (byte_range.start, byte_range.end) == expected
    assert byte_range.length == expected[1] - expected[0] + 1
    assert byte_range.content_range(size) == f"bytes {expected[0]}-{expected[1]}/{size}"


@pytest.mark.parametrize(
    ("header", "size"),
    [(None, 100), ("bytes=0-1,5-6", 100), ("bytes=0-1, 500-", 100)],
)
def test_parse_range_header_whole_file(header, size):
    assert parse_range_header(header, size) is None


@pytest.mark.parametrize(
    ("header", "size"),
    [
        ("bytes=100-", 100),
        ("bytes=100-200", 100),
        ("bytes=-0", 100),
        ("bytes=9-5", 100),
        ("bytes=0-", 0),
        ("bytes=-5", 0),
        ("items=0-9", 100),
        ("0-9", 100),
        ("bytes=", 100),
        ("bytes=-", 100),
        ("bytes=abc", 100),
        ("bytes=1.5-2", 100),
        ("bytes=+1-2", 100),
        ("bytes=0-1,x", 100),
        ("bytes=200-300,400-", 100),
        ("bytes=" + "1" * 20 + "-", 100),
        ("bytes=0-9" + " " * MAX_RANGE_HEADER_LENGTH, 100),
    ],
)
def test_parse_range_header_rejects_invalid_or_unsatisfiable(header, size):
    with pytest.raises(RangeNotSatisfiableError):
        parse_range_header(header, size)

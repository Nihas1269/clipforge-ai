import asyncio
import logging
import uuid

import pytest
import starlette.formparsers
from celery import Task
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app import database
from app.api.v1.videos import MULTIPART_OVERHEAD_BYTES, get_video_storage
from app.core.config import Settings, get_settings
from app.database import Base, create_db_engine, create_session_factory, get_db
from app.main import create_app
from app.media.ffmpeg import MediaError, MediaInfo
from app.models.processing_job import JobStatus, JobType, ProcessingJob
from app.models.video import Video, VideoStatus
from app.services.video_upload import (
    EBML,
    ISO_BMFF,
    detect_container,
    file_extension,
    sanitize_filename,
)
from app.storage import LocalStorage, StorageError, video_source_key
from app.worker import metadata
from app.worker.celery_app import celery_app, configure_celery_app
from app.worker.jobs import JobTask
from app.worker.metadata import EXTRACT_METADATA_TASK_NAME, QUEUE_FAILURE_MESSAGE
from tests.media_helpers import make_test_video, requires_ffmpeg

SQLITE_IN_MEMORY_URL = "sqlite:///:memory:"
SECRET = "test-only-jwt-signing-secret-0123456789abcdef"
PASSWORD = "s3cure-Passw0rd"
FRONTEND_ORIGIN = "http://localhost:3000"
EVIL_ORIGIN = "http://evil.example"

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
VIDEOS_URL = "/api/v1/videos"

MIB = 1024 * 1024
# A 24-byte ISO BMFF `ftyp` box, as at the start of an MP4 file.
MP4_HEADER = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
MOV_HEADER = b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00qt  "
# The EBML header magic that starts every WebM/MKV file.
EBML_HEADER = b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01"
CONTENT_MARKER = b"PRIVATE-CONTENT-MARKER"
MP4_BYTES = MP4_HEADER + CONTENT_MARKER + b"\x00" * 1000

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
def storage_dir(tmp_path):
    return tmp_path / "storage"


@pytest.fixture(autouse=True)
def published_jobs(monkeypatch):
    """Record published Celery messages instead of sending them to a broker.

    Uploads queue a metadata job; here it stays queued, so the video keeps
    the status `uploaded`. Tests that run the job use `eager_jobs`.
    """
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
    # Undo published_jobs' fake publishing (autouse fixtures run first).
    monkeypatch.setattr(JobTask, "apply_async", Task.apply_async)
    configure_celery_app(celery_app, Settings(APP_ENV="test"))
    yield
    configure_celery_app(celery_app, Settings(APP_ENV="test"))


@pytest.fixture
def make_client(session_factory, storage_dir, monkeypatch):
    """Build a TestClient using the test database and local storage in tmp_path."""
    monkeypatch.setenv("SECRET_KEY", SECRET)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("CORS_ORIGINS", FRONTEND_ORIGIN)
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(storage_dir))
    monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "1")
    for name in ("ALLOWED_VIDEO_EXTENSIONS", "ALLOWED_VIDEO_CONTENT_TYPES"):
        monkeypatch.delenv(name, raising=False)

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
        return TestClient(application)

    return factory


@pytest.fixture
def client(make_client):
    return make_client()


def _register(client, email="alice@example.com"):
    response = client.post(REGISTER_URL, json={"email": email, "password": PASSWORD})
    assert response.status_code == 201
    return response.json()["id"]


def _token(client, email="alice@example.com") -> str:
    response = client.post(LOGIN_URL, json={"email": email, "password": PASSWORD})
    assert response.status_code == 200
    client.cookies.clear()
    return response.json()["access_token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def token(client):
    _register(client)
    return _token(client)


def _upload(
    client,
    token=None,
    filename="clip.mp4",
    content=MP4_BYTES,
    content_type="video/mp4",
    field="file",
    headers=None,
):
    all_headers = dict(headers or {})
    if token:
        all_headers.update(_bearer(token))
    return client.post(
        VIDEOS_URL,
        files={field: (filename, content, content_type)},
        headers=all_headers,
    )


def _stored_files(storage_dir) -> list:
    if not storage_dir.exists():
        return []
    return [path for path in storage_dir.rglob("*") if path.is_file()]


def _video_count(db) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(Video))


def _assert_nothing_stored(db, storage_dir):
    assert _stored_files(storage_dir) == []
    assert _video_count(db) == 0


# Success


def test_valid_mp4_upload_is_stored_and_returns_201(client, db, token, storage_dir):
    response = _upload(client, token)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == VIDEO_FIELDS
    assert body["status"] == "uploaded"
    assert body["original_filename"] == "clip.mp4"
    assert body["content_type"] == "video/mp4"
    assert body["size_bytes"] == len(MP4_BYTES)
    assert "storage_key" not in response.text

    video = db.scalars(select(Video)).one()
    assert str(video.id) == body["id"]
    assert str(video.user_id) == body["user_id"]
    assert video.status is VideoStatus.UPLOADED
    assert video.storage_key == video_source_key(
        str(video.user_id), str(video.id), ".mp4"
    )
    storage = LocalStorage(storage_dir)
    assert storage.exists(video.storage_key)
    with storage.open(video.storage_key) as stored:
        assert stored.read() == MP4_BYTES
    assert _stored_files(storage_dir) == [storage_dir / video.storage_key]


@pytest.mark.parametrize(
    ("filename", "content", "content_type", "stored_type", "ext"),
    [
        (
            "clip.mov",
            MOV_HEADER + b"\x00" * 100,
            "video/quicktime",
            "video/quicktime",
            ".mov",
        ),
        ("clip.webm", EBML_HEADER + b"\x00" * 100, "video/webm", "video/webm", ".webm"),
        (
            "clip.mkv",
            EBML_HEADER + b"\x00" * 100,
            "video/x-matroska",
            "video/x-matroska",
            ".mkv",
        ),
        ("CLIP.MP4", MP4_BYTES, "Video/MP4; codecs=avc1", "video/mp4", ".mp4"),
        ("clip.mov", MP4_BYTES, "video/mp4", "video/quicktime", ".mov"),
    ],
)
def test_supported_formats_are_accepted(
    client, db, token, storage_dir, filename, content, content_type, stored_type, ext
):
    response = _upload(
        client, token, filename=filename, content=content, content_type=content_type
    )

    assert response.status_code == 201
    assert response.json()["content_type"] == stored_type
    video = db.scalars(select(Video)).one()
    assert video.storage_key.endswith(f"/source{ext}")
    assert LocalStorage(storage_dir).exists(video.storage_key)


def test_upload_belongs_to_the_authenticated_user(client, db, storage_dir):
    alice_id = _register(client, "alice@example.com")
    bob_id = _register(client, "bob@example.com")

    alice = _upload(client, _token(client, "alice@example.com"))
    bob = _upload(client, _token(client, "bob@example.com"))

    assert alice.json()["user_id"] == alice_id
    assert bob.json()["user_id"] == bob_id
    videos = {str(v.id): v for v in db.scalars(select(Video))}
    assert videos[alice.json()["id"]].storage_key.startswith(f"users/{alice_id}/")
    assert videos[bob.json()["id"]].storage_key.startswith(f"users/{bob_id}/")


# Metadata extraction after upload


def test_upload_queues_metadata_extraction(client, db, token, published_jobs):
    response = _upload(client, token)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "uploaded"
    job = db.scalars(select(ProcessingJob)).one()
    assert job.job_type == JobType.METADATA
    assert job.status == JobStatus.QUEUED
    assert str(job.video_id) == body["id"]
    assert str(job.user_id) == body["user_id"]
    assert job.payload == {"video_id": body["id"]}
    assert job.idempotency_key == body["id"]
    assert job.request_id == response.headers["x-request-id"]
    assert published_jobs == [
        {
            "task": EXTRACT_METADATA_TASK_NAME,
            "args": [str(job.id)],
            "task_id": job.celery_task_id,
        }
    ]


def test_rejected_upload_queues_nothing(client, db, token, published_jobs):
    response = _upload(client, token, filename="clip.avi")

    assert response.status_code == 415
    assert published_jobs == []
    assert db.scalar(select(func.count()).select_from(ProcessingJob)) == 0


def test_upload_whose_job_cannot_be_queued_is_marked_failed(
    client, db, token, storage_dir, monkeypatch
):
    monkeypatch.setitem(celery_app.conf, "broker_url", None)

    response = _upload(client, token)

    # The file was stored, so the upload itself succeeded.
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "failed"
    assert body["error_message"] == QUEUE_FAILURE_MESSAGE
    video = db.scalars(select(Video)).one()
    assert video.status is VideoStatus.FAILED
    job = db.scalars(select(ProcessingJob)).one()
    assert job.status == JobStatus.FAILED
    assert job.active_key is None
    assert LocalStorage(storage_dir).exists(video.storage_key)


def test_upload_runs_metadata_extraction(client, db, token, eager_jobs, monkeypatch):
    def fake_probe(path, *, timeout=None, settings=None):
        return MediaInfo(42.0, 1280, 720, 30.0, True, True, "h264", "aac")

    monkeypatch.setattr(metadata, "probe", fake_probe)

    response = _upload(client, token)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "ready"
    assert body["duration_seconds"] == 42.0
    assert (body["width"], body["height"], body["fps"]) == (1280, 720, 30.0)
    assert db.scalars(select(ProcessingJob)).one().status == JobStatus.SUCCEEDED


def test_upload_that_is_not_a_valid_video_ends_failed(
    client, db, token, eager_jobs, monkeypatch
):
    def fake_probe(path, *, timeout=None, settings=None):
        raise MediaError("ffprobe could not read the file.", "moov atom not found")

    monkeypatch.setattr(metadata, "probe", fake_probe)

    response = _upload(client, token)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "failed"
    assert body["error_message"] == metadata.INVALID_VIDEO_MESSAGE
    assert "moov" not in response.text
    assert db.scalars(select(ProcessingJob)).one().status == JobStatus.FAILED


@requires_ffmpeg
def test_real_upload_is_probed_and_ready(client, token, eager_jobs, tmp_path):
    source = make_test_video(tmp_path / "sample.mp4")

    response = _upload(client, token, content=source.read_bytes())

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "ready"
    assert body["duration_seconds"] == pytest.approx(2.0, abs=0.25)
    assert (body["width"], body["height"]) == (320, 240)


@requires_ffmpeg
def test_real_invalid_upload_ends_failed(client, token, eager_jobs):
    # Valid MP4 signature, but not a playable file.
    response = _upload(client, token)

    assert response.status_code == 201
    assert response.json()["status"] == "failed"
    assert response.json()["error_message"] == metadata.INVALID_VIDEO_MESSAGE


def test_file_at_exact_size_limit_is_accepted(client, token):
    content = MP4_HEADER + b"\x00" * (MIB - len(MP4_HEADER))

    response = _upload(client, token, content=content)

    assert response.status_code == 201
    assert response.json()["size_bytes"] == MIB


# Authentication


def test_unauthenticated_upload_returns_401(client, db, storage_dir):
    response = _upload(client)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    _assert_nothing_stored(db, storage_dir)


def test_upload_with_invalid_token_returns_401(client, db, storage_dir):
    response = _upload(client, token="not-a-jwt")

    assert response.status_code == 401
    _assert_nothing_stored(db, storage_dir)


def test_cookie_authenticated_upload_requires_trusted_origin(client, db, storage_dir):
    _register(client)
    login = client.post(
        LOGIN_URL, json={"email": "alice@example.com", "password": PASSWORD}
    )
    assert login.status_code == 200

    evil = _upload(client, headers={"Origin": EVIL_ORIGIN})
    assert evil.status_code == 403
    _assert_nothing_stored(db, storage_dir)

    trusted = _upload(client, headers={"Origin": FRONTEND_ORIGIN})
    assert trusted.status_code == 201


# Type validation


@pytest.mark.parametrize(
    "filename",
    ["clip.avi", "clip.exe", "clip", "clip.mp4.exe", "clip.", "dir/"],
)
def test_disallowed_extension_is_rejected(client, db, token, storage_dir, filename):
    response = _upload(client, token, filename=filename)

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_file_type"
    _assert_nothing_stored(db, storage_dir)


@pytest.mark.parametrize(
    "content_type", ["application/octet-stream", "text/plain", "video/avi", "image/png"]
)
def test_disallowed_content_type_is_rejected(
    client, db, token, storage_dir, content_type
):
    response = _upload(client, token, content_type=content_type)

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_content_type"
    _assert_nothing_stored(db, storage_dir)


@pytest.mark.parametrize(
    ("filename", "content", "content_type"),
    [
        # Signature from another container family than the extension.
        ("clip.mp4", EBML_HEADER + b"\x00" * 100, "video/mp4"),
        ("clip.webm", MP4_BYTES, "video/webm"),
        ("clip.mkv", MOV_HEADER, "video/x-matroska"),
        # Not a video at all, despite the extension and content type.
        ("clip.mp4", b"MZ\x90\x00" + b"\x00" * 100, "video/mp4"),
        ("clip.mp4", b"<html><script>alert(1)</script></html>", "video/mp4"),
        ("clip.mp4", b"\x00\x00", "video/mp4"),
        ("clip.mp4", b"\x00\x00\x00\x00ftypmp42", "video/mp4"),
        # Declared content type disagrees with the extension.
        ("clip.mp4", MP4_BYTES, "video/webm"),
    ],
)
def test_mismatched_signature_or_type_is_rejected(
    client, db, token, storage_dir, filename, content, content_type
):
    response = _upload(
        client, token, filename=filename, content=content, content_type=content_type
    )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "file_type_mismatch"
    _assert_nothing_stored(db, storage_dir)


def test_allowlists_are_configurable(make_client, db, storage_dir):
    client = make_client(
        ALLOWED_VIDEO_EXTENSIONS="MP4", ALLOWED_VIDEO_CONTENT_TYPES="video/mp4"
    )
    _register(client)
    token = _token(client)

    webm = _upload(
        client,
        token,
        filename="clip.webm",
        content=EBML_HEADER,
        content_type="video/webm",
    )
    mov = _upload(
        client, token, filename="clip.mov", content=MOV_HEADER, content_type="video/mp4"
    )
    mp4 = _upload(client, token)

    assert webm.status_code == 415
    assert mov.status_code == 415
    assert mp4.status_code == 201


def test_empty_file_is_rejected(client, db, token, storage_dir):
    response = _upload(client, token, content=b"")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "empty_file"
    _assert_nothing_stored(db, storage_dir)


# Request shape


def test_missing_file_field_is_rejected(client, db, token, storage_dir):
    response = _upload(client, token, field="video")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "missing_file"
    _assert_nothing_stored(db, storage_dir)


def test_non_multipart_request_is_rejected(client, db, token, storage_dir):
    response = client.post(
        VIDEOS_URL,
        content=MP4_BYTES,
        headers={**_bearer(token), "Content-Type": "video/mp4"},
    )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "invalid_request_content_type"
    _assert_nothing_stored(db, storage_dir)


def test_multipart_request_without_boundary_is_rejected(client, db, token, storage_dir):
    response = client.post(
        VIDEOS_URL,
        content=b"irrelevant",
        headers={**_bearer(token), "Content-Type": "multipart/form-data"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_multipart"
    _assert_nothing_stored(db, storage_dir)


def test_multiple_files_are_rejected(client, db, token, storage_dir):
    response = client.post(
        VIDEOS_URL,
        files=[
            ("file", ("a.mp4", MP4_BYTES, "video/mp4")),
            ("file", ("b.mp4", MP4_BYTES, "video/mp4")),
        ],
        headers=_bearer(token),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_multipart"
    _assert_nothing_stored(db, storage_dir)


def test_extra_form_fields_are_rejected(client, db, token, storage_dir):
    response = client.post(
        VIDEOS_URL,
        data={"storage_key": "users/x/videos/y/source.mp4"},
        files={"file": ("clip.mp4", MP4_BYTES, "video/mp4")},
        headers=_bearer(token),
    )

    assert response.status_code == 400
    _assert_nothing_stored(db, storage_dir)


# Size limit


def test_file_over_size_limit_is_rejected_and_nothing_remains(
    client, db, token, storage_dir
):
    content = MP4_HEADER + b"\x00" * (MIB + 1 - len(MP4_HEADER))

    response = _upload(client, token, content=content)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"
    _assert_nothing_stored(db, storage_dir)


def test_body_far_over_size_limit_is_rejected(client, db, token, storage_dir):
    content = MP4_HEADER + b"\x00" * (3 * MIB)

    response = _upload(client, token, content=content)

    assert response.status_code == 413
    _assert_nothing_stored(db, storage_dir)


BOUNDARY = "clipforge-test-boundary"
CHUNK = b"\x00" * (64 * 1024)
TOTAL_CHUNKS = 64  # 4 MiB, four times the 1 MiB limit


def _part_head(filename="big.mp4") -> bytes:
    return (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: video/mp4\r\n\r\n"
    ).encode() + MP4_HEADER


def _run_streamed_upload(application, token, extra_headers=(), disconnect_after=None):
    """Send a chunked upload straight to the ASGI app and count chunks read.

    With disconnect_after, the client disconnects once that many body
    messages have been sent.
    """
    state = {"pulled": 0}

    async def receive():
        index = state["pulled"]
        state["pulled"] += 1
        if disconnect_after is not None and index >= disconnect_after:
            return {"type": "http.disconnect"}
        if index == 0:
            return {"type": "http.request", "body": _part_head(), "more_body": True}
        if index <= TOTAL_CHUNKS:
            return {"type": "http.request", "body": CHUNK, "more_body": True}
        return {
            "type": "http.request",
            "body": f"\r\n--{BOUNDARY}--\r\n".encode(),
            "more_body": False,
        }

    messages = []

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": VIDEOS_URL,
        "raw_path": VIDEOS_URL.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"authorization", f"Bearer {token}".encode()),
            (b"content-type", f"multipart/form-data; boundary={BOUNDARY}".encode()),
            *extra_headers,
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    asyncio.run(application(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    return start["status"], state["pulled"]


def test_oversized_stream_is_aborted_before_it_is_fully_read(
    client, db, token, storage_dir
):
    status_code, pulled = _run_streamed_upload(client.app, token)

    assert status_code == 413
    # Reading stopped just past the limit plus the multipart allowance,
    # long before the 4 MiB body was consumed.
    limit_chunks = (MIB + MULTIPART_OVERHEAD_BYTES) // len(CHUNK) + 2
    assert pulled <= limit_chunks
    assert pulled < TOTAL_CHUNKS
    _assert_nothing_stored(db, storage_dir)


def test_oversized_content_length_is_rejected_without_reading_the_body(
    client, db, token, storage_dir
):
    declared = str(len(_part_head()) + TOTAL_CHUNKS * len(CHUNK)).encode()

    status_code, pulled = _run_streamed_upload(
        client.app, token, extra_headers=[(b"content-length", declared)]
    )

    assert status_code == 413
    assert pulled == 0
    _assert_nothing_stored(db, storage_dir)


def test_interrupted_upload_stores_nothing_and_closes_temp_files(
    client, db, token, storage_dir, monkeypatch, caplog
):
    spooled = []

    class RecordingSpooledFile(starlette.formparsers.SpooledTemporaryFile):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            spooled.append(self)

    monkeypatch.setattr(
        starlette.formparsers, "SpooledTemporaryFile", RecordingSpooledFile
    )

    with caplog.at_level(logging.INFO):
        status_code, _ = _run_streamed_upload(client.app, token, disconnect_after=3)

    assert status_code == 400
    # The file part had started, so a temporary file was spooled, and it is
    # closed (and so deleted) without waiting for garbage collection.
    assert spooled
    assert all(f.closed for f in spooled)
    assert [r for r in caplog.records if r.getMessage() == "video_upload_interrupted"]
    _assert_nothing_stored(db, storage_dir)


# File names


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("../../etc/passwd.mp4", "passwd.mp4"),
        ("/var/www/clip.mp4", "clip.mp4"),
        ("..\\..\\windows\\clip.mp4", "clip.mp4"),
        ("C:\\Users\\bob\\holiday.mp4", "holiday.mp4"),
        ("  spaced name.mp4 ", "spaced name.mp4"),
    ],
)
def test_path_like_filename_is_sanitized(
    client, db, token, storage_dir, filename, expected
):
    response = _upload(client, token, filename=filename)

    assert response.status_code == 201
    assert response.json()["original_filename"] == expected
    video = db.scalars(select(Video)).one()
    assert video.original_filename == expected
    # The storage key is generated from ids, never from the file name.
    assert video.storage_key == video_source_key(
        str(video.user_id), str(video.id), ".mp4"
    )
    assert _stored_files(storage_dir) == [storage_dir / video.storage_key]
    assert not (storage_dir.parent / "etc").exists()


def test_long_filename_is_truncated_keeping_extension(client, token):
    response = _upload(client, token, filename="a" * 300 + ".mp4")

    assert response.status_code == 201
    name = response.json()["original_filename"]
    assert len(name) == 255
    assert name.endswith(".mp4")


# Failure cleanup


def test_database_failure_removes_the_stored_file(
    client, session_factory, db, token, storage_dir
):
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

    response = _upload(client, token)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "upload_failed"
    assert "simulated" not in response.text
    _assert_nothing_stored(db, storage_dir)


def test_database_failure_does_not_log_the_filename(
    client, session_factory, db, token, storage_dir, caplog
):
    filename = "secret-holiday-plans.mp4"
    # Real flush/commit errors carry the bound parameters, including the
    # original file name, in their message.
    error = IntegrityError(
        "INSERT INTO videos (id, user_id, original_filename) VALUES (?, ?, ?)",
        ("video-id", "user-id", filename),
        Exception("UNIQUE constraint failed: videos.id"),
    )
    assert filename in str(error)

    def failing_db():
        session = session_factory()

        def fail_commit():
            raise error

        session.commit = fail_commit
        try:
            yield session
        finally:
            session.close()

    client.app.dependency_overrides[get_db] = failing_db

    with caplog.at_level(logging.INFO):
        response = _upload(client, token, filename=filename)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "upload_failed"
    assert "secret-holiday-plans" not in response.text
    assert "secret-holiday-plans" not in caplog.text
    failures = [r for r in caplog.records if r.getMessage() == "video_upload_db_failed"]
    assert len(failures) == 1
    assert failures[0].error_type == "IntegrityError"
    assert failures[0].exc_info is None
    _assert_nothing_stored(db, storage_dir)


class _FailAfterWriteStorage(LocalStorage):
    """Writes the object, then fails, as if the backend errored late."""

    def save(self, key, fileobj, *, max_bytes=None):
        super().save(key, fileobj, max_bytes=max_bytes)
        raise StorageError("simulated storage failure")


def test_storage_failure_removes_partial_object(client, db, token, storage_dir):
    client.app.dependency_overrides[get_video_storage] = lambda: _FailAfterWriteStorage(
        storage_dir
    )

    response = _upload(client, token)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "upload_failed"
    _assert_nothing_stored(db, storage_dir)


def test_misconfigured_storage_returns_503(make_client, db, monkeypatch):
    for name in ("S3_BUCKET", "S3_REGION", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)
    client = make_client(STORAGE_BACKEND="s3")
    _register(client)
    token = _token(client)

    response = _upload(client, token)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert _video_count(db) == 0


# Logging


def test_upload_outcome_is_logged_without_filename_or_contents(client, token, caplog):
    with caplog.at_level(logging.INFO):
        accepted = _upload(client, token, filename="secret-holiday-plans.mp4")
        rejected = _upload(client, token, filename="private-diary.avi")
        mismatched = _upload(
            client,
            token,
            filename="clip.webm",
            content=MP4_BYTES,
            content_type="video/webm",
        )
        # An unrecognized extension can hold part of a private file name.
        odd = _upload(
            client,
            token,
            filename="clip.my-secret-trip",
            content_type="text/x-secret-memo",
        )

    assert accepted.status_code == 201
    assert rejected.status_code == 415
    assert mismatched.status_code == 415
    assert odd.status_code == 415
    uploaded = [r for r in caplog.records if r.getMessage() == "video_uploaded"]
    assert len(uploaded) == 1
    record = uploaded[0]
    assert record.user_id == accepted.json()["user_id"]
    assert record.video_id == accepted.json()["id"]
    assert record.size_bytes == len(MP4_BYTES)
    assert record.content_type == "video/mp4"
    rejections = [
        r for r in caplog.records if r.getMessage() == "video_upload_rejected"
    ]
    assert [r.reason for r in rejections] == [
        "unsupported_file_type",
        "file_type_mismatch",
        "unsupported_file_type",
    ]
    assert all(r.user_id == accepted.json()["user_id"] for r in rejections)
    assert [(r.extension, r.content_type) for r in rejections] == [
        ("other", "video/mp4"),
        (".webm", "video/webm"),
        ("other", "other"),
    ]
    assert "secret-holiday-plans" not in caplog.text
    assert "private-diary" not in caplog.text
    assert "my-secret-trip" not in caplog.text
    assert "x-secret-memo" not in caplog.text
    assert CONTENT_MARKER.decode() not in caplog.text
    assert token not in caplog.text


# Helpers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ""),
        ("", ""),
        ("clip.mp4", "clip.mp4"),
        ("../../etc/passwd.mp4", "passwd.mp4"),
        ("a\\b\\c.mov", "c.mov"),
        ("a/b/", ""),
        ("..", ""),
        (".", ""),
        ("clip\x00name\n.mp4", "clipname.mp4"),
        ("\u202egnp.mp4", "gnp.mp4"),
        ("  clip.mp4  ", "clip.mp4"),
        ("cafe\u0301.mp4", "caf\u00e9.mp4"),
    ],
)
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_limits_length():
    assert sanitize_filename("x" * 10 + ".webm", max_length=8) == "xxx.webm"
    assert sanitize_filename("y" * 20, max_length=8) == "yyyyyyyy"
    assert sanitize_filename("." + "z" * 20, max_length=8) == "." + "z" * 7


@pytest.mark.parametrize(
    ("name", "expected"),
    [("a.MP4", ".mp4"), ("a.tar.mkv", ".mkv"), ("a", ""), ("a.", ""), (".mov", ".mov")],
)
def test_file_extension(name, expected):
    assert file_extension(name) == expected


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (MP4_HEADER, ISO_BMFF),
        (MOV_HEADER, ISO_BMFF),
        (EBML_HEADER, EBML),
        (b"", None),
        (b"\x00\x00\x00\x18ftyp"[:7], None),
        (b"\x00\x00\x00\x00ftypmp42", None),
        (b"RIFF\x00\x00\x00\x00AVI ", None),
        (b"\x1a\x45\xdf", None),
    ],
)
def test_detect_container(header, expected):
    assert detect_container(header) == expected


def test_upload_settings_parse_comma_separated_lists(monkeypatch):
    monkeypatch.setenv("ALLOWED_VIDEO_EXTENSIONS", " MP4, .mov ,, webm,.mp4")
    monkeypatch.setenv("ALLOWED_VIDEO_CONTENT_TYPES", "Video/MP4, ,video/webm")

    settings = Settings()

    assert settings.ALLOWED_VIDEO_EXTENSIONS == [".mp4", ".mov", ".webm"]
    assert settings.ALLOWED_VIDEO_CONTENT_TYPES == ["video/mp4", "video/webm"]


def test_upload_settings_defaults(monkeypatch):
    monkeypatch.delenv("ALLOWED_VIDEO_EXTENSIONS", raising=False)
    monkeypatch.delenv("ALLOWED_VIDEO_CONTENT_TYPES", raising=False)

    settings = Settings()

    assert settings.ALLOWED_VIDEO_EXTENSIONS == [".mp4", ".mov", ".webm", ".mkv"]
    assert settings.ALLOWED_VIDEO_CONTENT_TYPES == [
        "video/mp4",
        "video/quicktime",
        "video/webm",
        "video/x-matroska",
    ]


def test_generated_ids_are_unique_per_upload(client, token):
    first = _upload(client, token).json()["id"]
    second = _upload(client, token).json()["id"]

    assert uuid.UUID(first) != uuid.UUID(second)

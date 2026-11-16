"""Tests for the metadata extraction job (app.worker.metadata).

Celery runs eagerly against an in-memory SQLite database and LocalStorage in
tmp_path. Most tests replace probe() with a fake, so they need no FFmpeg; the
tests marked requires_ffmpeg probe real files generated at runtime and are
skipped when ffmpeg/ffprobe are not installed.
"""

import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app import database
from app.core.config import Settings, get_settings
from app.database import Base, create_db_engine, create_session_factory
from app.media.ffmpeg import FFmpegNotFoundError, MediaError, MediaInfo, MediaTimeoutError
from app.models.processing_job import JobStatus, JobType, ProcessingJob
from app.models.user import User
from app.models.video import Video, VideoStatus
from app.storage import LocalStorage, video_source_key
from app.worker import metadata
from app.worker.celery_app import TASK_MODULES, celery_app, configure_celery_app
from app.worker import jobs
from app.worker.jobs import JobError, enqueue_job, fail_stale_jobs
from app.worker.metadata import (
    FFMPEG_UNAVAILABLE_MESSAGE,
    FILE_MISSING_MESSAGE,
    FILE_UNREADABLE_MESSAGE,
    INVALID_LOCATION_MESSAGE,
    INVALID_PAYLOAD_MESSAGE,
    INVALID_VIDEO_MESSAGE,
    NO_VIDEO_STREAM_MESSAGE,
    PROBE_TIMEOUT_MESSAGE,
    UNEXPECTED_FAILURE_MESSAGE,
    VIDEO_MISSING_MESSAGE,
    extract_metadata,
    extract_video_metadata,
    queue_metadata_extraction,
)
from tests.media_helpers import (
    TEST_DURATION,
    TEST_FPS,
    TEST_HEIGHT,
    TEST_WIDTH,
    make_test_video,
    requires_ffmpeg,
)

PRIVATE_FILENAME = "secret-holiday-plans.mp4"
MP4_HEADER = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"

GOOD_INFO = MediaInfo(
    duration_seconds=12.5,
    width=1920,
    height=1080,
    fps=29.97,
    has_video=True,
    has_audio=True,
    video_codec="h264",
    audio_codec="aac",
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
def storage_dir(tmp_path):
    return tmp_path / "storage"


@pytest.fixture(autouse=True)
def worker_environment(session_factory, storage_dir, monkeypatch):
    """Run Celery eagerly against the test database and local storage."""
    monkeypatch.setattr(database, "_session_factory", session_factory)
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(storage_dir))
    get_settings.cache_clear()
    configure_celery_app(celery_app, Settings(APP_ENV="test"))
    yield
    configure_celery_app(celery_app, Settings(APP_ENV="test"))


@pytest.fixture
def db(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def storage(storage_dir):
    return LocalStorage(storage_dir)


@pytest.fixture
def probe_calls(monkeypatch):
    """Replace probe() with a fake; set `result` to a MediaInfo or an exception."""
    state = {"result": GOOD_INFO, "paths": []}

    def fake_probe(path, *, timeout=None, settings=None):
        state["paths"].append(path)
        result = state["result"]
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(metadata, "probe", fake_probe)
    return state


def _make_video(db, storage, content=MP4_HEADER + b"\x00" * 100, *, key=None, store=True):
    user = User(email=f"{uuid.uuid4().hex}@example.com", hashed_password="x")
    db.add(user)
    db.commit()
    video_id = uuid.uuid4()
    key = key or video_source_key(str(user.id), str(video_id), ".mp4")
    if store:
        storage.save(key, BytesIO(content))
    video = Video(
        id=video_id,
        user_id=user.id,
        original_filename=PRIVATE_FILENAME,
        content_type="video/mp4",
        size_bytes=len(content),
        storage_key=key,
        status=VideoStatus.UPLOADED,
    )
    db.add(video)
    db.commit()
    return video


def _read(model, row_id):
    with database.get_session_factory()() as session:
        return session.get(model, row_id)


def _assert_failed(video, job, message):
    stored = _read(Video, video.id)
    assert stored.status is VideoStatus.FAILED
    assert stored.error_message == message
    assert stored.duration_seconds is None
    assert stored.width is None and stored.height is None and stored.fps is None
    stored_job = _read(ProcessingJob, job.id)
    assert stored_job.status == JobStatus.FAILED
    assert stored_job.error_message == message
    assert stored_job.active_key is None


# Success


def test_metadata_task_saves_metadata_and_marks_ready(db, storage, probe_calls):
    video = _make_video(db, storage)

    job = queue_metadata_extraction(db, video)

    assert job.status == JobStatus.SUCCEEDED
    assert job.job_type == JobType.METADATA
    assert job.video_id == video.id
    assert job.user_id == video.user_id
    assert job.payload == {"video_id": str(video.id)}
    assert job.idempotency_key == str(video.id)
    stored = _read(Video, video.id)
    assert stored.status is VideoStatus.READY
    assert stored.error_message is None
    assert stored.duration_seconds == 12.5
    assert (stored.width, stored.height) == (1920, 1080)
    assert stored.fps == pytest.approx(29.97)
    # The file is probed in place, inside the storage root.
    assert probe_calls["paths"] == [(storage.root / video.storage_key).resolve()]


def test_video_at_exact_duration_limit_is_accepted(db, storage, probe_calls, monkeypatch):
    monkeypatch.setenv("MAX_VIDEO_DURATION_SECONDS", "60")
    get_settings.cache_clear()
    probe_calls["result"] = MediaInfo(60.0, 640, 360, 30.0, True, False, "h264", None)
    video = _make_video(db, storage)

    job = queue_metadata_extraction(db, video)

    assert job.status == JobStatus.SUCCEEDED
    assert _read(Video, video.id).status is VideoStatus.READY


def test_extraction_is_idempotent(db, storage, probe_calls):
    video = _make_video(db, storage)
    first = queue_metadata_extraction(db, video)

    # A redelivered message for the finished job does nothing.
    extract_metadata.apply(args=[str(first.id)])
    assert len(probe_calls["paths"]) == 1

    # Reprocessing re-probes and overwrites the same row.
    second = queue_metadata_extraction(db, video)

    assert second.id != first.id
    assert second.status == JobStatus.SUCCEEDED
    assert len(probe_calls["paths"]) == 2
    stored = _read(Video, video.id)
    assert stored.status is VideoStatus.READY
    assert (stored.duration_seconds, stored.width, stored.height) == (12.5, 1920, 1080)


def test_failed_video_can_be_processed_again(db, storage, probe_calls):
    video = _make_video(db, storage)
    probe_calls["result"] = MediaError("bad")
    queue_metadata_extraction(db, video)
    assert _read(Video, video.id).status is VideoStatus.FAILED

    probe_calls["result"] = GOOD_INFO
    job = queue_metadata_extraction(db, video)

    assert job.status == JobStatus.SUCCEEDED
    stored = _read(Video, video.id)
    assert stored.status is VideoStatus.READY
    assert stored.error_message is None


def test_task_is_registered_with_bounded_time_limits():
    assert "app.worker.metadata" in TASK_MODULES
    assert extract_metadata.name == metadata.EXTRACT_METADATA_TASK_NAME
    assert extract_metadata.soft_time_limit < extract_metadata.time_limit
    assert extract_metadata.acks_late is True


# Failures end in a terminal state for both the video and the job


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (MediaError("Invalid data found", "moov atom not found"), INVALID_VIDEO_MESSAGE),
        (FFmpegNotFoundError("'ffprobe' was not found"), FFMPEG_UNAVAILABLE_MESSAGE),
        (MediaTimeoutError("ffprobe did not finish"), PROBE_TIMEOUT_MESSAGE),
        (
            MediaInfo(30.0, None, None, None, False, True, None, "aac"),
            NO_VIDEO_STREAM_MESSAGE,
        ),
        (
            MediaInfo(None, 640, 360, 25.0, True, False, "h264", None),
            metadata.UNKNOWN_DURATION_MESSAGE,
        ),
        (
            MediaInfo(5.0, None, None, 25.0, True, False, "h264", None),
            metadata.UNKNOWN_DIMENSIONS_MESSAGE,
        ),
    ],
)
def test_unusable_file_marks_video_and_job_failed(db, storage, probe_calls, result, message):
    probe_calls["result"] = result
    video = _make_video(db, storage)

    job = queue_metadata_extraction(db, video)

    _assert_failed(video, job, message)
    # Tool output never reaches the stored messages.
    assert "moov" not in (_read(Video, video.id).error_message or "")


def test_video_longer_than_limit_is_failed(db, storage, probe_calls, monkeypatch):
    monkeypatch.setenv("MAX_VIDEO_DURATION_SECONDS", "60")
    get_settings.cache_clear()
    probe_calls["result"] = MediaInfo(61.0, 640, 360, 30.0, True, True, "h264", "aac")
    video = _make_video(db, storage)

    job = queue_metadata_extraction(db, video)

    message = "The video is too long (61 seconds); the maximum is 60 seconds."
    _assert_failed(video, job, message)


def test_missing_stored_file_fails(db, storage, probe_calls):
    video = _make_video(db, storage, store=False)

    job = queue_metadata_extraction(db, video)

    _assert_failed(video, job, FILE_MISSING_MESSAGE)
    assert probe_calls["paths"] == []


def test_storage_key_outside_the_videos_own_prefix_is_refused(db, storage, probe_calls):
    other_key = video_source_key(str(uuid.uuid4()), str(uuid.uuid4()), ".mp4")
    video = _make_video(db, storage, key=other_key)

    job = queue_metadata_extraction(db, video)

    _assert_failed(video, job, INVALID_LOCATION_MESSAGE)
    assert probe_calls["paths"] == []


def test_local_path_outside_the_storage_root_is_refused(db, storage, probe_calls, tmp_path):
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")

    class EscapingStorage(LocalStorage):
        @contextmanager
        def local_path(self, key):
            yield outside

    video = _make_video(db, storage)
    db.close()

    with pytest.raises(JobError) as excinfo:
        extract_video_metadata(db, video.id, storage=EscapingStorage(storage.root))

    assert excinfo.value.public_message == INVALID_LOCATION_MESSAGE
    assert probe_calls["paths"] == []
    assert _read(Video, video.id).status is VideoStatus.FAILED


def test_unexpected_error_fails_the_video_instead_of_leaving_it_processing(
    db, storage, probe_calls
):
    probe_calls["result"] = RuntimeError("/srv/storage/users/x boom")
    video = _make_video(db, storage)

    job = queue_metadata_extraction(db, video)

    stored = _read(Video, video.id)
    assert stored.status is VideoStatus.FAILED
    assert stored.error_message == UNEXPECTED_FAILURE_MESSAGE
    stored_job = _read(ProcessingJob, job.id)
    assert stored_job.status == JobStatus.FAILED
    assert stored_job.error_message == "The job failed unexpectedly (RuntimeError)."


def test_temporary_read_error_is_retried_then_succeeds(db, storage, probe_calls, monkeypatch):
    attempts = []
    original = LocalStorage.local_path

    def flaky_local_path(self, key):
        attempts.append(key)
        if len(attempts) == 1:
            raise OSError("disk hiccup")
        return original(self, key)

    monkeypatch.setattr(LocalStorage, "local_path", flaky_local_path)
    video = _make_video(db, storage)

    job = queue_metadata_extraction(db, video)

    assert len(attempts) == 2
    assert job.status == JobStatus.SUCCEEDED
    assert job.attempts == 2
    assert _read(Video, video.id).status is VideoStatus.READY


def test_persistent_read_error_fails_after_the_last_attempt(db, storage, probe_calls, monkeypatch):
    def broken_local_path(self, key):
        raise OSError("disk gone")

    monkeypatch.setattr(LocalStorage, "local_path", broken_local_path)
    video = _make_video(db, storage)

    job = queue_metadata_extraction(db, video)

    assert job.attempts == metadata.METADATA_MAX_ATTEMPTS
    _assert_failed(video, job, FILE_UNREADABLE_MESSAGE)


def test_retryable_error_before_last_attempt_keeps_video_processing(
    db, storage, monkeypatch
):
    def broken_local_path(self, key):
        raise OSError("disk gone")

    monkeypatch.setattr(LocalStorage, "local_path", broken_local_path)
    video = _make_video(db, storage)

    with pytest.raises(JobError) as excinfo:
        extract_video_metadata(db, video.id, final_attempt=False)

    assert excinfo.value.retryable is True
    assert _read(Video, video.id).status is VideoStatus.PROCESSING


def test_job_for_a_deleted_video_fails(db):
    job = enqueue_job(
        JobType.METADATA,
        extract_metadata,
        db=db,
        payload={"video_id": str(uuid.uuid4())},
    )

    assert job.status == JobStatus.FAILED
    assert job.error_message == VIDEO_MISSING_MESSAGE


@pytest.mark.parametrize("payload", [{}, {"video_id": "not-a-uuid"}])
def test_invalid_payload_fails_the_job(db, payload):
    job = enqueue_job(JobType.METADATA, extract_metadata, db=db, payload=payload)

    assert job.status == JobStatus.FAILED
    assert job.error_message == INVALID_PAYLOAD_MESSAGE


def test_payload_must_match_the_jobs_video(db, storage, probe_calls):
    video = _make_video(db, storage)

    job = enqueue_job(
        JobType.METADATA,
        extract_metadata,
        db=db,
        video_id=video.id,
        payload={"video_id": str(uuid.uuid4())},
    )

    assert job.status == JobStatus.FAILED
    assert job.error_message == INVALID_PAYLOAD_MESSAGE
    assert _read(Video, video.id).status is VideoStatus.UPLOADED


def test_queue_failure_marks_job_and_video_failed(db, storage, probe_calls, monkeypatch):
    monkeypatch.setitem(celery_app.conf, "task_always_eager", False)
    monkeypatch.setitem(celery_app.conf, "broker_url", None)
    video = _make_video(db, storage)

    assert queue_metadata_extraction(db, video) is None

    stored = _read(Video, video.id)
    assert stored.status is VideoStatus.FAILED
    assert stored.error_message == metadata.QUEUE_FAILURE_MESSAGE
    with database.get_session_factory()() as session:
        jobs = session.query(ProcessingJob).all()
    assert [job.status for job in jobs] == [JobStatus.FAILED]
    assert probe_calls["paths"] == []


def test_lost_worker_fails_the_job_and_the_processing_video(db, storage):
    video = _make_video(db, storage)
    video.status = VideoStatus.PROCESSING
    started = datetime.now(timezone.utc) - jobs.STALE_RUNNING_AFTER - timedelta(minutes=1)
    job = ProcessingJob(
        job_type=JobType.METADATA,
        status=JobStatus.RUNNING,
        user_id=video.user_id,
        video_id=video.id,
        attempts=1,
        max_attempts=3,
        active_key=f"metadata:{video.id}",
        started_at=started,
    )
    db.add(job)
    db.commit()

    assert fail_stale_jobs(db) == 1

    stored = _read(Video, video.id)
    assert stored.status is VideoStatus.FAILED
    assert stored.error_message == jobs.STALE_VIDEO_MESSAGE
    assert _read(ProcessingJob, job.id).status == JobStatus.FAILED


def test_logs_do_not_contain_file_names_or_tool_output(db, storage, probe_calls, caplog):
    caplog.set_level(logging.DEBUG)
    probe_calls["result"] = MediaError("bad", "PRIVATE-TOOL-OUTPUT")
    failing = _make_video(db, storage)
    queue_metadata_extraction(db, failing)
    probe_calls["result"] = GOOD_INFO
    succeeding = _make_video(db, storage)
    queue_metadata_extraction(db, succeeding)

    events = [r.getMessage() for r in caplog.records if r.name == "app.worker.metadata"]
    assert "video_metadata_started" in events
    assert "video_metadata_extracted" in events
    assert "video_metadata_invalid_file" in events
    app_records = [r for r in caplog.records if r.name.startswith("app.")]
    for record in app_records:
        text = f"{record.getMessage()} {record.__dict__!r}"
        assert PRIVATE_FILENAME not in text
        assert "PRIVATE-TOOL-OUTPUT" not in text


# Real FFmpeg


@requires_ffmpeg
def test_generated_video_is_probed_and_marked_ready(db, storage, tmp_path):
    source = make_test_video(tmp_path / "sample.mp4")
    video = _make_video(db, storage, content=source.read_bytes())

    job = queue_metadata_extraction(db, video)

    assert job.status == JobStatus.SUCCEEDED
    stored = _read(Video, video.id)
    assert stored.status is VideoStatus.READY
    assert stored.duration_seconds == pytest.approx(TEST_DURATION, abs=0.25)
    assert (stored.width, stored.height) == (TEST_WIDTH, TEST_HEIGHT)
    assert stored.fps == pytest.approx(TEST_FPS, abs=0.01)


@requires_ffmpeg
def test_invalid_upload_ends_in_failed(db, storage):
    video = _make_video(db, storage, content=MP4_HEADER + b"not really a video" * 50)

    job = queue_metadata_extraction(db, video)

    _assert_failed(video, job, INVALID_VIDEO_MESSAGE)


@requires_ffmpeg
def test_audio_only_upload_ends_in_failed(db, storage, tmp_path):
    source = make_test_video(tmp_path / "audio.mp4", with_video=False)
    video = _make_video(db, storage, content=source.read_bytes())

    job = queue_metadata_extraction(db, video)

    _assert_failed(video, job, NO_VIDEO_STREAM_MESSAGE)


@requires_ffmpeg
def test_real_video_over_duration_limit_ends_in_failed(db, storage, tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_VIDEO_DURATION_SECONDS", "1")
    get_settings.cache_clear()
    source = make_test_video(tmp_path / "sample.mp4")
    video = _make_video(db, storage, content=source.read_bytes())

    job = queue_metadata_extraction(db, video)

    stored = _read(Video, video.id)
    assert stored.status is VideoStatus.FAILED
    assert "maximum is 1 seconds" in stored.error_message
    assert _read(ProcessingJob, job.id).status == JobStatus.FAILED

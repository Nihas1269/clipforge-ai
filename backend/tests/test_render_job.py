"""Tests for the render job (app.worker.render).

Celery runs eagerly against an in-memory SQLite database. Most tests replace
FFmpeg and ffprobe with fakes; the end-to-end test at the bottom runs the
real tools on a 2-second lavfi test video and is skipped when they (or the
libass `ass` filter / libx264) are not available.
"""

import io
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import func, select, update
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app import database
from app.core.config import Settings, get_settings
from app.database import Base, create_db_engine, create_session_factory
from app.media.ffmpeg import (
    BYTES_PER_MB,
    FFmpegNotFoundError,
    MediaError,
    MediaInfo,
    MediaTimeoutError,
    probe,
    run_ffmpeg,
)
from app.models.caption import ClipCaption
from app.models.clip import Clip, ClipSource, ClipStatus
from app.models.export import Export, ExportAspectRatio, ExportStatus, ReframeMode
from app.models.processing_job import JobStatus, JobType, ProcessingJob
from app.models.storage_deletion import PendingStorageDeletion
from app.models.user import User
from app.models.video import Video, VideoStatus
from app.reframe import ReframeError
from app.render.pipeline import DURATION_TOLERANCE_SECONDS
from app.services.storage_cleanup import retry_pending_storage_deletions
from app.storage import (
    LocalStorage,
    StorageConfigurationError,
    StorageObjectTooLargeError,
    export_key,
    video_source_key,
)
from app.worker import render
from app.worker.celery_app import celery_app, configure_celery_app
from app.worker.jobs import (
    RETRY_DISPATCH_FAILURE_MESSAGE,
    STALE_EXPORT_MESSAGE,
    TIMEOUT_MESSAGE,
    JobDispatchError,
    JobError,
    JobTask,
    fail_stale_jobs,
)
from app.worker.render import queue_render, run_render
from tests.media_helpers import (
    FFMPEG_AVAILABLE,
    SKIP_REASON,
    TEST_HEIGHT,
    TEST_WIDTH,
    make_test_video,
)

FAKE_OUTPUT = b"\x00\x00\x00\x18ftypisom fake rendered video"
PRIVATE_STDERR = "/srv/private/users/secret-path.mp4: caption text Hello"


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
def storage(tmp_path):
    return LocalStorage(tmp_path / "storage")


@pytest.fixture(autouse=True)
def job_environment(session_factory, storage, monkeypatch):
    """Point the worker at the test database and storage; run Celery eagerly."""
    monkeypatch.setattr(database, "_session_factory", session_factory)
    monkeypatch.setattr(render, "get_storage", lambda settings=None: storage)
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


def _make_export(
    db,
    storage,
    *,
    aspect=ExportAspectRatio.PORTRAIT,
    include_captions=True,
    video_status=VideoStatus.READY,
    width=1920,
    height=1080,
    duration=60.0,
    start=10.0,
    end=25.0,
    source=b"source video bytes",
):
    user = User(email=f"{uuid.uuid4().hex}@example.com", hashed_password="x")
    db.add(user)
    db.flush()
    video_id = uuid.uuid4()
    key = video_source_key(str(user.id), str(video_id), ".mp4")
    # Written with a plain LocalStorage, so storages that fail on purpose
    # only fail when the render stores its output.
    source_storage = LocalStorage(storage.root)
    if isinstance(source, Path):
        with source.open("rb") as fileobj:
            source_storage.save(key, fileobj)
    elif source is not None:
        source_storage.save(key, io.BytesIO(source))
    video = Video(
        id=video_id,
        user_id=user.id,
        original_filename="talk.mp4",
        content_type="video/mp4",
        size_bytes=100,
        storage_key=key,
        status=video_status,
        width=width,
        height=height,
        duration_seconds=duration,
        fps=25.0,
    )
    clip = Clip(
        video_id=video_id,
        start_seconds=start,
        end_seconds=end,
        title="A clip",
        reason="Created manually.",
        score=1.0,
        status=ClipStatus.ACCEPTED,
        source=ClipSource.MANUAL,
    )
    db.add_all([video, clip])
    db.flush()
    db.add_all(
        [
            ClipCaption(
                clip_id=clip.id,
                index=0,
                start_seconds=0.0,
                end_seconds=0.6,
                text="Hello {\\b1}world",
            ),
            ClipCaption(
                clip_id=clip.id, index=1, start_seconds=0.6, end_seconds=1.0, text="Bye"
            ),
        ]
    )
    export = Export(
        clip_id=clip.id,
        user_id=user.id,
        aspect_ratio=aspect,
        reframe_mode=ReframeMode.CENTER,
        include_captions=include_captions,
    )
    db.add(export)
    db.commit()
    return export


class FakeTools:
    """Stands in for run_ffmpeg and probe; records what the render did."""

    def __init__(self, *, output_info=None, ffmpeg_error=None, write_output=True):
        self.output_info = output_info
        self.ffmpeg_error = ffmpeg_error
        self.write_output = write_output
        self.output_bytes = FAKE_OUTPUT
        self.calls = []
        self.subtitles = []
        self.workdirs = []

    def run_ffmpeg(self, args, **kwargs):
        assert isinstance(args, list) and all(isinstance(a, str) for a in args)
        self.calls.append(args)
        workdir = Path(args[-1]).parent
        self.workdirs.append(workdir)
        ass = workdir / render.SUBTITLES_FILENAME
        if ass.exists():
            self.subtitles.append(ass.read_text(encoding="utf-8"))
        if self.ffmpeg_error is not None:
            raise self.ffmpeg_error
        if self.write_output:
            Path(args[-1]).write_bytes(self.output_bytes)

    def probe(self, path, **kwargs):
        if Path(path).name == render.OUTPUT_FILENAME:
            return self.output_info
        return MediaInfo(60.0, 1920, 1080, 25.0, True, True, "h264", "aac")


def _output_info(width=1080, height=1920, duration=15.0, has_audio=True):
    return MediaInfo(duration, width, height, 25.0, True, has_audio, "h264", "aac")


@pytest.fixture
def tools(monkeypatch):
    fake = FakeTools(output_info=_output_info())
    monkeypatch.setattr(render, "run_ffmpeg", fake.run_ffmpeg)
    monkeypatch.setattr(render, "probe", fake.probe)
    return fake


def _reload(db, export):
    db.expire_all()
    return db.get(Export, export.id)


def _jobs(db):
    return db.scalars(select(ProcessingJob)).all()


# Success


def test_render_uploads_validated_output_and_completes_export(db, storage, tools):
    export = _make_export(db, storage)

    job = queue_render(db, export)

    assert job.job_type is JobType.RENDER
    assert job.status is JobStatus.SUCCEEDED
    export = _reload(db, export)
    assert export.status is ExportStatus.COMPLETED
    assert export.job_id == job.id
    key = f"users/{export.user_id}/exports/{export.id}.mp4"
    assert export.storage_key == key == export_key(str(export.user_id), str(export.id))
    assert export.size_bytes == len(FAKE_OUTPUT)
    assert export.completed_at is not None
    assert export.error_message is None
    with storage.open(key) as stored:
        assert stored.read() == FAKE_OUTPUT

    (args,) = tools.calls
    assert args[args.index("-ss") + 1] == "10"
    assert args[args.index("-t") + 1] == "15"
    vf = args[args.index("-vf") + 1]
    assert vf.startswith("crop=w=608:h=1080:x=656:y=0,scale=1080:1920")
    assert "ass=filename=" in vf
    # FFmpeg stops writing at the configured output size limit.
    max_mb = get_settings().FFMPEG_MAX_OUTPUT_FILE_MB
    assert args[args.index("-fs") + 1] == str(max_mb * BYTES_PER_MB)
    # Captions are clip-relative ASS events with override characters escaped.
    (ass,) = tools.subtitles
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert (
        "Dialogue: 0,0:00:00.00,0:00:00.60,Default,,0,0,0,,Hello ｛＼b1｝world" in ass
    )
    assert "{\\b1}" not in ass
    # The temporary directory is gone.
    assert not tools.workdirs[0].exists()


@pytest.mark.parametrize(
    ("aspect", "size"),
    [
        (ExportAspectRatio.SQUARE, (1080, 1080)),
        (ExportAspectRatio.VERTICAL, (1080, 1350)),
        (ExportAspectRatio.LANDSCAPE, (1920, 1080)),
    ],
)
def test_render_uses_the_output_size_of_the_aspect_ratio(
    db, storage, tools, aspect, size
):
    tools.output_info = _output_info(*size)
    export = _make_export(db, storage, aspect=aspect)

    queue_render(db, export)

    assert _reload(db, export).status is ExportStatus.COMPLETED
    vf = tools.calls[0][tools.calls[0].index("-vf") + 1]
    assert f"scale={size[0]}:{size[1]}" in vf


def test_render_without_captions_has_no_subtitle_filter(db, storage, tools):
    export = _make_export(db, storage, include_captions=False)

    queue_render(db, export)

    assert _reload(db, export).status is ExportStatus.COMPLETED
    vf = tools.calls[0][tools.calls[0].index("-vf") + 1]
    assert "ass=" not in vf
    assert tools.subtitles == []


def test_completed_export_is_not_rendered_again(db, storage, tools):
    export = _make_export(db, storage)
    job = queue_render(db, export)

    # A late duplicate of the message is skipped by the job claim ...
    render.render_export.apply(args=[str(job.id)])
    # ... and running the render again for the same job does nothing.
    assert run_render(db, export.id, job.id, storage=storage) is None

    assert len(tools.calls) == 1
    export = _reload(db, export)
    assert export.status is ExportStatus.COMPLETED
    assert len(_jobs(db)) == 1


def test_duplicate_render_requests_share_one_active_job(db, storage, monkeypatch):
    published = []
    monkeypatch.setitem(celery_app.conf, "task_always_eager", False)
    monkeypatch.setattr(
        JobTask, "apply_async", lambda self, args=None, **kw: published.append(args)
    )
    export = _make_export(db, storage)

    first = queue_render(db, export)
    second = queue_render(db, export)

    assert first.id == second.id
    assert len(published) == 1
    assert db.scalar(select(func.count()).select_from(ProcessingJob)) == 1
    assert first.idempotency_key == str(export.id)
    assert _reload(db, export).job_id == first.id


def test_export_owned_by_another_job_is_refused(db, storage, tools):
    export = _make_export(db, storage)
    other_job = uuid.uuid4()
    export.job_id = other_job
    db.commit()

    with pytest.raises(JobError) as excinfo:
        run_render(db, export.id, uuid.uuid4(), storage=storage)

    assert excinfo.value.public_message == render.EXPORT_NOT_RENDERABLE_MESSAGE
    export = _reload(db, export)
    assert export.status is ExportStatus.QUEUED
    assert export.job_id == other_job
    assert tools.calls == []


def _render_job(db, status):
    job = ProcessingJob(
        job_type=JobType.RENDER, status=status, attempts=0, max_attempts=3
    )
    db.add(job)
    db.commit()
    return job


@pytest.mark.parametrize("status", [JobStatus.CANCELLED, JobStatus.FAILED])
def test_requeue_takes_over_export_whose_job_ended(db, storage, tools, status):
    export = _make_export(db, storage)
    ended = _render_job(db, status)
    export.job_id = ended.id
    db.commit()

    job = queue_render(db, export)

    assert job.id != ended.id
    assert job.status is JobStatus.SUCCEEDED
    export = _reload(db, export)
    assert export.status is ExportStatus.COMPLETED
    assert export.job_id == job.id
    assert len(tools.calls) == 1


def test_requeue_records_new_job_before_the_worker_runs(
    db, storage, tools, monkeypatch
):
    published = []
    monkeypatch.setitem(celery_app.conf, "task_always_eager", False)
    monkeypatch.setattr(
        JobTask, "apply_async", lambda self, args=None, **kw: published.append(args)
    )
    export = _make_export(db, storage)
    cancelled = _render_job(db, JobStatus.CANCELLED)
    export.job_id = cancelled.id
    db.commit()

    job = queue_render(db, export)

    assert _reload(db, export).job_id == job.id
    (args,) = published
    render.render_export.apply(args=args)
    export = _reload(db, export)
    assert export.status is ExportStatus.COMPLETED
    assert export.job_id == job.id


def test_export_of_an_active_job_or_rendering_is_not_taken_over(db, storage, tools):
    export = _make_export(db, storage)
    active = _render_job(db, JobStatus.QUEUED)
    export.job_id = active.id
    db.commit()

    with pytest.raises(JobError) as excinfo:
        run_render(db, export.id, uuid.uuid4(), storage=storage)
    assert excinfo.value.public_message == render.EXPORT_NOT_RENDERABLE_MESSAGE

    # A rendering export is never taken over, even from a job that ended.
    ended = _render_job(db, JobStatus.CANCELLED)
    export = _reload(db, export)
    export.job_id = ended.id
    export.status = ExportStatus.RENDERING
    db.commit()
    with pytest.raises(JobError):
        run_render(db, export.id, uuid.uuid4(), storage=storage)

    export = _reload(db, export)
    assert export.status is ExportStatus.RENDERING
    assert export.job_id == ended.id
    assert tools.calls == []


def test_only_queued_exports_can_be_queued(db, storage, tools):
    export = _make_export(db, storage)
    queue_render(db, export)

    with pytest.raises(ValueError):
        queue_render(db, _reload(db, export))


# Failures


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (MediaError("ffmpeg failed.", PRIVATE_STDERR), render.RENDER_FAILED_MESSAGE),
        (MediaTimeoutError("too slow", PRIVATE_STDERR), render.RENDER_TIMEOUT_MESSAGE),
        (FFmpegNotFoundError("missing"), render.FFMPEG_UNAVAILABLE_MESSAGE),
    ],
)
def test_ffmpeg_failure_marks_export_failed_with_safe_message(
    db, storage, tools, error, message
):
    tools.ffmpeg_error = error
    export = _make_export(db, storage)

    job = queue_render(db, export)

    assert job.status is JobStatus.FAILED
    assert job.error_message == message
    assert job.attempts == 1
    export = _reload(db, export)
    assert export.status is ExportStatus.FAILED
    assert export.error_message == message
    assert "secret-path" not in export.error_message
    assert export.storage_key is None and export.size_bytes is None
    assert export.completed_at is None
    assert not storage.exists(export_key(str(export.user_id), str(export.id)))
    assert not tools.workdirs[0].exists()


@pytest.mark.parametrize(
    "output_info",
    [
        _output_info(width=1080, height=1080),
        _output_info(duration=15.0 + DURATION_TOLERANCE_SECONDS + 0.1),
        _output_info(has_audio=False),
        MediaInfo(None, None, None, None, False, True, None, "aac"),
    ],
)
def test_output_failing_validation_is_a_failed_render(db, storage, tools, output_info):
    tools.output_info = output_info
    export = _make_export(db, storage)

    job = queue_render(db, export)

    assert job.status is JobStatus.FAILED
    export = _reload(db, export)
    assert export.status is ExportStatus.FAILED
    assert export.error_message == render.INVALID_OUTPUT_MESSAGE
    assert not storage.exists(export_key(str(export.user_id), str(export.id)))


def test_missing_output_file_is_a_failed_render(db, storage, tools):
    tools.write_output = False
    export = _make_export(db, storage)

    queue_render(db, export)

    export = _reload(db, export)
    assert export.status is ExportStatus.FAILED
    assert export.error_message == render.INVALID_OUTPUT_MESSAGE


class FailingUploadStorage(LocalStorage):
    """Writes part of the object, then fails, like an interrupted upload."""

    def __init__(self, root):
        super().__init__(root)
        self.saves = 0
        self.deleted = []

    def save(self, key, fileobj, *, max_bytes=None):
        self.saves += 1
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fileobj.read(4))
        raise OSError("connection reset")

    def delete(self, key):
        self.deleted.append(key)
        super().delete(key)


def test_upload_failure_is_retried_then_fails_and_leaves_no_object(
    db, tmp_path, tools, monkeypatch
):
    failing = FailingUploadStorage(tmp_path / "storage")
    monkeypatch.setattr(render, "get_storage", lambda settings=None: failing)
    export = _make_export(db, failing)
    key = export_key(str(export.user_id), str(export.id))

    job = queue_render(db, export)

    assert job.status is JobStatus.FAILED
    assert job.attempts == render.RENDER_MAX_ATTEMPTS
    assert job.error_message == render.UPLOAD_FAILED_MESSAGE
    assert failing.saves == render.RENDER_MAX_ATTEMPTS
    assert failing.deleted == [key] * render.RENDER_MAX_ATTEMPTS
    assert not failing.exists(key)
    export = _reload(db, export)
    assert export.status is ExportStatus.FAILED
    assert export.error_message == render.UPLOAD_FAILED_MESSAGE
    assert export.storage_key is None
    assert all(not workdir.exists() for workdir in tools.workdirs)


def test_upload_succeeding_on_retry_completes_once(db, tmp_path, tools, monkeypatch):
    class FlakyStorage(LocalStorage):
        saves = 0

        def save(self, key, fileobj, *, max_bytes=None):
            FlakyStorage.saves += 1
            if FlakyStorage.saves == 1:
                raise OSError("timeout")
            return super().save(key, fileobj, max_bytes=max_bytes)

    flaky = FlakyStorage(tmp_path / "storage")
    monkeypatch.setattr(render, "get_storage", lambda settings=None: flaky)
    export = _make_export(db, flaky)

    job = queue_render(db, export)

    assert job.status is JobStatus.SUCCEEDED
    assert job.attempts == 2
    export = _reload(db, export)
    assert export.status is ExportStatus.COMPLETED
    assert export.size_bytes == len(FAKE_OUTPUT)


def test_soft_time_limit_during_upload_fails_at_once(db, tmp_path, tools, monkeypatch):
    class SlowUploadStorage(FailingUploadStorage):
        def save(self, key, fileobj, *, max_bytes=None):
            self.saves += 1
            path = self._resolve(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(fileobj.read(4))
            raise SoftTimeLimitExceeded()

    slow = SlowUploadStorage(tmp_path / "storage")
    monkeypatch.setattr(render, "get_storage", lambda settings=None: slow)
    export = _make_export(db, slow)
    key = export_key(str(export.user_id), str(export.id))

    job = queue_render(db, export)

    assert job.status is JobStatus.FAILED
    assert job.attempts == 1
    assert job.error_message == TIMEOUT_MESSAGE
    assert slow.saves == 1
    assert slow.deleted == [key]
    assert not slow.exists(key)
    export = _reload(db, export)
    assert export.status is ExportStatus.FAILED
    assert export.error_message == TIMEOUT_MESSAGE
    assert all(not workdir.exists() for workdir in tools.workdirs)


class RecordingStorage(LocalStorage):
    """Records deletions; `after_save` runs once the object is stored."""

    def __init__(self, root, after_save=None):
        super().__init__(root)
        self.after_save = after_save
        self.deleted = []

    def save(self, key, fileobj, *, max_bytes=None):
        size = super().save(key, fileobj, max_bytes=max_bytes)
        if self.after_save is not None:
            self.after_save()
        return size

    def delete(self, key):
        self.deleted.append(key)
        super().delete(key)


def test_export_changed_during_upload_removes_stored_object(
    db, tmp_path, tools, session_factory, monkeypatch
):
    def fail_export_elsewhere():
        with session_factory() as other:
            other.execute(
                update(Export).values(
                    status=ExportStatus.FAILED, error_message="Changed elsewhere."
                )
            )
            other.commit()

    recording = RecordingStorage(tmp_path / "storage", fail_export_elsewhere)
    monkeypatch.setattr(render, "get_storage", lambda settings=None: recording)
    export = _make_export(db, recording)
    key = export_key(str(export.user_id), str(export.id))

    job = queue_render(db, export)

    assert job.status is JobStatus.FAILED
    assert job.error_message == render.EXPORT_CHANGED_MESSAGE
    assert recording.deleted == [key]
    assert not recording.exists(key)
    export = _reload(db, export)
    assert export.status is ExportStatus.FAILED
    assert export.error_message == "Changed elsewhere."
    assert export.storage_key is None


def test_failing_final_update_removes_stored_object(db, tmp_path, tools, monkeypatch):
    def broken_complete(*args, **kwargs):
        raise RuntimeError("database connection lost")

    recording = RecordingStorage(tmp_path / "storage")
    monkeypatch.setattr(render, "get_storage", lambda settings=None: recording)
    monkeypatch.setattr(render, "_complete", broken_complete)
    export = _make_export(db, recording)
    key = export_key(str(export.user_id), str(export.id))

    job = queue_render(db, export)

    assert job.status is JobStatus.FAILED
    assert job.attempts == 1
    assert recording.deleted == [key]
    assert not recording.exists(key)
    export = _reload(db, export)
    assert export.status is ExportStatus.FAILED
    assert "database connection lost" not in export.error_message
    assert export.storage_key is None


def test_caption_write_failure_is_retried_with_its_own_message(
    db, storage, tools, monkeypatch
):
    # A subdirectory that does not exist makes writing the ASS file fail.
    monkeypatch.setattr(render, "SUBTITLES_FILENAME", "missing/captions.ass")
    export = _make_export(db, storage)

    job = queue_render(db, export)

    assert job.status is JobStatus.FAILED
    assert job.attempts == render.RENDER_MAX_ATTEMPTS
    assert job.error_message == render.CAPTIONS_FAILED_MESSAGE
    export = _reload(db, export)
    assert export.status is ExportStatus.FAILED
    assert export.error_message == render.CAPTIONS_FAILED_MESSAGE
    assert tools.calls == []


def test_video_not_ready_fails_without_rendering(db, storage, tools):
    export = _make_export(db, storage, video_status=VideoStatus.PROCESSING)

    queue_render(db, export)

    export = _reload(db, export)
    assert export.status is ExportStatus.FAILED
    assert export.error_message == render.VIDEO_NOT_READY_MESSAGE
    assert tools.calls == []


def test_clip_outside_video_fails(db, storage, tools):
    export = _make_export(db, storage, duration=20.0, start=10.0, end=25.0)

    queue_render(db, export)

    export = _reload(db, export)
    assert export.status is ExportStatus.FAILED
    assert export.error_message == render.CLIP_OUT_OF_RANGE_MESSAGE


def test_missing_source_file_fails(db, storage, tools):
    export = _make_export(db, storage, source=None)

    queue_render(db, export)

    export = _reload(db, export)
    assert export.status is ExportStatus.FAILED
    assert export.error_message == render.FILE_MISSING_MESSAGE
    assert tools.calls == []


def test_cancelled_render_stores_nothing(db, storage, tools, monkeypatch):
    monkeypatch.setattr(render.JobContext, "is_cancelled", lambda self: True)
    export = _make_export(db, storage)

    queue_render(db, export)

    export = _reload(db, export)
    assert export.status is ExportStatus.FAILED
    assert export.error_message == render.CANCELLED_MESSAGE
    assert not storage.exists(export_key(str(export.user_id), str(export.id)))


def test_stale_render_job_fails_its_export(db, storage):
    export = _make_export(db, storage)
    job = ProcessingJob(
        job_type=JobType.RENDER,
        status=JobStatus.RUNNING,
        attempts=1,
        max_attempts=3,
        started_at=datetime.now(timezone.utc) - timedelta(hours=3),
    )
    db.add(job)
    db.flush()
    export.job_id = job.id
    export.status = ExportStatus.RENDERING
    db.commit()

    # A partial object left by a worker killed during the upload.
    key = export_key(str(export.user_id), str(export.id))
    storage.save(key, io.BytesIO(b"partial"))

    assert fail_stale_jobs(db) == 1

    export = _reload(db, export)
    assert export.status is ExportStatus.FAILED
    assert export.error_message == STALE_EXPORT_MESSAGE
    # Its deletion is queued in the cleanup outbox, which removes it.
    (deletion,) = db.scalars(select(PendingStorageDeletion)).all()
    assert deletion.storage_key == key
    assert retry_pending_storage_deletions(db, storage) == 1
    assert not storage.exists(key)
    assert db.scalars(select(PendingStorageDeletion)).all() == []


def test_export_left_rendering_by_a_failed_retry_dispatch_is_recovered(
    db, tmp_path, tools, monkeypatch
):
    def broken_retry(self, *args, **kwargs):
        raise RuntimeError("broker down")

    failing = FailingUploadStorage(tmp_path / "storage")
    monkeypatch.setattr(render, "get_storage", lambda settings=None: failing)
    monkeypatch.setattr(JobTask, "retry", broken_retry)
    export = _make_export(db, failing)
    key = export_key(str(export.user_id), str(export.id))

    job = queue_render(db, export)

    # The upload failure is retryable, so the export was not failed, but the
    # retry could not be published and the job failed.
    assert job.status is JobStatus.FAILED
    assert job.error_message == RETRY_DISPATCH_FAILURE_MESSAGE
    assert job.attempts == 1
    export = _reload(db, export)
    assert export.status is ExportStatus.RENDERING
    assert export.job_id == job.id

    # No job was stale, but the export left rendering is failed.
    assert fail_stale_jobs(db) == 0

    export = _reload(db, export)
    assert export.status is ExportStatus.FAILED
    assert export.error_message == STALE_EXPORT_MESSAGE
    assert export.storage_key is None
    (deletion,) = db.scalars(select(PendingStorageDeletion)).all()
    assert deletion.storage_key == key


def test_recovery_leaves_exports_that_a_job_can_still_finish(db, storage):
    rendering = _make_export(db, storage)
    queued = _make_export(db, storage)
    active = _render_job(db, JobStatus.QUEUED)
    ended = _render_job(db, JobStatus.CANCELLED)
    rendering.status = ExportStatus.RENDERING
    rendering.job_id = active.id
    # queue_render() takes over a queued export whose job ended.
    queued.job_id = ended.id
    db.commit()

    assert fail_stale_jobs(db) == 0

    assert _reload(db, rendering).status is ExportStatus.RENDERING
    assert _reload(db, queued).status is ExportStatus.QUEUED
    assert db.scalars(select(PendingStorageDeletion)).all() == []


def _publish_failing(monkeypatch, before_failure=None):
    def apply_async(self, args=None, **kwargs):
        if before_failure is not None:
            before_failure()
        raise ConnectionError("broker down")

    monkeypatch.setitem(celery_app.conf, "task_always_eager", False)
    monkeypatch.setattr(JobTask, "apply_async", apply_async)


def test_queue_failure_marks_the_export_failed(db, storage, monkeypatch):
    _publish_failing(monkeypatch)
    export = _make_export(db, storage)

    with pytest.raises(JobDispatchError):
        queue_render(db, export)

    export = _reload(db, export)
    assert export.status is ExportStatus.FAILED
    assert export.error_message == render.QUEUE_FAILED_MESSAGE


def test_queue_failure_leaves_an_export_claimed_by_another_job(
    db, storage, session_factory, monkeypatch
):
    export = _make_export(db, storage)
    export_id = export.id
    other_id = _render_job(db, JobStatus.RUNNING).id

    def claimed_concurrently():
        with session_factory() as other_session:
            other_session.execute(
                update(Export)
                .where(Export.id == export_id)
                .values(status=ExportStatus.RENDERING, job_id=other_id)
            )
            other_session.commit()

    _publish_failing(monkeypatch, claimed_concurrently)

    with pytest.raises(JobDispatchError):
        queue_render(db, export)

    export = _reload(db, export)
    assert export.status is ExportStatus.RENDERING
    assert export.job_id == other_id
    assert export.error_message is None


def test_oversized_upload_fails_without_retry_and_leaves_no_object(
    db, tmp_path, tools, monkeypatch
):
    class TooLargeStorage(FailingUploadStorage):
        def save(self, key, fileobj, *, max_bytes=None):
            self.saves += 1
            path = self._resolve(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(fileobj.read(4))
            raise StorageObjectTooLargeError("too large")

    too_large = TooLargeStorage(tmp_path / "storage")
    monkeypatch.setattr(render, "get_storage", lambda settings=None: too_large)
    export = _make_export(db, too_large)
    key = export_key(str(export.user_id), str(export.id))

    job = queue_render(db, export)

    assert job.status is JobStatus.FAILED
    assert job.attempts == 1
    assert job.error_message == render.OUTPUT_TOO_LARGE_MESSAGE
    assert too_large.saves == 1
    assert too_large.deleted == [key]
    assert not too_large.exists(key)
    export = _reload(db, export)
    assert export.status is ExportStatus.FAILED
    assert export.error_message == render.OUTPUT_TOO_LARGE_MESSAGE
    assert export.storage_key is None


def test_output_stopped_at_the_size_limit_is_too_large(db, storage, tools, monkeypatch):
    # One "MB" is one byte here, so the fake output passes the limit.
    monkeypatch.setattr(render, "BYTES_PER_MB", 1)
    limit = get_settings().FFMPEG_MAX_OUTPUT_FILE_MB
    tools.output_bytes = b"\x00" * limit
    export = _make_export(db, storage)

    job = queue_render(db, export)

    (args,) = tools.calls
    assert args[args.index("-fs") + 1] == str(limit)
    assert job.status is JobStatus.FAILED
    assert job.attempts == 1
    export = _reload(db, export)
    assert export.status is ExportStatus.FAILED
    assert export.error_message == render.OUTPUT_TOO_LARGE_MESSAGE
    assert not storage.exists(export_key(str(export.user_id), str(export.id)))
    assert not tools.workdirs[0].exists()


def _video_of(db, export):
    clip = db.get(Clip, export.clip_id)
    return db.get(Video, clip.video_id)


def test_video_stored_outside_its_location_is_refused(db, storage, tools):
    export = _make_export(db, storage)
    video = _video_of(db, export)
    # Another user's key: the source is never read.
    video.storage_key = video_source_key(str(uuid.uuid4()), str(video.id), ".mp4")
    db.commit()

    job = queue_render(db, export)

    assert job.status is JobStatus.FAILED
    assert job.attempts == 1
    export = _reload(db, export)
    assert export.status is ExportStatus.FAILED
    assert export.error_message == render.INVALID_LOCATION_MESSAGE
    assert tools.calls == []


def test_export_of_another_users_clip_is_refused(db, storage, tools):
    export = _make_export(db, storage)
    other = User(email=f"{uuid.uuid4().hex}@example.com", hashed_password="x")
    db.add(other)
    db.flush()
    export.user_id = other.id
    db.commit()

    queue_render(db, export)

    export = _reload(db, export)
    assert export.status is ExportStatus.FAILED
    assert export.error_message == render.CLIP_MISSING_MESSAGE
    assert tools.calls == []


@pytest.mark.parametrize(
    "error", [ReframeError("The video could not be probed."), ValueError("bad")]
)
def test_reframe_failure_fails_without_rendering(
    db, storage, tools, monkeypatch, error
):
    def failing_plan(*args, **kwargs):
        raise error

    monkeypatch.setattr(render, "plan_reframe", failing_plan)
    export = _make_export(db, storage)

    job = queue_render(db, export)

    assert job.status is JobStatus.FAILED
    assert job.attempts == 1
    export = _reload(db, export)
    assert export.status is ExportStatus.FAILED
    assert export.error_message == render.REFRAME_FAILED_MESSAGE
    assert tools.calls == []


def test_invalid_captions_fail_without_rendering(db, storage, tools, monkeypatch):
    def failing_render_ass(*args, **kwargs):
        raise ValueError("caption text Hello is invalid")

    monkeypatch.setattr(render, "render_ass", failing_render_ass)
    export = _make_export(db, storage)

    job = queue_render(db, export)

    assert job.status is JobStatus.FAILED
    assert job.attempts == 1
    export = _reload(db, export)
    assert export.status is ExportStatus.FAILED
    assert export.error_message == render.CAPTIONS_FAILED_MESSAGE
    assert "Hello" not in export.error_message
    assert tools.calls == []


def test_unconfigured_storage_fails_the_export(db, storage, tools, monkeypatch):
    def unconfigured(settings=None):
        raise StorageConfigurationError("S3_BUCKET is not set.")

    export = _make_export(db, storage)
    monkeypatch.setattr(render, "get_storage", unconfigured)

    job = queue_render(db, export)

    assert job.status is JobStatus.FAILED
    assert job.attempts == 1
    assert job.error_message == render.STORAGE_UNAVAILABLE_MESSAGE
    export = _reload(db, export)
    assert export.status is ExportStatus.FAILED
    assert export.error_message == render.STORAGE_UNAVAILABLE_MESSAGE
    assert tools.calls == []


@pytest.mark.parametrize("payload", [{"export_id": "not-a-uuid"}, {}])
def test_invalid_payload_fails_the_job(db, tools, payload):
    job = ProcessingJob(
        job_type=JobType.RENDER,
        status=JobStatus.QUEUED,
        attempts=0,
        max_attempts=3,
        payload=payload,
    )
    db.add(job)
    db.commit()

    render.render_export.apply(args=[str(job.id)])

    db.expire_all()
    job = db.get(ProcessingJob, job.id)
    assert job.status is JobStatus.FAILED
    assert job.error_message == render.INVALID_PAYLOAD_MESSAGE
    assert tools.calls == []


def test_temp_dir_left_behind_is_logged(db, storage, tools, monkeypatch):
    errors = []
    monkeypatch.setattr(
        render.logger, "error", lambda event, *args, **kwargs: errors.append(event)
    )
    # The directory cannot be removed (for example a file held open).
    monkeypatch.setattr(tempfile.TemporaryDirectory, "cleanup", lambda self: None)
    export = _make_export(db, storage)

    try:
        queue_render(db, export)
    finally:
        for workdir in tools.workdirs:
            shutil.rmtree(workdir, ignore_errors=True)

    # The render itself still succeeds.
    assert _reload(db, export).status is ExportStatus.COMPLETED
    assert errors == ["render_temp_cleanup_failed"]


def test_export_key_layout():
    assert export_key("u1", "e1") == "users/u1/exports/e1.mp4"
    with pytest.raises(ValueError):
        export_key("../u", "e1")


# Real FFmpeg, end to end

_TOOL_SETTINGS = Settings(_env_file=None)


def _render_tools_available() -> bool:
    if not FFMPEG_AVAILABLE:
        return False
    try:
        filters = run_ffmpeg(["-filters"], timeout=30, settings=_TOOL_SETTINGS).stdout
        encoders = run_ffmpeg(["-encoders"], timeout=30, settings=_TOOL_SETTINGS).stdout
    except MediaError:
        return False
    return bool(re.search(rb"\sass\s", filters)) and b"libx264" in encoders


def test_real_render_end_to_end(db, storage, tmp_path):
    if not _render_tools_available():
        pytest.skip(
            SKIP_REASON + " (rendering also needs the libass 'ass' filter and libx264)"
        )
    sample = make_test_video(tmp_path / "sample.mp4")
    export = _make_export(
        db,
        storage,
        width=TEST_WIDTH,
        height=TEST_HEIGHT,
        duration=2.0,
        start=0.5,
        end=1.5,
        source=sample,
    )

    job = queue_render(db, export)

    assert job.status is JobStatus.SUCCEEDED, job.error_message
    export = _reload(db, export)
    assert export.status is ExportStatus.COMPLETED
    with storage.local_path(export.storage_key) as path:
        info = probe(path, settings=_TOOL_SETTINGS)
        assert path.stat().st_size == export.size_bytes
    assert (info.width, info.height) == (1080, 1920)
    assert info.duration_seconds == pytest.approx(1.0, abs=DURATION_TOLERANCE_SECONDS)
    assert info.has_audio is True

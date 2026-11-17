"""Tests for the transcription job, the transcript endpoints and subtitle downloads.

Celery runs eagerly against an in-memory SQLite database; FFmpeg audio
extraction is replaced by a function that writes a silent WAV file, and the
fake transcription provider (or a scripted one) is used, so no binaries,
network or credentials are needed.
"""

import io
import logging
import re
import uuid
import wave
from pathlib import Path

import pytest
from celery import Task
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app import database
from app.core.config import Settings, get_settings
from app.database import Base, create_db_engine, create_session_factory, get_db
from app.main import create_app
from app.media.ffmpeg import MediaError
from app.models.processing_job import JobStatus, JobType, ProcessingJob
from app.models.transcript import Transcript, TranscriptSegment
from app.models.user import User
from app.models.video import Video, VideoStatus
from app.repositories import transcripts as transcript_repository
from app.storage import LocalStorage, video_source_key
from app.transcription import (
    FakeTranscriptionProvider,
    TranscriptionError,
    TranscriptionResult,
    TranscriptionSegment,
    WordTiming,
)
from app.worker import transcription as transcription_worker
from app.worker.celery_app import TASK_MODULES, celery_app, configure_celery_app
from app.worker.jobs import JobError, JobTask
from app.worker.transcription import (
    INVALID_RESULT_MESSAGE,
    NO_AUDIO_MESSAGE,
    PROVIDER_FAILED_MESSAGE,
    TRANSCRIBE_VIDEO_TASK_NAME,
    TRANSCRIPTION_MAX_ATTEMPTS,
    VIDEO_NOT_READY_MESSAGE,
    run_transcription,
    transcribe_video,
)

SECRET = "test-only-jwt-signing-secret-0123456789abcdef"
PASSWORD = "s3cure-Passw0rd"
FRONTEND_ORIGIN = "http://localhost:3000"
EVIL_ORIGIN = "http://evil.example"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"

AUDIO_SECONDS = 12.0
PROVIDER_SECRET = "provider-said-hunter2"
SRT_TIMING = re.compile(r"^\d{2,}:\d{2}:\d{2},\d{3} --> \d{2,}:\d{2}:\d{2},\d{3}$")
VTT_TIMING = re.compile(r"^\d{2,}:\d{2}:\d{2}\.\d{3} --> \d{2,}:\d{2}:\d{2}\.\d{3}$")


def _transcription_url(video_id) -> str:
    return f"/api/v1/videos/{video_id}/transcription"


def _transcript_url(video_id, suffix="") -> str:
    return f"/api/v1/videos/{video_id}/transcript{suffix}"


# Fixtures


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
def fake_audio(monkeypatch):
    """Replace FFmpeg audio extraction with a silent WAV of `seconds` seconds."""
    state: dict = {"seconds": AUDIO_SECONDS, "outputs": [], "error": None}

    def fake_extract_audio(src, dst, sample_rate=16000, *, timeout=None, settings=None):
        assert Path(src).is_file()
        if state["error"] is not None:
            raise state["error"]
        dst = Path(dst)
        with wave.open(str(dst), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(sample_rate)
            audio.writeframes(b"\x00\x00" * int(state["seconds"] * sample_rate))
        state["outputs"].append(dst)
        return dst

    monkeypatch.setattr(transcription_worker, "extract_audio", fake_extract_audio)
    return state


@pytest.fixture
def use_provider(monkeypatch):
    """Make the worker use the given provider instead of the configured one."""

    def install(provider) -> None:
        monkeypatch.setattr(
            transcription_worker,
            "get_transcription_provider",
            lambda settings=None: provider,
        )

    return install


@pytest.fixture
def client(session_factory, storage_dir, monkeypatch) -> TestClient:
    monkeypatch.setenv("SECRET_KEY", SECRET)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("CORS_ORIGINS", FRONTEND_ORIGIN)
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(storage_dir))
    monkeypatch.delenv("TRANSCRIPTION_PROVIDER", raising=False)
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


def _register(client, email) -> uuid.UUID:
    response = client.post(REGISTER_URL, json={"email": email, "password": PASSWORD})
    assert response.status_code == 201
    return uuid.UUID(response.json()["id"])


def _headers(client, email) -> dict[str, str]:
    response = client.post(LOGIN_URL, json={"email": email, "password": PASSWORD})
    assert response.status_code == 200
    client.cookies.clear()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def alice(client):
    user_id = _register(client, "alice@example.com")
    return {"id": user_id, "headers": _headers(client, "alice@example.com")}


@pytest.fixture
def bob(client):
    user_id = _register(client, "bob@example.com")
    return {"id": user_id, "headers": _headers(client, "bob@example.com")}


def _make_video(
    session_factory,
    storage_dir,
    user_id,
    *,
    status=VideoStatus.READY,
    duration=AUDIO_SECONDS,
    filename="My talk.mp4",
) -> uuid.UUID:
    video_id = uuid.uuid4()
    key = video_source_key(str(user_id), str(video_id), ".mp4")
    LocalStorage(storage_dir).save(key, io.BytesIO(b"not-really-a-video"))
    with session_factory() as session:
        session.add(
            Video(
                id=video_id,
                user_id=user_id,
                original_filename=filename,
                content_type="video/mp4",
                size_bytes=18,
                storage_key=key,
                status=status,
                duration_seconds=duration,
                width=1280,
                height=720,
                fps=30.0,
            )
        )
        session.commit()
    return video_id


@pytest.fixture
def video_id(session_factory, storage_dir, alice):
    return _make_video(session_factory, storage_dir, alice["id"])


class ScriptedProvider:
    """A provider that returns a fixed result or raises a fixed error."""

    name = "scripted"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def transcribe(self, audio_path, language=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _result(*segments, provider="scripted", model="scripted-v1", language="en"):
    return TranscriptionResult(
        language=language,
        segments=tuple(segments),
        provider=provider,
        model=model,
    )


def _transcription_jobs(db) -> list[ProcessingJob]:
    db.expire_all()
    return list(
        db.scalars(
            select(ProcessingJob)
            .where(ProcessingJob.job_type == JobType.TRANSCRIPTION)
            .order_by(ProcessingJob.created_at)
        )
    )


def _counts(db) -> tuple[int, int]:
    db.expire_all()
    return (
        db.scalar(select(func.count()).select_from(Transcript)),
        db.scalar(select(func.count()).select_from(TranscriptSegment)),
    )


def _start(client, video_id, headers, **body):
    if body:
        return client.post(_transcription_url(video_id), json=body, headers=headers)
    return client.post(_transcription_url(video_id), headers=headers)


def _assert_temp_files_removed(fake_audio):
    assert fake_audio["outputs"], "audio extraction never ran"
    for output in fake_audio["outputs"]:
        assert not output.exists()
        assert not output.parent.exists()


# Triggering


def test_task_is_registered_with_the_worker():
    assert "app.worker.transcription" in TASK_MODULES
    assert transcribe_video.name == TRANSCRIBE_VIDEO_TASK_NAME
    assert transcribe_video.job_type == JobType.TRANSCRIPTION
    assert transcribe_video.job_max_attempts == TRANSCRIPTION_MAX_ATTEMPTS
    assert transcribe_video.soft_time_limit < transcribe_video.time_limit


def test_trigger_creates_job_and_transcript(
    client, db, alice, video_id, eager_jobs, fake_audio
):
    response = _start(client, video_id, alice["headers"], language="en")

    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"job_id", "status"}
    assert body["status"] == "succeeded"
    [job] = _transcription_jobs(db)
    assert str(job.id) == body["job_id"]
    assert job.status == JobStatus.SUCCEEDED
    assert job.video_id == video_id
    assert job.user_id == alice["id"]
    assert job.payload == {"video_id": str(video_id), "language": "en"}
    assert job.attempts == 1
    assert job.active_key is None

    job_response = client.get(f"/api/v1/jobs/{job.id}", headers=alice["headers"])
    assert job_response.json()["status"] == "succeeded"

    transcript = client.get(_transcript_url(video_id), headers=alice["headers"])
    assert transcript.status_code == 200
    data = transcript.json()
    assert data["video_id"] == str(video_id)
    assert data["language"] == "en"
    assert data["requested_language"] == "en"
    assert data["provider"] == "fake"
    assert data["model"] == "fake-v1"
    assert data["timing_granularity"] == "word"
    assert [s["index"] for s in data["segments"]] == [0, 1, 2]
    assert [(s["start_seconds"], s["end_seconds"]) for s in data["segments"]] == [
        (0.0, 5.0),
        (5.0, 10.0),
        (10.0, 12.0),
    ]
    assert data["segments"][0]["text"] == "This is a fake transcript segment."
    assert data["full_text"].startswith("This is a fake transcript segment. The fake")
    for segment in data["segments"]:
        uuid.UUID(segment["id"])
        assert segment["words"]
        for word in segment["words"]:
            assert segment["start_seconds"] <= word["start"] <= word["end"]
            assert word["end"] <= segment["end_seconds"]
    _assert_temp_files_removed(fake_audio)


def test_trigger_without_body_detects_the_language(
    client, db, alice, video_id, eager_jobs, fake_audio
):
    response = _start(client, video_id, alice["headers"])

    assert response.status_code == 202
    [job] = _transcription_jobs(db)
    assert job.payload == {"video_id": str(video_id), "language": None}
    data = client.get(_transcript_url(video_id), headers=alice["headers"]).json()
    assert data["requested_language"] is None
    assert data["language"] == "en"


def test_language_is_normalized_and_auto_means_detection(
    client, db, alice, video_id, published_jobs
):
    response = _start(client, video_id, alice["headers"], language=" HI-IN ")

    assert response.status_code == 202
    [job] = _transcription_jobs(db)
    assert job.payload["language"] == "hi-IN"


@pytest.mark.parametrize(
    "body",
    [
        {"language": "english!"},
        {"language": "x" * 36},
        {"language": 5},
        {"language": "en", "model": "large"},
    ],
)
def test_invalid_request_body_is_rejected(client, db, alice, video_id, body):
    response = client.post(
        _transcription_url(video_id), json=body, headers=alice["headers"]
    )

    assert response.status_code == 422
    assert _transcription_jobs(db) == []


def test_trigger_queues_one_job_per_video(client, db, alice, video_id, published_jobs):
    response = _start(client, video_id, alice["headers"])

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    [job] = _transcription_jobs(db)
    assert published_jobs == [
        {
            "task": TRANSCRIBE_VIDEO_TASK_NAME,
            "args": [str(job.id)],
            "task_id": job.celery_task_id,
        }
    ]
    assert job.idempotency_key == str(video_id)
    assert job.status == JobStatus.QUEUED


def test_retrigger_replaces_the_transcript(
    client, db, alice, video_id, eager_jobs, fake_audio, use_provider
):
    assert _start(client, video_id, alice["headers"]).status_code == 202
    first = client.get(_transcript_url(video_id), headers=alice["headers"]).json()

    use_provider(
        FakeTranscriptionProvider(segment_seconds=4.0, segment_texts=("Second run.",))
    )
    response = _start(client, video_id, alice["headers"], language="te")

    assert response.status_code == 202
    assert response.json()["status"] == "succeeded"
    second = client.get(_transcript_url(video_id), headers=alice["headers"]).json()
    assert second["id"] != first["id"]
    assert second["language"] == "te"
    assert [(s["start_seconds"], s["end_seconds"]) for s in second["segments"]] == [
        (0.0, 4.0),
        (4.0, 8.0),
        (8.0, 12.0),
    ]
    assert {s["text"] for s in second["segments"]} == {"Second run."}
    # The old transcript and its segments are gone, not kept alongside.
    assert _counts(db) == (1, 3)
    old_ids = {uuid.UUID(s["id"]) for s in first["segments"]}
    assert not set(db.scalars(select(TranscriptSegment.id))) & old_ids
    assert [job.status for job in _transcription_jobs(db)] == [
        JobStatus.SUCCEEDED,
        JobStatus.SUCCEEDED,
    ]


# Conflicts


@pytest.mark.parametrize(
    "video_status", [VideoStatus.UPLOADED, VideoStatus.PROCESSING, VideoStatus.FAILED]
)
def test_video_that_is_not_ready_returns_409(
    client, db, session_factory, storage_dir, alice, published_jobs, video_status
):
    video_id = _make_video(
        session_factory, storage_dir, alice["id"], status=video_status, duration=None
    )

    response = _start(client, video_id, alice["headers"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "video_not_ready"
    assert _transcription_jobs(db) == []
    assert published_jobs == []


def test_running_transcription_returns_409(client, db, alice, video_id, published_jobs):
    first = _start(client, video_id, alice["headers"])
    assert first.status_code == 202

    second = _start(client, video_id, alice["headers"], language="en")

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "transcription_in_progress"
    assert len(_transcription_jobs(db)) == 1
    assert len(published_jobs) == 1


def test_job_that_cannot_be_queued_returns_503_and_can_be_retried(
    client, db, alice, video_id, monkeypatch
):
    monkeypatch.setitem(celery_app.conf, "broker_url", None)

    response = _start(client, video_id, alice["headers"])

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "queue_unavailable"
    [job] = _transcription_jobs(db)
    assert job.status == JobStatus.FAILED
    assert job.active_key is None

    monkeypatch.setitem(celery_app.conf, "broker_url", "memory://")
    assert _start(client, video_id, alice["headers"]).status_code == 202


# Ownership and authentication


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("post", "transcription"),
        ("get", "transcript"),
        ("get", "transcript.srt"),
        ("get", "transcript.vtt"),
        ("get", "transcript/search?q=fake"),
    ],
)
def test_another_users_video_returns_404(
    client, db, alice, bob, video_id, eager_jobs, fake_audio, method, suffix
):
    assert _start(client, video_id, alice["headers"]).status_code == 202
    url = f"/api/v1/videos/{video_id}/{suffix}"

    response = getattr(client, method)(url, headers=bob["headers"])

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert "fake transcript" not in response.text
    assert len(_transcription_jobs(db)) == 1


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("post", "transcription"),
        ("get", "transcript"),
        ("get", "transcript.srt"),
        ("get", "transcript.vtt"),
        ("get", "transcript/search?q=fake"),
    ],
)
def test_unauthenticated_requests_return_401(client, db, video_id, method, suffix):
    response = getattr(client, method)(f"/api/v1/videos/{video_id}/{suffix}")

    assert response.status_code == 401
    assert _transcription_jobs(db) == []


def test_cookie_authenticated_trigger_requires_trusted_origin(
    client, db, alice, video_id, published_jobs
):
    login = client.post(
        LOGIN_URL, json={"email": "alice@example.com", "password": PASSWORD}
    )
    assert login.status_code == 200

    response = client.post(
        _transcription_url(video_id), headers={"Origin": EVIL_ORIGIN}
    )

    assert response.status_code == 403
    assert _transcription_jobs(db) == []


# Failures


def test_provider_failure_marks_job_failed_and_keeps_previous_transcript(
    client, db, alice, video_id, eager_jobs, fake_audio, use_provider, caplog
):
    assert _start(client, video_id, alice["headers"]).status_code == 202
    before = client.get(_transcript_url(video_id), headers=alice["headers"]).json()

    failing = ScriptedProvider(
        error=TranscriptionError(f"upstream 500 {PROVIDER_SECRET}")
    )
    use_provider(failing)
    with caplog.at_level(logging.INFO):
        response = _start(client, video_id, alice["headers"])

    assert response.status_code == 202
    assert response.json()["status"] == "failed"
    latest = _transcription_jobs(db)[-1]
    assert latest.status == JobStatus.FAILED
    assert latest.error_message == PROVIDER_FAILED_MESSAGE
    # Provider errors are treated as temporary and retried, then given up.
    assert latest.attempts == TRANSCRIPTION_MAX_ATTEMPTS
    assert failing.calls == TRANSCRIPTION_MAX_ATTEMPTS
    assert latest.active_key is None
    assert PROVIDER_SECRET not in caplog.text

    after = client.get(_transcript_url(video_id), headers=alice["headers"]).json()
    assert after == before
    assert _counts(db) == (1, 3)
    _assert_temp_files_removed(fake_audio)

    # A failed job does not block a new attempt.
    use_provider(FakeTranscriptionProvider())
    retry = _start(client, video_id, alice["headers"])
    assert retry.json()["status"] == "succeeded"


def test_provider_failure_without_previous_transcript_leaves_none(
    client, db, alice, video_id, eager_jobs, fake_audio, use_provider
):
    use_provider(ScriptedProvider(error=TranscriptionError("down")))

    response = _start(client, video_id, alice["headers"])

    assert response.json()["status"] == "failed"
    assert _counts(db) == (0, 0)
    missing = client.get(_transcript_url(video_id), headers=alice["headers"])
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "transcript_not_found"
    for suffix in (".srt", ".vtt"):
        assert (
            client.get(
                _transcript_url(video_id, suffix), headers=alice["headers"]
            ).status_code
            == 404
        )


def test_failure_while_saving_is_rolled_back(
    client, db, alice, video_id, eager_jobs, fake_audio, use_provider, monkeypatch
):
    assert _start(client, video_id, alice["headers"]).status_code == 202
    before = client.get(_transcript_url(video_id), headers=alice["headers"]).json()

    def replace_then_crash(*args, **kwargs):
        transcript_repository.replace_transcript(*args, **kwargs)
        raise RuntimeError("database went away")

    monkeypatch.setattr(transcription_worker, "replace_transcript", replace_then_crash)
    use_provider(FakeTranscriptionProvider(segment_texts=("Never stored.",)))

    response = _start(client, video_id, alice["headers"])

    assert response.json()["status"] == "failed"
    latest = _transcription_jobs(db)[-1]
    assert latest.error_message == "The job failed unexpectedly (RuntimeError)."
    assert latest.attempts == 1
    after = client.get(_transcript_url(video_id), headers=alice["headers"]).json()
    assert after == before
    assert _counts(db) == (1, 3)


def test_result_past_the_end_of_the_video_fails_without_retry(
    client, db, alice, video_id, eager_jobs, fake_audio, use_provider
):
    provider = ScriptedProvider(
        _result(
            TranscriptionSegment(0.0, 5.0, "ok"),
            TranscriptionSegment(5.0, 100.0, "late"),
        )
    )
    use_provider(provider)

    response = _start(client, video_id, alice["headers"])

    assert response.json()["status"] == "failed"
    job = _transcription_jobs(db)[-1]
    assert job.error_message == INVALID_RESULT_MESSAGE
    assert job.attempts == 1
    assert provider.calls == 1
    assert _counts(db) == (0, 0)


def test_non_result_from_provider_fails(
    client, db, alice, video_id, eager_jobs, fake_audio, use_provider
):
    use_provider(ScriptedProvider({"segments": []}))

    response = _start(client, video_id, alice["headers"])

    assert response.json()["status"] == "failed"
    assert _transcription_jobs(db)[-1].error_message == INVALID_RESULT_MESSAGE


def test_times_slightly_past_the_end_are_clamped_to_the_duration(
    client, db, alice, video_id, eager_jobs, fake_audio, use_provider
):
    use_provider(
        ScriptedProvider(
            _result(
                TranscriptionSegment(
                    10.0,
                    12.4,
                    "Last words\x00 here.",
                    words=(
                        WordTiming("Last", 10.0, 11.0),
                        WordTiming("words", 11.0, 12.4),
                    ),
                ),
                TranscriptionSegment(12.2, 12.6, "after the end"),
                provider="p" * 80,
                model=None,
            )
        )
    )

    response = _start(client, video_id, alice["headers"])

    assert response.json()["status"] == "succeeded"
    data = client.get(_transcript_url(video_id), headers=alice["headers"]).json()
    [segment] = data["segments"]
    assert (segment["start_seconds"], segment["end_seconds"]) == (10.0, AUDIO_SECONDS)
    assert segment["text"] == "Last words here."
    assert segment["words"][-1] == {
        "word": "words",
        "start": 11.0,
        "end": AUDIO_SECONDS,
    }
    assert data["provider"] == "p" * 50
    assert data["model"] is None


def test_audio_extraction_failure_fails_the_job(
    client, db, alice, video_id, eager_jobs, fake_audio
):
    fake_audio["error"] = MediaError(
        "ffmpeg failed.", "Stream map '0:a:0' matches no streams"
    )

    response = _start(client, video_id, alice["headers"])

    assert response.json()["status"] == "failed"
    job = _transcription_jobs(db)[-1]
    assert job.error_message == NO_AUDIO_MESSAGE
    assert job.attempts == 1
    assert "Stream map" not in job.error_message
    assert _counts(db) == (0, 0)


def test_segment_level_provider_does_not_claim_word_timings(
    client, alice, video_id, eager_jobs, fake_audio, use_provider
):
    use_provider(
        ScriptedProvider(
            _result(
                TranscriptionSegment(0.0, 3.0, "One."),
                TranscriptionSegment(3.0, 6.0, "Two."),
            )
        )
    )

    _start(client, video_id, alice["headers"])

    data = client.get(_transcript_url(video_id), headers=alice["headers"]).json()
    assert data["timing_granularity"] == "segment"
    assert [s["words"] for s in data["segments"]] == [None, None]


# Worker function


@pytest.fixture
def worker_user(db) -> User:
    user = User(email="worker@example.com", hashed_password="x")
    db.add(user)
    db.commit()
    return user


def test_cancelled_job_does_not_save(
    db, session_factory, storage_dir, worker_user, fake_audio
):
    video_id = _make_video(session_factory, storage_dir, worker_user.id)

    result = run_transcription(
        db,
        video_id,
        storage=LocalStorage(storage_dir),
        provider=FakeTranscriptionProvider(),
        is_cancelled=lambda: True,
    )

    assert result is None
    assert _counts(db) == (0, 0)
    _assert_temp_files_removed(fake_audio)


def test_worker_refuses_a_video_that_is_not_ready(
    db, session_factory, storage_dir, worker_user, fake_audio
):
    video_id = _make_video(
        session_factory, storage_dir, worker_user.id, status=VideoStatus.PROCESSING
    )

    with pytest.raises(JobError) as error:
        run_transcription(
            db,
            video_id,
            storage=LocalStorage(storage_dir),
            provider=FakeTranscriptionProvider(),
        )

    assert error.value.public_message == VIDEO_NOT_READY_MESSAGE
    assert fake_audio["outputs"] == []


def test_transcription_does_not_change_the_video_status(
    db, session_factory, storage_dir, worker_user, fake_audio
):
    video_id = _make_video(session_factory, storage_dir, worker_user.id)

    transcript = run_transcription(
        db,
        video_id,
        "en",
        storage=LocalStorage(storage_dir),
        provider=FakeTranscriptionProvider(),
    )

    assert transcript is not None
    db.expire_all()
    assert db.get(Video, video_id).status is VideoStatus.READY


# Subtitles and search


def test_srt_download(client, alice, video_id, eager_jobs, fake_audio):
    _start(client, video_id, alice["headers"])

    response = client.get(_transcript_url(video_id, ".srt"), headers=alice["headers"])

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-subrip; charset=utf-8"
    assert (
        response.headers["content-disposition"] == 'attachment; filename="My_talk.srt"'
    )
    assert response.headers["cache-control"] == "private, no-store"
    text = response.text
    assert text.startswith(
        "1\n00:00:00,000 --> 00:00:05,000\nThis is a fake transcript segment.\n\n"
        "2\n00:00:05,000 --> 00:00:10,000\n"
    )
    blocks = text.strip("\n").split("\n\n")
    assert [block.split("\n")[0] for block in blocks] == ["1", "2", "3"]
    assert all(SRT_TIMING.match(block.split("\n")[1]) for block in blocks)
    assert "00:00:10,000 --> 00:00:12,000" in text


def test_vtt_download(client, alice, video_id, eager_jobs, fake_audio):
    _start(client, video_id, alice["headers"])

    response = client.get(_transcript_url(video_id, ".vtt"), headers=alice["headers"])

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/vtt; charset=utf-8"
    assert (
        response.headers["content-disposition"] == 'attachment; filename="My_talk.vtt"'
    )
    text = response.text
    assert text.startswith("WEBVTT\n\n1\n00:00:00.000 --> 00:00:05.000\n")
    blocks = text.strip("\n").split("\n\n")
    assert blocks[0] == "WEBVTT"
    assert all(VTT_TIMING.match(block.split("\n")[1]) for block in blocks[1:])
    assert "," not in "".join(block.split("\n")[1] for block in blocks[1:])


def test_search_finds_segments_case_insensitively(
    client, alice, video_id, eager_jobs, fake_audio
):
    _start(client, video_id, alice["headers"])

    response = client.get(
        _transcript_url(video_id, "/search"),
        params={"q": "  DETERMINISTIC "},
        headers=alice["headers"],
    )

    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "DETERMINISTIC"
    assert data["total"] == 1
    [match] = data["items"]
    assert match["index"] == 1
    assert (match["start_seconds"], match["end_seconds"]) == (5.0, 10.0)

    limited = client.get(
        _transcript_url(video_id, "/search"),
        params={"q": "segment", "limit": 1},
        headers=alice["headers"],
    ).json()
    assert limited["total"] == 2
    assert [item["index"] for item in limited["items"]] == [0]


@pytest.mark.parametrize("params", [{"q": ""}, {"q": "   "}, {"q": "x" * 201}, {}])
def test_search_rejects_invalid_queries(
    client, alice, video_id, eager_jobs, fake_audio, params
):
    _start(client, video_id, alice["headers"])

    response = client.get(
        _transcript_url(video_id, "/search"), params=params, headers=alice["headers"]
    )

    assert response.status_code == 422


def test_search_without_transcript_returns_404(client, alice, video_id):
    response = client.get(
        _transcript_url(video_id, "/search"),
        params={"q": "x"},
        headers=alice["headers"],
    )

    assert response.status_code == 404
